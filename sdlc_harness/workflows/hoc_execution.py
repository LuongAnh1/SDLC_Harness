"""Run state, owner checkpoints and recovery for H-OC Phase 4 (plan sections 3.7–3.8)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
import json
from pathlib import Path

from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.codex import CodexWorker
from ..repo_context.plan_facts import clean_path
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..workspaces.agent_worktrees import AgentWorktrees, make_merge_gate
from .hoc_e2e import integration_e2e_session
from .hoc_execution_flow import HocExecutionContext, extend_integration_budget, run_hoc_execution
from .phase4 import _verification_policy_for_state

_PHASE = "hoc_execution"
_PLAN_STATUS = "waiting_implementation_plan_review"
_RUNNING = "hoc_executing"
_BASE_REVIEW = "waiting_hoc_base_review"
_FINAL_REVIEW = "waiting_hoc_execution_review"
_PLAN_INPUTS = ("plan_graph", "plan_graph_report", "plan_repo_facts")
_APPROVED_INPUTS = ("requirement_markdown", "requirement_trace", "design_markdown", "design_trace")
_RUN_ACTIONS = ["continue_run", "get_status", "restart_execution", "cancel_run"]
_REVIEW_ACTIONS = ["approve_checkpoint", "get_status", "restart_execution", "cancel_run"]
_BLOCKED_ACTIONS = ["get_status", "restart_execution", "cancel_run"]
INTEGRATION_BUDGET_ID = "hoc_integration_repairs"


class HocExecutionWorkflow:
    """Advance the H-OC flow from approval or continue-run without entering the H-API lanes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def approve_plan_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        state = self.store.read_state(run_id)
        checkpoint = self._checkpoint(state, _PLAN_STATUS, "implementation_plan_review", checkpoint_id)
        config = {
            "worker": self.settings.hoc_worker,
            "model": self.settings.hoc_codex_model,
            "effort": self.settings.hoc_codex_effort,
            "reviewer_model": self.settings.hoc_codex_reviewer_model,
            "reviewer_effort": self.settings.hoc_codex_reviewer_effort,
            "mode": self.settings.hoc_execution_mode,
            "pause_after_base": self.settings.hoc_base_review_pause,
        }
        if config["worker"] == "opencode":
            raise HarnessError("hoc_worker_unavailable", "The H-OC execution worker opencode is unavailable.", run_id=run_id)
        if config["worker"] != "codex" or config["mode"] not in {"a", "b"}:
            raise HarnessError("hoc_execution_config_invalid", "Invalid H-OC worker or execution mode.", run_id=run_id)
        worktrees = AgentWorktrees(self.settings, run_id=run_id)
        with worktrees.manager.hoc_execution_lock(run_id), worktrees.manager.locked_workspace(run_id) as record:
            state = self.store.read_state(run_id)
            latest = state["latest_artifacts"]
            if checkpoint.get("artifact_path") != latest.get("plan_graph"):
                raise HarnessError("checkpoint_not_current", "The Plan checkpoint refers to another Plan.", run_id=run_id)
            _verification_policy_for_state(state, run_id=run_id)
            approved = state["approved_artifacts"]
            refs = {key: latest.get(key) for key in _PLAN_INPUTS}
            refs.update({key: approved.get(key) for key in _APPROVED_INPUTS})
            pinned = self._freeze(run_id, refs)
            state["hoc_execution"] = {
                "config": config, "input_artifacts": pinned, "approval_commit": record.head_sha,
                "plan_checkpoint": checkpoint, "base_approved": False,
            }
            state["approved_artifacts"] = {**approved, **{key: pinned[key]["ref"] for key in _PLAN_INPUTS}}
            self._running(state)
        return self._advance(run_id)

    def approve_base_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        state = self.store.read_state(run_id)
        self._checkpoint(state, _BASE_REVIEW, "hoc_base_review", checkpoint_id)
        self._require_no_restart(state)
        state["hoc_execution"]["base_approved"] = True
        self._running(state)
        return self._advance(run_id)

    def approve_final_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        state = self.store.read_state(run_id)
        checkpoint = self._checkpoint(state, _FINAL_REVIEW, "hoc_execution_review", checkpoint_id)
        self._require_no_restart(state)
        state["approved_artifacts"]["hoc_execution_review"] = checkpoint
        return self._update(state, status="hoc_execution_approved", pending_checkpoint=None,
                            allowed_next_actions=["get_status"], last_error=None,
                            summary_for_user="Anh đã duyệt báo cáo thực thi H-OC.")

    def continue_execution(self, *, run_id: str) -> dict[str, object]:
        state = self.store.read_state(run_id)
        self._require_no_restart(state)
        if state.get("status") != _RUNNING:
            return self._response(state)
        return self._advance(run_id)

    def adjust_budget(self, *, run_id: str, budget_id: str, limit: int, reason: str) -> dict[str, object]:
        """Owner action: a run blocked only by its integration repair budget gets more rounds and resumes in place."""

        if budget_id != INTEGRATION_BUDGET_ID:
            raise HarnessError("budget_id_invalid", f"H-OC execution has only the {INTEGRATION_BUDGET_ID} budget.",
                               run_id=run_id)
        if not reason.strip():
            raise HarnessError("budget_reason_required", "A budget change needs a reason.", run_id=run_id)
        worktrees = AgentWorktrees(self.settings, run_id=run_id)
        with worktrees.manager.hoc_execution_lock(run_id):
            state = self.store.read_state(run_id)
            if state.get("current_phase") != _PHASE or state.get("status") != "blocked" or state.get("hoc_restart"):
                raise HarnessError("budget_not_exhausted", "Only a blocked H-OC run can get a larger budget.",
                                   run_id=run_id)
            event = extend_integration_budget(self.store, run_id, limit=limit, reason=reason.strip())
            state.setdefault("recoveries", []).append({
                "code": "adjust_budget", "budget_id": budget_id, "previous_limit": event["previous"], "limit": limit,
                "reason": reason.strip(), "recovered_at": utc_now_iso(),
            })
            return self._update(
                state, status=_RUNNING, pending_checkpoint=None, pending_step_review=None,
                allowed_next_actions=_RUN_ACTIONS, last_error=None,
                summary_for_user=f"Đã nâng ngân sách sửa tích hợp lên {limit} vòng. Anh chạy continue-run để thực thi tiếp.",
            )

    def restart_execution(self, *, run_id: str, reason: str) -> dict[str, object]:
        if not reason.strip():
            raise HarnessError("execution_restart_reason_required", "An execution restart needs a reason.", run_id=run_id)
        state = self.store.read_state(run_id)
        if state.get("current_phase") != _PHASE or state.get("status") not in {
            _RUNNING, _BASE_REVIEW, _FINAL_REVIEW, "blocked",
        } or not isinstance(state.get("hoc_execution"), dict):
            raise HarnessError("execution_restart_not_allowed", "This run cannot restart H-OC execution.", run_id=run_id)
        worktrees = AgentWorktrees(self.settings, run_id=run_id)
        manager = worktrees.manager
        with manager.hoc_execution_lock(run_id), manager.registry_lock(), manager.run_lock(run_id):
            state = self.store.read_state(run_id)
            record = manager.read(run_id)
            manager._validate_record(record, state=state, verify_git=False)
            h = worktrees.root(None)
            run_dir = self.store.run_dir(run_id).resolve()
            restart = state.get("hoc_restart")
            if restart is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                restart = {
                    "directory": f"execution_restart_{stamp}", "reason": reason.strip(),
                    "from_status": state["status"], "head": worktrees._git(h, "rev-parse", "HEAD").stdout.decode().strip(),
                }
                state["hoc_restart"] = restart
                self.store.write_state(state)
            audit = run_dir / "audit" / restart["directory"]
            if audit.resolve() != audit or audit.parent != run_dir / "audit":
                raise HarnessError("execution_restart_failed", "Invalid H-OC restart audit path.", run_id=run_id)
            audit.mkdir(parents=True, exist_ok=True)
            backup = audit / "state.json"
            if not backup.exists():
                self.store._write_json(backup, state)
            worktrees.restart_locked(
                approval_commit=state["hoc_execution"]["approval_commit"], archived_tip=restart["head"], audit=audit,
            )
            for name in ("hoc_execution", "hoc_execution_inputs"):
                self._archive(run_dir / name, audit / name)
            artifacts = run_dir / "artifacts"
            for pattern in ("codex_*", "hoc_gate_*", "hoc_pipeline_*"):
                for path in sorted(artifacts.glob(pattern)):
                    self._archive(path, audit / path.name)
            manager.refresh_locked(record)

            state = self.store.read_state(run_id)
            plan_checkpoint = state.pop("hoc_execution")["plan_checkpoint"]
            state.pop("hoc_call_active", None)
            state.pop("hoc_restart")
            for key in _PLAN_INPUTS:
                state["approved_artifacts"].pop(key, None)
            state["latest_artifacts"] = {
                key: value for key, value in state["latest_artifacts"].items() if not key.startswith("hoc_")
            }
            state.setdefault("recoveries", []).append({
                "code": "restart_execution", "from_status": restart["from_status"], "reason": restart["reason"],
                "audit_dir": self.store.relative(audit), "recovered_at": utc_now_iso(),
            })
            return self._update(
                state, status=_PLAN_STATUS, current_phase="implementation_planning", pending_checkpoint=plan_checkpoint,
                allowed_next_actions=["approve_checkpoint", "get_status", "cancel_run"], last_error=None,
                summary_for_user="Đã lưu audit và khôi phục checkpoint Plan H-OC để anh duyệt lại.",
            )

    def _advance(self, run_id: str) -> dict[str, object]:
        state = self.store.read_state(run_id)
        state["hoc_call_active"] = True
        self.store.write_state(state)
        worktrees = AgentWorktrees(self.settings, run_id=run_id)
        result, failure = None, None
        try:
            result = run_hoc_execution(self._context(state, worktrees))
        except HarnessError as exc:
            if exc.error_code == "run_workspace_locked":
                raise
            failure = exc
        except Exception as exc:
            failure = HarnessError("hoc_execution_failed", str(exc), run_id=run_id)

        manager = worktrees.manager
        try:
            with manager.run_lock(run_id):
                manager.refresh_locked(manager.read(run_id))
        except HarnessError as exc:
            failure = exc
        state = self.store.read_state(run_id)
        state["hoc_call_active"] = False
        if result is not None:
            state["latest_artifacts"].update({f"hoc_{key}": ref for key, ref in result.artifacts.items()})
        if failure is not None and failure.error_code == "llm_transport_failed":
            return self._update(state, last_error=failure.to_response(),
                                summary_for_user="Worker bị gián đoạn. Anh có thể tiếp tục lượt đang dở.")
        known_results = {"base_done", "wave_done", "integration_done", "base_review_pending", "execution_review_pending"}
        if failure is None and result.status not in known_results | {"stopped"}:
            failure = HarnessError("hoc_execution_failed", f"Unknown execution result {result.status!r}.", run_id=run_id)
        if failure is not None or result.status == "stopped":
            failure = failure or HarnessError("hoc_execution_stopped", result.reason, run_id=run_id)
            last_error = failure.to_response()
            if result and result.agent:
                last_error["agent"] = result.agent
            budget = failure.message.startswith("Integration repair budget exhausted")
            return self._update(state, status="blocked", last_error=last_error,
                                allowed_next_actions=_BLOCKED_ACTIONS + (["adjust_budget"] if budget else []),
                                summary_for_user=f"Thực thi H-OC đã dừng: {failure.message}")
        if result.status in {"base_done", "wave_done", "integration_done"}:
            return self._running(state)
        if result.status == "base_review_pending":
            kind = "hoc_base_review"
            return self._update(
                state, status=_BASE_REVIEW, last_error=None, allowed_next_actions=_REVIEW_ACTIONS,
                pending_checkpoint={
                    "type": kind, "checkpoint_id": f"checkpoint_{kind}_{run_id}",
                    "artifact_ref": "base_report_v1", "artifact_path": result.artifacts["base_report"],
                },
                summary_for_user="Báo cáo base đã sẵn sàng để anh duyệt.",
            )
        if result.status == "execution_review_pending":
            kind = "hoc_execution_review"
            return self._update(
                state, status=_FINAL_REVIEW, last_error=None, allowed_next_actions=_REVIEW_ACTIONS,
                pending_checkpoint={
                    "type": kind, "checkpoint_id": f"checkpoint_{kind}_{run_id}",
                    "artifact_ref": "hoc_execution_report_v1", "artifact_path": result.artifacts["execution_report"],
                    "review_projection_ref": result.artifacts["execution_report_markdown"],
                    "metrics_ref": result.artifacts["execution_metrics"],
                },
                summary_for_user="Báo cáo thực thi H-OC và metrics đã sẵn sàng để anh duyệt.",
            )

    def _context(self, state: dict[str, object], worktrees: AgentWorktrees) -> HocExecutionContext:
        run_id = state["run_id"]
        execution = state["hoc_execution"]
        config = execution["config"]
        data = {}
        for key, source in execution["input_artifacts"].items():
            raw = self._artifact(run_id, source["ref"]).read_bytes()
            if sha256(raw).hexdigest() != source["sha256"]:
                raise HarnessError("hoc_execution_input_changed", f"Frozen execution input changed: {key}.", run_id=run_id)
            data[key] = raw.decode("utf-8")
        plan = json.loads(data["plan_graph"])
        design = json.loads(data["design_trace"])
        requirement = json.loads(data["requirement_trace"])
        facts = json.loads(data["plan_repo_facts"])
        if plan["schema_version"] != "plan_graph_v1":
            raise ValueError("Unsupported H-OC Plan schema.")
        policy = _verification_policy_for_state(state, run_id=run_id)
        baseline = execution["approval_commit"]
        root = worktrees.root(None)
        files = worktrees._git(root, "ls-tree", "-r", "--name-only", "-z", worktrees._commit(root, baseline)).stdout
        tracked = set(files.decode("utf-8", errors="surrogateescape").split("\0"))
        design_files = {clean_path(path) for unit in design["units"] for path in unit["files"]}
        known_ids = set(_trace_ids(requirement)) | set(_trace_ids(design))
        known_ids.update(row["scenario"] for row in design.get("scenario_paths", []))
        return HocExecutionContext(
            settings=self.settings, store=self.store, run_id=run_id, plan=plan, mode=config["mode"],
            requirement_markdown=data["requirement_markdown"], design_markdown=data["design_markdown"],
            known_ids=known_ids, barrels=facts["barrels"],
            verification_policy=policy, worker=CodexWorker(
                self.settings, run_id=run_id, model=config["model"], effort=config["effort"],
                reviewer_model=config["reviewer_model"], reviewer_effort=config["reviewer_effort"],
            ),
            worktrees=worktrees, timeout=1800,
            gate_factory=partial(make_merge_gate, store=self.store, run_id=run_id, verification_policy=policy),
            e2e_session=partial(integration_e2e_session, store=self.store, run_id=run_id, verification_policy=policy),
            pause_after_base=config["pause_after_base"] and not execution["base_approved"],
            approval_commit=baseline, new_design_files=design_files - tracked,
            plan_report=data["plan_graph_report"], integration_facts=_integration_facts(policy.stage_ids, plan),
        )

    def _freeze(self, run_id: str, refs: dict[str, object]) -> dict[str, dict[str, str]]:
        sources = {key: self._artifact(run_id, ref) for key, ref in refs.items()}
        directory = self.store.run_dir(run_id).resolve() / "hoc_execution_inputs"
        if directory.resolve() != directory:
            raise HarnessError("hoc_execution_input_invalid", "The frozen input directory is redirected.", run_id=run_id)
        directory.mkdir(exist_ok=True)
        pinned = {}
        for key, source in sources.items():
            raw = source.read_bytes()
            target = directory / f"{key}{source.suffix}"
            if target.resolve() != target:
                raise HarnessError("hoc_execution_input_invalid", "A frozen input path is redirected.", run_id=run_id)
            target.write_bytes(raw)
            pinned[key] = {"ref": self.store.relative(target), "source": refs[key], "sha256": sha256(raw).hexdigest()}
        return pinned

    def _artifact(self, run_id: str, ref: object) -> Path:
        if isinstance(ref, str) and ref:
            path = (self.store.repo_root / ref).resolve()
            if path.is_relative_to(self.store.run_dir(run_id).resolve()) and path.is_file():
                return path
        raise HarnessError("hoc_execution_input_invalid", f"Run artifact is missing or outside the run: {ref!r}.", run_id=run_id)

    @staticmethod
    def _archive(source: Path, target: Path) -> None:
        if source.exists() or source.is_symlink():
            source.rename(target)

    @staticmethod
    def _checkpoint(state, status, kind, checkpoint_id):
        checkpoint = state.get("pending_checkpoint")
        if state.get("status") != status or not isinstance(checkpoint, dict) or checkpoint.get("type") != kind or (
            checkpoint_id is not None and checkpoint.get("checkpoint_id") != checkpoint_id
        ):
            raise HarnessError("checkpoint_not_current", "The requested checkpoint is not current.", run_id=state["run_id"])
        return checkpoint

    @staticmethod
    def _require_no_restart(state) -> None:
        if state.get("hoc_restart"):
            raise HarnessError("execution_restart_pending", "Continue the pending execution restart.", run_id=state["run_id"])

    def _running(self, state: dict[str, object]) -> dict[str, object]:
        return self._update(
            state, status=_RUNNING, current_phase=_PHASE, pending_checkpoint=None, pending_step_review=None,
            allowed_next_actions=_RUN_ACTIONS, last_error=None,
            summary_for_user="Thực thi H-OC đang ở ranh giới công đoạn.",
        )

    def _update(self, state: dict[str, object], **fields: object) -> dict[str, object]:
        state.update(fields, updated_at=utc_now_iso())
        self.store.write_state(state)
        return self._response(state)

    @staticmethod
    def _response(state: Mapping[str, object]) -> dict[str, object]:
        return {"ok": True, "created_new_run": False, **{key: state.get(key) for key in (
            "source_ref", "run_id", "current_phase", "status", "latest_artifacts", "pending_checkpoint",
            "pending_step_review", "allowed_next_actions", "last_error", "summary_for_user", "workspace",
        )}}


def _trace_ids(value):
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            yield value["id"]
        for nested in value.values():
            yield from _trace_ids(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _trace_ids(nested)


def _integration_facts(stages: tuple[str, ...], plan: Mapping[str, object]) -> str:
    roots = plan["test_rules"]["e2e_roots"]
    lines = ["Pinned verification stage order: " + ", ".join(stages), "E2E roots: " + (", ".join(roots) or "none")]
    if "browser_e2e" in stages and {"database_ready", "migrate_and_seed"} <= set(stages[:stages.index("browser_e2e")]):
        lines.append("Database preparation and seeding run once before browser E2E. All specs share that seeded database.")
    lines.extend((
        "Each spec creates its own uniquely named data and asserts only on that data. Never assume the size of a whole list.",
        "The integration check files verify the seed state.",
    ))
    return "\n".join(lines)
