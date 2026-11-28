"""Execute the approved H-OC plan through S6 (execution plan sections 1–4)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from contextvars import copy_context
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from queue import Empty, Queue
from threading import Event, Lock, get_ident
from typing import Protocol

from ..agents.developer import hoc_agent_state as states
from ..agents.developer import hoc_merge_queue as git
from ..agents.developer import hoc_packets as packets
from ..core.config import Settings
from ..core.errors import HarnessError
from ..events import emit_current
from ..llm.codex import CodexTurn
from ..profiles import VerificationStagePolicy
from ..repo_context.plan_facts import package_of
from ..repo_context.runtime_config import _workspaces
from ..storage.runtime import RuntimeStore
from ..verification.registry import resolve_verification_preparation_specs, resolve_verification_stage_specs
from ..verification.runner import HARNESS_LANE_STAGES, VerificationRunner
from ..workspaces.agent_worktrees import AgentWorktrees
from .hoc_e2e import e2e_command
from .hoc_execution_report import execution_metrics

_FIX_BUDGET = 2
_DB_STAGES = ("database_ready", "migrate_and_seed")
_INTERRUPT = "Your previous turn was interrupted; the worktree may already hold part of the work."
_FAILURES = {"own_gate_failed", "merge_conflict", "integration_gate_failed"}
_FLOW_EVENTS = {
    "TurnMeasured", "TurnStopped", "MergeStarted", "MergeAborted", "StageStarted", "StageFinished",
    "PipelineFinished", "PipelineRepairStarted", "FinalReview", "FindingDisposition", "BudgetExtended",
}
_INTEGRATION_ROUNDS = 2
_BUDGET_EXHAUSTED = "Integration repair budget exhausted"
_FAILURE_TEXT = 12_000  # characters of failing-test detail a repair packet carries


class _Worker(Protocol):
    def turn(self, *, role: str, root: Path, task: str, timeout: int, read_only: bool = False,
             env: dict[str, str] | None = None) -> CodexTurn: ...


@dataclass(frozen=True)
class HocExecutionContext:
    settings: Settings
    store: RuntimeStore
    run_id: str
    plan: Mapping[str, object]
    mode: str
    requirement_markdown: str
    design_markdown: str
    known_ids: Collection[str]
    barrels: Collection[str]
    verification_policy: VerificationStagePolicy
    worker: _Worker
    worktrees: AgentWorktrees
    gate_factory: Callable[..., Callable[[Path], git.GateResult]]
    timeout: int = 1800
    pause_after_base: bool = False
    new_design_files: Collection[str] = ()
    integration_facts: str = ""
    plan_report: str = ""
    pipeline_runner: VerificationRunner | None = None
    approval_commit: str | None = None
    # Opens the run's services for one integration turn and yields the turn's extra environment (hoc_e2e.py).
    e2e_session: Callable[[Path], AbstractContextManager[dict[str, str]]] | None = None


@dataclass(frozen=True)
class HocExecutionResult:
    status: str
    reason: str
    artifacts: dict[str, str]
    agent: str | None = None


@dataclass(frozen=True)
class _Actor:
    id: str
    number: int | None
    branch: str
    turns: tuple[tuple[str, ...], ...]
    units: dict[str, list[str]]


@dataclass(frozen=True)
class _Checked:
    result: git.QueueResult
    answer: str
    session_id: str
    causes: tuple[dict[str, str], ...] = ()
    details: dict[str, object] = field(default_factory=dict)


class _Stopped(RuntimeError):
    pass


class _Interrupted(RuntimeError):
    pass


def run_hoc_execution(context: HocExecutionContext) -> HocExecutionResult:
    """Advance the approved run by one stage boundary."""

    with context.worktrees.manager.hoc_execution_lock(context.run_id):
        return _Execution(context).run()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extend_integration_budget(store: RuntimeStore, run_id: str, *, limit: int, reason: str) -> dict[str, object]:
    """Owner action: raise the integration repair budget of a run stopped only by it, so continue-run resumes there.

    H, its commits and the journal stay as they are; the extension is one more journal event. The caller holds the
    run's H-OC execution lock.
    """

    directory = store.run_dir(run_id) / "hoc_execution"
    record_path, events_path = directory / "flow_v1.json", directory / "agent_events.jsonl"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not str((record.get("stopped") or {}).get("reason", "")).startswith(_BUDGET_EXHAUSTED):
        raise HarnessError("budget_not_exhausted", "The H-OC run did not stop on its integration repair budget.",
                           run_id=run_id)
    events = [json.loads(line) for line in events_path.read_bytes().splitlines() if line.strip()]
    current = next((int(event["limit"]) for event in reversed(events) if event["type"] == "BudgetExtended"),
                   _INTEGRATION_ROUNDS)
    if limit <= current:
        raise HarnessError("budget_limit_invalid", f"The new limit must exceed the current {current} rounds.",
                           run_id=run_id)
    event = {"type": "BudgetExtended", "agent_id": "integration", "version": len(events) + 1,
             "budget": "integration_repairs", "limit": limit, "previous": current, "reason": reason, "at": _now()}
    with events_path.open("ab") as log:
        log.write((json.dumps(event, ensure_ascii=True) + "\n").encode())
        log.flush()
        os.fsync(log.fileno())
    record.pop("stopped")
    store._write_json(record_path, record)
    return event


class _Execution:
    def __init__(self, ctx: HocExecutionContext):
        self.ctx = ctx
        self.dir = ctx.store.run_dir(ctx.run_id) / "hoc_execution"
        self.record_path = self.dir / "flow_v1.json"
        self.events_path = self.dir / "agent_events.jsonl"
        self.report_path = self.dir / "base_report_v1.json"
        self.execution_report = self.dir / "hoc_execution_report_v1.json"
        self.execution_notes = self.dir / "hoc_execution_report_v1.md"
        self.metrics_path = self.dir / "hoc_execution_metrics_v1.json"
        self.main = get_ident()
        self.requests = Queue()
        self.stop = Event()
        self.stop_lock = Lock()
        self.stopped_by = None
        self.transport_error: HarnessError | None = None
        self.h = ctx.worktrees.root(None)
        workspace = ctx.worktrees.manager.read(ctx.run_id)
        if ctx.mode not in {"a", "b"}:
            raise ValueError(f"Unknown H-OC execution mode {ctx.mode!r}")
        self.actors = {"base": _Actor("base", None, workspace.branch, (("base",),), {"base": ctx.plan["base"]["units"]})}
        self.actors["integration"] = _Actor(
            "integration", None, workspace.branch, (), {"integration": ctx.plan["integration"]["units"]},
        )
        for row in ctx.plan["schedule"][f"mode_{ctx.mode}"]["agents"]:
            name = row["agent_id"]
            number = int(name.removeprefix("agent-"))
            if name != f"agent-{number}" or number < 1 or name in self.actors:
                raise ValueError(f"Invalid H-OC agent identity {name!r}")
            turns = tuple(tuple(turn) for turn in row["turns"])
            units = {task: packets._tasks(ctx.plan, [task])[0]["units"] for turn in turns for task in turn}
            self.actors[name] = _Actor(name, number, f"{workspace.branch}-{name}", turns, units)
        self.unit_owners = {}
        for actor in self.actors.values():
            for task, units in actor.units.items():
                for unit in units:
                    if unit in self.unit_owners and self.unit_owners[unit] != (actor.id, task):
                        raise ValueError(f"Ambiguous H-OC ownership for unit {unit!r}")
                    self.unit_owners[unit] = (actor.id, task)
        self.barrels = packets._barrel_paths(ctx.plan, ctx.barrels)
        self.attributes = self.dir / "attributes"
        self.events = self._read_events()
        self.record = self._read_record()
        self.queue = git.MergeQueue()
        self.seq = 0
        self.commands = {spec.stage_id: spec.argv for spec in resolve_verification_stage_specs(
            ctx.verification_policy, run_id=ctx.run_id,
        )}

    def _read_events(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        with self.events_path.open("r+b") as log:
            data = log.read()
            end = data.rfind(b"\n") + 1
            if end != len(data):
                log.truncate(end)
            events = [json.loads(line) for line in data[:end].splitlines()]
        if any(event.get("version") != index for index, event in enumerate(events, 1)):
            raise ValueError("Invalid H-OC event sequence")
        return events

    def _read_record(self) -> dict[str, object]:
        plan_sha = sha256(json.dumps(self.ctx.plan, sort_keys=True).encode()).hexdigest()
        identity = {"schema": "hoc_execution_flow_v1", "mode": self.ctx.mode, "plan_sha256": plan_sha}
        if self.ctx.approval_commit is not None:
            identity["approval_commit"] = self.ctx.approval_commit
        if self.record_path.is_file():
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
            if any(record.get(key) != value for key, value in identity.items()):
                raise ValueError("The approved H-OC plan or mode changed")
            return record
        if self.events:
            raise ValueError("H-OC events exist without their flow record")
        head = self._head(self.h)
        if self.ctx.approval_commit is not None and head != self.ctx.approval_commit:
            raise ValueError("H moved before the approved execution started")
        return {**identity, "approval_commit": head}

    def _save(self, **fields: object) -> None:
        self.record.update(fields)
        self.ctx.store._write_json(self.record_path, self.record)

    def _head(self, root: Path) -> str:
        return git._git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()

    def _merge_head(self, root: Path) -> str | None:
        result = git._git(root, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False)
        if result.returncode not in (0, 1):
            raise git._GitError("Could not verify MERGE_HEAD")
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def _last(self, event_type: str) -> dict[str, object] | None:
        return next((event for event in reversed(self.events) if event["type"] == event_type), None)

    def _runs_specs(self, role: str, kind: str) -> bool:
        """The integration task's working turns run its own browser specs against Harness-started services."""

        return self.ctx.e2e_session is not None and role == "integration" and kind in {"integration", "fix"}

    def _integration_rounds(self) -> int:
        extended = self._last("BudgetExtended")
        return int(extended["limit"]) if extended else _INTEGRATION_ROUNDS

    def _projectable(self) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] not in _FLOW_EVENTS]

    def _state(self, actor: _Actor) -> dict[str, object]:
        return states.project_agent(actor.id, actor.branch, actor.units, self._projectable(), fix_budget=_FIX_BUDGET)

    def _claims(self) -> dict[str, str]:
        claims = states.scope_claims(self._state(actor) for actor in self.actors.values())
        if self.record.get("wave_prepared"):  # the base ran before any agent branched: its claims lock nothing
            claims = {path: owner for path, owner in claims.items() if owner != "base"}
        return claims

    def _append(self, actor: _Actor, event_type: str, **fields: object) -> dict[str, object]:
        event = {"type": event_type, "agent_id": actor.id, "version": len(self.events) + 1, **fields}
        with self.events_path.open("ab") as log:
            log.write((json.dumps(event, ensure_ascii=True) + "\n").encode())
            log.flush()
            os.fsync(log.fileno())
        self.events.append(event)
        if event_type not in _FLOW_EVENTS:
            self._snapshot(actor)
        public = {"TurnChecked": "hoc_turn_checked", "MergeFinished": "hoc_merge_finished",
                  "AgentStopped": "hoc_execution_stopped"}.get(event_type)
        if public:
            emit_current(category="workflow", event_type=public, message="", data={
                "agent_id": actor.id, "result": fields.get("result"), "sha": fields.get("sha"),
            })
        return event

    def _snapshot(self, actor: _Actor, *, local: bool = False) -> dict[str, object]:
        state = self._state(actor)
        self.ctx.store._write_json(self.dir / f"{actor.id}.json", state)
        if local:
            self.ctx.worktrees.stage_local_file(actor.number, name="agent-state.json", text=json.dumps(state, indent=2))
        return state

    def _on_main(self, fn: Callable, *args):
        if get_ident() == self.main:
            return fn(*args)
        result = Future()
        self.requests.put((fn, args, result))
        return result.result()

    def _serve(self, request) -> None:
        if request is not None:
            fn, args, result = request
            try:
                result.set_result(fn(*args))
            except Exception as exc:
                result.set_exception(exc)

    def _fail(self, actor: _Actor, reason: str) -> None:
        with self.stop_lock:
            if self.stopped_by is None:
                self.stopped_by = (actor, reason)
                self.stop.set()

    def _guard(self, actor: _Actor, fn: Callable, *args):
        try:
            return fn(*args)
        except _Interrupted:
            return None
        except Exception as exc:
            self._fail(actor, str(exc))
            return None

    def _result(self, status: str) -> HocExecutionResult:
        artifacts = {"flow": self.ctx.store.relative(self.record_path),
                     "agent_events": self.ctx.store.relative(self.events_path)}
        if self.report_path.is_file():
            artifacts["base_report"] = self.ctx.store.relative(self.report_path)
        for key, path in (
            ("execution_report", self.execution_report), ("execution_report_markdown", self.execution_notes),
            ("execution_metrics", self.metrics_path),
        ):
            if path.is_file():
                artifacts[key] = self.ctx.store.relative(path)
        if self.stopped_by:
            actor, reason = self.stopped_by
            self._append(actor, "AgentStopped", reason=reason)
            self._save(stopped={"agent": actor.id, "reason": reason})
            return HocExecutionResult("stopped", reason, artifacts, actor.id)
        if self.record.get("stopped"):
            stop = self.record["stopped"]
            return HocExecutionResult("stopped", stop["reason"], artifacts, stop["agent"])
        if self.transport_error is not None:
            raise self.transport_error
        return HocExecutionResult(status, "", artifacts)

    def _stage_inputs(self, actor: _Actor) -> None:
        self.ctx.worktrees.stage_local_file(actor.number, name="design.md", text=self.ctx.design_markdown)
        self.ctx.worktrees.stage_local_file(actor.number, name="requirement.md", text=self.ctx.requirement_markdown)

    def _prepare(self, actor: _Actor, ids: tuple[str, ...] | None = None) -> None:
        results = self.ctx.worktrees.prepare(actor.number, verification_policy=self.ctx.verification_policy,
                                            preparation_ids=ids)
        failed = next((result for result in results if result.status != "passed"), None)
        if failed is not None:
            raise _Stopped(f"{failed.stage_id} failed: {failed.reason}")

    def _create_agents(self) -> str:
        for actor in self.actors.values():
            if actor.number is not None and not self.stop.is_set():
                self.ctx.worktrees.create(actor.number, base_sha=self.record["approval_commit"])
                self._prepare(actor, ("dependency_setup",))
        return _now()

    def _sync_agents(self) -> None:
        for actor in self.actors.values():
            if actor.number is None:
                continue
            if self.ctx.worktrees.merge(actor.number, sha=self.record["base_commit"], attributes_file=self.attributes):
                raise _Stopped(f"Base merge conflicted for {actor.id}")
            self._prepare(actor)
            self._stage_inputs(actor)
            self._snapshot(actor, local=True)

    def run(self) -> HocExecutionResult:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)
        self._save()
        if self.record.get("stopped"):
            return self._result("stopped")
        stopped = next((event for event in reversed(self.events) if event["type"] == "AgentStopped"), None)
        extended = self._last("BudgetExtended")
        if stopped and not (extended and extended["version"] > stopped["version"]):  # an owner extension resumes
            self._save(stopped={"agent": stopped["agent_id"], "reason": stopped["reason"]})
            return self._result("stopped")
        self.attributes.write_text(git.attributes_content(self.barrels), encoding="utf-8")
        if "db_package" not in self.record:
            package_dirs, db_package = self._db_package()
            self._save(package_dirs=package_dirs, db_package=db_package)
        base = self.actors["base"]
        if "base_commit" not in self.record:
            self._stage_start("S0")
            self._guard(base, self._stage_inputs, base)
            if not self.stop.is_set():
                with ThreadPoolExecutor(max_workers=1) as pool:
                    setup = pool.submit(self._guard, base, self._create_agents)
                    self._guard(base, self._base, base)
                    self._stage_finish("S0", end=setup.result())
            return self._result("base_review_pending" if self.ctx.pause_after_base else "base_done")
        if self.ctx.pause_after_base and not self.record.get("wave_prepared"):
            return self._result("base_review_pending")
        if not self.record.get("wave_done"):
            self._stage_start("S3")
            if not self.record.get("wave_prepared"):
                self._guard(base, self._sync_agents)
                if not self.stop.is_set():
                    self._save(wave_prepared=True)
            if not self.stop.is_set():
                self._guard(base, self._wave)
            self._stage_finish("S3")
            if not self.stop.is_set():
                self._save(wave_done=True)
            return self._result("wave_done")
        integration = self.actors["integration"]
        if not self.record.get("integration_done"):
            self._stage_start("S4")
            self._guard(integration, self._integration)
            self._stage_finish("S4")
            if not self.stop.is_set():
                self._save(integration_done=True)
            return self._result("integration_done")
        if not self.record.get("execution_ready"):
            self._stage_start("S5")
            self._guard(integration, self._final_review)
            self._stage_finish("S5")
            if not self.stop.is_set():
                self._stage_start("S6")
                self._guard(integration, self._checkpoint)
                if not self.stop.is_set():
                    self._save(execution_ready=True)
        return self._result("execution_review_pending")

    def _stage_start(self, stage: str) -> None:
        if not any(row["type"] == "StageStarted" and row["stage"] == stage for row in self.events):
            self._append(self.actors["base"], "StageStarted", stage=stage, started_at=_now(),
                         agents=[actor.id for actor in self.actors.values() if actor.number is not None] if stage == "S3" else [])

    def _stage_finish(self, stage: str, *, end: str | None = None) -> None:
        if self.transport_error is not None and self.stopped_by is None:
            return
        extended = self._last("BudgetExtended")
        since = extended["version"] if extended else 0  # a stage stopped before an owner extension finishes again
        if not any(row["type"] == "StageFinished" and row["stage"] == stage and row["version"] > since
                   for row in self.events):
            self._append(self.actors["base"], "StageFinished", stage=stage, ended_at=end or _now(),
                         status="stopped" if self.stop.is_set() else "done")

    def _db_package(self) -> tuple[list[str], str | None]:
        specs = resolve_verification_preparation_specs(self.ctx.verification_policy, run_id=self.ctx.run_id)
        trigger = next((spec.trigger_path for spec in specs if spec.preparation_id == "generated_source_setup"), None)
        if trigger is None:
            return [], None
        dirs = [PurePosixPath(row["manifest"]).parent.as_posix() for row in _workspaces(self.h)]
        package = package_of(trigger, dirs)
        return dirs, package if package or "." in dirs else None

    def _gate(self, root: Path, previous: str | None = None) -> git.GateResult:
        extras = ()
        db_package = self.record.get("db_package")
        if previous and db_package is not None:
            changed = git._changed(root, previous, "HEAD")
            if any(package_of(path, self.record["package_dirs"]) == db_package for path in changed):
                extras = _DB_STAGES
        return self.ctx.gate_factory(extra_stage_ids=extras)(root)

    def _actor_events(self, actor: _Actor, kind: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == kind and event["agent_id"] == actor.id]

    def _unfinished(self, actor: _Actor) -> dict[str, object] | None:
        starts = self._actor_events(actor, "TurnStarted")
        checked = self._actor_events(actor, "TurnChecked")
        if starts and (not checked or starts[-1]["version"] > checked[-1]["version"]):
            return starts[-1]
        return None

    def _call(
        self, actor: _Actor, role: str, root: Path, kind: str, task: str, *,
        read_only: bool = False, format_only: bool = False,
    ) -> CodexTurn:
        if self.stop.is_set():
            raise _Interrupted("Execution is stopping")
        start = _now()
        reply = None
        failure = None
        transport_failed = False
        try:
            measured = next((row for row in reversed(self.events)
                             if row["type"] == "TurnMeasured" and row["role"] == role), None)
            if measured and measured.get("transport_failed") and not task.startswith(_INTERRUPT):
                task = _INTERRUPT + "\n\n" + task
            if self._runs_specs(role, kind):
                with self.ctx.e2e_session(root) as env:
                    reply = self.ctx.worker.turn(role=role, root=root, task=task, timeout=self.ctx.timeout,
                                                 read_only=read_only, env=env)
            else:
                reply = self.ctx.worker.turn(role=role, root=root, task=task, timeout=self.ctx.timeout,
                                             read_only=read_only)
            if reply.outside:
                raise _Stopped("G13 outside worktree: " + ", ".join(reply.outside))
            if (read_only or format_only) and reply.touched:
                raise _Stopped(f"{kind} turn edited files")
            return reply
        except Exception as exc:
            failure = str(exc)
            if isinstance(exc, HarnessError) and exc.error_code == "llm_transport_failed":
                transport_failed = True
                with self.stop_lock:
                    if self.transport_error is None:
                        self.transport_error = exc
                    self.stop.set()
                raise _Interrupted(failure) from exc
            self._fail(actor, failure)
            raise
        finally:
            self._on_main(self._measure, actor, kind, role, start, _now(), reply, failure, format_only, transport_failed)

    def _measure(self, actor, kind, role, start, end, reply, failure, format_only, transport_failed) -> None:
        self._append(actor, "TurnMeasured", turn_kind=kind, role=role, started_at=start, ended_at=end,
                     usage=reply.completion.usage if reply else {}, session_id=reply.session_id if reply else None,
                     error=failure, format_only=format_only, transport_failed=transport_failed)

    def _formatted(self, actor, role, root, kind, task, parse, *, read_only=False):
        reply = self._call(actor, role, root, kind, task, read_only=read_only)
        try:
            return reply, parse(reply.completion.content)
        except ValueError:
            reply = self._call(
                actor, role, root, kind, "Format-only retry. Do not change any file.\n\n" + task,
                read_only=read_only, format_only=True,
            )
        return reply, parse(reply.completion.content)

    def _scope(self, actor: _Actor, tasks: Collection[str]) -> tuple[frozenset[str], set[str]]:
        claims = self._claims()
        extensions = {path for path, owner in claims.items() if owner == actor.id}
        foreign = claims.keys() - extensions
        scope = packets.write_scope(self.ctx.plan, list(tasks), barrels=self.barrels)
        return (scope | extensions) - foreign, extensions

    def _accept(self, actor: _Actor, tasks: list[str], files: tuple[str, ...], evidence: dict) -> tuple[dict, ...]:
        claims = self._claims()
        conflicts = tuple(self._parallel(path, (), (claims[path],)) for path in files
                          if path in claims and claims[path] != actor.id)
        if not conflicts:
            self._append(actor, "ScopeExtended", groups=tasks, files=files, evidence=evidence)
        return conflicts

    def _parallel(self, path: str, tasks, agents) -> dict[str, str]:
        return {"file": path, "cause": "parallel",
                "detail": f"Tasks {', '.join(tasks) or 'none'} · agents {', '.join(agents)}"}

    def _check_blocked(self, status: packets.TurnEnd, scope: Collection[str]) -> None:
        if status.status == "blocked":
            if status.path in scope:
                raise _Stopped(f"G14 BLOCKED cited a path inside the turn scope: {status.path}")
            raise _Stopped(f"BLOCKED: {status.path} {status.reason}")

    def _review_scope(self, actor, tasks, root, files, answer, scope, parent):
        outside = packets.classify_outside_files(
            self.ctx.plan, files, mode=self.ctx.mode, agent_id=actor.id, barrels=self.barrels,
            claims=self._on_main(self._claims),
            concurrent=None if self.record.get("wave_prepared") else (),  # before the wave only the base works
        )
        if set(outside) != set(files):
            raise _Stopped("A shared barrel was unexpectedly rejected by the scope check")
        strict = tuple(self._parallel(path, item.tasks, item.agents) for path, item in outside.items()
                       if item.kind == "parallel")
        if strict:
            return strict

        def reasons(text):
            result = packets.parse_out_of_scope(text)
            if result.keys() != outside.keys():
                raise ValueError("Out of scope reasons do not match the actual changed files")
            return result

        try:
            why = reasons(answer)
        except ValueError:
            report = self._call(actor, actor.id, root, "reasons", packets.reasons_only_packet(files), format_only=True)
            self._check_blocked(packets.parse_turn_end(report.completion.content), scope)
            why = reasons(report.completion.content)
        diff = git.worktree_diff(root, files, against=parent or "HEAD")
        task = packets.reviewer_scope_packet(self.ctx.plan, tasks, outside=outside, reasons=why, diff=diff)
        _, verdicts = self._formatted(
            actor, f"reviewer-scope-{actor.id}", root, "reviewer-scope", task,
            lambda text: packets.parse_scope_review(text, files), read_only=True,
        )
        rejected = tuple({"file": path, "cause": "review_rejected", "detail": item.reason}
                         for path, item in verdicts.items() if item.verdict == "reject")
        if rejected:
            return rejected
        evidence = {path: {"class": outside[path].kind, "why": why[path], "reason": item.reason}
                    for path, item in verdicts.items()}
        return self._on_main(self._accept, actor, tasks, files, evidence)

    def _start(self, actor, tasks, kind, *, fix=None, fixes=None, sync_head=None, step=None,
               route_id=None, integration_pass=None, count_fix=True):
        root = self.ctx.worktrees.root(actor.number)
        state = self._state(actor)
        inbox = states.undelivered(state, self._projectable())
        self._append(actor, "Delivered", delivered_version=state["state_version"])
        started = self._append(
            actor, "TurnStarted", groups=tasks, turn_kind=kind, fix=kind == "fix" and count_fix, fix_packet=fix,
            fixes=fixes, sync_head=sync_head, start_head=self._head(root), started_at=_now(),
            step=step, route_id=route_id, integration_pass=integration_pass,
        )
        self._snapshot(actor, local=True)
        return started, inbox

    def _resume_inbox(self, actor, started) -> list[dict[str, object]]:
        earlier = [event for event in self._projectable() if event["version"] < started["version"]]
        deliveries = [event for event in earlier if event["type"] == "Delivered" and event["agent_id"] == actor.id]
        after = deliveries[-2]["delivered_version"] if len(deliveries) > 1 else 0
        self._snapshot(actor, local=True)
        return states.undelivered({"agent_id": actor.id, "delivered_version": after}, earlier)

    def _turn_job(self, actor, started, inbox, interrupted=False) -> _Checked:
        if self.stop.is_set():
            raise _Interrupted("Execution is stopping")
        root = self.ctx.worktrees.root(actor.number)
        tasks = started["groups"]
        kind, sync = started["turn_kind"], started.get("sync_head")
        fix = dict(started["fix_packet"]) if started.get("fix_packet") else None
        parent = self._merge_head(root)
        pending = ()
        current_head = self._head(root)
        if interrupted and current_head != started["start_head"]:
            parents = git._git(root, "show", "-s", "--format=%P", "HEAD").stdout.decode("ascii").split()
            if not sync or parents != [started["start_head"], sync]:
                raise _Stopped("Interrupted turn left an unrecorded commit")
        if sync and parent is None and current_head == started["start_head"]:
            if not interrupted or not git._status(root):
                pending = self.ctx.worktrees.merge(actor.number, sha=sync, attributes_file=self.attributes,
                                                   keep_conflict=True)
                parent = self._merge_head(root)
                if parent is None:
                    self._prepare(actor)
            elif git._git(root, "merge-base", "--is-ancestor", sync, "HEAD", check=False).returncode != 0:
                raise _Stopped("Interrupted repair has uncommitted edits before H was synced")
        if parent:
            pending = pending or tuple(sorted(git._paths(git._git(root, "diff", "--name-only", "-z",
                                                                  "--diff-filter=U", "--").stdout)))
        scope_tasks = list(actor.units) if parent else tasks
        scope, extensions = self._on_main(self._scope, actor, scope_tasks)
        if set(pending) - scope:
            raise _Stopped("Merge conflicts outside total agent scope: " + ", ".join(sorted(set(pending) - scope)))
        if fix is not None:
            fix.update(merged_latest=bool(sync and not parent), pending_conflicts=pending)
        packet = packets.turn_packet(
            self.ctx.plan, tasks, mode=self.ctx.mode, role=actor.id, barrels=self.barrels,
            new_design_files=self.ctx.new_design_files, typecheck=self.commands["typecheck"], unit=self.commands["unit"],
            events=inbox, fix=fix, extensions=extensions, integration_pass=started.get("integration_pass"),
            integration_facts=self.ctx.integration_facts,
            e2e_command=e2e_command() if self._runs_specs(actor.id, kind) else None,
        )
        declared = packets.write_scope(self.ctx.plan, tasks, barrels=self.barrels) | extensions
        if parent:
            packet += "\nA Git merge is pending. Resolve it within your total agent scope.\n"
            packet += "\n".join(f"- {path}" for path in sorted(scope))
        if declared - scope and not parent:
            packet += "\nThese files are reserved by another agent: " + ", ".join(sorted(declared - scope))
        if interrupted:
            packet = _INTERRUPT + "\n\n" + packet
        message = f"H-OC {actor.id} {kind}: {', '.join(tasks)}"
        editable_head = self._head(root)
        reply, status = self._formatted(actor, actor.id, root, kind, packet, packets.parse_turn_end)
        self._check_blocked(status, scope)
        answer = reply.completion.content
        details = {}
        route_id = started.get("route_id")
        route = self._on_main(self._routed_request, actor, route_id) if route_id else None
        if route and route["kind"] == "reviewer":
            ids = {item["finding_id"] for item in route["findings"]}
            details["rejections"] = packets.parse_rejected_findings(answer, ids)
        if actor.id == "integration" and route_id is None and (started.get("step") or "").startswith("pipeline-repair-"):
            details["product_fixes"] = packets.parse_product_fixes(answer, self.unit_owners.keys())
        before = editable_head if actor.number is None else None
        check = git.check_merge_resolution if parent else git.check_turn
        gate = (lambda path: git.GateResult(True, "scope", "Pipeline follows the integration turn.", "")
                if actor.id == "integration" else self._gate(path, before))

        def checked():
            if self._head(root) != editable_head or self._merge_head(root) != parent:
                raise _Stopped("A worker changed the Harness-owned Git state")
            allowed, _ = self._on_main(self._scope, actor, scope_tasks)
            return check(root, scope=allowed, barrels=(), gate=gate, message=message)

        result = checked()
        causes = ()
        if result.result == "scope_rejected" and not result.conflict_markers:
            if any(path.casefold().split("/")[:2] == ["docs", "sdlc-local"] for path in result.files):
                raise _Stopped("A read-only local snapshot would enter Git")
            causes = self._review_scope(actor, tasks, root, result.files, answer, scope, parent)
            if not causes:
                result = checked()
        if result.result == "git_error":
            raise _Stopped(result.error)
        if result.result == "no_changes" and actor.number is None:
            result = git.QueueResult("no_changes", gate=gate(root))
        if result.result == "no_changes" and route and route["kind"] == "reviewer":
            previous = self._on_main(self._route_rejections, actor, route_id)
            if ids != set(previous) | details["rejections"].keys():
                raise _Stopped("A finding must have a repair commit or a rejection with evidence")
            details["fulfilled"] = True
        return _Checked(result, answer, reply.session_id, causes, details)

    def _record_check(self, actor, started, checked, *, queued: str | None = None, seq: int | None = None):
        result = checked.result
        fields = {"groups": started["groups"], "turn_kind": started["turn_kind"], "result": queued or result.result,
                  "checked_result": result.result, "files": result.files, "session_id": checked.session_id,
                  "causes": checked.causes, "conflict_markers": result.conflict_markers,
                  "fixes": started.get("fixes"), "received_at": _now(), "step": started.get("step"),
                  "route_id": started.get("route_id"), "turn_version": started["version"], **checked.details}
        if result.gate:
            fields["gate"] = asdict(result.gate)
        if result.sha:
            fields.update(sha=result.sha, seq=seq)
        if started.get("step") == "base-review" or (started.get("fix_packet") or {}).get("kind") == "reviewer":
            fields["review_answer"] = checked.answer
        self._append(actor, "TurnChecked", **fields)
        self._snapshot(actor, local=True)

    def _direct_turn(self, actor, tasks, *, kind, fix=None, step=None, route_id=None,
                     integration_pass=None, count_fix=True):
        started = self._unfinished(actor)
        interrupted = started is not None
        if started:
            if started.get("step") != step or started.get("route_id") != route_id:
                raise _Stopped("Interrupted turn does not match the requested H task")
            inbox = self._resume_inbox(actor, started)
        else:
            started, inbox = self._start(actor, tasks, kind, fix=fix, step=step, route_id=route_id,
                                        integration_pass=integration_pass, count_fix=count_fix)
        checked = self._turn_job(actor, started, inbox, interrupted)
        seq = len(self._actor_events(actor, "TurnChecked")) + 1
        self._record_check(actor, started, checked, seq=seq)
        result = checked.result
        if result.result == "queued":
            self._finish_direct_commit(actor, result.sha, asdict(result.gate))
        return checked

    def _finish_direct_commit(self, actor, sha, gate):
        failures = [commit["sha"] for commit in self._state(actor)["commits"] if commit["result"] == "own_gate_failed"]
        self._append(actor, "MergeFinished", sha=sha, result="merged", integration_head=sha,
                     carried=failures, gate=gate, merged_at=_now())
        self._snapshot(actor, local=True)

    def _base(self, actor: _Actor) -> None:
        previous = self.record["approval_commit"]
        self._stage_start("S1")
        if not self.record.get("base_ready"):
            checks = self._actor_events(actor, "TurnChecked")
            checked = checks[-1] if checks else None
            while not self.stop.is_set():
                if checked and checked["result"] == "queued" and not self._unfinished(actor):
                    if checked["sha"] not in {event["sha"] for event in self._actor_events(actor, "MergeFinished")}:
                        self._finish_direct_commit(actor, checked["sha"], checked["gate"])
                    break
                if checked and checked["result"] == "no_changes" and not self._unfinished(actor):
                    if not checked["gate"]["ok"]:
                        raise _Stopped("Base gate failed without an editable commit")
                    break
                fix = self._fix_packet(checked) if checked else None
                if self._state(actor)["status"] == "stopped":
                    raise _Stopped("Base fix budget exhausted")
                self._direct_turn(actor, ["base"], kind="fix" if fix else "base", fix=fix)
                checked = self._actor_events(actor, "TurnChecked")[-1]
            self._save(base_ready=True)
        self._stage_finish("S1")
        if self.stop.is_set():
            return

        self._stage_start("S2")
        findings = self.record.get("base_findings")
        if findings is None:
            diff = git._git(self.h, "diff", "--no-ext-diff", previous, "HEAD", "--").stdout.decode("utf-8", errors="replace")
            ref = self.ctx.worktrees.stage_local_file(None, name="base.diff", text=diff)
            units = actor.units["base"]
            _, findings = self._formatted(
                actor, "reviewer-base", self.h, "reviewer-base", packets.reviewer_base_packet(units, ref),
                lambda text: packets.parse_base_review(text, units, self.ctx.known_ids), read_only=True,
            )
            self._save(base_findings=findings)
        disposition = self.record.get("base_disposition")
        if findings and disposition is None:
            latest = self._drive_direct(actor, ["base"], "base-review", kind="fix",
                                        fix={"kind": "reviewer", "findings": findings}, repair_scope=False)
            review_checks = [row for row in self._actor_events(actor, "TurnChecked") if row.get("step") == "base-review"]
            disposition = {"result": latest["result"], "sha": latest.get("sha"), "answer": review_checks[0]["review_answer"],
                           "finding_ids": [item["finding_id"] for item in findings]}
            self._save(base_disposition=disposition)
        tip = self._head(self.h)
        diff_stat = git._git(self.h, "diff", "--stat", previous, tip, "--").stdout.decode("utf-8", errors="replace")
        self.ctx.store._write_json(self.report_path, {
            "schema": "hoc_base_report_v1", "approval_commit": previous, "base_commit": tip, "diff_stat": diff_stat,
            "gate_results": [event["gate"] for event in self._actor_events(actor, "TurnChecked") if "gate" in event],
            "findings": findings, "disposition": disposition,
        })
        self._save(base_commit=tip)
        self._stage_finish("S2")

    def _fix_packet(self, event: Mapping[str, object]) -> dict[str, object]:
        result = event["result"]
        packet = {"kind": result, "files": event.get("files", ()), "suspects": event.get("suspects", ())}
        if result == "scope_rejected":
            packet["causes"] = event.get("causes", ())
            packet["conflict_markers"] = event.get("conflict_markers", ())
        if result in {"own_gate_failed", "integration_gate_failed"}:
            packet["log_ref"] = (event.get("gate") or {}).get("log_ref", "")
        return packet

    def _next(self, actor: _Actor) -> tuple[dict, list, bool] | None:
        started = self._unfinished(actor)
        if started:
            return started, self._resume_inbox(actor, started), True
        state = self._state(actor)
        if state["status"] == "stopped":
            raise _Stopped("Agent fix budget exhausted")
        if states.needs_fix(state):
            checked = self._actor_events(actor, "TurnChecked")
            last = checked[-1]
            if last["result"] == "scope_rejected":
                return (*self._start(actor, last["groups"], "fix", fix=self._fix_packet(last), fixes=last.get("fixes"),
                                     route_id=last.get("route_id")),
                        False)
            groups = {group["group_id"] for group in state["groups"] if group["status"] == "fix_requested"}
            failures = [item for item in state["commits"] if item["result"] in _FAILURES]
            selected = next((item for item in failures if groups.intersection(item["groups"])), None)
            if selected is None:
                pending = next((row for row in reversed(self._actor_events(actor, "FixRequested"))
                                if row.get("route_id") and groups.intersection(row["groups"])), None)
                if pending is None:
                    raise _Stopped("No failed commit or routed request is recorded for the fix")
                return (*self._start(actor, pending["groups"], "fix", fix=self._route_packet(pending), sync_head=self.green,
                                     route_id=pending["route_id"], count_fix=False), False)
            related = [item for item in failures if set(item["groups"]) == set(selected["groups"])]
            latest = related[-1]
            outcome = next(event for event in reversed(self.events) if event["version"] == latest["result_version"])
            checked_for_fix = next(row for row in checked if row.get("sha") == latest["sha"])
            sync = self.green if latest["result"] in {"merge_conflict", "integration_gate_failed"} else None
            return (*self._start(actor, selected["groups"], "fix", fix=self._fix_packet(outcome),
                                 fixes=selected["sha"], sync_head=sync, route_id=checked_for_fix.get("route_id")), False)
        opened = {tuple(event["groups"]) for event in self._actor_events(actor, "TurnStarted")
                  if event["turn_kind"] == "group"}
        next_turn = next((turn for turn in actor.turns if turn not in opened), None)
        if next_turn:
            return (*self._start(actor, list(next_turn), "group"), False)
        return None

    def _receive_turn(self, actor, started, result: _Checked | None) -> None:
        if result is None:
            if self.stopped_by is not None:
                self._append(actor, "TurnStopped", turn_kind=started["turn_kind"], ended_at=_now())
            self._snapshot(actor, local=True)
            return
        checked = result.result
        if checked.sha:
            self.seq += 1
            commit = git.Commit(self.seq, actor.id, checked.sha, started.get("fixes"))
            effective = self.queue.submit(commit, checked)
            self._record_check(actor, started, result, queued=effective, seq=self.seq)
        else:
            self._record_check(actor, started, result)
            if checked.result == "no_changes" and not result.details.get("fulfilled"):
                self._fail(actor, "The group turn produced no changes")

    def _replay_queue(self) -> None:
        self.green = self.record["base_commit"]
        self.queue = git.MergeQueue()
        self.seq = 0
        for event in self.events:
            role, kind = event["agent_id"], event["type"]
            if self.actors[role].number is None:
                if kind == "MergeFinished" and event["result"] == "merged":
                    self.green = event["integration_head"]
                continue
            if kind == "TurnChecked" and event.get("sha"):
                self.seq = event["seq"]
                item = git.Commit(self.seq, role, event["sha"], event.get("fixes"))
                status = self.queue.submit(item, git.QueueResult(event["checked_result"]))
                if status != event["result"]:
                    raise _Stopped("The recorded turn does not match the replayed merge queue")
            elif kind == "MergeFinished":
                ticket = self.queue.take()
                if ticket is None or ticket[0].sha != event["sha"]:
                    raise _Stopped("The recorded merge does not match the replayed merge queue")
                changes = self.queue.finish(ticket[0], git.QueueResult(event["result"]))
                if any(tuple(event[key]) != getattr(changes, key) for key in ("held", "released", "carried")):
                    raise _Stopped("The recorded merge transitions do not match the replayed queue")
                if event["result"] == "merged":
                    self.green = event["integration_head"]

    def _recover_h(self) -> None:
        if self._merge_head(self.h):
            git._git(self.h, "merge", "--abort")
        if self._head(self.h) != self.green:
            git._git(self.h, "reset", "--hard", git._commit_sha(self.h, self.green))

    def _integrate(self, commit, carried, previous):
        if self.stop.is_set():
            return None
        result = git.merge_commit(
            self.h, commit.sha, attributes_file=self.attributes, barrels=self.barrels,
            gate=lambda root: self._gate(root, previous), carried=carried,
        )
        if result.result == "git_error":
            raise _Stopped(result.error)
        return result

    def _receive_merge(self, item: git.Commit, result: git.QueueResult | None) -> None:
        actor = self.actors[item.agent_id]
        if result is None:
            self._append(actor, "MergeAborted", sha=item.sha, ended_at=_now())
            return
        changes = self.queue.finish(item, result)
        finished_at = _now()
        self._append(actor, "MergeFinished", sha=item.sha, result=result.result,
                     integration_head=result.integration_head, gate=asdict(result.gate) if result.gate else None,
                     files=result.files, suspects=result.suspects, finished_at=finished_at,
                     merged_at=finished_at if result.result == "merged" else None, **asdict(changes))
        if result.result == "merged":
            self.green = result.integration_head

    def _submit(self, pool, actor, job, *args):
        future = pool.submit(copy_context().run, self._guard, actor, job, *args)
        future.add_done_callback(lambda _: self.requests.put(None))
        return future

    def _wave(self) -> None:
        self._replay_queue()
        self._recover_h()
        agents = [actor for actor in self.actors.values() if actor.number is not None]
        for actor in agents:
            self._snapshot(actor, local=True)
        jobs = {}
        merge = None
        with ThreadPoolExecutor(max_workers=max(1, len(agents))) as workers, ThreadPoolExecutor(max_workers=1) as merger:
            while True:
                while True:
                    try:
                        self._serve(self.requests.get_nowait())
                    except Empty:
                        break
                if merge is not None and merge[1].done():
                    item, future = merge
                    self._guard(self.actors[item.agent_id], self._receive_merge, item, future.result())
                    if item.agent_id not in jobs:
                        self._guard(self.actors[item.agent_id],
                                    lambda actor: self._snapshot(actor, local=True), self.actors[item.agent_id])
                    merge = None
                for agent_id in sorted(jobs):
                    started, future = jobs[agent_id]
                    if future.done():
                        self._guard(self.actors[agent_id], self._receive_turn, self.actors[agent_id], started, future.result())
                        del jobs[agent_id]
                for actor in agents:
                    if self.stop.is_set():
                        break
                    if actor.id not in jobs:
                        work = self._guard(actor, self._next, actor)
                        if work:
                            jobs[actor.id] = (work[0], self._submit(workers, actor, self._turn_job, actor, *work))
                if merge is None and not self.stop.is_set():
                    ticket = self.queue.take()
                    if ticket:
                        item, carried = ticket
                        actor = self.actors[item.agent_id]
                        self._append(actor, "MergeStarted", sha=item.sha, integration_head=self.green, started_at=_now())
                        merge = (item, self._submit(merger, actor, self._integrate, item, carried, self.green))
                if not jobs and merge is None:
                    break
                self._serve(self.requests.get())
        if not self.stop.is_set() and any(self._state(actor)["status"] != "done" for actor in agents):
            raise _Stopped("The wave is idle with unmerged work")

    def _routed_request(self, actor, route_id):
        return next(row for row in self._actor_events(actor, "FixRequested") if row.get("route_id") == route_id)

    def _route_rejections(self, actor, route_id):
        return {
            finding: evidence for row in self._actor_events(actor, "TurnChecked") if row.get("route_id") == route_id
            for finding, evidence in row.get("rejections", {}).items()
        }

    def _route_packet(self, request):
        if request["kind"] == "reviewer":
            return {"kind": "reviewer", "findings": request["findings"], "final": True}
        return {"kind": "integration", "requests": request["requests"]}

    def _drive_direct(self, actor, tasks, step, *, kind, fix=None, route_id=None, integration_pass=None,
                      repair_scope=True):
        """Run one H task and its scope or own-gate repairs, reusing journaled checks."""

        while True:
            checks = [row for row in self._actor_events(actor, "TurnChecked") if row.get("step") == step]
            latest = checks[-1] if checks else None
            if latest and not self._unfinished(actor):
                if latest["result"] in {"queued", "no_changes"} and latest["gate"]["ok"]:
                    if latest["result"] == "queued" and latest["sha"] not in {
                        row["sha"] for row in self._actor_events(actor, "MergeFinished")
                    }:
                        self._finish_direct_commit(actor, latest["sha"], latest["gate"])
                    return latest
                if latest["result"] == "no_changes" or (latest["result"] == "scope_rejected" and not repair_scope):
                    raise _Stopped(f"{actor.id} failed its gate or scope check")
                if self._state(actor)["status"] == "stopped":
                    raise _Stopped(f"{actor.id} fix budget exhausted")
            self._direct_turn(actor, tasks, kind="fix" if latest else kind,
                              fix=self._fix_packet(latest) if latest else fix, step=step, route_id=route_id,
                              integration_pass=integration_pass, count_fix=latest is not None)

    def _pipeline(self, step: str) -> dict[str, object]:
        actor = self.actors["integration"]
        existing = next((row for row in self._actor_events(actor, "PipelineFinished") if row["step"] == step), None)
        if existing:
            return existing
        policy = self.ctx.verification_policy
        stage_ids = tuple(stage for stage in policy.stage_ids if stage not in HARNESS_LANE_STAGES)
        start = _now()
        runner = self.ctx.pipeline_runner or VerificationRunner()
        result = runner.run_harness_stages(target_path=self.h, run_id=self.ctx.run_id,
                                           verification_policy=policy, stage_ids=stage_ids)
        log_path = self.ctx.store.write_json_artifact(
            self.ctx.run_id, f"hoc_pipeline_{len(self._actor_events(actor, 'PipelineFinished')) + 1}.json", result.to_dict(),
        )
        by_id = {item.stage_id: item for item in result.stage_results}
        stages = [{"stage_id": stage, "status": by_id[stage].status if stage in by_id else "not_run"}
                  for stage in stage_ids]
        failed = next((item.stage_id for item in (*result.preparation_results, *result.stage_results)
                       if item.status != "passed"), None)
        passed = result.status == "passed" and not failed and all(item["status"] == "passed" for item in stages)
        return self._append(actor, "PipelineFinished", step=step, status="passed" if passed else "failed",
                            stage=failed or ("preflight" if not passed else None), stages=stages,
                            started_at=start, ended_at=_now(), head=self._head(self.h), summary=result.reason,
                            log_ref=self.ctx.store.relative(log_path))

    def _repair_pipeline(self, failure: dict[str, object]) -> None:
        actor = self.actors["integration"]
        while failure["status"] != "passed":
            repairs = self._actor_events(actor, "PipelineRepairStarted")
            repair = next((row for row in repairs if row["pipeline_version"] == failure["version"]), None)
            if repair is None:
                if len(repairs) >= self._integration_rounds():
                    raise _Stopped(f"{_BUDGET_EXHAUSTED} at {failure['stage']}")
                repair = self._append(actor, "PipelineRepairStarted", round=len(repairs) + 1,
                                      pipeline_version=failure["version"])
            step = f"pipeline-repair-{repair['round']}"
            fix = self._pipeline_fix(failure)
            checked = self._drive_direct(actor, ["integration"], step, kind="fix", fix=fix)
            self._route_fixes(step, "integration", checked.get("product_fixes", []))
            if self.stop.is_set():
                return
            failure = self._pipeline(f"{step}-rerun")

    def _pipeline_fix(self, failure: Mapping[str, object]) -> dict[str, object]:
        """A failed pipeline's repair packet: the log staged in H and every failing test, extracted mechanically.

        Playwright stops each test at its first failure, so the packet lists all failing tests at once and points
        at the page snapshot Playwright writes for each (`error-context.md`).
        """

        text = (self.ctx.store.repo_root / str(failure["log_ref"])).read_text(encoding="utf-8")
        local = self.ctx.worktrees.stage_local_file(None, name="pipeline.json", text=text)
        result = json.loads(text)
        step = next((item for item in (*(result.get("preparation_results") or []), *(result.get("stage_results") or []))
                     if item.get("stage_id") == failure["stage"]), {})
        output = f"{step.get('stdout') or ''}\n{step.get('stderr') or ''}".replace("\r\n", "\n")
        blocks = re.findall(r"^ +\d+\) \[[^\]\n]+\] › .*?(?=^ +\d+\) \[|^ +\d+ (?:failed|flaky|passed)\b|\Z)",
                            output, re.M | re.S)
        blocks = [re.split(r"\n +attachment #", block, maxsplit=1)[0].rstrip() for block in blocks]
        details = "\n\n".join(blocks) if blocks else output.strip()[-4000:]
        contexts = sorted({path.replace("\\", "/") for path in re.findall(r"Error Context: (\S+error-context\.md)", output)})
        return {"kind": "integration", "stage": failure["stage"], "log_ref": local,
                "failures": details[:_FAILURE_TEXT], "error_contexts": contexts}

    def _integration(self) -> None:
        actor = self.actors["integration"]
        for pass_number in (1, 2):
            step = f"integration-{pass_number}"
            self._drive_direct(actor, ["integration"], step, kind="integration", integration_pass=pass_number)
            self._repair_pipeline(self._pipeline(step))
            if self.stop.is_set():
                return

    def _route_fixes(self, batch: str, kind: str, items) -> list[dict[str, object]]:
        by_owner: dict[str, list[dict[str, object]]] = {}
        groups: dict[str, set[str]] = {}
        for item in items:
            owned_units: dict[str, list[str]] = {}
            for unit in item["units"] if kind == "reviewer" else [item["unit"]]:
                owner, task = self.unit_owners[unit]
                owned_units.setdefault(owner, []).append(unit)
                groups.setdefault(owner, set()).add(task)
            for owner, units in owned_units.items():
                by_owner.setdefault(owner, []).append({**item, "units": units} if kind == "reviewer" else item)
        routed = []
        for owner, assigned in sorted(by_owner.items()):
            actor = self.actors[owner]
            route_id = f"{batch}:{owner}"
            request = next((row for row in self._actor_events(actor, "FixRequested")
                            if row.get("route_id") == route_id), None)
            if request is None:
                content = {"findings" if kind == "reviewer" else "requests": assigned}
                request = self._append(actor, "FixRequested", groups=sorted(groups[owner]), kind=kind,
                                       route_id=route_id, **content)
            routed.append(request)
        for request in routed:
            actor = self.actors[request["agent_id"]]
            if actor.number is None:
                checked = self._drive_direct(actor, request["groups"], request["route_id"], kind="fix",
                                             fix=self._route_packet(request), route_id=request["route_id"])
                if kind == "integration" and checked["result"] == "no_changes":
                    raise _Stopped(f"{actor.id} did not commit its routed product fix")
        if any(self.actors[request["agent_id"]].number is not None for request in routed):
            self._wave()
        return routed

    def _final_review(self) -> None:
        actor = self.actors["integration"]
        reviews = self._actor_events(actor, "FinalReview")
        if reviews:
            findings = reviews[-1]["findings"]
        else:
            if not self.ctx.plan_report.strip():
                raise _Stopped("The approved Plan report is missing")
            diff = git._git(self.h, "diff", "--no-ext-diff", self.record["approval_commit"], "HEAD", "--").stdout
            pipeline = self._actor_events(actor, "PipelineFinished")[-1]
            stage = self.ctx.worktrees.stage_local_file
            plan_ref = stage(None, name="plan_graph_v1.md", text=self.ctx.plan_report)
            diff_ref = stage(None, name="execution.diff", text=diff.decode("utf-8", errors="replace"))
            log = Path(pipeline["log_ref"])
            pipeline_ref = stage(None, name="pipeline.json", text=(self.ctx.store.repo_root / log).read_text(encoding="utf-8"))
            _, findings = self._formatted(
                actor, "reviewer-final", self.h, "reviewer-final",
                packets.reviewer_final_packet(plan_ref, diff_ref, pipeline_ref),
                lambda text: packets.parse_base_review(text, self.unit_owners.keys(), self.ctx.known_ids), read_only=True,
            )
            self._append(actor, "FinalReview", findings=findings)
        if findings:
            routed = self._route_fixes("final-review", "reviewer", findings)
            if self.stop.is_set():
                return
            for request in routed:
                self._record_dispositions(request)
        self._repair_pipeline(self._pipeline("final-review"))

    def _record_dispositions(self, request: dict[str, object]) -> None:
        actor = self.actors[request["agent_id"]]
        route_id = request["route_id"]
        responses = [row for row in self._actor_events(actor, "TurnChecked") if row.get("route_id") == route_id]
        sha = next((row["sha"] for row in reversed(responses) if row.get("sha")), None)
        if sha:
            merged = next((row for row in reversed(self._actor_events(actor, "MergeFinished"))
                           if row["result"] == "merged" and sha in (row["sha"], *row.get("carried", ()))), None)
            sha = merged["sha"] if merged else None
        rejections = self._route_rejections(actor, route_id)
        done = {row["finding_id"] for row in self._actor_events(actor, "FindingDisposition")
                if row["route_id"] == route_id}
        for item in request["findings"]:
            finding = item["finding_id"]
            if finding in done:
                continue
            if finding in rejections:
                self._append(actor, "FindingDisposition", route_id=route_id, finding_id=finding, result="rejected",
                             units=item["units"], evidence=rejections[finding])
            elif sha:
                self._append(actor, "FindingDisposition", route_id=route_id, finding_id=finding, result="fixed",
                             units=item["units"], sha=sha)
            else:
                raise _Stopped(f"No repair or rejection recorded for {finding} on {actor.id}")

    def _checkpoint(self) -> None:
        integration = self.actors["integration"]
        pipeline = self._actor_events(integration, "PipelineFinished")[-1]
        tip = self._head(self.h)
        if pipeline["status"] != "passed" or pipeline["head"] != tip:
            raise _Stopped("The final H head has no passing pipeline")
        findings = self._actor_events(integration, "FinalReview")[-1]["findings"]
        dispositions = self._project_report_events("FindingDisposition", "finding_id", "units", "result", "sha", "evidence")
        diff_stat = {}
        ctx = self.ctx
        tasks = {"base": ctx.plan["base"], **{group["group_id"]: group for group in ctx.plan["groups"]},
                 "integration": ctx.plan["integration"]}
        for task, item in tasks.items():
            files = sorted(packets._task_files(item))
            diff_stat[task] = git._git(
                self.h, "diff", "--stat", self.record["approval_commit"],
                tip, "--", *(f":(literal){path}" for path in files),
            ).stdout.decode("utf-8", errors="replace") if files else ""
        report = {
            "schema": "hoc_execution_report_v1", "head": tip, "approval_commit": self.record["approval_commit"],
            "pipeline": pipeline, "diff_stat": diff_stat, "base_report": ctx.store.relative(self.report_path),
            "findings": [{**item, "dispositions": [row for row in dispositions if row["finding_id"] == item["finding_id"]]}
                         for item in findings],
            "scope_extensions": self._project_report_events("ScopeExtended", "groups", "files", "evidence"),
            "scope_rejections": [row for row in self._project_report_events("TurnChecked", "groups", "causes")
                                 if row.get("causes")],
            "integration_routes": [row for row in self._project_report_events("FixRequested", "groups", "kind", "requests")
                                   if row["kind"] == "integration"],
            "flaky_specs": [], "browser_failures": [
                {"stage": row["stage"], "log_ref": row["log_ref"]} for row in self._actor_events(integration, "PipelineFinished")
                if row["stage"] == "browser_e2e"
            ],
        }
        ctx.store._write_json(self.execution_report, report)
        self._write_notes(report)
        self._stage_finish("S6")
        ctx.store._write_json(self.metrics_path, execution_metrics(self.events))

    def _project_report_events(self, kind, *fields):
        return [{key: row[key] for key in ("agent_id", *fields) if key in row}
                for row in self.events if row["type"] == kind]

    def _write_notes(self, report: dict[str, object]) -> None:
        lines = ["# H-OC execution review", "", f"Head {report['head']}", "", "## Pipeline", ""]
        lines.extend(f"- {stage['stage_id']} · {stage['status']}" for stage in report["pipeline"]["stages"])
        lines += ["", "## Findings", ""]
        if not report["findings"]:
            lines.append("None.")
        for item in report["findings"]:
            lines.append(f"- {item['finding_id']} · {item['problem']}")
            for row in item["dispositions"]:
                lines.append(f"  - {row['agent_id']} · {row['result']} · {row.get('sha') or row.get('evidence')}")
        lines += ["", "## Task diffs", ""]
        for task, stat in report["diff_stat"].items():
            lines.append(f"- {task} · {stat.strip().splitlines()[-1].strip() if stat else 'No changes'}")
        lines += ["", "## Scope and integration", ""]
        for extension in report["scope_extensions"]:
            for path, evidence in extension.get("evidence", {}).items():
                lines.append(f"- {extension['agent_id']} extended {path} · {evidence['reason']}")
        for rejected in report["scope_rejections"]:
            for cause in rejected["causes"]:
                lines.append(f"- {rejected['agent_id']} · {cause['cause']} · {cause['file']}")
        for route in report["integration_routes"]:
            lines.append(f"- Product fixes for {route['agent_id']} · {', '.join(row['unit'] for row in route['requests'])}")
        for failure in report["browser_failures"]:
            lines.append(f"- {failure['stage']} failed · {failure['log_ref']}")
        for spec in report["flaky_specs"]:
            lines.append(f"- Flaky spec {spec}")
        lines += ["", "[Base report](base_report_v1.json)", ""]
        self.execution_notes.write_text("\n".join(lines), encoding="utf-8")
