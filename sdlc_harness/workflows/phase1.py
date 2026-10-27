"""Workflow Phase 1: use case -> formal use case -> requirement checkpoint.

Phase này tạo structured requirement model, kiểm tra grounding/clarification,
render các projection và lưu checkpoint để Niko có thể kiểm soát từng bước.
"""

from __future__ import annotations

from dataclasses import replace

from ..artifacts.writers import write_use_case_packet
from ..artifacts import (
    load_source_snapshot_v2,
    read_requirement_gap_report,
    read_requirement_model_v2,
    read_requirement_source_texts,
    read_source_snapshot_v2,
    write_requirement_v2_artifacts,
    write_requirement_gap_report,
    write_requirement_v2_revision_artifacts,
    write_source_snapshot_v2,
)
from ..core.config import Settings
from ..core.errors import HarnessError
from ..profiles import build_profile_snapshot, profile_status_metadata
from ..sources.fixtures import UseCaseSource, load_use_case, source_provenance_entry
from ..sources.jira import load_jira_issue_source
from ..llm.client import LlmClient
from ..agents.developer import (
    COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
    IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY,
    developer_transport_retry_allowed,
    repair_scope_mismatch_retry_allowed,
    resolve_implementation_plan_contract,
)
from ..agents.requirements import (
    RequirementClarificationUpdater,
    RequirementCoverageReviewer,
    RequirementModelV2,
    RequirementModelV2Refiner,
    RequirementSourceCatalog,
    CLARIFICATION_DISPOSITION_FIELD,
    AGENT_DEFAULT_ORIGIN,
    ASSUMPTION_CHALLENGE_ARTIFACT_KEY,
    ASSUMPTION_CHALLENGE_ARTIFACT_PREFIX,
    DEFERRED_TO_DESIGN,
    LEGACY_BLOCKING_POLICY_VERSION,
    REASONABLE_DEFAULT_POLICY_VERSION,
    REASONABLE_DEFAULT_ARTIFACT_KEY,
    REASONABLE_DEFAULT_ARTIFACT_PREFIX,
    SHADOW_ARTIFACT_KEY,
    SHADOW_ARTIFACT_PREFIX,
    SHADOW_POLICY_VERSION,
    build_requirement_gap_report,
    build_clarification_policy_shadow,
    build_agent_reasonable_default_report,
    build_requirement_quality_checklist,
    challenge_agent_assumption,
    assumption_challenge_quality_summary,
    close_clarification_session,
    find_clarification_submission,
    gap_report_summary,
    normalize_and_validate_requirement_model_v2,
    record_session_answer,
    resolve_clarification_answer,
    select_current_requirement_question,
    session_pending_ids,
    shadow_error_summary,
    shadow_quality_summary,
    reasonable_default_quality_summary,
    start_clarification_session,
)
from ..agents.requirements.session_v1 import (
    SESSION_FILENAME,
    read_requirement_session,
    requirement_session_summary,
)
from ..agents.designer import DESIGNER_CONTRACT_STATE_KEY, GRAPH_VERSION
from ..agents.validator import VERTICAL_TRACE_GRAPH_VERSION, VERTICAL_TRACE_STATE_KEY
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint, sync_implementation_plan_checkpoint, sync_requirement_checkpoint
from .repo_discovery import REPO_DISCOVERY_CONTRACT_STATE_KEY, REPO_DISCOVERY_CONTRACT_VERSION

ALLOWED_REQUIREMENT_ACTIONS = [
    "approve_checkpoint",
    "clarify_more",
    "revalidate_requirement",
    "request_change",
    "cancel_run",
    "get_status",
]

ALLOWED_CODE_REVIEW_READY_ACTIONS = ["approve_checkpoint", "get_status", "sync_run", "cancel_run"]
ALLOWED_REQUIREMENT_CLARIFICATION_ACTIONS = [
    "answer_clarification",
    "revalidate_requirement",
    "get_status",
    "sync_run",
    "cancel_run",
]
ALLOWED_REQUIREMENT_DRAFTING_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_REQUIREMENT_CLARIFICATION_CONTINUE_ACTIONS = [
    "clarify_more",
    "revalidate_requirement",
    "get_status",
    "sync_run",
    "cancel_run",
]


class PhaseOneWorkflow:
    """Điều phối Phase 1 từ fixture/Jira tới checkpoint requirement.

    Workflow này sở hữu source loading, refiner, validator, artifact runtime và
    sync Jira/Confluence. Niko gọi HTTP API và đọc response đã tóm tắt.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.llm_client = LlmClient(settings)
        self.refiner_v2 = RequirementModelV2Refiner(self.llm_client, store=self.store)
        self.clarification_updater = RequirementClarificationUpdater(self.llm_client)
        self.coverage_reviewer = RequirementCoverageReviewer(self.llm_client)

    def start_or_get_run(self, *, dataset: str, use_case: str, requested_by: str) -> dict[str, object]:
        """Tạo run mới hoặc trả active run hiện tại cho cùng source_ref."""

        source = load_use_case(dataset=dataset, use_case=use_case, repo_root=self.settings.repo_root)
        return self._start_from_source(source=source, requested_by=requested_by)

    def start_from_jira(self, *, issue_key: str, requested_by: str) -> dict[str, object]:
        """Tạo run từ Jira issue hoặc trả active run hiện tại cho cùng issue."""

        source = load_jira_issue_source(settings=self.settings, issue_key=issue_key)
        return self._start_from_source(source=source, requested_by=requested_by)

    def _start_from_source(self, *, source: UseCaseSource, requested_by: str) -> dict[str, object]:
        """Chạy Phase 1 từ source đã được load, bất kể source là fixture hay Jira."""

        active_state = self.store.find_active_run(source.source_ref)
        if active_state and str(active_state.get(DESIGNER_CONTRACT_STATE_KEY) or "") == GRAPH_VERSION:
            return self._status_response(active_state, created_new_run=False)

        run_id = self.store.new_run_id()
        now = utc_now_iso()
        state: dict[str, object] = {
            "ok": True,
            "run_id": run_id,
            "source_ref": source.source_ref,
            "dataset": source.dataset,
            "use_case": source.use_case,
            "requested_by": requested_by,
            "requirement_contract_version": self.settings.requirement_contract_version,
            "requirement_clarification_policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
            REPO_DISCOVERY_CONTRACT_STATE_KEY: REPO_DISCOVERY_CONTRACT_VERSION,
            "created_at": now,
            "updated_at": now,
            "current_phase": "requirement_drafting",
            "status": "requirement_drafting",
            "latest_artifacts": {},
            "pending_checkpoint": None,
            "allowed_next_actions": list(ALLOWED_REQUIREMENT_DRAFTING_ACTIONS),
            "last_sync": None,
            "last_error": None,
            "llm": {
                "mode": self.settings.llm_mode,
                "model": self.settings.llm_model,
            },
            "summary_for_user": "Đang chuẩn hóa use case thành Requirement Model V2.",
        }
        state[DESIGNER_CONTRACT_STATE_KEY] = GRAPH_VERSION
        state[VERTICAL_TRACE_STATE_KEY] = VERTICAL_TRACE_GRAPH_VERSION
        state[IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY] = (
            COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION
        )
        state["vertical_trace_mode"] = self.settings.vertical_trace_mode
        if self.settings.project_profile_id:
            state["profile_snapshot"] = build_profile_snapshot(
                project_profile_id=self.settings.project_profile_id,
                target_remote=self.settings.target_repo_remote,
                target_base_branch=self.settings.target_base_branch,
            ).to_dict()
        self.store.write_state(state)
        self.store.register_active_run(source.source_ref, run_id)

        return self._start_v2(source=source, state=state, resume=False)

    def continue_requirement_run(self, *, run_id: str) -> dict[str, object]:
        """Tiếp tục Requirement loop từ immutable snapshot của chính run."""

        state = self.store.read_state(run_id)
        if str(state.get("status") or "") not in {"requirement_drafting", "requirement_paused"}:
            raise HarnessError(
                "requirement_session_not_resumable",
                f"Requirement run cannot continue from status {state.get('status')!r}.",
                run_id=run_id,
            )
        artifacts = self._normalized_artifacts(state)
        source_snapshot_ref = artifacts.get("requirement_source_snapshot")
        if not source_snapshot_ref:
            raise HarnessError(
                "requirement_session_not_resumable",
                "Requirement run has no frozen source snapshot.",
                run_id=run_id,
            )
        source = load_source_snapshot_v2(
            store=self.store,
            run_id=run_id,
            artifact_ref=source_snapshot_ref,
        )
        return self._start_v2(source=source, state=state, resume=True)

    def _start_v2(
        self,
        *,
        source: UseCaseSource,
        state: dict[str, object],
        resume: bool,
    ) -> dict[str, object]:
        """Create the canonical V2 model and stop at clarification or approval."""

        run_id = str(state["run_id"])
        try:
            existing_artifacts = self._normalized_artifacts(state)
            if resume:
                use_case_packet_path = existing_artifacts.get("use_case_packet", "")
                source_snapshot_path = existing_artifacts.get("requirement_source_snapshot", "")
                if not use_case_packet_path or not source_snapshot_path:
                    raise HarnessError(
                        "requirement_session_not_resumable",
                        "Requirement run is missing its frozen input artifacts.",
                        run_id=run_id,
                    )
            else:
                use_case_packet_path = write_use_case_packet(store=self.store, run_id=run_id, source=source)
                source_snapshot_path = write_source_snapshot_v2(store=self.store, run_id=run_id, source=source)
                existing_artifacts.update(
                    {
                        "use_case_packet": use_case_packet_path,
                        "requirement_source_snapshot": source_snapshot_path,
                    }
                )
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "latest_artifacts": existing_artifacts,
                        "status": "requirement_drafting",
                        "current_phase": "requirement_drafting",
                        "allowed_next_actions": list(ALLOWED_REQUIREMENT_DRAFTING_ACTIONS),
                    }
                )
                self.store.write_state(state)
            source_entry = source_provenance_entry(source=source, artifact_ref=source_snapshot_path)
            verified_source_text = read_source_snapshot_v2(
                store=self.store,
                run_id=run_id,
                expected_source=source_entry,
            )
            source_catalog = RequirementSourceCatalog(
                run_id=run_id,
                sources=[source_entry],
                source_text_by_id={source.source_id: verified_source_text},
            )
            refinement_kwargs: dict[str, object] = {
                "run_id": run_id,
                "source_artifact_ref": source_snapshot_path,
                "source_catalog": source_catalog,
            }
            if resume:
                refinement_kwargs["resume"] = True
            refinement = self.refiner_v2.refine(source, **refinement_kwargs)
            policy_validation = normalize_and_validate_requirement_model_v2(
                refinement.model,
                source_catalog.source_text_by_id,
                clarification_policy_version=self._clarification_policy_version(state),
            )
            refinement = replace(
                refinement,
                model=policy_validation.model,
                final_report=policy_validation.report,
            )
            coverage = self.coverage_reviewer.review(
                model=refinement.model,
                source_text_by_id=source_catalog.source_text_by_id,
                validation_report=refinement.final_report,
                semantic=True,
                question_limit=5,
            )
            refinement = replace(
                refinement,
                model=coverage.model,
                final_report=coverage.validation_report,
            )
            policy_validation = normalize_and_validate_requirement_model_v2(
                refinement.model,
                source_catalog.source_text_by_id,
                clarification_policy_version=self._clarification_policy_version(state),
            )
            refinement = replace(
                refinement,
                model=policy_validation.model,
                final_report=policy_validation.report,
            )
            quality_checklist = build_requirement_quality_checklist(
                model=refinement.model,
                validation_report=refinement.final_report,
                analysis=coverage.analysis,
            )
            gap_report = refinement.gap_report or build_requirement_gap_report(
                run_id=run_id,
                model=refinement.model,
            )
            gap_report_path = refinement.gap_report_path or write_requirement_gap_report(
                store=self.store,
                run_id=run_id,
                report=gap_report,
            )
            artifact_paths = {
                "use_case_packet": use_case_packet_path,
                "requirement_source_snapshot": source_snapshot_path,
                **(
                    {"requirement_source_manifest": refinement.source_manifest_path}
                    if refinement.source_manifest_path
                    else {}
                ),
                **(
                    {"requirement_source_journal": refinement.source_journal_path}
                    if refinement.source_journal_path
                    else {}
                ),
                "requirement_gap_report": gap_report_path,
                **(
                    {"requirement_delta": refinement.delta_artifact_paths[-1]}
                    if refinement.delta_artifact_paths
                    else {}
                ),
                **({"requirement_loop_session": refinement.session_path} if refinement.session_path else {}),
                "requirement_clarification_analysis": self.store.relative(
                    self.store.write_json_artifact(
                        run_id,
                        f"requirement_clarification_analysis_v1_r{refinement.model.revision}.json",
                        coverage.analysis,
                    )
                ),
                "requirement_quality_checklist": self.store.relative(
                    self.store.write_json_artifact(
                        run_id,
                        f"requirement_quality_checklist_v1_r{refinement.model.revision}.json",
                        quality_checklist,
                    )
                ),
                **write_requirement_v2_artifacts(
                    store=self.store,
                    run_id=run_id,
                    refinement_result=refinement,
                ),
            }
            clarification_session = start_clarification_session(refinement.model)
            selection = select_current_requirement_question(
                refinement.model,
                allowed_question_ids=(
                    session_pending_ids(clarification_session)
                    if clarification_session is not None
                    else None
                ),
            )
            self._apply_v2_checkpoint_state(
                state=state,
                model=refinement.model,
                artifact_paths=artifact_paths,
                current_question=selection.current_question.to_dict() if selection.current_question else None,
                remaining_blocking_count=selection.remaining_blocking_count,
                quality_report={**refinement.to_report_dict(), "quality_checklist": quality_checklist},
                gap_report=gap_report,
                latest_decision=None,
                clarification_session=clarification_session,
            )
            self.store.write_state(state)
            last_sync = self._sync_requirement_state(state)
            state["last_sync"] = last_sync
            state["updated_at"] = utc_now_iso()
            state["summary_for_user"] = self._v2_summary(state, last_sync)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=not resume)
        except HarnessError as exc:
            if exc.error_code == "requirement_session_not_resumable" and resume:
                raise
            latest_artifacts = self._normalized_artifacts(state)
            for key, filename in (
                ("requirement_loop_session", SESSION_FILENAME),
                ("requirement_source_manifest", "requirement_source_manifest_v1.json"),
                ("requirement_source_journal", "requirement_source_journal_v1.json"),
                ("requirement_gap_report", "requirement_gap_report_v1.json"),
                ("requirement_delta", "requirement_delta_v1_a1.json"),
            ):
                path = self.store.artifact_path(run_id, filename)
                if path.exists():
                    latest_artifacts[key] = self.store.relative(path)
            if exc.error_code == "llm_transport_failed":
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "requirement_drafting",
                        "status": "requirement_paused",
                        "last_error": HarnessError(exc.error_code, exc.message, run_id=run_id).to_response(),
                        "latest_artifacts": latest_artifacts,
                        "pending_checkpoint": None,
                        "allowed_next_actions": list(ALLOWED_REQUIREMENT_DRAFTING_ACTIONS),
                        "summary_for_user": (
                            "Requirement loop đã tạm dừng vì kết nối LLM gián đoạn; "
                            "có thể dùng continue_run để tiếp tục đúng session cũ."
                        ),
                    }
                )
                state["requirement_execution"] = self._requirement_execution_summary(run_id)
                self.store.write_state(state)
                return self._status_response(state, created_new_run=not resume)
            gap_path = latest_artifacts.get("requirement_gap_report")
            if gap_path:
                gap_report = read_requirement_gap_report(
                    store=self.store,
                    run_id=run_id,
                    artifact_ref=gap_path,
                )
            else:
                gap_report = build_requirement_gap_report(run_id=run_id, error=exc)
                gap_path = write_requirement_gap_report(
                    store=self.store,
                    run_id=run_id,
                    report=gap_report,
                )
                latest_artifacts["requirement_gap_report"] = gap_path
            quality_reports = state.get("quality_reports")
            if not isinstance(quality_reports, dict):
                quality_reports = {}
            quality_reports["requirement_gaps"] = gap_report_summary(gap_report)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "status": "failed",
                    "last_error": exc.to_response(),
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "summary_for_user": f"Phase 1 V2 lỗi khi chuẩn hóa requirement: {exc.message}",
                }
            )
            self.store.write_state(state)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc

    def revalidate_requirement(self, *, run_id: str) -> dict[str, object]:
        """Chạy lại validator tất định; không gọi LLM hoặc sinh clarification mới."""

        state = self.store.read_state(run_id)
        if str(state.get("requirement_contract_version") or "v2") != "v2":
            raise HarnessError(
                "requirement_revalidation_not_supported",
                "Requirement revalidation requires Requirement Contract V2.",
                run_id=run_id,
            )
        if str(state.get("status") or "") not in {
            "waiting_requirement_approval",
            "waiting_requirement_clarification",
            "waiting_requirement_clarification_continue",
        }:
            raise HarnessError(
                "requirement_revalidation_not_ready",
                f"Run cannot revalidate requirement from status {state.get('status')!r}.",
                run_id=run_id,
            )
        artifacts = self._normalized_artifacts(state)
        model_ref = artifacts.get("requirement_model")
        if not model_ref:
            raise HarnessError("requirement_model_v2_missing", "Canonical Requirement V2 artifact is missing.", run_id=run_id)
        model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
        source_text_by_id = read_requirement_source_texts(store=self.store, run_id=run_id, model=model)
        validation = normalize_and_validate_requirement_model_v2(
            model,
            source_text_by_id,
            clarification_policy_version=self._clarification_policy_version(state),
        )
        if validation.model.to_dict() != model.to_dict():
            raise HarnessError(
                "requirement_model_v2_invalid",
                "Canonical Requirement is stale for its pinned clarification policy.",
                run_id=run_id,
            )
        quality_checklist = build_requirement_quality_checklist(
            model=validation.model,
            validation_report=validation.report,
        )
        artifacts["requirement_revalidation"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_revalidation_v1_r{model.revision}.json",
                {
                    "artifact_version": "requirement_revalidation_v1",
                    "run_id": run_id,
                    "requirement_revision": model.revision,
                    "mode": "deterministic_only",
                    "validation_report": validation.report.to_dict(),
                    "quality_checklist": quality_checklist,
                },
            )
        )
        artifacts["requirement_quality_checklist"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_quality_checklist_v1_r{model.revision}.json",
                quality_checklist,
            )
        )
        shadow_quality = self._write_clarification_policy_shadow(
            model=validation.model,
            artifact_paths=artifacts,
        )
        quality_reports = state.get("quality_reports")
        reports = dict(quality_reports) if isinstance(quality_reports, dict) else {}
        reports["requirement_model_v2"] = {
            "action": "revalidate_requirement",
            "mode": "deterministic_only",
            "quality_checklist": quality_checklist,
            "final_report": validation.report.to_dict(),
        }
        reports["requirement_clarification_policy_shadow"] = shadow_quality
        state.update(
            {
                "updated_at": utc_now_iso(),
                "latest_artifacts": artifacts,
                "quality_reports": reports,
                "last_error": None,
                "summary_for_user": (
                    "Đã kiểm tra lại Requirement V2 bằng validator tất định; "
                    "không gọi LLM, không sinh câu hỏi mới và không đổi revision."
                ),
            }
        )
        self.store.write_state(state)
        last_sync = self._sync_requirement_state(state)
        state["last_sync"] = last_sync
        state["updated_at"] = utc_now_iso()
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def clarify_more(self, *, run_id: str) -> dict[str, object]:
        """Mở đúng một lượt semantic clarification mới theo yêu cầu người dùng."""

        state = self.store.read_state(run_id)
        if str(state.get("requirement_contract_version") or "v2") != "v2":
            raise HarnessError(
                "requirement_revalidation_not_supported",
                "Requirement revalidation requires Requirement Contract V2.",
                run_id=run_id,
            )
        status = str(state.get("status") or "")
        if status == "waiting_requirement_clarification_continue":
            return self._continue_clarification_session(state)
        if status != "waiting_requirement_approval":
            raise HarnessError(
                "requirement_clarification_not_ready",
                f"Run cannot clarify requirement further from status {state.get('status')!r}.",
                run_id=run_id,
            )
        artifacts = self._normalized_artifacts(state)
        model_ref = artifacts.get("requirement_model")
        if not model_ref:
            raise HarnessError("requirement_model_v2_missing", "Canonical Requirement V2 artifact is missing.", run_id=run_id)
        model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
        source_text_by_id = read_requirement_source_texts(store=self.store, run_id=run_id, model=model)
        current_validation = normalize_and_validate_requirement_model_v2(
            model,
            source_text_by_id,
            clarification_policy_version=self._clarification_policy_version(state),
        )
        coverage = self.coverage_reviewer.review(
            model=current_validation.model,
            source_text_by_id=source_text_by_id,
            validation_report=current_validation.report,
            semantic=True,
            question_limit=5,
        )
        revised_model = coverage.model
        revised_report = coverage.validation_report
        if revised_model.to_dict() != model.to_dict():
            revised_payload = revised_model.to_dict()
            revised_payload["revision"] = model.revision + 1
            revised_validation = normalize_and_validate_requirement_model_v2(
                RequirementModelV2.from_dict(revised_payload),
                source_text_by_id,
                clarification_policy_version=self._clarification_policy_version(state),
            )
            revised_model = revised_validation.model
            revised_report = revised_validation.report
            artifacts.update(
                write_requirement_v2_revision_artifacts(
                    store=self.store,
                    run_id=run_id,
                    model=revised_model,
                    validation_report=revised_report,
                    audit={
                        "action": "clarify_more",
                        "coverage_analysis": coverage.analysis,
                        "final_report": revised_report.to_dict(),
                    },
                )
            )
        analysis = dict(coverage.analysis)
        analysis["output_requirement_revision"] = revised_model.revision
        artifacts["requirement_clarification_analysis"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_clarification_analysis_v1_r{revised_model.revision}.json",
                analysis,
            )
        )
        quality_checklist = build_requirement_quality_checklist(
            model=revised_model,
            validation_report=revised_report,
            analysis=analysis,
        )
        artifacts["requirement_quality_checklist"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_quality_checklist_v1_r{revised_model.revision}.json",
                quality_checklist,
            )
        )
        previous_gap_report = None
        gap_ref = artifacts.get("requirement_gap_report")
        if gap_ref:
            previous_gap_report = read_requirement_gap_report(store=self.store, run_id=run_id, artifact_ref=gap_ref)
        gap_report = build_requirement_gap_report(
            run_id=run_id,
            model=revised_model,
            source_manifest_input_hash=(
                str(previous_gap_report.get("source_manifest_input_hash") or "") or None
                if isinstance(previous_gap_report, dict)
                else None
            ),
            previous_report=previous_gap_report,
        )
        artifacts["requirement_gap_report"] = write_requirement_gap_report(
            store=self.store,
            run_id=run_id,
            report=gap_report,
        )
        clarification_session = start_clarification_session(
            revised_model,
            previous=(
                state.get("requirement_clarification_session")
                if isinstance(state.get("requirement_clarification_session"), dict)
                else None
            ),
        )
        selection = select_current_requirement_question(
            revised_model,
            allowed_question_ids=(session_pending_ids(clarification_session) if clarification_session else None),
        )
        self._apply_v2_checkpoint_state(
            state=state,
            model=revised_model,
            artifact_paths=artifacts,
            current_question=selection.current_question.to_dict() if selection.current_question else None,
            remaining_blocking_count=selection.remaining_blocking_count,
            quality_report={
                "action": "clarify_more",
                "coverage_analysis": analysis,
                "quality_checklist": quality_checklist,
                "final_report": revised_report.to_dict(),
            },
            gap_report=gap_report,
            latest_decision=(
                state.get("latest_requirement_decision")
                if isinstance(state.get("latest_requirement_decision"), dict)
                else None
            ),
            clarification_session=clarification_session,
        )
        self.store.write_state(state)
        last_sync = self._sync_requirement_state(state)
        state["last_sync"] = last_sync
        state["updated_at"] = utc_now_iso()
        state["summary_for_user"] = self._v2_summary(state, last_sync)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_clarification_session(self, state: dict[str, object]) -> dict[str, object]:
        run_id = str(state["run_id"])
        artifacts = self._normalized_artifacts(state)
        model_ref = artifacts.get("requirement_model")
        if not model_ref:
            raise HarnessError("requirement_model_v2_missing", "Canonical Requirement V2 artifact is missing.", run_id=run_id)
        model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
        previous = state.get("requirement_clarification_session")
        session = start_clarification_session(
            model,
            previous=previous if isinstance(previous, dict) else None,
        )
        selection = select_current_requirement_question(
            model,
            allowed_question_ids=(session_pending_ids(session) if session else None),
        )
        gap_ref = artifacts.get("requirement_gap_report")
        gap_report = (
            read_requirement_gap_report(store=self.store, run_id=run_id, artifact_ref=gap_ref)
            if gap_ref
            else build_requirement_gap_report(run_id=run_id, model=model)
        )
        quality_reports = state.get("quality_reports")
        quality_report = (
            quality_reports.get("requirement_model_v2", {})
            if isinstance(quality_reports, dict)
            else {}
        )
        self._apply_v2_checkpoint_state(
            state=state,
            model=model,
            artifact_paths=artifacts,
            current_question=selection.current_question.to_dict() if selection.current_question else None,
            remaining_blocking_count=selection.remaining_blocking_count,
            quality_report=quality_report if isinstance(quality_report, dict) else {},
            gap_report=gap_report,
            latest_decision=(
                state.get("latest_requirement_decision")
                if isinstance(state.get("latest_requirement_decision"), dict)
                else None
            ),
            clarification_session=session,
        )
        self.store.write_state(state)
        last_sync = self._sync_requirement_state(state)
        state["last_sync"] = last_sync
        state["updated_at"] = utc_now_iso()
        state["summary_for_user"] = self._v2_summary(state, last_sync)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def answer_clarification(
        self,
        *,
        run_id: str,
        question_id: str,
        answer: str,
        answered_by: str,
    ) -> dict[str, object]:
        """Apply one answer to the current blocking Requirement V2 question."""

        state = self.store.read_state(run_id)
        if str(state.get("requirement_contract_version") or "v2") != "v2":
            raise HarnessError(
                "requirement_clarification_not_supported",
                "Clarification answers require Requirement Contract V2.",
                run_id=run_id,
            )
        artifacts = self._normalized_artifacts(state)
        model_ref = artifacts.get("requirement_model")
        if not model_ref:
            raise HarnessError("requirement_model_v2_missing", "Canonical Requirement V2 artifact is missing.", run_id=run_id)
        model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
        question_record = next(
            (
                item
                for item in model.open_questions
                if isinstance(item, dict) and str(item.get("id") or "") == question_id
            ),
            {},
        )
        normalized_submission = resolve_clarification_answer(question_record, answer)
        duplicate = find_clarification_submission(model, question_id, normalized_submission)
        if duplicate is not None:
            return self._status_response(state, created_new_run=False)
        if state.get("status") != "waiting_requirement_clarification":
            raise HarnessError(
                "requirement_clarification_not_ready",
                f"Run is not waiting for requirement clarification: {run_id}",
                run_id=run_id,
            )
        checkpoint = state.get("pending_checkpoint")
        current_question = checkpoint.get("current_question") if isinstance(checkpoint, dict) else None
        if (
            not isinstance(current_question, dict)
            or current_question.get("id") != question_id
        ):
            raise HarnessError(
                "requirement_question_not_current",
                f"Question is not current: {question_id}",
                run_id=run_id,
            )
        resolved_answer = resolve_clarification_answer(current_question, answer)
        source_text_by_id = read_requirement_source_texts(store=self.store, run_id=run_id, model=model)
        result = self.clarification_updater.apply(
            source_text_by_id=source_text_by_id,
            model=model,
            question_id=question_id,
            answer=resolved_answer,
            answered_by=answered_by,
        )
        policy_validation = normalize_and_validate_requirement_model_v2(
            result.model,
            source_text_by_id,
            clarification_policy_version=self._clarification_policy_version(state),
        )
        result = replace(
            result,
            model=policy_validation.model,
            final_report=policy_validation.report,
        )
        revised_artifacts = write_requirement_v2_revision_artifacts(
            store=self.store,
            run_id=run_id,
            model=result.model,
            validation_report=result.final_report,
            audit=result.to_report_dict(),
        )
        artifacts.update(revised_artifacts)
        quality_checklist = build_requirement_quality_checklist(
            model=result.model,
            validation_report=result.final_report,
        )
        artifacts["requirement_quality_checklist"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_quality_checklist_v1_r{result.model.revision}.json",
                quality_checklist,
            )
        )
        previous_gap_report = None
        gap_ref = artifacts.get("requirement_gap_report")
        if gap_ref:
            previous_gap_report = read_requirement_gap_report(
                store=self.store,
                run_id=run_id,
                artifact_ref=gap_ref,
            )
        gap_report = build_requirement_gap_report(
            run_id=run_id,
            model=result.model,
            source_manifest_input_hash=(
                str(previous_gap_report.get("source_manifest_input_hash") or "") or None
                if isinstance(previous_gap_report, dict)
                else None
            ),
            previous_report=previous_gap_report,
        )
        artifacts["requirement_gap_report"] = write_requirement_gap_report(
            store=self.store,
            run_id=run_id,
            report=gap_report,
        )
        session_value = state.get("requirement_clarification_session")
        clarification_session = (
            record_session_answer(session_value, question_id)
            if isinstance(session_value, dict)
            else start_clarification_session(result.model)
        )
        selection = select_current_requirement_question(
            result.model,
            allowed_question_ids=(
                session_pending_ids(clarification_session)
                if clarification_session is not None
                else None
            ),
        )
        if clarification_session is not None and selection.current_question is None:
            clarification_session = close_clarification_session(
                clarification_session,
                exhausted=selection.remaining_blocking_count > 0,
            )
        self._apply_v2_checkpoint_state(
            state=state,
            model=result.model,
            artifact_paths=artifacts,
            current_question=selection.current_question.to_dict() if selection.current_question else None,
            remaining_blocking_count=selection.remaining_blocking_count,
            quality_report={**result.to_report_dict(), "quality_checklist": quality_checklist},
            gap_report=gap_report,
            latest_decision=result.decision,
            clarification_session=clarification_session,
        )
        self.store.write_state(state)
        last_sync = self._sync_requirement_state(state)
        state["last_sync"] = last_sync
        state["updated_at"] = utc_now_iso()
        state["summary_for_user"] = self._v2_summary(state, last_sync)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def challenge_assumption(
        self,
        *,
        run_id: str,
        assumption_id: str,
        instruction: str,
        challenged_by: str,
    ) -> dict[str, object]:
        """Thu hồi một assumption tự động rồi mở lại source question cho con người."""

        state = self.store.read_state(run_id)
        policy_version = self._clarification_policy_version(state)
        if (
            str(state.get("requirement_contract_version") or "v2") != "v2"
            or policy_version != REASONABLE_DEFAULT_POLICY_VERSION
        ):
            raise HarnessError(
                "requirement_assumption_challenge_not_ready",
                "Assumption challenge requires a compatible Requirement V2 run.",
                run_id=run_id,
            )
        checkpoint = state.get("pending_checkpoint")
        active_ids = (
            checkpoint.get("challengeable_assumption_ids")
            if isinstance(checkpoint, dict)
            else None
        )
        if not isinstance(active_ids, list) or assumption_id not in active_ids:
            raise HarnessError(
                "requirement_assumption_stale",
                f"Challengeable assumption is no longer active: {assumption_id}",
                run_id=run_id,
            )
        if (
            state.get("status") != "waiting_requirement_approval"
            or not isinstance(checkpoint, dict)
            or checkpoint.get("type") != "requirement_approval"
        ):
            raise HarnessError(
                "requirement_assumption_challenge_not_ready",
                "Assumptions can only be challenged at the Requirement Approval checkpoint.",
                run_id=run_id,
            )

        artifacts = self._normalized_artifacts(state)
        model_ref = artifacts.get("requirement_model")
        if not model_ref:
            raise HarnessError(
                "requirement_model_v2_missing",
                "Canonical Requirement V2 artifact is missing.",
                run_id=run_id,
            )
        model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
        source_text_by_id = read_requirement_source_texts(store=self.store, run_id=run_id, model=model)
        challenge = challenge_agent_assumption(
            model,
            assumption_id=assumption_id,
            instruction=instruction,
            challenged_by=challenged_by,
        )
        validation = normalize_and_validate_requirement_model_v2(
            challenge.model,
            source_text_by_id,
            clarification_policy_version=policy_version,
        )
        clarification_session = start_clarification_session(validation.model)
        selection = select_current_requirement_question(
            validation.model,
            allowed_question_ids=(
                session_pending_ids(clarification_session)
                if clarification_session is not None
                else None
            ),
        )
        challenged_question_id = str(challenge.decision.get("question_id") or "")
        if selection.current_question is None or selection.current_question.id != challenged_question_id:
            raise HarnessError(
                "requirement_assumption_integrity_failed",
                "Challenged assumption did not reopen its source question.",
                run_id=run_id,
            )
        report = challenge.build_report(validation.model)
        revised_artifacts = write_requirement_v2_revision_artifacts(
            store=self.store,
            run_id=run_id,
            model=validation.model,
            validation_report=validation.report,
            audit={
                "action": "challenge_assumption",
                "mode": "deterministic_only",
                "assumption_challenge": report,
            },
        )
        artifacts.update(revised_artifacts)
        artifacts[ASSUMPTION_CHALLENGE_ARTIFACT_KEY] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"{ASSUMPTION_CHALLENGE_ARTIFACT_PREFIX}_r{validation.model.revision}.json",
                report,
            )
        )

        quality_checklist = build_requirement_quality_checklist(
            model=validation.model,
            validation_report=validation.report,
        )
        artifacts["requirement_quality_checklist"] = self.store.relative(
            self.store.write_json_artifact(
                run_id,
                f"requirement_quality_checklist_v1_r{validation.model.revision}.json",
                quality_checklist,
            )
        )
        previous_gap_report = None
        gap_ref = artifacts.get("requirement_gap_report")
        if gap_ref:
            previous_gap_report = read_requirement_gap_report(
                store=self.store,
                run_id=run_id,
                artifact_ref=gap_ref,
            )
        gap_report = build_requirement_gap_report(
            run_id=run_id,
            model=validation.model,
            source_manifest_input_hash=(
                str(previous_gap_report.get("source_manifest_input_hash") or "") or None
                if isinstance(previous_gap_report, dict)
                else None
            ),
            previous_report=previous_gap_report,
        )
        artifacts["requirement_gap_report"] = write_requirement_gap_report(
            store=self.store,
            run_id=run_id,
            report=gap_report,
        )
        quality_report = {
            "action": "challenge_assumption",
            "mode": "deterministic_only",
            "assumption_challenge": report,
            "final_report": validation.report.to_dict(),
            "quality_checklist": quality_checklist,
        }
        self._apply_v2_checkpoint_state(
            state=state,
            model=validation.model,
            artifact_paths=artifacts,
            current_question=selection.current_question.to_dict(),
            remaining_blocking_count=selection.remaining_blocking_count,
            quality_report=quality_report,
            gap_report=gap_report,
            latest_decision=challenge.decision,
            clarification_session=clarification_session,
        )
        reports = state.get("quality_reports")
        if isinstance(reports, dict):
            reports[ASSUMPTION_CHALLENGE_ARTIFACT_KEY] = assumption_challenge_quality_summary(report)
        self.store.write_state(state)
        last_sync = self._sync_requirement_state(state)
        state["last_sync"] = last_sync
        state["updated_at"] = utc_now_iso()
        state["summary_for_user"] = (
            f"Đã thu hồi assumption {assumption_id}; Requirement V2 cần người dùng "
            f"trả lời lại câu {challenged_question_id}. Sync status={last_sync.get('status')}."
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _apply_v2_checkpoint_state(
        self,
        *,
        state: dict[str, object],
        model,
        artifact_paths: dict[str, str],
        current_question: dict[str, object] | None,
        remaining_blocking_count: int,
        quality_report: dict[str, object],
        gap_report: dict[str, object],
        latest_decision: dict[str, object] | None,
        clarification_session: dict[str, object] | None,
    ) -> None:
        run_id = str(state["run_id"])
        deferred_question_ids = self._deferred_question_ids(model)
        agent_default_assumption_ids = self._agent_default_metadata(model)
        policy_version = self._clarification_policy_version(state)
        challengeable_assumption_ids = agent_default_assumption_ids
        shadow_quality = self._write_clarification_policy_shadow(
            model=model,
            artifact_paths=artifact_paths,
        )
        reasonable_default_quality = None
        if policy_version == REASONABLE_DEFAULT_POLICY_VERSION:
            reasonable_default_quality = self._write_agent_reasonable_default_report(
                model=model,
                artifact_paths=artifact_paths,
            )
        assumption_fields = {
            "challengeable_assumption_count": len(challengeable_assumption_ids),
            "challengeable_assumption_ids": challengeable_assumption_ids,
        }
        if current_question is not None:
            status = "waiting_requirement_clarification"
            current_phase = "requirement_clarification"
            checkpoint = {
                "checkpoint_id": f"checkpoint_requirement_clarification_{run_id}",
                "type": "requirement_clarification",
                "artifact_ref": f"requirement_model_v2_r{model.revision}",
                "artifact_path": artifact_paths["requirement_model"],
                "current_question": current_question,
                "remaining_blocking_count": remaining_blocking_count,
                "deferred_to_design_count": len(deferred_question_ids),
                "deferred_to_design_question_ids": deferred_question_ids,
                **assumption_fields,
            }
            allowed = ALLOWED_REQUIREMENT_CLARIFICATION_ACTIONS
        elif remaining_blocking_count > 0:
            status = "waiting_requirement_clarification_continue"
            current_phase = "requirement_clarification"
            checkpoint = {
                "checkpoint_id": f"checkpoint_requirement_clarification_continue_{run_id}",
                "type": "requirement_clarification_continue",
                "artifact_ref": f"requirement_model_v2_r{model.revision}",
                "artifact_path": artifact_paths["requirement_model"],
                "remaining_blocking_count": remaining_blocking_count,
                "session_limit_reached": True,
                "deferred_to_design_count": len(deferred_question_ids),
                "deferred_to_design_question_ids": deferred_question_ids,
                **assumption_fields,
            }
            allowed = ALLOWED_REQUIREMENT_CLARIFICATION_CONTINUE_ACTIONS
        else:
            status = "waiting_requirement_approval"
            current_phase = status
            checkpoint = {
                "checkpoint_id": f"checkpoint_requirement_{run_id}",
                "type": "requirement_approval",
                "artifact_ref": f"requirement_model_v2_r{model.revision}",
                "artifact_path": artifact_paths["requirement_model"],
                "review_projection_ref": artifact_paths["requirement_packet"],
                "deferred_to_design_count": len(deferred_question_ids),
                "deferred_to_design_question_ids": deferred_question_ids,
                **assumption_fields,
            }
            allowed = list(ALLOWED_REQUIREMENT_ACTIONS)
            if challengeable_assumption_ids:
                allowed.insert(1, "challenge_assumption")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": current_phase,
                "status": status,
                "latest_artifacts": artifact_paths,
                "pending_checkpoint": checkpoint,
                "allowed_next_actions": list(allowed),
                "last_error": None,
                "quality_reports": {
                    "requirement_model_v2": quality_report,
                    "requirement_gaps": gap_report_summary(gap_report),
                    "requirement_clarification_policy_shadow": shadow_quality,
                    **(
                        {"requirement_reasonable_defaults": reasonable_default_quality}
                        if reasonable_default_quality is not None
                        else {}
                    ),
                    "requirement_clarification_disposition": {
                        "policy_version": self._clarification_policy_version(state),
                        "deferred_to_design_count": len(deferred_question_ids),
                        "deferred_to_design_question_ids": deferred_question_ids,
                        **assumption_fields,
                    },
                },
                "latest_requirement_decision": latest_decision,
                "requirement_clarification_session": clarification_session,
            }
        )

    @staticmethod
    def _deferred_question_ids(model: RequirementModelV2) -> list[str]:
        ids: list[str] = []
        for question in model.open_questions:
            if not isinstance(question, dict) or question.get("status") != "open":
                continue
            disposition = question.get(CLARIFICATION_DISPOSITION_FIELD)
            if isinstance(disposition, dict) and disposition.get("value") == DEFERRED_TO_DESIGN:
                question_id = str(question.get("id") or "")
                if question_id:
                    ids.append(question_id)
        return ids

    @staticmethod
    def _agent_default_metadata(model: RequirementModelV2) -> list[str]:
        return [
            str(assumption.get("id") or "")
            for assumption in model.assumptions
            if isinstance(assumption, dict)
            and assumption.get("origin") == AGENT_DEFAULT_ORIGIN
            and str(assumption.get("id") or "")
        ]

    @staticmethod
    def _clarification_policy_version(state: dict[str, object]) -> str:
        value = str(state.get("requirement_clarification_policy_version") or "")
        if not value:
            return LEGACY_BLOCKING_POLICY_VERSION
        if value not in {
            LEGACY_BLOCKING_POLICY_VERSION,
            SHADOW_POLICY_VERSION,
            REASONABLE_DEFAULT_POLICY_VERSION,
        }:
            raise HarnessError(
                "requirement_clarification_policy_invalid",
                f"Unsupported Requirement clarification policy: {value!r}.",
                run_id=str(state.get("run_id") or "") or None,
            )
        return value

    def _write_clarification_policy_shadow(
        self,
        *,
        model: RequirementModelV2,
        artifact_paths: dict[str, str],
    ) -> dict[str, object]:
        """Ghi shadow report theo kiểu fail-open; không có decision authority."""

        try:
            report = build_clarification_policy_shadow(model)
            artifact_paths[SHADOW_ARTIFACT_KEY] = self.store.relative(
                self.store.write_json_artifact(
                    model.run_id,
                    f"{SHADOW_ARTIFACT_PREFIX}_r{model.revision}.json",
                    report,
                )
            )
            return shadow_quality_summary(report)
        except Exception as exc:  # Shadow observability không được chặn workflow chính.
            artifact_paths.pop(SHADOW_ARTIFACT_KEY, None)
            return shadow_error_summary(exc)

    def _write_agent_reasonable_default_report(
        self,
        *,
        model: RequirementModelV2,
        artifact_paths: dict[str, str],
    ) -> dict[str, object]:
        report = build_agent_reasonable_default_report(model)
        artifact_paths[REASONABLE_DEFAULT_ARTIFACT_KEY] = self.store.relative(
            self.store.write_json_artifact(
                model.run_id,
                f"{REASONABLE_DEFAULT_ARTIFACT_PREFIX}_r{model.revision}.json",
                report,
            )
        )
        return reasonable_default_quality_summary(report)

    def _normalized_artifacts(self, state: dict[str, object]) -> dict[str, str]:
        value = state.get("latest_artifacts")
        if not isinstance(value, dict):
            raise HarnessError("sync_not_ready", "Run artifacts are not ready.", run_id=str(state.get("run_id") or ""))
        return {str(key): str(item) for key, item in value.items()}

    def _requirement_sync_context(self, state: dict[str, object]) -> dict[str, object]:
        checkpoint = state.get("pending_checkpoint")
        context: dict[str, object] = {
            "requirement_contract_version": str(state.get("requirement_contract_version") or "v2"),
            "status": str(state.get("status") or ""),
        }
        if isinstance(checkpoint, dict):
            context["checkpoint_type"] = str(checkpoint.get("type") or "")
            context["current_question"] = checkpoint.get("current_question")
            context["remaining_blocking_count"] = checkpoint.get("remaining_blocking_count", 0)
            context["deferred_to_design_count"] = checkpoint.get("deferred_to_design_count", 0)
            context["deferred_to_design_question_ids"] = checkpoint.get(
                "deferred_to_design_question_ids",
                [],
            )
            context["challengeable_assumption_count"] = checkpoint.get(
                "challengeable_assumption_count",
                0,
            )
            context["challengeable_assumption_ids"] = checkpoint.get(
                "challengeable_assumption_ids",
                [],
            )
        decision = state.get("latest_requirement_decision")
        if isinstance(decision, dict):
            context["decision_summary"] = {
                "id": decision.get("id"),
                "question_id": decision.get("question_id"),
                "status": decision.get("status"),
                "answered_by": decision.get("answered_by"),
            }
        quality_reports = state.get("quality_reports")
        requirement_quality = (
            quality_reports.get("requirement_model_v2")
            if isinstance(quality_reports, dict)
            else None
        )
        checklist = (
            requirement_quality.get("quality_checklist")
            if isinstance(requirement_quality, dict)
            else None
        )
        if isinstance(checklist, dict):
            context["quality_checklist"] = {
                "status": checklist.get("status"),
                "blocking_failure_count": checklist.get("blocking_failure_count", 0),
                "blocking_question_ids": checklist.get("blocking_question_ids", []),
            }
        assumption_challenge = (
            quality_reports.get(ASSUMPTION_CHALLENGE_ARTIFACT_KEY)
            if isinstance(quality_reports, dict)
            else None
        )
        if isinstance(assumption_challenge, dict):
            context["assumption_challenge"] = {
                "status": assumption_challenge.get("status"),
                "assumption_id": assumption_challenge.get("assumption_id"),
                "origin": assumption_challenge.get("origin"),
                "question_id": assumption_challenge.get("question_id"),
                "challenged_by": assumption_challenge.get("challenged_by"),
            }
        return context

    def _sync_requirement_state(self, state: dict[str, object]) -> dict[str, object]:
        return sync_requirement_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=str(state["run_id"]),
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=self._normalized_artifacts(state),
            checkpoint_context=self._requirement_sync_context(state),
        )

    @staticmethod
    def _v2_summary(state: dict[str, object], last_sync: dict[str, object]) -> str:
        checkpoint = state.get("pending_checkpoint")
        if state.get("status") == "waiting_requirement_clarification" and isinstance(checkpoint, dict):
            question = checkpoint.get("current_question")
            question_text = question.get("text") if isinstance(question, dict) else ""
            return (
                f"Requirement V2 cần anh làm rõ: {question_text} "
                f"Sync status={last_sync.get('status')}."
            )
        if state.get("status") == "waiting_requirement_clarification_continue":
            return (
                "Phiên làm rõ Requirement V2 đã đủ 5 câu; vẫn còn câu hỏi blocking. "
                f"Chỉ dùng clarify_more khi người dùng muốn mở thêm một lượt. "
                f"Sync status={last_sync.get('status')}."
            )
        challengeable_count = checkpoint.get("challengeable_assumption_count", 0) if isinstance(checkpoint, dict) else 0
        return (
            "Requirement V2 đã hết câu hỏi blocking và đang chờ duyệt. "
            f"Assumptions có thể phản đối={challengeable_count}. "
            f"Sync status={last_sync.get('status')}."
        )

    def get_status(self, *, run_id: str) -> dict[str, object]:
        """Đọc state hiện tại của run để Niko phản hồi người dùng."""

        state = self.store.read_state(run_id)
        return self._status_response(state, created_new_run=False)

    def sync_run(self, *, run_id: str) -> dict[str, object]:
        """Sync lại checkpoint hiện tại của run lên tool theo config hiện tại."""

        state = self.store.read_state(run_id)
        artifact_paths = state.get("latest_artifacts")
        if not isinstance(artifact_paths, dict):
            from ..core.errors import HarnessError

            raise HarnessError("sync_not_ready", f"Run is not ready for sync: {run_id}", run_id=run_id)
        source_ref = str(state.get("source_ref", ""))
        normalized_artifacts = {str(key): str(value) for key, value in artifact_paths.items()}
        checkpoint = state.get("pending_checkpoint")
        checkpoint_type = checkpoint.get("type") if isinstance(checkpoint, dict) else ""
        if checkpoint_type == "implementation_plan_review" and "implementation_plan" in normalized_artifacts:
            last_sync = sync_implementation_plan_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
            )
            waiting_text = "Run vẫn đang chờ anh review implementation plan trước khi sang Phase 4."
        elif checkpoint_type == "design_approval" and "design_artifact" in normalized_artifacts:
            last_sync = sync_design_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
            )
            waiting_text = "Run vẫn đang chờ anh review design trước khi sang Phase 3."
        elif checkpoint_type in {
            "requirement_approval",
            "requirement_clarification",
            "requirement_clarification_continue",
        } and "requirement_packet" in normalized_artifacts:
            last_sync = sync_requirement_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
                checkpoint_context=self._requirement_sync_context(state),
            )
            if checkpoint_type == "requirement_clarification":
                waiting_text = "Run vẫn đang chờ anh trả lời câu hỏi làm rõ requirement hiện tại."
            elif checkpoint_type == "requirement_clarification_continue":
                waiting_text = "Phiên làm rõ đã đạt giới hạn; dùng continue_run để mở tối đa 5 câu tiếp theo."
            else:
                waiting_text = "Run vẫn đang chờ anh duyệt requirement trước khi chuyển sang design."
        else:
            from ..core.errors import HarnessError

            raise HarnessError("sync_not_ready", f"Run is not ready for sync: {run_id}", run_id=run_id)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "last_sync": last_sync,
                "summary_for_user": (
                    f"Đã chạy sync cho run {run_id}; sync status={last_sync.get('status')}. "
                    f"{waiting_text}"
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        """Chuẩn hóa JSON response cho cả start và get-status."""

        allowed_next_actions = [str(action) for action in state.get("allowed_next_actions", []) if action]
        last_error = state.get("last_error")
        latest_artifacts = state.get("latest_artifacts")
        public_artifacts = dict(latest_artifacts) if isinstance(latest_artifacts, dict) else {}
        public_artifacts.pop("requirement_loop_session", None)
        legacy_verification_repair = (
            str(state.get("status") or "") == "blocked"
            and isinstance(last_error, dict)
            and last_error.get("error_code") == "test_failed"
            and isinstance(latest_artifacts, dict)
            and bool(latest_artifacts.get("developer_work_packet"))
            and bool(latest_artifacts.get("code_change_report"))
            and bool(latest_artifacts.get("test_report"))
            and not bool(latest_artifacts.get("implementation_repair_feedback"))
            and not (
                isinstance(state.get("quality_reports"), dict)
                and bool(state["quality_reports"].get("verification"))
            )
        )
        if legacy_verification_repair and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if repair_scope_mismatch_retry_allowed(state) and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if developer_transport_retry_allowed(state) and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        profiled_qa_path_retry = (
            str(state.get("status") or "") == "blocked"
            and str(state.get("current_phase") or "") == "qa_testing"
            and isinstance(last_error, dict)
            and last_error.get("error_code") == "developer_act_unsafe_path"
            and str(last_error.get("message") or "").startswith(
                "Unsafe or unsupported Developer.act path:"
            )
            and isinstance(state.get("profile_snapshot"), dict)
            and bool(state.get("profile_snapshot"))
        )
        if profiled_qa_path_retry and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if (
            str(state.get("status") or "") in {"implementation_repair_needed", "implementation_traced"}
            and "revalidate_implementation" not in allowed_next_actions
        ):
            allowed_next_actions.insert(1 if allowed_next_actions else 0, "revalidate_implementation")
        response = {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
            "requirement_clarification_policy_version": state.get(
                "requirement_clarification_policy_version",
                LEGACY_BLOCKING_POLICY_VERSION,
            ),
            REPO_DISCOVERY_CONTRACT_STATE_KEY: state.get(REPO_DISCOVERY_CONTRACT_STATE_KEY),
            DESIGNER_CONTRACT_STATE_KEY: state.get(DESIGNER_CONTRACT_STATE_KEY),
            VERTICAL_TRACE_STATE_KEY: state.get(VERTICAL_TRACE_STATE_KEY),
            IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY: resolve_implementation_plan_contract(state),
            "vertical_trace_mode": state.get("vertical_trace_mode"),
            "execution_profile": profile_status_metadata(state),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": public_artifacts,
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": allowed_next_actions,
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "developer_execution": state.get("developer_execution", {}),
            "designer_execution": state.get("designer_execution", {}),
            "traceability_diagnostics": state.get("traceability_diagnostics", []),
        }
        requirement_execution = self._requirement_execution_summary(str(state.get("run_id") or ""))
        if requirement_execution is not None:
            response["requirement_execution"] = requirement_execution
        checkpoint = response.get("pending_checkpoint")
        last_sync = response.get("last_sync")
        last_error = response.get("last_error")
        if (
            response.get("status") == "failed"
            and str(response.get("current_phase") or "").startswith("design_")
            and isinstance(last_error, dict)
            and last_error.get("error_code") == "validator_traceability_failed"
        ):
            allowed_actions = [str(action) for action in response["allowed_next_actions"]]
            if "revalidate_design" not in allowed_actions:
                allowed_actions.insert(0, "revalidate_design")
            response["allowed_next_actions"] = allowed_actions
        if response.get("current_phase") == "implementation_planning" and response.get("status") in {
            "waiting_implementation_plan_review",
            "blocked",
            "design_blocked",
        }:
            allowed_actions = [str(action) for action in response["allowed_next_actions"]]
            if "revalidate_implementation_plan" not in allowed_actions:
                allowed_actions.insert(0, "revalidate_implementation_plan")
            response["allowed_next_actions"] = allowed_actions
        if response.get("current_phase") == "design_review" and response.get("status") in {
            "waiting_design_approval",
            "blocked",
        }:
            allowed_actions = [str(action) for action in response["allowed_next_actions"]]
            if "revalidate_design" not in allowed_actions:
                allowed_actions.insert(0, "revalidate_design")
            response["allowed_next_actions"] = allowed_actions
        if (
            isinstance(checkpoint, dict)
            and isinstance(last_sync, dict)
            and str(last_sync.get("status") or "") in {"failed", "partial_failed"}
        ):
            allowed_actions = [str(action) for action in response["allowed_next_actions"]]
            if "sync_run" not in allowed_actions:
                insert_at = allowed_actions.index("cancel_run") if "cancel_run" in allowed_actions else len(allowed_actions)
                allowed_actions.insert(insert_at, "sync_run")
            response["allowed_next_actions"] = allowed_actions
        if (
            response.get("status") == "code_review_ready"
            and isinstance(checkpoint, dict)
            and checkpoint.get("type") == "code_review"
        ):
            run_id = str(response.get("run_id") or "")
            pull_request_url = str(checkpoint.get("pull_request_url") or "").strip()
            if pull_request_url:
                draft_pr_summary = f"Draft PR: {pull_request_url}. "
            else:
                draft_pr_summary = "Draft PR đã được tạo. "
            response["allowed_next_actions"] = list(ALLOWED_CODE_REVIEW_READY_ACTIONS)
            response["summary_for_user"] = (
                f"Checkpoint Code Review đã sẵn sàng cho run {run_id}. "
                f"{draft_pr_summary}"
                "Anh có thể approve checkpoint để chuyển Draft PR thành Ready for review."
            )
        return response

    def _requirement_execution_summary(self, run_id: str) -> dict[str, object] | None:
        if not run_id or not self.store.artifact_path(run_id, SESSION_FILENAME).exists():
            return None
        try:
            session = read_requirement_session(store=self.store, run_id=run_id)
            return requirement_session_summary(session)
        except HarnessError:
            return {"schema_version": "requirement_loop_session_v1", "status": "invalid"}
