"""Phase 4 Runtime/Integration Closure cycle (L5.3.3): contract planning and owner checkpoint.

`implementation_traced` -> Closure Planner -> Harness rule review -> Closure Validator ->
(blocker: Planner patch, bounded) -> `waiting_runtime_closure_review` -> `runtime_closure_ready`.

The whole planning loop is one transition and follows the Phase 3 replay model: with step
review on, every provider exchange is persisted under the planning attempt and a resume
replays approved exchanges byte-identically, so only the next new call reaches the provider.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from ..agents.developer import DeveloperExecutionError
from ..agents.developer.closure_contract import (
    CLOSURE_DEVELOPER_STAGE_IDS,
    CLOSURE_PROBE_SERVICES,
    CLOSURE_VERIFICATION_POLICY_VERSION,
    CONTRACT_SCHEMA_VERSION,
    approved_closure_changes,
    build_closure_work_packet,
    closure_new_file_policies,
    closure_path_policy_view,
    closure_repair_feedback,
    closure_runnable_status,
    derive_closure_context,
    extend_closure_work_packet,
    planner_input_view,
    render_runtime_probe_spec,
    review_closure_planner_output,
    route_closure_problems,
    runtime_probe_failures,
    runtime_probe_run,
)
from ..agents.developer.closure_planner import INCONCLUSIVE, RuntimeClosurePlanner
from ..agents.developer.runtime_config import (
    env_runtime_config_changes,
    runtime_config_execution_issues,
    runtime_config_source_issues,
    worktree_dirty_paths,
)
from ..agents.validator.closure_review import FROZEN_CONTRACT_CONFLICT_CATEGORY, RuntimeClosureValidator
from ..artifacts.writers import write_phase4_code_change_report
from ..core.errors import HarnessError
from ..llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ..profiles import build_runtime_closure_stage_policy, get_project_profile
from ..profiles.runtime_config import runtime_config_policy, runtime_config_prompt_context
from ..repo_context.runtime_config import collect_repo_runtime_config
from ..storage.runtime import utc_now_iso
from ..verification import RUNTIME_PROBE_SPEC_PATH
from ..verification.runner import _command_failure_kind
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import (
    _bounded_diagnostic,
    _dict_items,
    _int_value,
    _is_repairable_code_trace_failure,
    _next_phase4_artifact_version,
    _nonrepairable_code_trace_error_code,
    _planning_policy_for_state,
    _required_phase4_artifact,
    _string_items,
)

# Error codes a later continue_run retries, and the closure stage it resumes.
_RETRYABLE_BLOCKS = {
    (INCONCLUSIVE, "planning"): "planning",
    (INCONCLUSIVE, "reviewing"): "reviewing",
    ("runtime_closure_environment_failed", "verifying"): "verifying",
}

ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_RUNTIME_CLOSURE_REVIEW_ACTIONS = ["approve_checkpoint", "get_status", "cancel_run"]
RUNTIME_CLOSURE_CHECKPOINT_TYPE = "runtime_closure_review"
RUNTIME_PROBE_CAPABILITY = (
    "Proves that the service boots and answers the approved request with the expected HTTP status and, "
    "optionally, a JSON array of at least N items. It observes no DOM, focus, CSS or layout."
)
MAX_SOURCE_FILE_CHARS = 32000
MAX_SOURCE_TOTAL_CHARS = 160000
_SCRIPT_FILE = re.compile(r"[A-Za-z0-9_./-]+\.(?:ts|tsx|mts|cts|js|mjs|cjs|prisma)\b")
_RELATIVE_IMPORT = re.compile(r"""(?:from\s+|import\s*\(?\s*)["'](\.{1,2}/[^"']+)["']""")
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


class PhaseFourRuntimeClosureCycle:
    """Own the closure planning transition; composed by PhaseFourWorkflow."""

    # ------------------------------------------------------------------ routing

    def _runtime_closure_enforced(self, state: dict[str, object]) -> bool:
        pinned_mode = str(_dict_value(state.get("runtime_closure")).get("mode") or "")
        if pinned_mode:
            return pinned_mode == "enforce"
        return self.settings.runtime_closure_mode == "enforce"

    def _runtime_closure_retry_stage(self, state: dict[str, object]) -> str:
        """The closure stage a continue_run retries from a retryable block, or ``""``."""

        if str(state.get("status") or "") != "blocked":
            return ""
        error_code = str(_dict_value(state.get("last_error")).get("error_code") or "")
        # QA refused an unbound run; once closure is enforced the run can enter closure instead.
        if error_code == "qa_requires_runtime_closure" and self._runtime_closure_enforced(state):
            return "planning"
        if str(state.get("current_phase") or "") != "runtime_closure":
            return ""
        blocked_from = str(_dict_value(state.get("runtime_closure")).get("blocked_from") or "planning")
        return _RETRYABLE_BLOCKS.get((error_code, blocked_from), "")

    def _runtime_closure_retry_allowed(self, state: dict[str, object]) -> bool:
        return bool(self._runtime_closure_retry_stage(state))

    def _resume_runtime_closure_stage(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        stage = self._runtime_closure_retry_stage(state)
        if stage == "verifying":
            return self._continue_runtime_closure_verification(state=state, run_id=run_id)
        if stage == "reviewing":
            return self._continue_runtime_closure_review(state=state, run_id=run_id)
        return self._continue_runtime_closure_planning(state=state, run_id=run_id, new_attempt=True)

    # ------------------------------------------------------------------ planning

    def _continue_runtime_closure_planning(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        pin = self._pin_runtime_closure(state, new_attempt=new_attempt)
        attempt = int(pin.get("planning_attempt") or 1)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            target_path = target_info.path
            planning_policy = _planning_policy_for_state(state, run_id=run_id)
            context = derive_closure_context(
                run_id=run_id,
                work_packet=work_packet,
                bound_inputs=_closure_bound_inputs(state),
            )
            version = _next_closure_contract_version(self.store, run_id)
            latest_artifacts["runtime_closure_context"] = _write_closure_json(
                self.store, run_id, f"runtime_closure_context_v{version}.json", context
            )
            repo = _ClosureRepository(target_path)
            repo_runtime_config = collect_repo_runtime_config(
                target_path=target_path,
                source_files=list(target_info.source_files),
            )
            view = _closure_prompt_view(
                context=context,
                work_packet=work_packet,
                planning_policy=planning_policy,
                repo_runtime_config=repo_runtime_config,
                source_files=repo.grounding_files(context=context, work_packet=work_packet, planning_policy=planning_policy),
            )
            outcome = self._run_closure_contract_loop(
                run_id=run_id,
                attempt=attempt,
                context=context,
                view=view,
                work_packet=work_packet,
                planning_policy=planning_policy,
                repo_runtime_config=repo_runtime_config,
                repo=repo,
                source_issues=runtime_config_source_issues(
                    repo_root=target_path,
                    changes=[
                        {**change, "kind": "env_var"} for change in _dict_items(context.get("plan_env_changes"))
                    ],
                ),
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_runtime_closure_step_review(
                state=state, run_id=run_id, progress=exc.progress, latest_artifacts=latest_artifacts
            )
        except HarnessError as exc:
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc)

        review_path = _write_closure_json(
            self.store, run_id, f"runtime_closure_contract_review_v{version}.json", outcome["review"]
        )
        latest_artifacts["runtime_closure_contract_review"] = review_path
        if not outcome["ok"]:
            error = HarnessError(outcome["error_code"], outcome["message"], run_id=run_id)
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=error)

        contract = outcome["contract"]
        runnable = closure_runnable_status(contract)
        contract_path = _write_closure_json(self.store, run_id, f"runtime_closure_contract_v{version}.json", contract)
        markdown_path = _write_closure_text(
            self.store,
            run_id,
            f"runtime_closure_contract_v{version}.md",
            _closure_contract_markdown(contract=contract, runnable=runnable, review=outcome["review"], version=version),
        )
        latest_artifacts.update(
            {
                "runtime_closure_contract": contract_path,
                "runtime_closure_contract_markdown": markdown_path,
            }
        )
        checkpoint_id = f"checkpoint_runtime_closure_{run_id}_v{version}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "waiting_runtime_closure_review",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": RUNTIME_CLOSURE_CHECKPOINT_TYPE,
                    "artifact_path": markdown_path,
                    "contract_path": contract_path,
                },
                "pending_step_review": None,
                "runtime_closure": {**pin, "status": "waiting_review", "contract_version_number": version},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "runtime_closure_contract": {
                        "ok": True,
                        "version": version,
                        "rounds": len(_dict_items(outcome["review"].get("rounds"))),
                        **runnable,
                        "input_count": len(_dict_items(contract.get("runtime_inputs"))),
                        "new_write_targets": list(_string_items(contract.get("new_write_targets"))),
                        "unresolved_count": len(_dict_items(contract.get("unresolved"))),
                        "reachability_unverified_count": len(_dict_items(outcome["review"].get("reachability_unverified"))),
                        "warning_count": len(_dict_items(outcome["review"].get("warnings"))),
                        "report": review_path,
                    },
                },
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_REVIEW_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Runtime closure contract v{version} chờ owner duyệt cho run {run_id}: "
                    f"{len(_dict_items(contract.get('runtime_inputs')))} input, runnable={runnable['runnable_status']}, "
                    f"{len(_string_items(contract.get('new_write_targets')))} write target mới. "
                    f"Đọc `{markdown_path}` rồi approve_checkpoint."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _run_closure_contract_loop(
        self,
        *,
        run_id: str,
        attempt: int,
        context: dict[str, object],
        view: dict[str, object],
        work_packet: dict[str, object],
        planning_policy,
        repo_runtime_config: dict[str, object],
        repo: "_ClosureRepository",
        source_issues: list[dict[str, object]],
    ) -> dict[str, object]:
        planner, validator = self._closure_agents(run_id=run_id, attempt=attempt)
        limit = max(0, int(self.settings.implementation_plan_repair_limit))
        known_refs = _closure_known_refs(context=context, work_packet=work_packet)
        rounds: list[dict[str, object]] = []
        previous_round: dict[str, object] | None = None
        validator_round: dict[str, object] | None = None
        previous_fingerprint = ""
        for round_number in range(1, limit + 2):
            proposal = planner.propose(context=context, planner_view=view, previous_round=previous_round, run_id=run_id)
            rule = review_closure_planner_output(
                context=context,
                planner_output=proposal["output"],
                planning_policy=planning_policy,
                repo_runtime_config=repo_runtime_config,
                symbol_exists=repo.symbol_exists,
                references=repo.references,
                runtime_config_source_issues=source_issues,
            )
            record: dict[str, object] = {
                "round": round_number,
                "planner_attempts": proposal["attempts"],
                "rule_review": {key: rule[key] for key in ("ok", "issues", "reachability_unverified", "config_unsupported")},
            }
            rounds.append(record)
            if rule["config_unsupported"]:
                return _loop_failure(
                    rounds,
                    "runtime_closure_config_unsupported",
                    "Closure contract needs a runtime config kind Harness cannot execute yet: "
                    + "; ".join(item["message"] for item in rule["issues"] if item["error_code"] == "runtime_closure_config_unsupported"),
                )
            if rule["ok"]:
                review = validator.review_contract(
                    contract=rule["contract"],
                    validator_view={
                        **view,
                        "harness_rule_review": {"ok": True, "reachability_unverified": rule["reachability_unverified"]},
                    },
                    known_refs=known_refs | _contract_object_refs(rule["contract"]),
                    previous_round=validator_round,
                    run_id=run_id,
                )
                record["validator"] = review
                if review["ok"]:
                    return {
                        "ok": True,
                        "contract": rule["contract"],
                        "review": {
                            "schema_version": "runtime_closure_contract_review_v1",
                            "ok": True,
                            "rounds": rounds,
                            "warnings": review["warnings"],
                            "reachability_unverified": rule["reachability_unverified"],
                            "summary": review["summary"],
                        },
                    }
                blocking = list(review["blocking_findings"])
                validator_round = {"previous_blocking_findings": blocking}
            else:
                blocking = list(rule["issues"])
            fingerprint = hashlib.sha256(
                json.dumps([proposal["output"], blocking], sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if fingerprint == previous_fingerprint:
                return _loop_failure(rounds, "runtime_closure_contract_invalid", "Closure contract repair stalled: the same output and blockers repeated.")
            previous_fingerprint = fingerprint
            previous_round = {"previous_output": proposal["output"], "blocking_issues": blocking}
        return _loop_failure(
            rounds,
            "runtime_closure_contract_invalid",
            f"Closure contract still has blockers after {limit} patch round(s).",
        )

    def _closure_agents(self, *, run_id: str, attempt: int) -> tuple[RuntimeClosurePlanner, RuntimeClosureValidator]:
        planner, validator = self.closure_planner, self.closure_validator
        if not bool(self.runtime_closure_step_review.get("enabled")):
            return planner, validator
        base_dir = f"runtime_closure_execution/step_review/attempt_{attempt}"

        def reviewable(client, role: str, label: str) -> ReviewableLlmClient:
            return ReviewableLlmClient(
                client,
                store=self.store,
                run_id=run_id,
                review=self.runtime_closure_step_review,
                call_prefix=f"runtime-closure-{role}",
                execution_dir=f"{base_dir}/{role}",
                error_prefix="runtime_closure",
                label=label,
            )

        return (
            RuntimeClosurePlanner(reviewable(planner.llm_client, "planner", "Closure Planner")),
            RuntimeClosureValidator(reviewable(validator.llm_client, "validator", "Closure Validator")),
        )

    def _pin_runtime_closure(self, state: dict[str, object], *, new_attempt: bool) -> dict[str, object]:
        pin = _dict_value(state.get("runtime_closure"))
        if not pin.get("mode"):
            pin = {
                "mode": "enforce",
                "contract_version": CONTRACT_SCHEMA_VERSION,
                "verification_policy_version": CLOSURE_VERIFICATION_POLICY_VERSION,
                "planning_attempt": 1,
                "pinned_at": utc_now_iso(),
            }
        elif new_attempt:
            pin = {**pin, "planning_attempt": int(pin.get("planning_attempt") or 1) + 1}
        pin = {**pin, "status": "planning"}
        state["runtime_closure"] = pin
        self.store.write_state(state)
        return pin

    # ------------------------------------------------------------------ pause / block

    def _pause_for_runtime_closure_step_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        progress: dict[str, object],
        latest_artifacts: dict[str, str],
        resume_status: str = "",
    ) -> dict[str, object]:
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closure_step_review_required",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "pending_step_review": progress,
                "runtime_closure_step_review": dict(self.runtime_closure_step_review),
                # Empty = contract planning; otherwise the post-execution status to resume.
                "runtime_closure_resume": {"status": resume_status},
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Runtime closure đang chờ duyệt {progress.get('review_stage')} "
                    f"{progress.get('call_id')} cho run {run_id}."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def continue_runtime_closure_step_review(self, *, run_id: str) -> dict[str, object]:
        """Replay approved closure exchanges with the operator decision for the pending one."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != "runtime_closure_step_review_required":
                raise HarnessError(
                    "runtime_closure_step_review_not_pending",
                    f"Run has no pending runtime closure step review: {run_id}",
                    run_id=run_id,
                )
            self._require_frozen_llm_mode(state, run_id=run_id)
            pinned = _dict_value(state.get("runtime_closure_step_review"))
            decision = {
                key: self.runtime_closure_step_review[key]
                for key in ("call_id", "decision", "note")
                if self.runtime_closure_step_review.get(key) is not None
            }
            self.runtime_closure_step_review = {**pinned, **decision, "enabled": True}
            state["pending_step_review"] = None
            resume_status = str(_dict_value(state.get("runtime_closure_resume")).get("status") or "")
            state["runtime_closure_resume"] = None
            if resume_status == "runtime_closure_verified":
                state["status"] = resume_status
                return self._continue_runtime_closure_review(state=state, run_id=run_id)
            return self._continue_runtime_closure_planning(state=state, run_id=run_id)

    def _block_runtime_closure(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        exc: HarnessError,
    ) -> dict[str, object]:
        pin = _dict_value(state.get("runtime_closure"))
        raw = str(getattr(exc, "raw_response", "") or "")
        if raw:
            latest_artifacts["runtime_closure_raw_response"] = _write_closure_text(
                self.store,
                run_id,
                f"runtime_closure_raw_response_attempt{int(pin.get('planning_attempt') or 1)}.txt",
                raw,
            )
        blocked_from = str(pin.get("status") or "planning")
        retryable = (exc.error_code, blocked_from) in _RETRYABLE_BLOCKS
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "pending_step_review": None,
                "runtime_closure": {**pin, "status": "blocked", "blocked_from": blocked_from},
                "allowed_next_actions": (
                    ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS if retryable else ["get_status", "cancel_run"]
                ),
                "last_error": exc.to_response(),
                "summary_for_user": f"Runtime closure bị chặn cho run {run_id}: {exc.message}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ execution (L5.3.4)

    def _runtime_closure_execution_active(self, state: dict[str, object]) -> bool:
        execution = _dict_value(state.get("developer_execution"))
        return str(execution.get("mode") or "") in {"runtime_closure", "runtime_closure_repair"}

    def _continue_runtime_closure_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run or resume the Developer tool loop over the approved closure units."""

        pin = _dict_value(state.get("runtime_closure"))
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        artifact_version = _int_value(pin.get("artifact_version"))
        try:
            frozen_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            frozen_packet = _read_json_artifact(self._existing_artifact_path(frozen_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=frozen_packet)
            packet_ref = str(pin.get("work_packet") or "")
            if packet_ref:
                closure_packet = _read_json_artifact(self._existing_artifact_path(packet_ref, run_id=run_id), run_id=run_id)
            else:
                contract_ref = str(pin.get("approved_contract") or "")
                if not contract_ref or str(pin.get("status") or "") != "approved":
                    raise HarnessError(
                        "runtime_closure_contract_not_approved",
                        f"Runtime closure execution needs an owner-approved contract: {run_id}",
                        run_id=run_id,
                    )
                contract = _read_json_artifact(self._existing_artifact_path(contract_ref, run_id=run_id), run_id=run_id)
                context_ref = _required_phase4_artifact(artifact_paths, "runtime_closure_context", run_id=run_id)
                context = _read_json_artifact(self._existing_artifact_path(context_ref, run_id=run_id), run_id=run_id)
                packet_version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_work_packet")
                closure_packet = build_closure_work_packet(
                    contract=contract,
                    context=context,
                    frozen_packet=frozen_packet,
                    version=packet_version,
                    contract_ref=contract_ref,
                    new_file_policies=closure_new_file_policies(_planning_policy_for_state(state, run_id=run_id)),
                )
                packet_ref = _write_closure_json(
                    self.store, run_id, f"runtime_closure_work_packet_v{packet_version}.json", closure_packet
                )
                artifact_version = _next_phase4_artifact_version(self.store, run_id)
                closure_paths = set(_string_items(_dict_value(closure_packet.get("output_contract")).get("allowed_paths")))
                # Every other change in the worktree is approved earlier output: freeze it hash-bound.
                frozen_dirty = [
                    path
                    for path in worktree_dirty_paths(target_info.path)
                    if path not in closure_paths and path != ".codegraph" and not path.startswith(".codegraph/")
                ]
                pin = {
                    **pin,
                    "status": "executing",
                    "work_packet": packet_ref,
                    "artifact_version": artifact_version,
                    "expected_dirty_paths": frozen_dirty,
                    # G14 compares these after every closure session.
                    "expected_dirty_hashes": {
                        path: _file_sha256(target_info.path / path) for path in frozen_dirty
                    },
                    "frozen_packet_sha256": _file_sha256(
                        self._existing_artifact_path(frozen_path, run_id=run_id)
                    ),
                    "repair_iterations": 0,
                    "scope_extensions": [],
                }
                latest_artifacts["runtime_closure_work_packet"] = packet_ref
                state.update({"runtime_closure": pin, "latest_artifacts": latest_artifacts, "updated_at": utc_now_iso()})
                self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=closure_packet,
                target_path=target_info.path,
                latest_artifacts=latest_artifacts,
                mode="runtime_closure",
                current_phase="runtime_closure",
                repair_iteration=0,
                session_iteration=1,
                artifact_version=artifact_version,
                manifest_key="runtime_closure_execution_manifest",
                journal_key="runtime_closure_execution_journal",
                expected_dirty_paths=_string_items(pin.get("expected_dirty_paths")),
                session_scope="runtime_closure",
            )
            runtime_config_files = self._materialize_runtime_config(
                run_id=run_id,
                work_packet=closure_packet,
                target_path=target_info.path,
                artifact_version=artifact_version,
                latest_artifacts=latest_artifacts,
            )
            applied_files = [*result.files, *runtime_config_files]
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report={
                    "run_id": run_id,
                    "source_ref": state.get("source_ref"),
                    "issue_key": frozen_packet.get("issue_key"),
                    "target_repo": str(target_info.path),
                    "branch": target_info.current_branch,
                    "summary": result.summary,
                    "files": applied_files,
                    "execution_mode": "tool_loop",
                    "runtime_closure_work_packet": packet_ref,
                    "developer_execution_manifest": result.manifest_path,
                    "developer_execution_journal": result.journal_path,
                },
                version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            for key, value in (
                ("runtime_closure_execution_manifest", exc.manifest_path),
                ("runtime_closure_execution_journal", exc.journal_path),
                ("runtime_closure_execution_session", exc.session_path),
                ("developer_failure_classification", exc.failure_path),
            ):
                if value:
                    latest_artifacts[key] = value
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "runtime_closure",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS if paused else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "developer_execution": self._developer_execution_state(
                        state,
                        executor="tool_loop",
                        mode="runtime_closure",
                        repair_iteration=0,
                        session_iteration=1,
                        artifact_version=artifact_version,
                        progress=exc.progress,
                        status="paused" if paused else "blocked",
                    ),
                    "summary_for_user": (
                        f"Developer runtime closure {'tạm dừng' if paused else 'bị chặn'} cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "runtime_closure",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Runtime closure bị chặn khi chuẩn bị Developer: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts["code_change_report"] = report_path
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closure_written",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "runtime_closure": self._with_session_scope_extensions(
                    {**_dict_value(state.get("runtime_closure")), "status": "written"},
                    journal_ref=result.journal_path,
                    run_id=run_id,
                ),
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Developer đã thực thi {len(_dict_items(closure_packet.get('execution_units')))} closure unit, "
                    f"cập nhật {len(applied_files)} file cho run {run_id}. Chưa chạy closure verification."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ post-execution (L5.3.6)

    def _closure_run_inputs(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        pin = _dict_value(state.get("runtime_closure"))
        artifact_paths = _string_dict(state.get("latest_artifacts"))

        def read(ref: str) -> dict[str, object]:
            return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)

        contract_ref, packet_ref = str(pin.get("approved_contract") or ""), str(pin.get("work_packet") or "")
        if not contract_ref or not packet_ref:
            raise HarnessError(
                "runtime_closure_contract_not_approved",
                f"Runtime closure post-execution needs an approved contract and a closure packet: {run_id}",
                run_id=run_id,
            )
        frozen_ref = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
        frozen_packet = read(frozen_ref)
        review_ref = artifact_paths.get("runtime_closure_contract_review")
        return {
            # Validator warnings the owner accepted by approving the contract checkpoint.
            "accepted_contract_warnings": _dict_items(read(review_ref).get("warnings")) if review_ref else [],
            "pin": pin,
            "contract": read(contract_ref),
            "packet": read(packet_ref),
            "frozen_packet": frozen_packet,
            "frozen_packet_file": self._existing_artifact_path(frozen_ref, run_id=run_id),
            "context": read(_required_phase4_artifact(artifact_paths, "runtime_closure_context", run_id=run_id)),
            "target_info": self._workspace_target_info(run_id=run_id, work_packet=frozen_packet),
            "planning_policy": _planning_policy_for_state(state, run_id=run_id),
        }

    def _set_closure_stage(self, state: dict[str, object], stage: str) -> dict[str, object]:
        pin = {**_dict_value(state.get("runtime_closure")), "status": stage}
        state["runtime_closure"] = pin
        self.store.write_state(state)
        return pin

    def _continue_runtime_closure_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """G14, then the closure verification suite from a clean database (mechanical only)."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        self._set_closure_stage(state, "verifying")
        try:
            inputs = self._closure_run_inputs(state=state, run_id=run_id)
            contract, packet = inputs["contract"], inputs["packet"]
            target_path = inputs["target_info"].path
            g14 = _closure_post_execution_check(
                contract=contract,
                packet=packet,
                pin=inputs["pin"],
                repo=_ClosureRepository(target_path),
                frozen_packet_file=inputs["frozen_packet_file"],
                target_path=target_path,
            )
            if g14["blocked"]:
                raise HarnessError(g14["blocked"][0], g14["blocked"][1], run_id=run_id)
            if g14["issues"]:
                return self._route_closure_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    source="runtime_closure_verification",
                    summary=f"G14 found {len(g14['issues'])} missing closure output(s) after Developer execution.",
                    problems=g14["issues"],
                    extra={"g14_issues": g14["issues"]},
                )
            spec_file = target_path / RUNTIME_PROBE_SPEC_PATH
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            spec_file.write_text(render_runtime_probe_spec(contract), encoding="utf-8")
            policy = build_runtime_closure_stage_policy(
                project=get_project_profile(inputs["planning_policy"].project_profile_id)
            )
            result = self.verification_runner.run(
                target_path=target_path,
                work_packet={**packet, "verification_stage_ids": list(policy.stage_ids)},
                verification_policy=policy,
                runtime_probe=runtime_probe_run(contract),
            )
            classification = _classify_closure_verification(result)
            probe_stage = next((item for item in result.stage_results if item.stage_id == "runtime_probe"), None)
            probe_results = [dict(item) for item in (probe_stage.probe_results if probe_stage else ())]
            probe_failures = runtime_probe_failures(contract, probe_results)
            version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_verification")
            report = {
                "schema_version": "runtime_closure_verification_v1",
                "run_id": run_id,
                "policy_version": policy.policy_version,
                **result.to_dict(),
                "classification": classification,
                "probe_results": probe_results,
                "probe_failures": probe_failures,
            }
            report_path = _write_closure_json(self.store, run_id, f"runtime_closure_verification_v{version}.json", report)
            latest_artifacts["runtime_closure_verification"] = report_path
            state["quality_reports"] = {
                **_dict_value(state.get("quality_reports")),
                "runtime_closure_verification": {
                    "ok": result.status == "passed",
                    "status": result.status,
                    "classification": classification,
                    "planned_stage_ids": list(result.planned_stage_ids),
                    "executed_stage_ids": list(result.executed_stage_ids),
                    "stage_results": [item.to_dict() for item in result.stage_results],
                    "probe_results": probe_results,
                    "report": report_path,
                },
            }
            if classification["kind"] == "implementation":
                return self._route_closure_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    source="runtime_closure_verification",
                    summary=result.reason,
                    problems=_verification_problems(
                        result=result,
                        classification=classification,
                        probe_failures=probe_failures,
                        known_paths=_closure_known_paths(inputs),
                    ),
                    extra={"verification": _verification_feedback(result), "probe_failures": probe_failures},
                )
        except HarnessError as exc:
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc)

        if classification["kind"] != "passed":
            error_code = (
                "runtime_closure_environment_failed"
                if classification["kind"] == "environment"
                else "runtime_closure_verification_policy_failed"
            )
            return self._block_runtime_closure(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=HarnessError(error_code, f"{classification['stage_id'] or 'verification'}: {result.reason}", run_id=run_id),
            )
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closure_verified",
                "latest_artifacts": latest_artifacts,
                "runtime_closure": {**_dict_value(state.get("runtime_closure")), "status": "verified"},
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Closure verification passed for run {run_id}: {len(probe_results)} probe(s) answered as approved."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _route_closure_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        source: str,
        summary: str,
        problems: list[dict[str, object]],
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Route mechanically to closure units (rules 1-5) or owning Plan units (D11 a); one shared counter."""

        routed = route_closure_problems(
            problems=problems,
            contract=inputs["contract"],
            packet=inputs["packet"],
            frozen_packet=inputs["frozen_packet"],
            planning_policy=inputs["planning_policy"],
            target_path=inputs["target_info"].path,
        )
        if routed["conflicts"]:
            raise HarnessError(
                "runtime_closure_scope_conflict",
                "Repair would touch paths outside the approved closure and Plan scope: "
                + "; ".join(routed["conflicts"]),
                run_id=run_id,
            )
        pin = _dict_value(state.get("runtime_closure"))
        iterations = _int_value(pin.get("repair_iterations"))
        if iterations >= max(0, int(self.settings.implementation_repair_limit)):
            raise HarnessError(
                "runtime_closure_repair_limit_exceeded",
                f"Runtime closure repair limit reached ({iterations}) with an open {source} failure: {summary}",
                run_id=run_id,
            )
        version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_repair_feedback")
        feedback = closure_repair_feedback(
            run_id=run_id,
            version=version,
            repair_iteration=iterations + 1,
            source=source,
            summary=summary,
            findings=routed["findings"],
            plan_extensions=routed["plan_extensions"],
            allowed_paths=_string_items(_dict_value(inputs["packet"].get("output_contract")).get("allowed_paths")),
            extra=extra or {},
        )
        latest_artifacts["runtime_closure_repair_feedback"] = _write_closure_json(
            self.store, run_id, f"runtime_closure_repair_feedback_v{version}.json", feedback
        )
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closure_repair_needed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "runtime_closure": {**pin, "status": "repair_needed"},
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Runtime closure cần repair ({source}) cho run {run_id}: {summary} "
                    f"Routed to units {sorted({unit for item in routed['findings'] for unit in item['unit_ids']})}."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_runtime_closure_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Developer repair over the cited closure units (plus D11 a Plan units), then back to G14."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("runtime_closure"))
        session = _dict_value(pin.get("repair_session"))
        iteration = _int_value(pin.get("repair_iterations")) + 1
        artifact_version = _int_value(session.get("artifact_version"))
        try:
            inputs = self._closure_run_inputs(state=state, run_id=run_id)
            target_path = inputs["target_info"].path
            feedback_ref = _required_phase4_artifact(latest_artifacts, "runtime_closure_repair_feedback", run_id=run_id)
            feedback = _read_json_artifact(self._existing_artifact_path(feedback_ref, run_id=run_id), run_id=run_id)
            if session.get("feedback") != feedback_ref:
                packet = inputs["packet"]
                packet_ref = str(pin.get("work_packet") or "")
                extensions = _dict_items(feedback.get("plan_extensions"))
                if extensions:
                    packet_version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_work_packet")
                    packet = extend_closure_work_packet(
                        packet=packet,
                        frozen_packet=inputs["frozen_packet"],
                        extensions=extensions,
                        version=packet_version,
                    )
                    packet_ref = _write_closure_json(
                        self.store, run_id, f"runtime_closure_work_packet_v{packet_version}.json", packet
                    )
                allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
                artifact_version = _next_phase4_artifact_version(self.store, run_id)
                recorded = [
                    {"path": path, "rule": "D11a", "plan_unit_id": str(item.get("plan_unit_id") or ""), "feedback": feedback_ref}
                    for item in extensions
                    for path in _string_items(item.get("paths"))
                ]
                pin = {
                    **pin,
                    "status": "repairing",
                    "work_packet": packet_ref,
                    "scope_extensions": [
                        *_dict_items(pin.get("scope_extensions")),
                        *[item for item in recorded if item not in _dict_items(pin.get("scope_extensions"))],
                    ],
                    "repair_session": {
                        "feedback": feedback_ref,
                        "artifact_version": artifact_version,
                        "expected_dirty_paths": [
                            path
                            for path in worktree_dirty_paths(target_path)
                            if path not in allowed and path != ".codegraph" and not path.startswith(".codegraph/")
                        ],
                    },
                }
                latest_artifacts["runtime_closure_work_packet"] = packet_ref
                state.update({"runtime_closure": pin, "latest_artifacts": latest_artifacts, "updated_at": utc_now_iso()})
                self.store.write_state(state)
                inputs["packet"] = packet
            session = _dict_value(pin.get("repair_session"))
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=inputs["packet"],
                target_path=target_path,
                latest_artifacts=latest_artifacts,
                mode="runtime_closure_repair",
                current_phase="runtime_closure",
                repair_iteration=iteration,
                session_iteration=iteration,
                artifact_version=artifact_version,
                manifest_key="runtime_closure_repair_execution_manifest",
                journal_key="runtime_closure_repair_execution_journal",
                repair_feedback=feedback,
                repair_scope="runtime_closure",
                expected_dirty_paths=_string_items(session.get("expected_dirty_paths")),
            )
            runtime_config_files = self._materialize_runtime_config(
                run_id=run_id,
                work_packet=inputs["packet"],
                target_path=target_path,
                artifact_version=artifact_version,
                latest_artifacts=latest_artifacts,
            )
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report={
                    "run_id": run_id,
                    "source_ref": state.get("source_ref"),
                    "issue_key": inputs["frozen_packet"].get("issue_key"),
                    "target_repo": str(target_path),
                    "branch": inputs["target_info"].current_branch,
                    "summary": result.summary,
                    "files": [*result.files, *runtime_config_files],
                    "repair_iteration": iteration,
                    "repair_feedback": feedback_ref,
                    "repair_source": str(feedback.get("feedback_source") or ""),
                    "execution_mode": "tool_loop",
                    "runtime_closure_work_packet": pin.get("work_packet"),
                    "developer_execution_manifest": result.manifest_path,
                    "developer_execution_journal": result.journal_path,
                },
                version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            for key, value in (
                ("runtime_closure_repair_execution_manifest", exc.manifest_path),
                ("runtime_closure_repair_execution_journal", exc.journal_path),
                ("runtime_closure_repair_execution_session", exc.session_path),
                ("developer_failure_classification", exc.failure_path),
            ):
                if value:
                    latest_artifacts[key] = value
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "runtime_closure",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "allowed_next_actions": (
                        ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS if paused else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "developer_execution": self._developer_execution_state(
                        state,
                        executor="tool_loop",
                        mode="runtime_closure_repair",
                        repair_iteration=iteration,
                        session_iteration=iteration,
                        artifact_version=artifact_version,
                        progress=exc.progress,
                        status="paused" if paused else "blocked",
                    ),
                    "summary_for_user": f"Runtime closure repair {'tạm dừng' if paused else 'bị chặn'} cho run {run_id}: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc)

        latest_artifacts["code_change_report"] = report_path
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closure_written",
                "latest_artifacts": latest_artifacts,
                "runtime_closure": self._with_session_scope_extensions(
                    {
                        **_dict_value(state.get("runtime_closure")),
                        "status": "written",
                        "repair_iterations": iteration,
                        "repair_session": None,
                    },
                    journal_ref=result.journal_path,
                    run_id=run_id,
                ),
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Runtime closure repair {iteration} xong cho run {run_id}; chạy lại G14 và verification.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_runtime_closure_review(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Code Validator regression (cached tasks), then one closure code review, then runtime_closed."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        self._set_closure_stage(state, "reviewing")
        try:
            inputs = self._closure_run_inputs(state=state, run_id=run_id)
            contract, packet, pin = inputs["contract"], inputs["packet"], inputs["pin"]
            target_path = inputs["target_info"].path
            verification = _dict_value(_dict_value(state.get("quality_reports")).get("runtime_closure_verification"))
            changes = approved_closure_changes(contract=contract, packet=packet, pin=pin)
            trace_result = self._validate_code_trace_snapshot(
                state=state,
                artifact_paths=latest_artifacts,
                run_id=run_id,
                target_path=target_path,
                work_packet=inputs["frozen_packet"],
                verification_evidence=verification,
                approved_closure_changes=changes,
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_code_validation_step_review(state=state, run_id=run_id, progress=exc.progress)
        except HarnessError as exc:
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc)

        try:
            version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_regression")
            latest_artifacts["runtime_closure_regression"] = _write_closure_json(
                self.store, run_id, f"runtime_closure_regression_v{version}.json", trace_result.to_dict()
            )
            if not trace_result.ok:
                if not _is_repairable_code_trace_failure(trace_result):
                    raise HarnessError(
                        _nonrepairable_code_trace_error_code(trace_result),
                        f"Code Validator regression after closure did not pass: {trace_result.summary}",
                        run_id=run_id,
                    )
                return self._route_closure_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    source="validator_code_trace",
                    summary=str(trace_result.summary),
                    problems=_regression_problems(trace_result),
                    extra={"missing_links": list(getattr(trace_result, "missing_links", []) or [])},
                )
            reviewer = self._closure_code_reviewer(run_id=run_id, iteration=_int_value(pin.get("repair_iterations")))
            review = reviewer.review_code(
                contract=contract,
                code_view={
                    "accepted_contract_warnings": inputs["accepted_contract_warnings"],
                    **_closure_code_view(
                        contract=contract,
                        packet=packet,
                        pin=pin,
                        repo=_ClosureRepository(target_path),
                        verification=verification,
                    ),
                },
                known_refs=_closure_known_refs(context=inputs["context"], work_packet=inputs["frozen_packet"])
                | _contract_object_refs(contract),
                run_id=run_id,
            )
            review_version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_code_review")
            latest_artifacts["runtime_closure_code_review"] = _write_closure_json(
                self.store, run_id, f"runtime_closure_code_review_v{review_version}.json", review
            )
            conflicts = [
                item for item in review["blocking_findings"] if item["category"] == FROZEN_CONTRACT_CONFLICT_CATEGORY
            ]
            if conflicts:
                raise HarnessError(
                    "runtime_closure_frozen_contract_conflict",
                    "Closure code review needs a frozen Requirement/Design/Plan change: "
                    + "; ".join(item["issue"] for item in conflicts),
                    run_id=run_id,
                )
            if review["blocking_findings"]:
                return self._route_closure_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    source="runtime_closure_code_review",
                    summary=f"Closure code review found {len(review['blocking_findings'])} blocker(s).",
                    problems=[
                        {
                            "source": "runtime_closure_code_review",
                            "issue": item["issue"],
                            "expected_change": item["expected_change"],
                            "input_ids": item["input_ids"],
                            "unit_ids": item["unit_ids"],
                            "probe_ids": item["probe_ids"],
                            "paths": [],
                        }
                        for item in review["blocking_findings"]
                    ],
                    extra={"findings": review["blocking_findings"]},
                )
        except LlmStepReviewRequired as exc:
            return self._pause_for_runtime_closure_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status="runtime_closure_verified",
            )
        except HarnessError as exc:
            return self._block_runtime_closure(state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc)
        return self._close_runtime_closure(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            contract=contract,
            verification=verification,
            review=review,
        )

    def _closure_code_reviewer(self, *, run_id: str, iteration: int) -> RuntimeClosureValidator:
        validator = self.closure_validator
        if not bool(self.runtime_closure_step_review.get("enabled")):
            return validator
        return RuntimeClosureValidator(
            ReviewableLlmClient(
                validator.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.runtime_closure_step_review,
                call_prefix="runtime-closure-code-review",
                execution_dir=f"runtime_closure_execution/step_review/code_review_r{iteration}",
                error_prefix="runtime_closure",
                label="Closure code review",
            )
        )

    def _close_runtime_closure(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        contract: dict[str, object],
        verification: dict[str, object],
        review: dict[str, object],
    ) -> dict[str, object]:
        pin = _dict_value(state.get("runtime_closure"))
        runnable = closure_runnable_status(contract)
        report_version = _next_closure_artifact_version(self.store, run_id, "runtime_closure_report", suffix=".md")
        report_path = _write_closure_text(
            self.store,
            run_id,
            f"runtime_closure_report_v{report_version}.md",
            _closure_report_markdown(
                contract=contract,
                runnable=runnable,
                verification=verification,
                review=review,
                pin=pin,
            ),
        )
        latest_artifacts["runtime_closure_report"] = report_path
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "runtime_closure",
                "status": "runtime_closed",
                "latest_artifacts": latest_artifacts,
                "runtime_closure": {**pin, "status": "runtime_closed", "closed_at": utc_now_iso()},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "runtime_closure": {
                        "ok": True,
                        **runnable,
                        "repair_iterations": _int_value(pin.get("repair_iterations")),
                        "scope_extensions": _dict_items(pin.get("scope_extensions")),
                        "probe_results": _dict_items(verification.get("probe_results")),
                        "review_warning_count": len(_dict_items(review.get("warnings"))),
                        "report": report_path,
                    },
                },
                "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Runtime closure đã đóng cho run {run_id}: runnable={runnable['runnable_status']}, "
                    f"blocked scenarios={runnable['blocked_scenario_ids'] or 'none'}. Báo cáo: `{report_path}`."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _with_session_scope_extensions(
        self,
        pin: dict[str, object],
        *,
        journal_ref: str,
        run_id: str,
    ) -> dict[str, object]:
        """Copy the D11 (b) new files a Developer session recorded into the run's scope_extensions."""

        if not journal_ref:
            return pin
        journal = _read_json_artifact(self._existing_artifact_path(journal_ref, run_id=run_id), run_id=run_id)
        known = {(str(item.get("path") or ""), str(item.get("rule") or "")) for item in _dict_items(pin.get("scope_extensions"))}
        added = [
            {"path": str(row.get("path") or ""), "rule": "D11b", "item_id": str(row.get("item_id") or ""), "journal": journal_ref}
            for row in _dict_items(journal.get("scope_extensions"))
            if str(row.get("path") or "") and (str(row.get("path") or ""), "D11b") not in known
        ]
        return {**pin, "scope_extensions": [*_dict_items(pin.get("scope_extensions")), *added]} if added else pin

    def _runtime_closure_publication_paths(self, *, state: dict[str, object], run_id: str) -> list[str]:
        """Closure files published with the Draft PR (D13): closure packet paths, D11 extensions, probe spec."""

        pin = _dict_value(state.get("runtime_closure"))
        if str(pin.get("status") or "") != "runtime_closed" or not pin.get("work_packet"):
            return []
        packet = _read_json_artifact(
            self._existing_artifact_path(str(pin["work_packet"]), run_id=run_id), run_id=run_id
        )
        return sorted(
            {
                *_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")),
                *(str(item.get("path") or "") for item in _dict_items(pin.get("scope_extensions"))),
                RUNTIME_PROBE_SPEC_PATH,
            }
            - {""}
        )

    # ------------------------------------------------------------------ checkpoint

    def approve_runtime_closure_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Owner approves the closure contract; execution starts in a later transition."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != "waiting_runtime_closure_review":
                raise HarnessError(
                    "checkpoint_not_current",
                    f"Run is not waiting for runtime closure review: {run_id}",
                    run_id=run_id,
                )
            checkpoint = _dict_value(state.get("pending_checkpoint"))
            if checkpoint.get("type") != RUNTIME_CLOSURE_CHECKPOINT_TYPE:
                raise HarnessError(
                    "checkpoint_not_current",
                    f"Pending checkpoint is not a runtime closure review: {run_id}",
                    run_id=run_id,
                )
            current_id = str(checkpoint.get("checkpoint_id") or "")
            if checkpoint_id and checkpoint_id != current_id:
                raise HarnessError(
                    "checkpoint_not_current",
                    f"Checkpoint {checkpoint_id} is not the current runtime closure checkpoint {current_id}.",
                    run_id=run_id,
                )
            pin = _dict_value(state.get("runtime_closure"))
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "runtime_closure",
                    "status": "runtime_closure_ready",
                    "pending_checkpoint": None,
                    "runtime_closure": {
                        **pin,
                        "status": "approved",
                        "approved_checkpoint_id": current_id,
                        "approved_contract": str(checkpoint.get("contract_path") or ""),
                        "approved_at": utc_now_iso(),
                    },
                    "allowed_next_actions": ALLOWED_RUNTIME_CLOSURE_AUTOMATION_ACTIONS,
                    "last_error": None,
                    "summary_for_user": f"Owner đã duyệt runtime closure contract cho run {run_id}.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)


# ---------------------------------------------------------------------- helpers


class _ClosureRepository:
    """Read-only repository facts for gates G4/G8 and Planner grounding."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._texts: dict[str, str | None] = {}

    def text(self, path: str) -> str | None:
        if path not in self._texts:
            candidate = (self.root / path).resolve()
            try:
                candidate.relative_to(self.root)
                self._texts[path] = candidate.read_text(encoding="utf-8", errors="replace") if candidate.is_file() else None
            except (OSError, ValueError):
                self._texts[path] = None
        return self._texts[path]

    def symbol_exists(self, path: str, symbol: str) -> bool:
        # shortcut: declaration regex, not the TypeScript AST; switch to the AST extractor if
        # re-exports or computed exports become common providers.
        text = self.text(path)
        if text is None:
            return False
        name = re.escape(symbol)
        return bool(
            re.search(rf"\b(?:function\*?|class|const|let|var|interface|type|enum)\s+{name}\b", text)
            or re.search(rf"\bexport\s*\{{[^}}]*\b{name}\b[^}}]*\}}", text)
        )

    def references(self, earlier: str, later: str) -> bool | None:
        """True when `earlier` imports `later`'s module and names its symbol; None when unknown."""

        earlier_path, _, _ = earlier.partition("#")
        later_path, _, later_symbol = later.partition("#")
        text = self.text(earlier_path)
        if text is None or not later_symbol or later_symbol not in text:
            return None
        if earlier_path == later_path:
            return True
        target = _strip_source_suffix(later_path)
        base = PurePosixPath(earlier_path).parent
        for specifier in _RELATIVE_IMPORT.findall(text):
            resolved = _strip_source_suffix(_normalize_posix(str(base / specifier)))
            # `./x` may name `x.ts` or the directory module `x/index.ts`.
            if resolved and (resolved == target or f"{resolved}/index" == target):
                return True
        return None

    def grounding_files(
        self,
        *,
        context: dict[str, object],
        work_packet: dict[str, object],
        planning_policy,
    ) -> list[dict[str, object]]:
        """Verbatim source of composition hops, providers, attachment owners and script entry files."""

        ordered: list[str] = []

        def add(path: str) -> None:
            path = path.partition("#")[0]
            if path and path not in ordered and self.text(path) is not None:
                ordered.append(path)

        for item in _dict_items(context.get("runtime_inputs")):
            for path in item.get("composition_paths") or []:
                for hop in _string_items(path):
                    add(hop)
            add(str(item.get("consumer") or ""))
            add(str(item.get("provided_by") or ""))
            add(str(_dict_value(item.get("env")).get("read_by") or ""))
        bound_components: set[str] = set()
        for unit in _dict_items(work_packet.get("execution_units")):
            for binding in _dict_items(unit.get("runtime_bindings")):
                add(str(_dict_value(binding.get("attachment")).get("owner_path") or ""))
                target = str(_dict_value(binding.get("target")).get("path") or "")
                component = planning_policy.component_for_path(target)
                if component:
                    bound_components.add(component)
        for path in self._script_entry_files(planning_policy=planning_policy, components=bound_components):
            add(path)
        # One level of relative imports: the behavior a hop or provider delegates to (for example the
        # module a route calls) decides which status a probe can expect. Added last, so the budget
        # truncates them before the primary files.
        for path in list(ordered):
            for imported in self._relative_imports(path):
                # Only editable source modules: stylesheets and generated code never decide runtime behavior.
                if planning_policy.normalize_editable_path(imported) == imported:
                    add(imported)

        files: list[dict[str, object]] = []
        total = 0
        for path in ordered:
            content = self.text(path) or ""
            budget = max(0, min(MAX_SOURCE_FILE_CHARS, MAX_SOURCE_TOTAL_CHARS - total))
            included = content[:budget]
            total += len(included)
            files.append(
                {
                    "path": path,
                    "content": included,
                    "chars_total": len(content),
                    "content_truncated": len(included) < len(content),
                }
            )
        return files

    def _relative_imports(self, path: str) -> list[str]:
        """Existing files a module imports through `./` or `../` specifiers, resolved like TypeScript."""

        text = self.text(path)
        if text is None:
            return []
        base = PurePosixPath(path).parent
        found: list[str] = []
        for specifier in _RELATIVE_IMPORT.findall(text):
            resolved = _normalize_posix(str(base / specifier))
            if not resolved:
                continue
            stem = _strip_source_suffix(resolved)
            # Source modules only: `./x.css` and other assets have no source suffix and resolve to nothing.
            candidates = [*(f"{stem}{suffix}" for suffix in _SOURCE_SUFFIXES),
                          *(f"{resolved}/index{suffix}" for suffix in _SOURCE_SUFFIXES)]
            match = next((item for item in candidates if self.text(item) is not None), "")
            if match and match not in found:
                found.append(match)
        return found

    def _script_entry_files(self, *, planning_policy, components: set[str]) -> list[str]:
        manifests = [self.root / "package.json"]
        for parent in ("apps", "packages"):
            if (self.root / parent).is_dir():
                manifests.extend(sorted((self.root / parent).glob("*/package.json")))
        found: list[str] = []
        for manifest in manifests:
            try:
                scripts = dict(json.loads(manifest.read_text(encoding="utf-8")).get("scripts") or {})
            except (OSError, ValueError, AttributeError):
                continue
            base = manifest.parent.relative_to(self.root).as_posix()
            for command in scripts.values():
                for token in _SCRIPT_FILE.findall(str(command)):
                    path = _normalize_posix(token if base == "." else f"{base}/{token}")
                    candidates = [path]
                    config_text = self.text(path) if path else None
                    if config_text is not None and planning_policy.normalize_editable_path(path) != path:
                        # A tool config named by a script may itself name the entry file (one level only).
                        config_base = PurePosixPath(path).parent.as_posix()
                        candidates += [
                            _normalize_posix(f"{config_base}/{inner}") for inner in _SCRIPT_FILE.findall(config_text)
                        ]
                    for candidate in candidates:
                        if (
                            candidate
                            and candidate not in found
                            and planning_policy.normalize_editable_path(candidate) == candidate
                            and planning_policy.component_for_path(candidate) in components
                            and self.text(candidate) is not None
                        ):
                            found.append(candidate)
        return found


_HARNESS_OWNED_PATHS = {".env.example", RUNTIME_PROBE_SPEC_PATH}
_PROBE_SERVICE_MARKER = re.compile(r"\[(api|web)\]")


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    except OSError:
        return ""


def _closure_post_execution_check(
    *,
    contract: dict[str, object],
    packet: dict[str, object],
    pin: dict[str, object],
    repo: _ClosureRepository,
    frozen_packet_file: Path,
    target_path: Path,
) -> dict[str, object]:
    """G14: providers, stand-ins and env config exist; scope and frozen artifacts are untouched."""

    issues: list[dict[str, object]] = []
    env_inputs: dict[str, list[str]] = {}
    for item in _dict_items(contract.get("runtime_inputs")):
        resolution = _dict_value(item.get("resolution"))
        provider = _dict_value(resolution.get("provider"))
        input_id = str(item.get("input_id") or "")
        if provider.get("kind") == "env_var":
            env_inputs.setdefault(str(provider.get("env_key") or ""), []).append(input_id)
        if provider.get("kind") != "symbol" or resolution.get("kind") == "unresolved_external":
            continue
        path, _, symbol = str(provider.get("ref") or "").partition("#")
        if not repo.symbol_exists(path, symbol):
            issues.append(
                {
                    "source": "g14",
                    "check": "provider_missing",
                    "input_ids": [input_id],
                    "paths": [path],
                    "issue": f"Provider `{path}#{symbol}` of input `{input_id}` does not exist after closure execution.",
                    "expected_change": f"`{path}` declares `{symbol}` as the approved provider.",
                }
            )
    for stand_in in _dict_items(contract.get("verification_stand_ins")):
        module, start_export = str(stand_in.get("module") or ""), str(stand_in.get("start_export") or "")
        if not repo.symbol_exists(module, start_export):
            issues.append(
                {
                    "source": "g14",
                    "check": "stand_in_missing",
                    "input_ids": [str(stand_in.get("for_input_id") or "")],
                    "paths": [module],
                    "issue": f"Stand-in `{module}#{start_export}` does not exist after closure execution.",
                    "expected_change": f"`{module}` exports `{start_export}` with the approved start signature.",
                }
            )
    for issue in runtime_config_execution_issues(
        repo_root=target_path,
        changes=env_runtime_config_changes(packet.get("runtime_config_impact")),
    ):
        issues.append(
            {
                "source": "g14",
                "check": str(issue.get("error_code") or "runtime_config"),
                "input_ids": env_inputs.get(str(issue.get("name") or ""), []),
                "paths": [str(issue.get("read_by") or "")] if issue.get("read_by") else [],
                "issue": str(issue.get("message") or ""),
                "expected_change": "The approved env var is read with its approved default and .env.example keeps it.",
            }
        )

    blocked: tuple[str, str] | None = None
    if _file_sha256(frozen_packet_file) != str(pin.get("frozen_packet_sha256") or ""):
        blocked = ("runtime_closure_frozen_contract_conflict", "The frozen Developer work packet changed during runtime closure.")
    allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
    extensions = {str(item.get("path") or "") for item in _dict_items(pin.get("scope_extensions"))}
    baselines = _dict_value(pin.get("expected_dirty_hashes"))
    exempt = allowed | extensions | _HARNESS_OWNED_PATHS
    drifted = sorted(
        path for path, sha in baselines.items() if path not in exempt and _file_sha256(target_path / path) != sha
    )
    outside = sorted(
        path
        for path in worktree_dirty_paths(target_path)
        if path not in exempt
        and path not in baselines
        and path != ".codegraph"
        and not path.startswith(".codegraph/")
    )
    if blocked is None and (drifted or outside):
        blocked = (
            "runtime_closure_scope_conflict",
            f"Files changed outside the approved closure scope: drifted baselines={drifted}, new={outside}.",
        )
    return {"issues": issues, "blocked": blocked}


def _classify_closure_verification(result) -> dict[str, object]:
    """G16: environment / policy / implementation from stage ids, timeouts and tool markers only."""

    if result.status == "passed":
        return {"kind": "passed", "stage_id": "", "timed_out": False, "webserver_names": [], "reason": ""}
    failed = [item for item in result.stage_results if item.status != "passed"]
    stage = next((item for item in failed if item.stage_id != "cleanup"), failed[0] if failed else None)
    if stage is None:
        return {
            "kind": result.failure_kind or "environment",
            "stage_id": "",
            "timed_out": bool(result.timed_out),
            "webserver_names": [],
            "reason": result.reason,
        }
    text = f"{stage.stdout}\n{stage.stderr}"
    kind = stage.failure_kind or "implementation"
    if stage.stage_id in {"database_ready", "cleanup"}:
        kind = "environment"
    elif stage.timed_out:
        # By runtime_probe the database, migrations and build already passed: a service that never
        # becomes ready is a composition defect, unless a tool marker says the machine is at fault.
        environment_marker = _command_failure_kind(stdout=stage.stdout, stderr=stage.stderr) == "environment"
        kind = "implementation" if stage.stage_id == "runtime_probe" and not environment_marker else "environment"
    elif kind == "policy" and stage.stage_id == "runtime_probe" and "config.webserver" in text.casefold():
        kind = "implementation"
    return {
        "kind": kind,
        "stage_id": stage.stage_id,
        "timed_out": bool(stage.timed_out),
        "webserver_names": sorted(set(_PROBE_SERVICE_MARKER.findall(text))),
        "reason": stage.reason,
    }


def _closure_known_paths(inputs: dict[str, object]) -> list[str]:
    return sorted(
        {
            *_string_items(_dict_value(inputs["packet"].get("output_contract")).get("allowed_paths")),
            *_string_items(_dict_value(inputs["frozen_packet"].get("output_contract")).get("allowed_paths")),
        }
    )


def _verification_problems(
    *,
    result,
    classification: dict[str, object],
    probe_failures: list[dict[str, object]],
    known_paths: list[str],
) -> list[dict[str, object]]:
    problems = [
        {
            "source": "runtime_probe",
            "probe": failure,
            "input_ids": _string_items(failure.get("proves_input_ids")),
            "paths": [],
            "issue": (
                f"Probe {failure['probe_id']} ({failure['method']} {failure['path']} on {failure['service']}) expected "
                f"status {failure['expected_status']}"
                + (
                    f" and >= {failure['expected_min_items']} item(s)"
                    if failure.get("expected_min_items") is not None
                    else ""
                )
                + f" but got status {failure['actual_status']} with {failure['actual_items']} item(s)."
            ),
            "expected_change": f"Probe {failure['probe_id']} answers as approved for scenarios {failure['scenario_ids']}.",
        }
        for failure in probe_failures
    ]
    if problems:
        return problems
    stage = next(
        (item for item in result.stage_results if item.stage_id == classification.get("stage_id")),
        None,
    )
    text = f"{stage.stdout}\n{stage.stderr}" if stage else f"{result.stdout}\n{result.stderr}"
    return [
        {
            "source": str(classification.get("stage_id") or "verification"),
            "paths": [path for path in known_paths if path and path in text.replace("\\", "/")],
            "webserver_names": list(classification.get("webserver_names") or []),
            "issue": f"Closure verification stage `{classification.get('stage_id')}` failed: {classification.get('reason')}",
            "expected_change": f"Stage `{classification.get('stage_id')}` passes from a clean, freshly seeded database.",
        }
    ]


def _verification_feedback(result) -> dict[str, object]:
    return {
        "status": result.status,
        "reason": result.reason,
        "failure_kind": result.failure_kind,
        "planned_stage_ids": list(result.planned_stage_ids),
        "executed_stage_ids": list(result.executed_stage_ids),
        "stage_results": [
            {
                **{key: value for key, value in item.to_dict().items() if key not in {"stdout", "stderr"}},
                "stdout": _bounded_diagnostic(item.stdout),
                "stderr": _bounded_diagnostic(item.stderr),
            }
            for item in result.stage_results
        ],
    }


def _regression_problems(trace_result) -> list[dict[str, object]]:
    problems = []
    for link in list(getattr(trace_result, "missing_links", []) or []):
        if not isinstance(link, dict) or link.get("blocking") is False:
            continue
        paths = sorted(
            {
                value.partition("#")[0].replace("\\", "/")
                for key in ("code_refs", "test_refs")
                for value in _string_items(link.get(key))
                if value
            }
        )
        problems.append(
            {
                "source": "validator_code_trace",
                "unit_ids": _string_items(link.get("unit_ids")),
                "paths": paths,
                "issue": str(link.get("message") or link.get("issue") or ""),
                "expected_change": str(link.get("expected_change") or "The approved claim holds again after closure."),
            }
        )
    return problems


def _closure_code_view(
    *,
    contract: dict[str, object],
    packet: dict[str, object],
    pin: dict[str, object],
    repo: _ClosureRepository,
    verification: dict[str, object],
) -> dict[str, object]:
    paths = list(
        dict.fromkeys(
            [
                *_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")),
                *(str(item.get("path") or "") for item in _dict_items(pin.get("scope_extensions"))),
                RUNTIME_PROBE_SPEC_PATH,
            ]
        )
    )
    sources = []
    total = 0
    for path in paths:
        content = repo.text(path)
        if content is None:
            continue
        included = content[: max(0, min(MAX_SOURCE_FILE_CHARS, MAX_SOURCE_TOTAL_CHARS - total))]
        total += len(included)
        sources.append(
            {"path": path, "content": included, "chars_total": len(content), "content_truncated": len(included) < len(content)}
        )
    probes = {str(item.get("probe_id") or ""): item for item in _dict_items(contract.get("runtime_probes"))}
    by_scenario: dict[str, list[dict[str, object]]] = {}
    for outcome in _dict_items(verification.get("probe_results")):
        probe = probes.get(str(outcome.get("probe_id") or ""), {})
        for scenario_id in _string_items(probe.get("scenario_ids")):
            by_scenario.setdefault(scenario_id, []).append(outcome)
    return {
        "closure_sources": sources,
        "probe_results_by_scenario": by_scenario,
        "verification_stages": [
            {"stage_id": item.get("stage_id"), "status": item.get("status")}
            for item in _dict_items(verification.get("stage_results"))
        ],
        "scope_extensions": _dict_items(pin.get("scope_extensions")),
    }


def _closure_report_markdown(
    *,
    contract: dict[str, object],
    runnable: dict[str, object],
    verification: dict[str, object],
    review: dict[str, object],
    pin: dict[str, object],
) -> str:
    """The only valid input for a later Documentation Closure: proven commands, env and probe outcomes."""

    def cell(value: object) -> str:
        return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")

    outcomes = {str(item.get("probe_id") or ""): item for item in _dict_items(verification.get("probe_results"))}
    blocked = {
        scenario: str(item.get("reason") or "")
        for item in _dict_items(contract.get("unresolved"))
        for scenario in _string_items(item.get("blocks_scenarios"))
    }
    lines = [
        "# Runtime Closure Report",
        "",
        f"- Run: `{contract.get('run_id')}`",
        f"- Runnable status: `{runnable['runnable_status']}`",
        f"- Blocked scenarios: {', '.join(runnable['blocked_scenario_ids']) or 'none'}",
        f"- Repair iterations: {_int_value(pin.get('repair_iterations'))}",
        "",
        "## Commands (closure verification, in order)",
        "",
    ]
    lines += [
        f"- `{item.get('stage_id')}`: `{' '.join(_string_items(item.get('argv'))) or '(not started)'}` → {item.get('status')}"
        for item in _dict_items(verification.get("stage_results"))
    ] or ["- none"]
    lines += [
        "",
        "## Environment",
        "",
        "- `DATABASE_URL`, `COMPOSE_PROJECT_NAME`, `SDLC_POSTGRES_PORT`: isolated per run by the Harness.",
    ]
    lines += [
        f"- `{item.get('env_key')}`: loopback URL of stand-in `{item.get('stand_in_id')}` "
        f"(`{item.get('module')}`), verification only; production keeps the approved default."
        for item in _dict_items(contract.get("verification_stand_ins"))
    ]
    lines += ["", "## Probes by scenario", "", "| Scenario | Probe | Request | Expected | Actual | Result |", "| --- | --- | --- | --- | --- | --- |"]
    for probe in _dict_items(contract.get("runtime_probes")):
        outcome = _dict_value(outcomes.get(str(probe.get("probe_id") or "")))
        observation = _dict_value(outcome.get("observation"))
        expected = f"{probe.get('expect_status')}" + (
            f", ≥{probe.get('expect_json_array_min_items')} items" if probe.get("expect_json_array_min_items") is not None else ""
        )
        actual = f"{observation.get('actual_status')}" + (
            f", {observation.get('actual_items')} items" if observation.get("actual_items") is not None else ""
        )
        for scenario_id in _string_items(probe.get("scenario_ids")):
            lines.append(
                "| "
                + " | ".join(
                    cell(value)
                    for value in (
                        scenario_id,
                        probe.get("probe_id"),
                        f"{probe.get('method')} {probe.get('service')}{probe.get('path')}",
                        expected,
                        actual if observation else "",
                        outcome.get("status"),
                    )
                )
                + " |"
            )
    for scenario_id, reason in sorted(blocked.items()):
        lines.append(f"| {cell(scenario_id)} | — | — | — | — | blocked: {cell(reason)} |")
    lines += ["", "## Scope extensions (D11)", ""]
    lines += [
        f"- `{item.get('path')}` ({item.get('rule')}, Plan unit `{item.get('plan_unit_id')}`)"
        for item in _dict_items(pin.get("scope_extensions"))
    ] or ["- none"]
    lines += ["", "## Closure review warnings", ""]
    lines += [
        f"- [{item.get('category')}] {item.get('issue')}" for item in _dict_items(review.get("warnings"))
    ] or ["- none"]
    return "\n".join(lines)


def _closure_prompt_view(
    *,
    context: dict[str, object],
    work_packet: dict[str, object],
    planning_policy,
    repo_runtime_config: dict[str, object],
    source_files: list[dict[str, object]],
) -> dict[str, object]:
    cited_nodes = {ref for item in _dict_items(context.get("runtime_inputs")) for ref in _string_items(item.get("source_refs"))}
    policy = runtime_config_policy(planning_policy.project_profile_id)
    return {
        "closure_context": {
            "run_id": context.get("run_id"),
            "entrypoints": context.get("entrypoints"),
            "scenario_ids": context.get("scenario_ids"),
            "runtime_inputs": planner_input_view(context),
            "plan_allowed_paths": context.get("plan_allowed_paths"),
            "approved_runtime_decision_ids": context.get("approved_runtime_decision_ids"),
        },
        "approved_runtime_decisions": _dict_items(work_packet.get("approved_runtime_decisions")),
        "canonical_requirements": _dict_items(work_packet.get("canonical_requirements")),
        "surface_contracts": [
            {
                "unit_id": unit.get("unit_id"),
                "scenario_id": unit.get("scenario_id"),
                "design_node_id": surface.get("design_node_id"),
                "surface_contract": surface.get("surface_contract"),
            }
            for unit in _dict_items(work_packet.get("execution_units"))
            for surface in _dict_items(unit.get("surface_contracts"))
            if str(surface.get("design_node_id") or "") in cited_nodes
        ],
        "plan_runtime_config_impact": work_packet.get("runtime_config_impact"),
        "path_policy": closure_path_policy_view(planning_policy),
        "stage_capabilities": {
            "developer_stage_ids": list(CLOSURE_DEVELOPER_STAGE_IDS),
            "probe_services": list(CLOSURE_PROBE_SERVICES),
            "runtime_probe": RUNTIME_PROBE_CAPABILITY,
            # Ordered closure suite: what runs (and how the database is prepared) before the probes.
            "verification_sequence": build_runtime_closure_stage_policy(
                project=get_project_profile(planning_policy.project_profile_id)
            ).prompt_context()["stage_capabilities"],
        },
        "runtime_config_policy": runtime_config_prompt_context(policy) if policy else None,
        "repo_runtime_config": repo_runtime_config,
        "source_files": source_files,
    }


def _closure_known_refs(*, context: dict[str, object], work_packet: dict[str, object]) -> set[str]:
    refs = {ref for item in _dict_items(context.get("runtime_inputs")) for ref in _string_items(item.get("source_refs"))}
    refs |= set(_string_items(context.get("scenario_ids")))
    refs |= set(_string_items(context.get("approved_runtime_decision_ids")))
    refs |= {str(item.get("id") or "") for item in _dict_items(work_packet.get("canonical_requirements"))}
    refs |= {
        node_id
        for unit in _dict_items(work_packet.get("execution_units"))
        for node_id in _string_items(unit.get("design_node_ids"))
    }
    return refs - {""}


def _contract_object_refs(contract: dict[str, object]) -> set[str]:
    """IDs the contract itself introduces (closure source_refs, CD-*) are citable as refs too."""

    return {
        ref
        for item in _dict_items(contract.get("runtime_inputs"))
        for ref in _string_items(item.get("source_refs"))
    } | {str(item.get("id") or "") for item in _dict_items(contract.get("demo_decisions"))}


def _closure_bound_inputs(state: dict[str, object]) -> dict[str, object]:
    reports = _dict_value(state.get("quality_reports"))
    return {
        "vertical_design_graph_digest": _dict_value(reports.get("design_vertical_graph")).get("graph_digest"),
        "implementation_plan_sha256": _dict_value(reports.get("implementation_plan_review")).get(
            "implementation_plan_sha256"
        ),
        "code_snapshot_fingerprint": _dict_value(reports.get("implementation_traceability")).get(
            "snapshot_fingerprint"
        ),
        "verification_policy_version": CLOSURE_VERIFICATION_POLICY_VERSION,
    }


def _loop_failure(rounds: list[dict[str, object]], error_code: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "review": {"schema_version": "runtime_closure_contract_review_v1", "ok": False, "rounds": rounds, "error_code": error_code},
    }


def _next_closure_contract_version(store, run_id: str) -> int:
    return _next_closure_artifact_version(store, run_id, "runtime_closure_contract")


def _next_closure_artifact_version(store, run_id: str, prefix: str, *, suffix: str = ".json") -> int:
    artifact_dir = store.run_dir(run_id) / "artifacts"
    versions = [
        int(match.group(1))
        for path in (artifact_dir.iterdir() if artifact_dir.exists() else [])
        for match in [re.fullmatch(rf"{re.escape(prefix)}_v(\d+){re.escape(suffix)}", path.name)]
        if match
    ]
    return max(versions, default=0) + 1


def _write_closure_json(store, run_id: str, name: str, payload: dict[str, object]) -> str:
    return _write_closure_text(store, run_id, name, json.dumps(payload, ensure_ascii=False, indent=2))


def _write_closure_text(store, run_id: str, name: str, content: str) -> str:
    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / name
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return store.relative(path)


def _closure_contract_markdown(
    *,
    contract: dict[str, object],
    runnable: dict[str, object],
    review: dict[str, object],
    version: int,
) -> str:
    def cell(value: object) -> str:
        return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# Runtime Closure Contract v{version}",
        "",
        f"- Run: `{contract.get('run_id')}`",
        f"- Runnable status: `{runnable['runnable_status']}`",
        f"- Blocked scenarios: {', '.join(runnable['blocked_scenario_ids']) or 'none'}",
        "",
        "## Runtime inputs",
        "",
        "| Input | Name | Origin | Design source | Resolution | Provider | Decision |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in _dict_items(contract.get("runtime_inputs")):
        resolution = _dict_value(item.get("resolution"))
        provider = _dict_value(resolution.get("provider"))
        provider_text = provider.get("ref") or (
            f"env {provider.get('env_key')} @ {provider.get('read_by')} ({provider.get('change_ref')})" if provider else ""
        )
        lines.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    item.get("input_id"),
                    item.get("name"),
                    item.get("origin"),
                    item.get("design_source"),
                    resolution.get("kind"),
                    provider_text,
                    resolution.get("decision_ref"),
                )
            )
            + " |"
        )
    lines += ["", "## Demo decisions (owner approves)", ""]
    for item in _dict_items(contract.get("demo_decisions")) or [{}]:
        if item:
            lines.append(f"- **{item.get('id')}** ({item.get('scope')}): {item.get('statement')} — inputs {item.get('input_ids')}")
        else:
            lines.append("- none")
    lines += ["", "## New write targets outside the frozen Plan (owner approves)", ""]
    lines += [f"- `{path}`" for path in _string_items(contract.get("new_write_targets"))] or ["- none"]
    lines += ["", "## Unresolved inputs", ""]
    lines += [
        f"- `{item.get('input_id')}` blocks {item.get('blocks_scenarios')}: {item.get('reason')}"
        for item in _dict_items(contract.get("unresolved"))
    ] or ["- none"]
    lines += ["", "## Closure units", ""]
    for unit in _dict_items(contract.get("closure_units")):
        lines.append(
            f"- **{unit.get('unit_id')}** `{unit.get('key')}` ({unit.get('component_id')}): {unit.get('objective')} "
            f"— writes {unit.get('write_paths')}, resolves {unit.get('resolves_input_ids')}, depends on {unit.get('depends_on')}"
        )
    lines += ["", "## Runtime probes", ""]
    for probe in _dict_items(contract.get("runtime_probes")):
        lines.append(
            f"- **{probe.get('probe_id')}** {probe.get('scenario_ids')} {probe.get('service')} {probe.get('method')} "
            f"`{probe.get('path')}` → {probe.get('expect_status')}"
            f"{' (≥' + str(probe.get('expect_json_array_min_items')) + ' items)' if probe.get('expect_json_array_min_items') is not None else ''}"
            f", proves {probe.get('proves_input_ids')}"
            # The owner approves these literal request values here.
            f", headers `{json.dumps(probe.get('headers') or {}, ensure_ascii=False, sort_keys=True)}`"
            f", json_body `{json.dumps(probe.get('json_body'), ensure_ascii=False, sort_keys=True)}`"
        )
    lines += ["", "## Verification stand-ins", ""]
    lines += [
        f"- **{item.get('stand_in_id')}** for `{item.get('for_input_id')}`: `{item.get('module')}#{item.get('start_export')}`, "
        f"env `{item.get('env_key')}`, decision {item.get('decision_ref')}"
        for item in _dict_items(contract.get("verification_stand_ins"))
    ] or ["- none"]
    lines += ["", "## Runtime config changes", ""]
    lines += [f"- {json.dumps(item, ensure_ascii=False)}" for item in _dict_items(contract.get("runtime_config_changes"))] or ["- none"]
    lines += ["", "## Validator warnings", ""]
    lines += [
        f"- [{item.get('category')}] {item.get('issue')} — expected: {item.get('expected_change')}"
        for item in _dict_items(review.get("warnings"))
    ] or ["- none"]
    unverified = _dict_items(review.get("reachability_unverified"))
    lines += ["", "## Reachability not verified mechanically", ""]
    lines += [f"- `{item.get('from')}` → `{item.get('to')}` ({item.get('input_id')})" for item in unverified] or ["- none"]
    return "\n".join(lines)


def _normalize_posix(value: str) -> str:
    parts: list[str] = []
    for part in value.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return ""
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _strip_source_suffix(path: str) -> str:
    for suffix in _SOURCE_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path
