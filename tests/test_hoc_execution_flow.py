"""Exercise S0-S3 with real temporary Git worktrees and scripted worker turns."""

from __future__ import annotations

from dataclasses import replace
import json
import shutil
import threading
import time

import pytest

from sdlc_harness.agents.developer.hoc_merge_queue import GateResult
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.llm.codex import CodexTurn
from sdlc_harness.profiles import build_verification_stage_policy
from sdlc_harness.workflows import hoc_execution_flow as flow
from tests import test_agent_worktrees as fixtures

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

GIT = fixtures.git
WRITE = fixtures.write
COMMIT = fixtures.commit
GREEN = GateResult(True, "unit", "passed", "")
RED = GateResult(False, "unit", "failed", "gate.log")
workspace = fixtures.workspace


def plan():
    common = {"tests": [], "new_tests": []}
    return {
        "schema_version": "plan_graph_v1", "base": {**common, "units": ["U-1"], "files": ["src/base.ts"]},
        "integration": {**common, "units": [], "files": []}, "conflicts": [], "test_intents": [],
        "groups": [
            {"group_id": "G-1", **common, "units": ["U-2"], "files": ["src/a.ts"]},
            {"group_id": "G-2", **common, "units": ["U-3"], "files": ["src/b.ts"]},
            {"group_id": "G-3", **common, "units": ["U-4"], "files": ["src/c.ts"]},
        ],
        "schedule": {
            "mode_a": {"agents": [{"agent_id": "agent-1", "groups": ["G-1", "G-2", "G-3"],
                                  "turns": [["G-1"], ["G-2"], ["G-3"]]}]},
            "mode_b": {"agents": [{"agent_id": "agent-1", "groups": ["G-1", "G-3"], "turns": [["G-1"], ["G-3"]]},
                                  {"agent_id": "agent-2", "groups": ["G-2"], "turns": [["G-2"]]}]},
        },
    }


class Worker:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []
        self.envs = []
        self.lock = threading.Lock()

    def turn(self, *, role, root, task, timeout, read_only=False, env=None):
        with self.lock:
            self.calls.append((role, task, read_only))
            self.envs.append((role, env))
        if self.callback:
            override = self.callback(role, root, task, read_only)
            if override is not None:
                return override if isinstance(override, CodexTurn) else answer(role, override)
        if role == "base":
            WRITE(root, "src/base.ts", "foundation\n")
        elif role == "reviewer-base":
            assert read_only
            assert "src/base.ts" in (root / "docs/sdlc-local/base.diff").read_text(encoding="utf-8")
            return answer(role, "none")
        else:
            group = task_group(task)
            suffix = {"G-1": "a", "G-2": "b", "G-3": "c"}[group]
            WRITE(root, f"src/{suffix}.ts", f"{suffix}\n")
        return answer(role, "DONE")


def answer(role, text, *, outside=()):
    return CodexTurn(LlmCompletion(text, usage={"total_tokens": 5}), f"session-{role}", (), outside)


def task_group(task):
    work = task.split("## Tasks and units", 1)[1].split("## Write scope", 1)[0]
    return work.strip().split()[1].strip(chr(96))


def context(workspace, *, mode="b", callback=None, gate=None, approved=None, pause=False):
    worktrees, _, _, _ = workspace
    return flow.HocExecutionContext(
        settings=worktrees.manager.settings, store=worktrees.manager.store, run_id=fixtures.RUN,
        plan=approved or plan(), mode=mode, requirement_markdown="# Requirement", design_markdown="# Design",
        known_ids={"R-1", "D-1"}, barrels={fixtures.BARREL},
        verification_policy=build_verification_stage_policy(project=fixtures.PROJECT),
        worker=Worker(callback), worktrees=worktrees,
        gate_factory=gate or (lambda **options: lambda root: GREEN), pause_after_base=pause,
    )


def record(ctx, name):
    return ctx.store.run_dir(ctx.run_id) / "hoc_execution" / name


def events(ctx):
    path = record(ctx, "agent_events.jsonl")
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.endswith(b"}")]


def execute(ctx):
    base = flow.run_hoc_execution(ctx)
    assert base.status == "base_done", base
    return flow.run_hoc_execution(ctx)


def wait_for(predicate):
    deadline = time.monotonic() + 12
    while not predicate():
        if time.monotonic() > deadline:
            raise RuntimeError("Test synchronization timed out")
        time.sleep(0.01)


@pytest.mark.parametrize("mode", ["a", "b"])
def test_wave_happy_path_union_and_no_idle_wake(workspace, mode):
    _, owner, h, initial = workspace

    def worker(role, root, task, read_only):
        if role.startswith("agent-") and task_group(task) in {"G-1", "G-2"}:
            suffix = "a" if task_group(task) == "G-1" else "b"
            existing = (root / fixtures.BARREL).read_text(encoding="utf-8")
            WRITE(root, fixtures.BARREL, existing + f'export {{ {suffix} }} from "./{suffix}";\n')
        return None

    ctx = context(workspace, mode=mode, callback=worker)
    result = execute(ctx)
    assert result.status == "wave_done", result
    assert {line for line in (h / fixtures.BARREL).read_text(encoding="utf-8").splitlines()} == {
        'export { base } from "./base";', 'export { a } from "./a";', 'export { b } from "./b";',
    }
    assert GIT(owner, "rev-parse", "HEAD") == initial
    assert GIT(owner, "branch", "--show-current") == "main"
    calls = [role for role, _, _ in ctx.worker.calls if role.startswith("agent-")]
    assert sorted(calls) == (["agent-1"] * 3 if mode == "a" else ["agent-1", "agent-1", "agent-2"])
    assert len([event for event in events(ctx) if event["type"] == "Delivered" and event["agent_id"] != "base"]) == 3
    for actor in sorted(set(calls)):
        state = json.loads(record(ctx, f"{actor}.json").read_bytes())
        assert state["status"] == "done"
        root = ctx.worktrees.root(int(actor[-1]))
        assert json.loads((root / "docs/sdlc-local/agent-state.json").read_bytes()) == state
    assert json.loads(record(ctx, "flow_v1.json").read_bytes())["wave_done"]
    assert len(ctx.worker.calls) == 5


def test_base_pauses_then_runs_wave_after_review_and_repair(workspace):
    _, _, h, approval = workspace
    finding = "## Findings\n\n| F-n | Units | Requirement/Design ID | Problem | Evidence |\n| --- | --- | --- | --- | --- |\n| F-1 | U-1 | R-1, D-1 | Missing guard | The base diff |\n"

    def worker(role, root, task, read_only):
        if role == "reviewer-base":
            if not task.startswith("Format-only"):
                return finding.replace("R-1", "R-unknown")
            return finding
        if role == "base" and task.startswith("# Fix"):
            assert "F-1" in task and "Missing guard" in task
            WRITE(root, "src/base.ts", "guarded\n")
            return "DONE"
        return None

    ctx = context(workspace, callback=worker, pause=True)
    first = flow.run_hoc_execution(ctx)
    assert first.status == "base_review_pending", first
    assert flow.run_hoc_execution(ctx).status == "base_review_pending"
    report = json.loads(record(ctx, "base_report_v1.json").read_bytes())
    assert report["approval_commit"] == approval and report["base_commit"] == GIT(h, "rev-parse", "HEAD")
    assert report["findings"][0]["finding_id"] == "F-1"
    assert report["disposition"]["result"] == "queued" and len(report["gate_results"]) == 2
    assert [role for role, _, _ in ctx.worker.calls] == ["base", "reviewer-base", "reviewer-base", "base"]
    assert flow.run_hoc_execution(replace(ctx, pause_after_base=False)).status == "wave_done"


def test_base_gate_failure_is_repaired_before_reviewer_base(workspace):
    _, _, h, _ = workspace

    def worker(role, root, task, read_only):
        if role == "base" and task.startswith("# Fix"):
            assert "own_gate_failed" in task
            WRITE(root, "src/base.ts", "repaired\n")
            return "DONE"
        return None

    def gate(**options):
        return lambda root: RED if root == h and (root / "src/base.ts").read_text() != "repaired\n" else GREEN

    ctx = context(workspace, callback=worker, gate=gate)
    result = flow.run_hoc_execution(ctx)
    assert result.status == "base_done", result
    assert [role for role, _, _ in ctx.worker.calls] == ["base", "base", "reviewer-base"]
    report = json.loads(record(ctx, "base_report_v1.json").read_bytes())
    assert [gate["ok"] for gate in report["gate_results"]] == [False, True]


def test_protocol_failure_after_one_format_retry_stops_without_a_commit(workspace):
    def worker(role, root, task, read_only):
        if role == "agent-1":
            if not task.startswith("Format-only"):
                WRITE(root, "src/a.ts", "draft\n")
            return "invalid completion"
        return None

    ctx = context(workspace, mode="a", callback=worker)
    result = execute(ctx)
    assert result.status == "stopped" and result.agent == "agent-1"
    assert "Turn protocol error" in result.reason
    inventoried = [row for row in events(ctx) if row["type"] == "TurnMeasured" and row["agent_id"] == "agent-1"]
    assert [row["format_only"] for row in inventoried] == [False, True]
    assert len({row["session_id"] for row in inventoried}) == 1
    assert not any(row["type"] == "MergeStarted" for row in events(ctx))
    assert (ctx.worktrees.root(1) / "src/a.ts").exists()


@pytest.mark.parametrize("always_fail", [False, True])
def test_own_gate_fix_precedes_next_group_and_obeys_budget(workspace, always_fail):
    _, _, h, _ = workspace

    def worker(role, root, task, read_only):
        if role == "agent-1" and task_group(task) == "G-1":
            repair = task.startswith("# Fix")
            if repair:
                assert "own_gate_failed" in task and "already merged the latest H" not in task
            phase = sum(1 for name, _, _ in ctx.worker.calls if name == role)
            WRITE(root, "src/a.ts", f"{'bad' if not repair or always_fail else 'good'} {phase}\n")
            return "DONE"
        return None

    def gate(**options):
        def check(root):
            candidate = root / "src/a.ts"
            return RED if root != h and candidate.is_file() and candidate.read_text().startswith("bad") else GREEN
        return check

    ctx = context(workspace, mode="a", callback=worker, gate=gate)
    result = execute(ctx)
    calls = [task_group(task) for role, task, _ in ctx.worker.calls if role == "agent-1"]
    if always_fail:
        assert result.status == "stopped" and "budget" in result.reason and result.agent == "agent-1"
        assert calls == ["G-1", "G-1", "G-1"]
    else:
        assert result.status == "wave_done", result
        assert calls == ["G-1", "G-1", "G-2", "G-3"]


def test_interaction_fix_syncs_h_and_carries_later_commit(workspace):
    _, _, h, _ = workspace

    def worker(role, root, task, read_only):
        if role == "agent-1" and task_group(task) == "G-1":
            wait_for(lambda: (h / "src/b.ts").is_file())
            if task.startswith("# Fix"):
                assert "integration_gate_failed" in task and "merged the latest H" in task
                assert (root / "src/b.ts").is_file() and (root / "src/c.ts").is_file()
                WRITE(root, "src/a.ts", "fixed\n")
            else:
                WRITE(root, "src/a.ts", "bad together\n")
            return "DONE"
        return None

    def gate(**options):
        def check(root):
            a = root / "src/a.ts"
            if root == h and a.is_file() and a.read_text().startswith("bad"):
                wait_for(lambda: any(event["type"] == "TurnChecked" and event.get("groups") == ["G-3"]
                                     for event in events(ctx)))
                return RED
            return GREEN
        return check

    ctx = context(workspace, callback=worker, gate=gate)
    result = execute(ctx)
    assert result.status == "wave_done", result
    history = events(ctx)
    failed = next(event for event in history if event.get("result") == "integration_gate_failed")
    assert failed["suspects"]
    assert failed["held"]
    fixed = next(event for event in history if event["type"] == "MergeFinished" and event.get("carried"))
    assert {failed["sha"], *failed["held"]} <= set(fixed["carried"])


@pytest.mark.parametrize("foreign_edit", [False, True])
def test_conflict_is_kept_and_rechecked_against_h(workspace, foreign_edit):
    _, _, h, _ = workspace
    approved = plan()
    approved["groups"][0]["files"].append("src/shared.ts")
    approved["groups"][1]["files"].append("src/shared.ts")

    def worker(role, root, task, read_only):
        if role == "agent-2":
            WRITE(root, "src/shared.ts", "from H\n")
        if role != "agent-1" or task_group(task) != "G-1":
            return None
        if not task.startswith("# Fix"):
            wait_for(lambda: (h / "src/b.ts").is_file())
            WRITE(root, "src/shared.ts", "agent change\n")
        else:
            assert GIT(root, "rev-parse", "--verify", "MERGE_HEAD")
            if "scope_rejected" in task:
                assert "parallel" in task and "src/b.ts" in task
                WRITE(root, "src/b.ts", "b\n")
            else:
                assert "<<<<<<<" in (root / "src/shared.ts").read_text()
                WRITE(root, "src/shared.ts", "resolved\n")
                if foreign_edit:
                    WRITE(root, "src/b.ts", "overwritten\n")
        return "DONE"

    ctx = context(workspace, approved=approved, callback=worker)
    result = execute(ctx)
    assert result.status == "wave_done", result
    assert (h / "src/shared.ts").read_text() == "resolved\n"
    assert (h / "src/b.ts").read_text() == "b\n"
    rejected = [event for event in events(ctx) if event.get("result") == "scope_rejected"]
    assert len(rejected) == int(foreign_edit)
    if foreign_edit:
        assert rejected[0]["causes"][0]["cause"] == "parallel"


@pytest.mark.parametrize("outcome", ["parallel", "accepted", "rejected"])
def test_scope_review_policy(workspace, outcome):
    outside = "src/b.ts" if outcome == "parallel" else "src/new.ts"

    def worker(role, root, task, read_only):
        if role == "reviewer-scope-agent-1":
            assert outcome != "parallel" and read_only
            assert "| src/new.ts | unowned | none | Needed by U-2 |" in task and "+temporary" in task
            verdict = "accept" if outcome == "accepted" else "reject"
            return f"| File | Verdict | Reason |\n| --- | --- | --- |\n| src/new.ts | {verdict} | Scope review reason |"
        if role != "agent-1":
            return None
        if "Out-of-scope reasons" in task:
            return "## Out of scope\n\n| File | Why |\n| --- | --- |\n| src/new.ts | Needed by U-2 |\n\nDONE"
        if task_group(task) == "G-1":
            if task.startswith("# Fix"):
                assert ("parallel" if outcome == "parallel" else "Scope review reason") in task
                (root / outside).unlink()
                return "DONE"
            WRITE(root, "src/a.ts", "a\n")
            WRITE(root, outside, "temporary\n")
            return "DONE"
        if outcome == "accepted":
            assert "src/new.ts" + chr(96) + " (extended)" in task
        return None

    ctx = context(workspace, callback=worker)
    result = execute(ctx)
    assert result.status == "wave_done", result
    history = events(ctx)
    assert sum(event["type"] == "ScopeExtended" for event in history) == int(outcome == "accepted")
    assert sum(event["type"] == "TurnMeasured" and event["turn_kind"] == "reasons" for event in history) == (
        0 if outcome == "parallel" else 1
    )
    rejected = [event for event in history if event.get("result") == "scope_rejected"]
    if outcome != "accepted":
        assert rejected[0]["causes"][0]["cause"] == ("parallel" if outcome == "parallel" else "review_rejected")


def test_base_may_extend_into_a_later_task_which_keeps_its_file(workspace):
    _, _, h, _ = workspace

    def worker(role, root, task, read_only):
        if role == "base":
            if "Out-of-scope reasons" in task:
                return "## Out of scope\n\n| File | Why |\n| --- | --- |\n| src/b.ts | The base type breaks it |\n\nDONE"
            assert "No agent has started yet" in task
            WRITE(root, "src/base.ts", "foundation\n")
            WRITE(root, "src/b.ts", "adapted by base\n")
            return "DONE"
        if role == "reviewer-scope-base":
            assert read_only and "| src/b.ts | shared | G-2 |" in task
            return "| File | Verdict | Reason |\n| --- | --- | --- |\n| src/b.ts | accept | Keeps the gate green |"
        if role == "agent-2":
            assert "`src/b.ts`" in task.split("## Write scope", 1)[1].split("## Existing tests", 1)[0]
            assert (root / "src/b.ts").read_text(encoding="utf-8") == "adapted by base\n"
        return None

    ctx = context(workspace, callback=worker)
    result = execute(ctx)
    assert result.status == "wave_done", result
    history = events(ctx)
    assert [event["agent_id"] for event in history if event["type"] == "ScopeExtended"] == ["base"]
    assert not [event for event in history if event.get("result") == "scope_rejected"]
    assert (h / "src/b.ts").read_text(encoding="utf-8") == "b\n"


@pytest.mark.parametrize("violation", ["blocked", "g13"])
def test_stop_drains_inflight_turn_without_starting_another_job(workspace, violation):
    entered = threading.Barrier(2)

    def worker(role, root, task, read_only):
        if role not in {"agent-1", "agent-2"}:
            return None
        entered.wait(12)
        if role == "agent-1":
            if violation == "blocked":
                return "BLOCKED: src/unplanned.ts dependency"
            return answer(role, "DONE", outside=("../another-project.ts",))
        wait_for(lambda: any(event["agent_id"] == "agent-1" and event["type"] == "TurnStopped" for event in events(ctx)))
        return None

    ctx = context(workspace, callback=worker)
    result = execute(ctx)
    assert result.status == "stopped" and result.agent == "agent-1"
    assert ("BLOCKED" if violation == "blocked" else "G13") in result.reason
    history = events(ctx)
    assert not any(event["type"] == "MergeStarted" for event in history)
    assert any(event["type"] == "TurnChecked" and event["agent_id"] == "agent-2" for event in history)


def test_resume_interrupted_turn_reuses_role_and_worktree(workspace):
    def worker(role, root, task, read_only):
        if role == "agent-1" and task_group(task) == "G-1":
            assert task.startswith(flow._INTERRUPT)
            assert (root / "src/a.ts").read_text() == "partial\n"
        return None

    ctx = context(workspace, mode="a", callback=worker)
    assert flow.run_hoc_execution(ctx).status == "base_done"
    state = flow._Execution(ctx)
    state._sync_agents()
    state._save(wave_prepared=True)
    state._start(state.actors["agent-1"], ["G-1"], "group")
    WRITE(ctx.worktrees.root(1), "src/a.ts", "partial\n")
    result = flow.run_hoc_execution(ctx)
    assert result.status == "wave_done", result
    assert len([row for row in events(ctx) if row["type"] == "TurnStarted" and row["groups"] == ["G-1"]]) == 1
    assert any(row.get("session_id") == "session-agent-1" for row in events(ctx))


@pytest.mark.parametrize("pending_merge", [False, True])
def test_resume_interrupted_merge_goes_back_to_last_green_and_requeues(workspace, pending_merge):
    _, _, h, _ = workspace
    ctx = context(workspace, mode="a")
    assert flow.run_hoc_execution(ctx).status == "base_done"
    state = flow._Execution(ctx)
    state._sync_agents()
    state._save(wave_prepared=True)
    actor = state.actors["agent-1"]
    started, inbox = state._start(actor, ["G-1"], "group")
    state._receive_turn(actor, started, state._turn_job(actor, started, inbox))
    ticket, _ = state.queue.take()
    green = GIT(h, "rev-parse", "HEAD")
    state._append(actor, "MergeStarted", sha=ticket.sha, integration_head=green)
    flags = ["--no-commit"] if pending_merge else []
    GIT(h, "merge", "--no-ff", "--no-edit", *flags, ticket.sha)
    result = flow.run_hoc_execution(ctx)
    assert result.status == "wave_done", result
    attempts = [row for row in events(ctx) if row["type"] == "MergeStarted" and row["sha"] == ticket.sha]
    assert len(attempts) == 2 and all(row["integration_head"] == green for row in attempts)
    assert GIT(h, "status", "--porcelain") == ""


def test_q5_uses_the_workspace_owning_the_pinned_trigger(workspace, monkeypatch):
    _, _, h, _ = workspace
    package = "packages/storage"
    trigger = f"{package}/schema.prisma"
    COMMIT(h, f"{package}/package.json", '{"name":"arbitrary-name"}\n')
    monkeypatch.setattr(flow, "resolve_verification_preparation_specs", lambda *a, **k: [
        type("Preparation", (), {"preparation_id": "generated_source_setup", "trigger_path": trigger})(),
    ])
    approved = plan()
    approved["base"]["files"].append(trigger)
    approved["groups"][0]["files"].append(f"{package}/repository.ts")
    gate_calls = []

    def gate(*, extra_stage_ids):
        def check(root):
            gate_calls.append((root, extra_stage_ids))
            return GREEN
        return check

    def worker(role, root, task, read_only):
        if role == "base":
            WRITE(root, trigger, "schema\n")
        elif role == "agent-1" and task_group(task) == "G-1":
            WRITE(root, f"{package}/repository.ts", "data\n")
        return None

    ctx = context(workspace, mode="a", approved=approved, callback=worker, gate=gate)
    result = execute(ctx)
    assert result.status == "wave_done", result
    assert [extra for root, extra in gate_calls if root == h] == [flow._DB_STAGES, flow._DB_STAGES, (), ()]
    assert all(not extra for root, extra in gate_calls if root != h)
