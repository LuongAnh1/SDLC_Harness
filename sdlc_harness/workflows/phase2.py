"""Workflow Phase 2: approved requirement -> validated design checkpoint."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

from ..agents.designer import DesignerAgent, parse_design_model
from ..agents.validator import DesignValidationResult, DesignValidator, ValidatorAgent
from ..artifacts import read_approved_requirement_input, read_frozen_requirement_input
from ..artifacts.writers import write_phase2_design_artifacts
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint

ALLOWED_DESIGN_ACTIONS = [
    "approve_checkpoint",
    "revalidate_design",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseTwoWorkflow:
    """Coordinate Designer and hybrid Design Validator before human review."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        llm_client = LlmClient(settings)
        self.designer = DesignerAgent(llm_client)
        self.validator = ValidatorAgent(llm_client)
        self.design_validator = DesignValidator(llm_client)

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Approve the requirement checkpoint and create a validated design."""

        state = self.store.read_state(run_id)
        self._require_requirement_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        approved = read_approved_requirement_input(store=self.store, state=state, run_id=run_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_drafting",
                "status": "design_drafting",
                "approved_artifacts": approved.approved_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": None,
                "summary_for_user": "Đã duyệt requirement checkpoint, đang tạo design draft.",
            }
        )
        self.store.write_state(state)
        try:
            draft = self.designer.draft(
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                canonical_requirement=approved.canonical_model,
            )
            return self._run_validation_loop(
                state=state,
                approved=approved,
                design_model=draft.design_model,
                base_analysis=draft.analysis_report,
                artifact_paths=artifact_paths,
                start_version=1,
                existing_versions=[],
            )
        except HarnessError as exc:
            return self._handle_error(state, run_id=run_id, error=exc)

    def revalidate_design(self, *, run_id: str) -> dict[str, object]:
        """Validate and, when needed, repair the latest Phase 2 design version."""

        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        phase = str(state.get("current_phase") or "")
        if phase != "design_review" or status not in {"waiting_design_approval", "blocked"}:
            raise HarnessError(
                "design_revalidation_not_allowed",
                f"Run is not waiting on a design checkpoint: {run_id}",
                run_id=run_id,
            )
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        design_path = _required_artifact(
            artifact_paths,
            "design_artifact",
            run_id=run_id,
        )
        resolved_design_path = Path(str(design_path))
        if not resolved_design_path.is_absolute():
            resolved_design_path = self.store.repo_root / resolved_design_path
        design_model = resolved_design_path.resolve().read_text(encoding="utf-8")
        analysis_path = artifact_paths.get("designer_analysis", "")
        base_analysis = "Revalidated the latest approved-requirement design."
        if analysis_path:
            candidate = (self.store.repo_root / analysis_path).resolve()
            if candidate.is_file():
                base_analysis = candidate.read_text(encoding="utf-8")
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        versions = _design_versions(state)
        start_version = _next_design_version(self.store.run_dir(run_id) / "artifacts")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_revalidating",
                "status": "design_revalidating",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": None,
                "summary_for_user": f"Đang revalidate design từ V{start_version}.",
            }
        )
        self.store.write_state(state)
        try:
            return self._run_validation_loop(
                state=state,
                approved=approved,
                design_model=design_model,
                base_analysis=base_analysis,
                artifact_paths=artifact_paths,
                start_version=start_version,
                existing_versions=versions,
            )
        except HarnessError as exc:
            return self._handle_error(state, run_id=run_id, error=exc)

    def experiment_design_consensus(
        self,
        *,
        source_run_id: str,
        experiment_root: Path | None = None,
        llm_call_budget: int = 16,
    ) -> dict[str, object]:
        """Run the isolated progressive validator without touching production state."""

        from .design_experiment import ProgressiveDesignExperiment

        root = experiment_root or (self.settings.repo_root / ".experiments" / "design-validator")
        return ProgressiveDesignExperiment(
            self.settings,
            experiment_root=root,
            llm_call_budget=llm_call_budget,
        ).run(source_run_id=source_run_id)

    def _copy_requirement_snapshot(
        self,
        *,
        source_run_id: str,
        experiment_run_id: str,
        approved_artifacts: dict[str, str],
        source_artifact_ref: str,
    ) -> dict[str, str]:
        destination = self.store.run_dir(experiment_run_id) / "artifacts" / "requirement_snapshot"
        destination.mkdir(parents=True, exist_ok=True)
        copied: dict[str, str] = {}
        refs = {**approved_artifacts}
        if source_artifact_ref:
            refs["source_snapshot"] = source_artifact_ref
        for key, artifact_ref in refs.items():
            source = (self.store.repo_root / artifact_ref).resolve()
            if not source.is_file():
                raise HarnessError(
                    "approved_requirement_not_found",
                    f"Cannot copy frozen requirement artifact: {artifact_ref}",
                    run_id=source_run_id,
                )
            target = destination / source.name
            shutil.copy2(source, target)
            copied[key] = self.store.relative(target)
        return copied

    def _run_validation_loop(
        self,
        *,
        state: dict[str, object],
        approved: object,
        design_model: str,
        base_analysis: str,
        artifact_paths: dict[str, str],
        start_version: int,
        existing_versions: list[str],
        sync_checkpoint: bool = True,
        experiment_mode: bool = False,
    ) -> dict[str, object]:
        run_id = str(state.get("run_id") or "")
        versions = list(existing_versions)
        merged_artifacts = dict(artifact_paths)
        for stale_key in ("design_repair_ledger", "last_design_candidate", "last_candidate_validation_report"):
            merged_artifacts.pop(stale_key, None)
        design_paths: dict[str, str] = {}
        validation: DesignValidationResult | None = None
        previous_quality_report = ""
        seen_states: dict[str, int] = {}
        stalled = False
        oscillating = False
        passed = False
        inconclusive = False
        trajectory: list[dict[str, object]] = []
        repair_limit = self.settings.design_repair_limit

        for repair_iteration in range(repair_limit + 1):
            version = start_version + repair_iteration
            version_label = f"v{version}"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "status": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "summary_for_user": (
                        f"Đang chạy Design Validator hybrid cho V{version}."
                        if repair_iteration == 0
                        else f"Designer đang repair design vòng {repair_iteration}/{repair_limit}."
                    ),
                }
            )
            self.store.write_state(state)

            if repair_iteration > 0:
                design_model = self.designer.update_design(
                    requirement_packet=approved.requirement_packet,
                    formal_use_case=approved.formal_use_case,
                    current_design=design_model,
                    audit_report=validation.feedback if validation is not None else "",
                    canonical_requirement=approved.canonical_model,
                )

            parsed = parse_design_model(design_model)
            traceability = self.validator.construct_traceability(
                requirement_packet=approved.requirement_packet,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                canonical_requirement=approved.canonical_model,
            )
            validation = self.design_validator.review(
                requirement_packet=approved.requirement_packet,
                design_artifact=design_model,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                traceability=traceability,
                canonical_requirement=approved.canonical_model,
                repair_iterations=repair_iteration,
                validation_trials=self.settings.design_validation_trials,
            )
            design_paths = write_phase2_design_artifacts(
                store=self.store,
                run_id=run_id,
                designer_analysis=_analysis_for_version(
                    base_analysis=base_analysis,
                    version=version,
                    previous_quality_report=previous_quality_report,
                ),
                design_model=design_model,
                requirement_graph=traceability.requirement_graph,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                class_trace=traceability.class_trace,
                sequence_trace=traceability.sequence_trace,
                missing_links=traceability.missing_links,
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                version=version,
                repair_iterations=repair_iteration,
                traceability_mode=traceability.mode,
                requirement_contract_version=approved.contract_version,
                requirement_artifact_refs=approved.approved_artifacts,
                requirement_revision=approved.canonical_model.revision,
                design_validation=validation.to_dict(),
            )
            if version_label not in versions:
                versions.append(version_label)
            merged_artifacts = {**merged_artifacts, **design_paths}
            quality_reports = _quality_reports(
                state,
                validation,
                versions,
                design_paths,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": quality_reports,
                }
            )
            repair_state = _repair_state_fingerprint(validation=validation)
            consensus = validation.semantic_consensus
            trajectory_entry = {
                "version": version_label,
                "repair_iteration": repair_iteration,
                "blocking_count": len(validation.blocking_findings),
                "warning_count": len(validation.warnings),
                "disputed_count": len(validation.disputed_findings),
                "inconclusive": validation.inconclusive,
                "blocker_keys": sorted(item.canonical_key for item in validation.blocking_findings),
                "fingerprint": repair_state,
                "requested_trials": consensus.get("requested_trials", 1),
                "valid_trials": consensus.get("valid_trials", 1),
                "quorum": consensus.get("quorum", 1),
                "consistency_score": consensus.get("consistency_score", 1.0),
            }
            trajectory.append(trajectory_entry)
            if experiment_mode:
                experiment = _dict_value(state.get("experiment"))
                experiment["trajectory"] = trajectory
                state["experiment"] = experiment
            self.store.write_state(state)
            if validation.inconclusive:
                inconclusive = True
                break
            if validation.ok:
                passed = True
                break
            if repair_state in seen_states:
                stalled = seen_states[repair_state] == repair_iteration - 1
                oscillating = not stalled
                break
            seen_states[repair_state] = repair_iteration
            if repair_iteration >= repair_limit:
                break
            previous_quality_report = design_paths["design_quality_report"]

        if validation is None:
            raise HarnessError("designer_failed", "Phase 2 did not produce a design validation result.", run_id=run_id)
        if not passed:
            if inconclusive:
                error_code = "design_validation_inconclusive"
            elif stalled:
                error_code = "design_repair_stalled"
            elif oscillating:
                error_code = "design_repair_oscillating"
            else:
                error_code = "design_repair_exhausted"
            error = HarnessError(
                error_code,
                f"Design still has {len(validation.blocking_findings)} blocking finding(s).",
                run_id=run_id,
            )
            terminal_status = (
                {
                    "design_validation_inconclusive": "design_experiment_inconclusive",
                    "design_repair_stalled": "design_experiment_stalled",
                    "design_repair_oscillating": "design_experiment_oscillating",
                    "design_repair_exhausted": "design_experiment_exhausted",
                }[error_code]
                if experiment_mode
                else "blocked"
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": terminal_status,
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ["get_status"]
                        if experiment_mode
                        else ["revalidate_design", "get_status", "request_change", "cancel_run"]
                    ),
                    "last_error": error.to_response(),
                    "summary_for_user": (
                        f"Design chưa pass hybrid validator: còn {len(validation.blocking_findings)} blocker, "
                        f"{len(validation.warnings)} warning; trạng thái={error_code}."
                    ),
                }
            )
            if experiment_mode:
                self._write_experiment_report(state)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if experiment_mode:
            disputed_blockers = sum(
                1 for item in validation.disputed_findings if item.severity == "blocking"
            )
            status = (
                "design_experiment_converged_strong"
                if disputed_blockers == 0
                else "design_experiment_converged_by_consensus"
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_experiment",
                    "status": status,
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status"],
                    "last_error": None,
                    "last_sync": {"status": "disabled", "mode": "none"},
                    "quality_reports": _quality_reports(state, validation, versions, design_paths),
                    "summary_for_user": (
                        f"Thử nghiệm design consensus hội tụ tại {versions[-1]}: "
                        f"0 blocker đồng thuận, {len(validation.disputed_findings)} finding còn tranh chấp."
                    ),
                }
            )
            self._write_experiment_report(state)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=True)

        if not sync_checkpoint:
            raise HarnessError("internal_error", "Checkpoint sync disabled outside experiment mode.", run_id=run_id)
        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": _artifact_ref(design_paths["design_artifact"]),
                    "artifact_path": design_paths["design_artifact"],
                },
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": _quality_reports(
                    state,
                    validation,
                    versions,
                    design_paths,
                ),
                "summary_for_user": (
                    f"Design V{start_version + len(versions) - len(existing_versions) - 1} đã pass hybrid validator: "
                    f"0 blocker, {len(validation.warnings)} warning; sync status={last_sync.get('status')}. "
                    "Run đang chờ anh review design trước khi sang Phase 3."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _write_experiment_report(self, state: dict[str, object]) -> None:
        run_id = str(state.get("run_id") or "")
        experiment = _dict_value(state.get("experiment"))
        trajectory = experiment.get("trajectory") if isinstance(experiment.get("trajectory"), list) else []
        payload = {
            "version": "design_consensus_experiment_v1",
            "run_id": run_id,
            "source_run_id": experiment.get("source_run_id"),
            "source_ref": state.get("source_ref"),
            "status": state.get("status"),
            "stop_reason": _experiment_stop_reason(str(state.get("status") or "")),
            "llm": state.get("llm"),
            "validation_trials": experiment.get("validation_trials"),
            "quorum": experiment.get("quorum"),
            "repair_limit": experiment.get("repair_limit"),
            "sync_disabled": True,
            "trajectory": trajectory,
            "quality_reports": state.get("quality_reports", {}),
            "last_error": state.get("last_error"),
            "completed_at": utc_now_iso(),
        }
        json_path = self.store.write_json_artifact(run_id, "design_consensus_experiment.json", payload)
        markdown_path = self.store.artifact_path(run_id, "design_consensus_experiment.md")
        rows = [
            "| Version | Blockers | Warnings | Disputed | Valid trials | Consistency |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for item in trajectory:
            if not isinstance(item, dict):
                continue
            rows.append(
                "| "
                + " | ".join(
                    [
                        str(item.get("version") or ""),
                        str(item.get("blocking_count") or 0),
                        str(item.get("warning_count") or 0),
                        str(item.get("disputed_count") or 0),
                        f"{item.get('valid_trials', 0)}/{item.get('requested_trials', 0)}",
                        str(item.get("consistency_score") or 0),
                    ]
                )
                + " |"
            )
        markdown_path.write_text(
            "\n".join(
                [
                    "# Design Validator Consensus Experiment",
                    "",
                    f"- Experiment run: `{run_id}`",
                    f"- Source run: `{experiment.get('source_run_id', '')}`",
                    f"- Status: `{state.get('status', '')}`",
                    f"- Stop reason: `{payload['stop_reason']}`",
                    f"- Trials/quorum: `{experiment.get('validation_trials', '')}/{experiment.get('quorum', '')}`",
                    f"- Repair limit: `{experiment.get('repair_limit', '')}`",
                    "- External sync: `disabled`",
                    "",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        latest = _string_dict(state.get("latest_artifacts"))
        latest["design_consensus_experiment"] = self.store.relative(json_path)
        latest["design_consensus_experiment_markdown"] = self.store.relative(markdown_path)
        state["latest_artifacts"] = latest

    def _handle_error(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        error: HarnessError,
    ) -> dict[str, object]:
        requirement_error = _requirement_blocking_error(error, run_id=run_id)
        retryable_validator = error.error_code == "design_semantic_review_failed"
        if requirement_error is not None or retryable_validator:
            effective = requirement_error or error
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_design", "get_status", "request_change", "cancel_run"],
                    "last_error": effective.to_response(),
                    "summary_for_user": f"Phase 2 bị chặn: {effective.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": error.to_response(),
                "summary_for_user": f"Phase 2 lỗi khi tạo hoặc kiểm tra design: {error.message}",
            }
        )
        self.store.write_state(state)
        if not error.run_id:
            raise HarnessError(error.error_code, error.message, run_id=run_id) from error
        raise error

    def _require_requirement_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_requirement_approval":
            raise HarnessError("checkpoint_not_current", f"Run is not waiting for requirement approval: {run_id}", run_id=run_id)
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError("no_pending_checkpoint", f"Run does not have a pending checkpoint: {run_id}", run_id=run_id)
        if checkpoint.get("type") != "requirement_approval":
            raise HarnessError("checkpoint_not_current", f"Pending checkpoint is not a requirement checkpoint: {run_id}", run_id=run_id)
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError("checkpoint_not_current", f"Checkpoint is not current: {checkpoint_id}", run_id=run_id)

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        return {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": state.get("allowed_next_actions", []),
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
        }


def _quality_reports(
    state: dict[str, object],
    validation: DesignValidationResult,
    versions: list[str],
    paths: dict[str, str],
) -> dict[str, object]:
    summary = (
        f"{len(validation.blocking_findings)} blocker(s), {len(validation.warnings)} warning(s); "
        f"traceability={'passed' if validation.traceability.ok else 'failed'}."
    )
    return {
        **_dict_value(state.get("quality_reports")),
        "design_traceability": {
            "ok": validation.traceability.ok,
            "mode": validation.traceability.mode,
            "versions": versions,
            "summary": _traceability_summary(validation.traceability.missing_links),
            "report": paths["design_quality_report"],
        },
        "design_validation": {
            "ok": validation.ok,
            "mode": validation.mode,
            "versions": versions,
            "blocking_count": len(validation.blocking_findings),
            "warning_count": len(validation.warnings),
            "disputed_count": len(validation.disputed_findings),
            "inconclusive": validation.inconclusive,
            "requested_trials": validation.semantic_consensus.get("requested_trials", 1),
            "valid_trials": validation.semantic_consensus.get("valid_trials", 1),
            "quorum": validation.semantic_consensus.get("quorum", 1),
            "consistency_score": validation.semantic_consensus.get("consistency_score", 1.0),
            "summary": summary,
            "report": paths["design_validation_report"],
        },
    }


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _traceability_summary(missing_links: dict[str, list[dict[str, str]]]) -> str:
    total = len(missing_links.get("class_diagram", [])) + len(missing_links.get("sequence_diagram", []))
    return "không phát hiện missing link class/sequence." if total == 0 else f"còn {total} missing link class/sequence cần review."


def _requirement_blocking_error(error: HarnessError, *, run_id: str) -> HarnessError | None:
    if error.error_code == "validator_traceability_failed" and "traceable requirement" in error.message.lower():
        return HarnessError("requirement_traceability_blocked", error.message, run_id=run_id)
    return None


def _analysis_for_version(*, base_analysis: str, version: int, previous_quality_report: str) -> str:
    if version == 1:
        return base_analysis
    return (
        f"{base_analysis.rstrip()}\n\n## Repair Context\n\n"
        f"- Design version: v{version}\n"
        f"- Previous quality report: `{previous_quality_report or 'revalidation'}`\n"
    )


def _repair_state_fingerprint(
    *,
    validation: DesignValidationResult,
) -> str:
    signatures = []
    for finding in validation.blocking_findings:
        signatures.append(finding.canonical_key)
    content = "\n".join(sorted(signatures))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _experiment_stop_reason(status: str) -> str:
    return {
        "design_experiment_converged_strong": "zero_consensus_blockers_without_disputed_blockers",
        "design_experiment_converged_by_consensus": "zero_consensus_blockers_with_minority_blocker_observations",
        "design_experiment_stalled": "same_blocker_set_repeated_consecutively",
        "design_experiment_oscillating": "previous_blocker_set_reappeared",
        "design_experiment_exhausted": "repair_limit_reached",
        "design_experiment_inconclusive": "one_or_more_semantic_trials_failed",
        "design_experiment_failed": "workflow_error",
    }.get(status, status or "unknown")


def _design_versions(state: dict[str, object]) -> list[str]:
    quality = _dict_value(_dict_value(state.get("quality_reports")).get("design_validation"))
    versions = quality.get("versions")
    return [str(item) for item in versions] if isinstance(versions, list) else []


def _next_design_version(artifact_dir: Path) -> int:
    versions = []
    for path in artifact_dir.glob("design_artifact_v*.md"):
        suffix = path.stem.rsplit("_v", 1)[-1]
        if suffix.isdigit():
            versions.append(int(suffix))
    return max(versions, default=0) + 1


def _required_artifact(artifacts: dict[str, str], key: str, *, run_id: str) -> str:
    value = str(artifacts.get(key) or "").strip()
    if not value:
        raise HarnessError("design_artifact_not_found", f"Run is missing artifact `{key}`.", run_id=run_id)
    return value


def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]
