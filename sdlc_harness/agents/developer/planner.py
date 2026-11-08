"""Developer.plan cho Phase 3: biến design đã duyệt thành kế hoạch sửa code.

Module này chưa sửa target repo. Nó chỉ đọc requirement/design/trace graph và
tạo implementation plan có cấu trúc để Validator có thể kiểm tra trước khi
human duyệt sang Phase 4. Khi Validator hybrid phát hiện thiếu sót, module này
tạo version plan kế tiếp theo feedback nhưng vẫn không ghi vào target repo.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from ..requirements import RequirementModelV2
from ...core.config import phase3_llm_call_options
from ...profiles.runtime_config import (
    canonical_runtime_config_impact,
    runtime_config_policy,
    runtime_config_prompt_context,
)
from ...core.errors import HarnessError
from ...llm.client import strip_code_fence
from ...profiles import (
    LanguagePromptDirective,
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PlanningPathPolicy,
    QA_INTENT_STAGE_OWNER,
    VerificationStagePolicy,
    build_implementation_signature_contract,
    get_language_profile,
    resolve_language_prompt_directive,
)
from .component_plan import (
    COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
    build_component_work_units,
    canonicalize_work_unit_dependencies,
    component_plan_projections,
    merge_work_unit_updates,
    test_intent_id,
    rebind_legacy_mappings_to_work_units,
    unit_write_scope,
)


@dataclass(frozen=True)
class ImplementationPlanDraft:
    """Kết quả Developer.plan gồm bản Markdown và JSON máy đọc được."""

    markdown: str
    structured: dict[str, object]


class DeveloperPlanAgent:
    """Vai Developer ở Phase 3: lập kế hoạch, chưa sinh code."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def _llm_plan(
        self,
        *,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        repo_context: dict[str, object],
        base_mappings: list[dict[str, object]],
        base_test_intents: list[dict[str, object]],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str,
        repair_feedback: str,
        previous_review: dict[str, object],
        canonical_requirement: RequirementModelV2 | None,
        planning_policy: PlanningPathPolicy,
        prompt_directive: LanguagePromptDirective | None,
        verification_policy: VerificationStagePolicy,
    ) -> dict[str, object]:
        """Nhờ Developer LLM đọc context repo và viết plan giàu ngữ cảnh hơn."""

        if not self.llm_client:
            return {}
        prompt = _developer_plan_prompt(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            repo_context=repo_context,
            base_mappings=base_mappings,
            base_test_intents=base_test_intents,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repair_feedback=repair_feedback,
            previous_review=previous_review,
            canonical_requirement=canonical_requirement,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
        )
        raw = _complete_phase3_json(
            self.llm_client,
            system=(
                "You are SDLC Harness Developer.plan. Return JSON only. "
                "Create a concrete implementation plan, but do not write code."
            ),
            prompt=prompt,
            profile_name="developer_repair" if repair_feedback.strip() else "developer_plan",
        )
        try:
            return _parse_developer_plan_payload(raw)
        except HarnessError as exc:
            return {"_developer_llm_parse_error": exc.message}

    def plan(
        self,
        *,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str = "",
        repo_context: dict[str, object] | None = None,
        repair_feedback: str = "",
        previous_review: dict[str, object] | None = None,
        workspace_mode: str = "",
        workspace_ref: str = "",
        base_sha: str = "",
        canonical_requirement: RequirementModelV2 | None = None,
        requirement_graph: dict[str, object] | None = None,
        requirement_artifact_refs: dict[str, str] | None = None,
        planning_policy: PlanningPathPolicy | None = None,
        prompt_directive: LanguagePromptDirective | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        contract_version: str = PROFILED_IMPLEMENTATION_PLAN_VERSION,
        vertical_design_graph: dict[str, object] | None = None,
        vertical_trace_graph: dict[str, object] | None = None,
        repo_landscape: dict[str, object] | None = None,
        component_plan_inputs: dict[str, object] | None = None,
    ) -> ImplementationPlanDraft:
        """Tạo plan V1 từ graph đã duyệt để checkpoint không còn prose mơ hồ."""

        verification_policy = _resolve_verification_policy(
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        assert planning_policy is not None
        repo_context = repo_context or {}
        if contract_version == COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION:
            return self._plan_component_aware(
                run_id=run_id,
                source_ref=source_ref,
                issue_key=issue_key,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                target_repo_path=target_repo_path,
                target_repo_remote=target_repo_remote,
                target_base_branch=target_base_branch,
                planned_branch=planned_branch,
                target_files=target_files,
                repo_context=repo_context,
                workspace_mode=workspace_mode,
                workspace_ref=workspace_ref,
                base_sha=base_sha,
                requirement_graph=requirement_graph or {},
                requirement_artifact_refs=requirement_artifact_refs or {},
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
                vertical_design_graph=vertical_design_graph or {},
                vertical_trace_graph=vertical_trace_graph or {},
                repo_landscape=repo_landscape or {},
                component_plan_inputs=component_plan_inputs or {},
            )
        mappings = _design_mappings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
            target_repo_path=target_repo_path,
            requirement_design_trace=requirement_design_trace,
            planning_policy=planning_policy,
        )
        design_adjustments = _design_adjustments(
            class_graph=class_graph,
            class_targets=_class_target_files(
                class_graph=class_graph,
                target_files=target_files,
                planning_policy=planning_policy,
            ),
            target_repo_path=target_repo_path,
            requirement_design_trace=requirement_design_trace,
            planning_policy=planning_policy,
        )
        mappings = _apply_design_adjustments(mappings, design_adjustments)
        test_intents = _test_intents(
            mappings=mappings,
            test_command=test_command,
            verification_policy=verification_policy,
        )
        llm_payload = self._llm_plan(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            repo_context=repo_context,
            base_mappings=mappings,
            base_test_intents=test_intents,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repair_feedback=repair_feedback,
            previous_review=previous_review or {},
            canonical_requirement=canonical_requirement,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
        )
        mappings = _merge_mappings(
            mappings,
            _dict_items(llm_payload.get("design_mappings")),
            planning_policy=planning_policy,
        )
        mappings = _apply_design_adjustments(mappings, design_adjustments)
        test_intents = _dict_items(llm_payload.get("test_intents")) or test_intents
        requirement_coverage = _requirement_coverage(
            requirement_graph=requirement_graph or {},
            requirement_design_trace=requirement_design_trace,
            mappings=mappings,
        ) if canonical_requirement is not None else []
        llm_parse_error = str(llm_payload.get("_developer_llm_parse_error") or "").strip()
        implementation_steps = _apply_design_adjustments_to_steps(
            _dict_items(llm_payload.get("implementation_steps"))
            or _implementation_steps_from_mappings(mappings, planning_policy=planning_policy),
            design_adjustments,
        )
        implementation_steps = _canonicalize_implementation_steps(
            implementation_steps,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        proposed_files = _canonical_proposed_files(
            proposed_files=_dict_items(llm_payload.get("proposed_files")),
            implementation_steps=implementation_steps,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        proposed_files = _ensure_executable_test_files(
            proposed_files=proposed_files,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        signature_contract = (
            build_implementation_signature_contract(
                planning_policy=planning_policy,
                mappings=mappings,
                class_graph=class_graph,
                candidate=llm_payload.get("implementation_signature_contract"),
            )
            if planning_policy and verification_policy
            else None
        )
        structured = {
            "version": PROFILED_IMPLEMENTATION_PLAN_VERSION,
            "planning_mode": (
                "llm_developer_with_codegraph_context_v1"
                if self.llm_client and not llm_parse_error
                else "deterministic_graph_mapping_after_llm_parse_error_v1"
                if self.llm_client
                else "deterministic_graph_mapping_v1"
            ),
            "run_id": run_id,
            "source_ref": source_ref,
            "issue_key": issue_key,
            "target_repository": {
                "local_path": target_repo_path,
                "remote": target_repo_remote,
                "base_branch": target_base_branch,
                "planned_branch": planned_branch,
                "workspace_mode": workspace_mode,
                "workspace_ref": workspace_ref,
                "base_sha": base_sha,
            },
            "planning_profile": planning_policy.metadata(),
            "verification_stage_ids": list(verification_policy.stage_ids),
            "inputs": {
                "requirement_design_trace_version": requirement_design_trace.get("version", ""),
                "class_graph_version": class_graph.get("version", ""),
                "sequence_graph_version": sequence_graph.get("version", ""),
            },
            "requirement_contract": {
                "version": "v2" if canonical_requirement is not None else "v1",
                "revision": canonical_requirement.revision if canonical_requirement is not None else 0,
                "artifact_refs": requirement_artifact_refs or {},
            },
            "repo_context": _repo_context_summary(repo_context),
            "repo_evidence": _string_items(llm_payload.get("repo_evidence"))
            or _repo_context_evidence_lines(repo_context),
            "implementation_steps": implementation_steps,
            "proposed_files": proposed_files,
            "design_mappings": mappings,
            **(
                {"implementation_signature_contract": signature_contract}
                if signature_contract is not None
                else {}
            ),
            "design_adjustments": design_adjustments,
            "requirement_coverage": requirement_coverage,
            "test_intents": test_intents,
            "assumptions": [
                "Phase 3 tạo branch/worktree riêng cho run để lập kế hoạch; Phase 4 mới sửa code, chạy test và mở Draft PR.",
                "Các target file chưa tồn tại được hiểu là file Phase 4 có thể tạo mới trong run workspace.",
            ],
            "open_questions": [
                "Nếu human muốn đổi kiến trúc hoặc component target, cần request_change trước khi duyệt plan.",
            ],
            "developer_llm": {
                "used": bool(self.llm_client),
                "summary": str(llm_payload.get("summary") or "").strip(),
                "parse_error": llm_parse_error,
                "repair_feedback_attached": bool(repair_feedback.strip()),
            },
            "out_of_scope": [
                "Không sửa source/test hoặc tạo commit trong Phase 3.",
                "Không ghi file source/test trong target repo ở Phase 3.",
                "Không tạo Pull Request trong Phase 3.",
            ],
        }
        markdown = _markdown_plan(
            structured=structured,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
        )
        return ImplementationPlanDraft(markdown=markdown, structured=structured)

    def repair_plan(
        self,
        *,
        previous_draft: ImplementationPlanDraft,
        review_report: dict[str, object],
        developer_feedback: str,
        iteration: int,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str = "",
        repo_context: dict[str, object] | None = None,
        workspace_mode: str = "",
        workspace_ref: str = "",
        base_sha: str = "",
        canonical_requirement: RequirementModelV2 | None = None,
        requirement_graph: dict[str, object] | None = None,
        requirement_artifact_refs: dict[str, str] | None = None,
        planning_policy: PlanningPathPolicy | None = None,
        prompt_directive: LanguagePromptDirective | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        contract_version: str = PROFILED_IMPLEMENTATION_PLAN_VERSION,
        vertical_design_graph: dict[str, object] | None = None,
        vertical_trace_graph: dict[str, object] | None = None,
        repo_landscape: dict[str, object] | None = None,
        component_plan_inputs: dict[str, object] | None = None,
    ) -> ImplementationPlanDraft:
        """Apply a repair patch to the previous candidate without rebuilding it from scratch."""

        verification_policy = _resolve_verification_policy(
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        assert planning_policy is not None
        previous = dict(previous_draft.structured)
        if contract_version == COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION:
            return self._repair_component_aware(
                previous=previous,
                review_report=review_report,
                developer_feedback=developer_feedback,
                iteration=iteration,
                run_id=run_id,
                source_ref=source_ref,
                issue_key=issue_key,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                target_repo_path=target_repo_path,
                target_repo_remote=target_repo_remote,
                target_base_branch=target_base_branch,
                planned_branch=planned_branch,
                target_files=target_files,
                repo_context=repo_context or {},
                workspace_mode=workspace_mode,
                workspace_ref=workspace_ref,
                base_sha=base_sha,
                requirement_graph=requirement_graph or {},
                requirement_artifact_refs=requirement_artifact_refs or {},
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
                vertical_design_graph=vertical_design_graph or {},
                vertical_trace_graph=vertical_trace_graph or {},
                repo_landscape=repo_landscape or {},
                component_plan_inputs=component_plan_inputs or {},
            )
        previous_mappings = _dict_items(previous.get("design_mappings"))
        previous_tests = _dict_items(previous.get("test_intents"))
        compact_review = _compact_review(review_report)
        repair_context = {
            **_repo_context_summary(repo_context or {}),
            "previous_plan_candidate": _repair_candidate(previous),
        }
        llm_payload = self._llm_plan(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repo_context=repair_context,
            base_mappings=previous_mappings,
            base_test_intents=previous_tests,
            repair_feedback=developer_feedback,
            previous_review=compact_review,
            canonical_requirement=canonical_requirement,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
        )
        mappings = _merge_mappings(
            previous_mappings,
            _dict_items(llm_payload.get("design_mappings")),
            planning_policy=planning_policy,
        )
        adjustments = _dict_items(previous.get("design_adjustments"))
        mappings = _apply_design_adjustments(mappings, adjustments)
        test_intents = _dict_items(llm_payload.get("test_intents")) or previous_tests
        implementation_steps = _apply_design_adjustments_to_steps(
            _dict_items(llm_payload.get("implementation_steps"))
            or _dict_items(previous.get("implementation_steps")),
            adjustments,
        )
        implementation_steps = _canonicalize_implementation_steps(
            implementation_steps,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        proposed_files = _canonical_proposed_files(
            proposed_files=[
                *_dict_items(previous.get("proposed_files")),
                *_dict_items(llm_payload.get("proposed_files")),
            ],
            implementation_steps=implementation_steps,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        proposed_files = _ensure_executable_test_files(
            proposed_files=proposed_files,
            mappings=mappings,
            planning_policy=planning_policy,
        )
        signature_contract = (
            _merge_implementation_signature_contract(
                planning_policy=planning_policy,
                mappings=mappings,
                class_graph=class_graph,
                previous=previous.get("implementation_signature_contract"),
                patch=llm_payload.get("implementation_signature_contract"),
            )
            if planning_policy and verification_policy
            else None
        )
        requirement_coverage = (
            _requirement_coverage(
                requirement_graph=requirement_graph or {},
                requirement_design_trace=requirement_design_trace,
                mappings=mappings,
            )
            if canonical_requirement is not None
            else []
        )
        llm_parse_error = str(llm_payload.get("_developer_llm_parse_error") or "").strip()
        history = _dict_items(previous.get("repair_history"))
        history.append(
            {
                "iteration": iteration,
                "source": "validator_review_plan",
                "previous_plan_version": f"implementation_plan_v{iteration}",
                "issue_count": int(review_report.get("issue_count") or len(_string_items(review_report.get("issues")))),
                "feedback": developer_feedback,
            }
        )
        structured = {
            **previous,
            "version": (
                PROFILED_IMPLEMENTATION_PLAN_VERSION
                if verification_policy
                else previous.get("version", "implementation_plan_structured_v1")
            ),
            "planning_mode": "llm_repair_patch_v1" if not llm_parse_error else "deterministic_repair_after_llm_parse_error_v1",
            "repo_context": _repo_context_summary(repo_context or {}),
            "repo_evidence": _string_items(llm_payload.get("repo_evidence")) or list(previous.get("repo_evidence", [])),
            "implementation_steps": implementation_steps,
            "proposed_files": proposed_files,
            "design_mappings": mappings,
            **(
                {"implementation_signature_contract": signature_contract}
                if signature_contract is not None
                else {}
            ),
            "design_adjustments": adjustments,
            "requirement_coverage": requirement_coverage,
            "verification_stage_ids": (
                list(verification_policy.stage_ids)
                if verification_policy
                else _string_items(previous.get("verification_stage_ids"))
            ),
            "test_intents": test_intents,
            "repair_iteration": iteration,
            "repair_source": "validator_review_plan",
            "repair_history": history,
            "developer_llm": {
                "used": bool(self.llm_client),
                "summary": str(llm_payload.get("summary") or "").strip(),
                "parse_error": llm_parse_error,
                "repair_feedback_attached": True,
            },
        }
        markdown = _markdown_plan(
            structured=structured,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
        )
        markdown += (
            "\n\n## Repair Feedback Addressed\n\n"
            f"- Repair iteration: {iteration}\n"
            f"- Validator feedback: {developer_feedback or 'No free-form feedback provided.'}\n"
        )
        return ImplementationPlanDraft(markdown=markdown, structured=structured)

    def _plan_component_aware(
        self,
        *,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        repo_context: dict[str, object],
        workspace_mode: str,
        workspace_ref: str,
        base_sha: str,
        requirement_graph: dict[str, object],
        requirement_artifact_refs: dict[str, str],
        planning_policy: PlanningPathPolicy,
        prompt_directive: LanguagePromptDirective | None,
        verification_policy: VerificationStagePolicy,
        vertical_design_graph: dict[str, object],
        vertical_trace_graph: dict[str, object],
        repo_landscape: dict[str, object],
        component_plan_inputs: dict[str, object],
    ) -> ImplementationPlanDraft:
        skeleton = build_component_work_units(
            design_graph=vertical_design_graph,
            trace_graph=vertical_trace_graph,
            repo_landscape=repo_landscape,
            planning_policy=planning_policy,
        )
        legacy_mappings = _design_mappings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
            target_repo_path=target_repo_path,
            requirement_design_trace={},
            planning_policy=planning_policy,
        )
        legacy_mappings = rebind_legacy_mappings_to_work_units(
            units=skeleton,
            design_graph=vertical_design_graph,
            legacy_mappings=legacy_mappings,
            planning_policy=planning_policy,
        )
        signature_targets = _implementation_signature_targets(
            mappings=legacy_mappings,
            class_graph=class_graph,
        )
        slices, fallback_reason = _scenario_plan_slices(skeleton)
        sliced = bool(slices) and not fallback_reason
        if not sliced:
            slices = [("", skeleton)]

        units = skeleton
        completed_unit_ids: set[str] = set()
        summaries: list[str] = []
        repo_evidence: list[str] = []
        signature_entries: dict[tuple[str, str], dict[str, object]] = {}
        runtime_config_decision: object = None
        for scenario_id, base_slice_units in slices:
            slice_ids = {str(unit.get("unit_id") or "") for unit in base_slice_units}
            current_slice = [
                unit for unit in units if str(unit.get("unit_id") or "") in slice_ids
            ]
            slice_signature_targets = _signature_targets_for_units(
                signature_targets=signature_targets,
                units=current_slice,
                planning_policy=planning_policy,
            )
            llm_payload = self._component_plan_llm(
                run_id=run_id,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                units=current_slice,
                vertical_design_graph=vertical_design_graph,
                vertical_trace_graph=vertical_trace_graph,
                repo_context=repo_context,
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
                signature_targets=slice_signature_targets,
                repair_feedback="",
                previous_review={},
                component_plan_inputs=component_plan_inputs,
                runtime_config_decision=runtime_config_decision,
                scenario_id=scenario_id,
                prior_plan_context=_prior_scenario_plan_context(
                    units=units,
                    current_units=current_slice,
                    completed_unit_ids=completed_unit_ids,
                    signature_entries=list(signature_entries.values()),
                ),
                allowed_unit_ids=slice_ids,
                required_unit_ids=slice_ids,
            )
            units = merge_work_unit_updates(
                units=units,
                updates=llm_payload.get("work_unit_updates"),
                planning_policy=planning_policy,
            )
            units = canonicalize_work_unit_dependencies(units)
            completed_unit_ids.update(slice_ids)
            summary = str(llm_payload.get("summary") or "").strip()
            if summary:
                summaries.append(f"{scenario_id}: {summary}" if scenario_id else summary)
            for item in _string_items(llm_payload.get("repo_evidence")):
                if item not in repo_evidence:
                    repo_evidence.append(item)
            for entry in _dict_items(
                _object_dict(llm_payload.get("implementation_signature_contract")).get("entries")
            ):
                key = (
                    str(entry.get("target_path") or "").strip().replace("\\", "/"),
                    str(entry.get("target_symbol") or "").strip(),
                )
                if key[0]:
                    signature_entries[key] = entry
            if isinstance(component_plan_inputs.get("repo_runtime_config"), dict):
                runtime_config_decision = llm_payload.get("runtime_config_impact")

        llm_payload = {
            "summary": "; ".join(summaries),
            "repo_evidence": repo_evidence,
            "implementation_signature_contract": {
                "entries": list(signature_entries.values())
            },
            "runtime_config_impact": runtime_config_decision,
        }
        units = canonicalize_work_unit_dependencies(units)
        projections = component_plan_projections(
            units=units,
            design_graph=vertical_design_graph,
            requirement_graph=requirement_graph,
            legacy_mappings=legacy_mappings,
            planning_policy=planning_policy,
        )
        projections["requirement_coverage"] = _merge_requirement_coverage(
            projections["requirement_coverage"],
            _requirement_coverage(
                requirement_graph=requirement_graph,
                requirement_design_trace=requirement_design_trace,
                mappings=projections["design_mappings"],
            ),
        )
        signature_contract = build_implementation_signature_contract(
            planning_policy=planning_policy,
            mappings=projections["design_mappings"],
            class_graph=class_graph,
            candidate=_bind_target_signature_patch(
                candidate=llm_payload.get("implementation_signature_contract"),
                signature_targets=signature_targets,
            ),
            bind_to_mapped_symbol=True,
        )
        parse_error = str(llm_payload.get("_developer_llm_parse_error") or "").strip()
        structured: dict[str, object] = {
            "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
            "planning_mode": (
                "llm_component_aware_scenario_sliced_v1"
                if self.llm_client and not parse_error and sliced
                else "llm_component_aware_plan_v1"
                if self.llm_client and not parse_error
                else "deterministic_component_plan_after_llm_parse_error_v1"
                if self.llm_client
                else "deterministic_component_plan_v1"
            ),
            "run_id": run_id,
            "source_ref": source_ref,
            "issue_key": issue_key,
            "target_repository": {
                "local_path": target_repo_path,
                "remote": target_repo_remote,
                "base_branch": target_base_branch,
                "planned_branch": planned_branch,
                "workspace_mode": workspace_mode,
                "workspace_ref": workspace_ref,
                "base_sha": base_sha,
            },
            "planning_profile": planning_policy.metadata(),
            "verification_stage_ids": list(verification_policy.stage_ids),
            "inputs": dict(component_plan_inputs),
            "requirement_contract": {
                "version": "v2",
                "revision": int(component_plan_inputs.get("requirement_revision") or 0),
                "artifact_refs": requirement_artifact_refs,
            },
            "repo_context": _repo_context_summary(repo_context),
            "repo_evidence": _string_items(llm_payload.get("repo_evidence"))
            or _repo_context_evidence_lines(repo_context),
            "work_units": units,
            **projections,
            "implementation_signature_contract": signature_contract,
            **_runtime_config_slot(component_plan_inputs, llm_payload.get("runtime_config_impact")),
            "design_adjustments": [],
            "assumptions": [
                "Phase 3 only plans approved full-stack work; Phase 4 owns source mutation.",
                "Compatibility fields are rendered from work_units for the current Phase 4 executor.",
            ],
            "open_questions": [],
            "developer_llm": {
                "used": bool(self.llm_client),
                "summary": str(llm_payload.get("summary") or "").strip(),
                "parse_error": parse_error,
                "repair_feedback_attached": False,
                "planning_scenarios": [scenario_id for scenario_id, _ in slices if scenario_id],
                "scenario_slicing_fallback_reason": fallback_reason,
            },
            "out_of_scope": [
                "No source/test mutation or command execution in Phase 3.",
                "Per-unit Developer execution remains L5.2.",
            ],
        }
        return ImplementationPlanDraft(
            markdown=_markdown_plan(
                structured=structured,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
            ),
            structured=structured,
        )

    def _repair_component_aware(
        self,
        *,
        previous: dict[str, object],
        review_report: dict[str, object],
        developer_feedback: str,
        iteration: int,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        repo_context: dict[str, object],
        workspace_mode: str,
        workspace_ref: str,
        base_sha: str,
        requirement_graph: dict[str, object],
        requirement_artifact_refs: dict[str, str],
        planning_policy: PlanningPathPolicy,
        prompt_directive: LanguagePromptDirective | None,
        verification_policy: VerificationStagePolicy,
        vertical_design_graph: dict[str, object],
        vertical_trace_graph: dict[str, object],
        repo_landscape: dict[str, object],
        component_plan_inputs: dict[str, object],
    ) -> ImplementationPlanDraft:
        skeleton = build_component_work_units(
            design_graph=vertical_design_graph,
            trace_graph=vertical_trace_graph,
            repo_landscape=repo_landscape,
            planning_policy=planning_policy,
        )
        preserved = merge_work_unit_updates(
            units=skeleton,
            updates=previous.get("work_units"),
            planning_policy=planning_policy,
        )
        preserved = canonicalize_work_unit_dependencies(preserved)
        base_mappings = _design_mappings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
            target_repo_path=target_repo_path,
            requirement_design_trace={},
            planning_policy=planning_policy,
        )
        legacy_mappings = rebind_legacy_mappings_to_work_units(
            units=preserved,
            design_graph=vertical_design_graph,
            legacy_mappings=base_mappings,
            planning_policy=planning_policy,
        )
        signature_targets = _implementation_signature_targets(
            mappings=legacy_mappings,
            class_graph=class_graph,
        )
        llm_payload = self._component_plan_llm(
            run_id=run_id,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            units=preserved,
            vertical_design_graph=vertical_design_graph,
            vertical_trace_graph=vertical_trace_graph,
            repo_context=repo_context,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
            signature_targets=signature_targets,
            repair_feedback=developer_feedback,
            # The review summary also describes warnings; repair sees the blocking issues only.
            previous_review={
                key: value for key, value in _compact_review(review_report).items() if key != "summary"
            },
            component_plan_inputs=component_plan_inputs,
            runtime_config_decision=previous.get("runtime_config_impact"),
        )
        affected_unit_ids = set(_affected_unit_ids(review_report))
        proposed_updates = _dict_items(llm_payload.get("work_unit_updates"))
        if affected_unit_ids:
            proposed_updates = [
                item
                for item in proposed_updates
                if str(item.get("unit_id") or "") in affected_unit_ids
            ]
        units = merge_work_unit_updates(
            units=preserved,
            updates=proposed_updates,
            planning_policy=planning_policy,
        )
        units = canonicalize_work_unit_dependencies(units)
        affected_paths = {
            str(target.get("path") or "")
            for unit in units
            if str(unit.get("unit_id") or "") in affected_unit_ids
            for target in _dict_items(unit.get("planned_code_targets"))
        }
        if any(_dict_items(item.get("remove_planned_code_targets")) for item in proposed_updates):
            # Mappings bound before the patch may still point at a target the patch removed.
            legacy_mappings = rebind_legacy_mappings_to_work_units(
                units=units,
                design_graph=vertical_design_graph,
                legacy_mappings=base_mappings,
                planning_policy=planning_policy,
            )
            signature_targets = _implementation_signature_targets(
                mappings=legacy_mappings,
                class_graph=class_graph,
            )
        projections = component_plan_projections(
            units=units,
            design_graph=vertical_design_graph,
            requirement_graph=requirement_graph,
            legacy_mappings=legacy_mappings,
            planning_policy=planning_policy,
        )
        projections["requirement_coverage"] = _merge_requirement_coverage(
            projections["requirement_coverage"],
            _requirement_coverage(
                requirement_graph=requirement_graph,
                requirement_design_trace=requirement_design_trace,
                mappings=projections["design_mappings"],
            ),
        )
        signature_contract = _merge_implementation_signature_contract(
            planning_policy=planning_policy,
            mappings=projections["design_mappings"],
            class_graph=class_graph,
            previous=previous.get("implementation_signature_contract"),
            patch=_bind_target_signature_patch(
                candidate=_signature_patch_for_repair(
                    llm_payload=llm_payload,
                    developer_feedback=developer_feedback,
                ),
                # A repair re-signs only the targets of the units the review cited.
                signature_targets=[
                    target
                    for target in signature_targets
                    if not affected_unit_ids or str(target.get("target_path") or "") in affected_paths
                ],
            ),
            bind_to_mapped_symbol=True,
        )
        history = _dict_items(previous.get("repair_history"))
        history.append(
            {
                "iteration": iteration,
                "source": "validator_review_plan",
                "affected_unit_ids": sorted(affected_unit_ids),
                "feedback": developer_feedback,
            }
        )
        parse_error = str(llm_payload.get("_developer_llm_parse_error") or "").strip()
        structured = {
            **previous,
            "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
            "planning_mode": "llm_component_plan_repair_v1" if not parse_error else "deterministic_component_repair_after_llm_parse_error_v1",
            "run_id": run_id,
            "source_ref": source_ref,
            "issue_key": issue_key,
            "target_repository": {
                "local_path": target_repo_path,
                "remote": target_repo_remote,
                "base_branch": target_base_branch,
                "planned_branch": planned_branch,
                "workspace_mode": workspace_mode,
                "workspace_ref": workspace_ref,
                "base_sha": base_sha,
            },
            "inputs": dict(component_plan_inputs),
            "requirement_contract": {
                "version": "v2",
                "revision": int(component_plan_inputs.get("requirement_revision") or 0),
                "artifact_refs": requirement_artifact_refs,
            },
            "repo_context": _repo_context_summary(repo_context),
            "repo_evidence": _string_items(llm_payload.get("repo_evidence")) or list(previous.get("repo_evidence", [])),
            "work_units": units,
            **projections,
            "implementation_signature_contract": signature_contract,
            **_runtime_config_slot(
                component_plan_inputs,
                llm_payload.get("runtime_config_impact"),
                previous=previous.get("runtime_config_impact"),
            ),
            "verification_stage_ids": list(verification_policy.stage_ids),
            "repair_iteration": iteration,
            "repair_source": "validator_review_plan",
            "repair_history": history,
            "developer_llm": {
                "used": bool(self.llm_client),
                "summary": str(llm_payload.get("summary") or "").strip(),
                "parse_error": parse_error,
                "repair_feedback_attached": True,
            },
        }
        markdown = _markdown_plan(
            structured=structured,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
        )
        markdown += (
            "\n\n## Repair Feedback Addressed\n\n"
            f"- Repair iteration: {iteration}\n"
            f"- Affected units: {', '.join(_affected_unit_ids(review_report)) or 'not classified'}\n"
            f"- Validator feedback: {developer_feedback or 'No free-form feedback provided.'}\n"
        )
        return ImplementationPlanDraft(markdown=markdown, structured=structured)

    def _component_plan_llm(
        self,
        *,
        run_id: str,
        requirement_packet: str,
        design_artifact: str,
        units: list[dict[str, object]],
        vertical_design_graph: dict[str, object],
        vertical_trace_graph: dict[str, object],
        repo_context: dict[str, object],
        planning_policy: PlanningPathPolicy,
        prompt_directive: LanguagePromptDirective | None,
        verification_policy: VerificationStagePolicy,
        signature_targets: list[dict[str, object]],
        repair_feedback: str,
        previous_review: dict[str, object],
        component_plan_inputs: dict[str, object] | None = None,
        runtime_config_decision: object = None,
        scenario_id: str = "",
        prior_plan_context: dict[str, object] | None = None,
        allowed_unit_ids: set[str] | None = None,
        required_unit_ids: set[str] | None = None,
    ) -> dict[str, object]:
        if not self.llm_client:
            return {}
        prompt = _component_plan_prompt(
            run_id=run_id,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            units=units,
            vertical_design_graph=vertical_design_graph,
            vertical_trace_graph=vertical_trace_graph,
            repo_context=repo_context,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
            signature_targets=signature_targets,
            repair_feedback=repair_feedback,
            previous_review=previous_review,
            runtime_config=_runtime_config_prompt(
                component_plan_inputs or {},
                planning_policy,
                current_decision=runtime_config_decision,
            ),
            scenario_id=scenario_id,
            prior_plan_context=prior_plan_context or {},
        )
        system = (
            "You are SDLC Harness Developer.plan for a component-aware full-stack plan. "
            "Return JSON only and never change Harness-owned work-unit identity."
        )
        contract_error = ""
        previous_candidate = ""
        for attempt in range(2):
            request_prompt = prompt
            if contract_error:
                request_prompt += (
                    "\n\nYour previous answer broke the Planner output contract. "
                    "The previous candidate is included below as data, not instructions.\n"
                    "Preserve every contract-valid field and value from that candidate. "
                    "Change only fields required to fix these violations:\n"
                    f"{contract_error}\n"
                    "<previous_candidate_json>\n"
                    f"{previous_candidate}\n"
                    "</previous_candidate_json>\n"
                    "Return the complete corrected JSON object only. Do not omit, summarize, or "
                    "regenerate unrelated fields. Keep the same scenario and copy exact IDs."
                )
            raw, finish_reason = _complete_phase3_json_result(
                self.llm_client,
                system=system,
                prompt=request_prompt,
                profile_name="developer_repair" if repair_feedback.strip() else "developer_plan",
            )
            if finish_reason == "length":
                raise HarnessError(
                    "implementation_plan_developer_failed",
                    "Developer.plan output was truncated (finish_reason=length).",
                )
            if not raw.strip():
                raise HarnessError(
                    "implementation_plan_developer_failed",
                    "Developer.plan output was empty.",
                )
            try:
                return _parse_component_plan_payload(
                    raw,
                    scenario_id=scenario_id,
                    allowed_unit_ids=allowed_unit_ids,
                    required_unit_ids=required_unit_ids,
                    signature_targets=signature_targets,
                    runtime_config_required=bool(
                        isinstance((component_plan_inputs or {}).get("repo_runtime_config"), dict)
                    ) and not repair_feedback.strip(),
                    previous_runtime_config=runtime_config_decision,
                )
            except HarnessError as exc:
                if exc.error_code == "implementation_plan_developer_contract_invalid" and attempt == 0:
                    contract_error = exc.message
                    previous_candidate = raw
                    continue
                raise
        raise AssertionError("unreachable")


_RUNTIME_CONFIG_RULES = """Runtime configuration rules:
- Return the full current `runtime_config_impact`: `required` with typed `changes`, or
  `not_required` with a reason. During scenario construction it covers the accumulated plan
  through this scenario, not future units that this call does not own; repair covers the full
  current plan. Design never lists configuration; decide it from planned code and repository facts.
- A change is needed when planned code reads a value that differs per environment (for
  example the base URL of an external system), imports a package not listed in the importing
  workspace's manifest, or needs a service the product itself runs that compose lacks. This
  includes sibling workspace packages: use their repository `version` in an `npm_package`
  change for the consuming manifest. Reuse a dependency only when that consuming manifest
  already lists it. An external system is reached through its configured URL; never add a
  compose service or stub for it.
- Before deciding the status, perform dependency closure for every planned call, adapter or
  injected dependency whose provider target is in another workspace. Identify the source
  target that owns production wiring, its consuming manifest, the provider workspace package,
  and whether that manifest declares the package. Optional or defaulted injection still needs
  the provider package wherever its production default is wired; package existence elsewhere
  in the monorepo is not a declaration in the consuming manifest. A `not_required` reason must
  name every cross-workspace package check and its consuming manifest; a generic claim that all
  imports already exist is insufficient.
- Each change names its owning `unit_id` and only the fields listed for its kind. `read_by`
  and `used_by` must be planned source targets of that unit. Defaults are non-secret and
  keep the product and its tests runnable without the variable set.
- Never edit locked configuration; Harness materializes manifests, lockfiles and compose.
- In a later scenario or repair, `current_decision` is the plan's present decision. Return
  the full updated decision and preserve changes owned by earlier scenarios exactly; only
  the current scenario may add or update its own changes.
"""


def _runtime_config_prompt(
    component_plan_inputs: dict[str, object],
    planning_policy: PlanningPathPolicy,
    current_decision: object = None,
) -> dict[str, object]:
    facts = component_plan_inputs.get("repo_runtime_config")
    policy = runtime_config_policy(planning_policy.project_profile_id)
    if not isinstance(facts, dict) or policy is None:
        return {}
    context: dict[str, object] = {"repository": facts, "policy": runtime_config_prompt_context(policy)}
    if isinstance(current_decision, dict):
        context["current_decision"] = canonical_runtime_config_impact(current_decision)
    return context


def _runtime_config_slot(
    component_plan_inputs: dict[str, object],
    proposed: object,
    *,
    previous: object = None,
) -> dict[str, object]:
    """Harness-created decision slot; a repair without a new decision keeps the previous one."""

    if not isinstance(component_plan_inputs.get("repo_runtime_config"), dict):
        return {}
    decision = canonical_runtime_config_impact(proposed)
    if not decision["status"] and previous is not None:
        decision = canonical_runtime_config_impact(previous)
    return {"runtime_config_impact": decision}


def _scenario_plan_slices(
    units: list[dict[str, object]],
) -> tuple[list[tuple[str, list[dict[str, object]]]], str]:
    """Group a topological plan by scenario when every cross-scenario edge stays forward."""

    ordered = sorted(units, key=lambda item: int(item.get("order") or 0))
    scenario_order: list[str] = []
    grouped: dict[str, list[dict[str, object]]] = {}
    for unit in ordered:
        scenario_id = str(unit.get("scenario_id") or "").strip()
        if not scenario_id:
            return [], "work unit has no scenario_id"
        if scenario_id not in grouped:
            grouped[scenario_id] = []
            scenario_order.append(scenario_id)
        grouped[scenario_id].append(unit)
    rank = {scenario_id: index for index, scenario_id in enumerate(scenario_order)}
    scenario_by_unit = {
        str(unit.get("unit_id") or ""): str(unit.get("scenario_id") or "")
        for unit in ordered
    }
    for unit in ordered:
        current = str(unit.get("scenario_id") or "")
        for dependency in _string_items(unit.get("depends_on")):
            dependency_scenario = scenario_by_unit.get(dependency, current)
            if dependency_scenario != current and rank[dependency_scenario] >= rank[current]:
                return [], (
                    f"cross-scenario dependency {dependency}->{unit.get('unit_id')} "
                    "requires interleaved planning"
                )
    return [(scenario_id, grouped[scenario_id]) for scenario_id in scenario_order], ""


def _signature_targets_for_units(
    *,
    signature_targets: list[dict[str, object]],
    units: list[dict[str, object]],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    allowed_paths = {
        str(target.get("path") or "")
        for unit in units
        for target in _dict_items(unit.get("planned_code_targets"))
    }
    for unit in units:
        allowed_paths.update(_string_items(unit_write_scope(unit, planning_policy).get("files")))
    return [
        target
        for target in signature_targets
        if str(target.get("target_path") or "") in allowed_paths
    ]


def _prior_scenario_plan_context(
    *,
    units: list[dict[str, object]],
    current_units: list[dict[str, object]],
    completed_unit_ids: set[str],
    signature_entries: list[dict[str, object]],
) -> dict[str, object]:
    if not completed_unit_ids:
        return {}
    current_dependencies = {
        dependency
        for unit in current_units
        for dependency in _string_items(unit.get("depends_on"))
    }
    current_entities = {
        str(entity.get("entity") or "")
        for unit in current_units
        for entity in _dict_items(unit.get("code_entities"))
        if str(entity.get("entity") or "")
    }
    current_paths = {
        str(target.get("path") or "")
        for unit in current_units
        for target in _dict_items(unit.get("planned_code_targets"))
        if str(target.get("path") or "")
    }
    relevant: list[dict[str, object]] = []
    relevant_paths: set[str] = set()
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        if unit_id not in completed_unit_ids:
            continue
        unit_entities = {
            str(entity.get("entity") or "")
            for entity in _dict_items(unit.get("code_entities"))
            if str(entity.get("entity") or "")
        }
        unit_paths = {
            str(target.get("path") or "")
            for target in _dict_items(unit.get("planned_code_targets"))
            if str(target.get("path") or "")
        }
        if (
            unit_id not in current_dependencies
            and not (unit_entities & current_entities)
            and not (unit_paths & current_paths)
        ):
            continue
        relevant_paths.update(unit_paths)
        verification_targets: list[dict[str, object]] = []
        seen_verification_targets: set[tuple[str, tuple[str, ...]]] = set()
        for intent in [
            *_dict_items(unit.get("test_intents")),
            *_dict_items(unit.get("qa_test_intents")),
        ]:
            stage_id = str(intent.get("stage_id") or "").strip()
            target_paths = tuple(_string_items(intent.get("target_paths")))
            key = (stage_id, target_paths)
            if not stage_id or not target_paths or key in seen_verification_targets:
                continue
            seen_verification_targets.add(key)
            verification_targets.append(
                {"stage_id": stage_id, "target_paths": list(target_paths)}
            )
        relevant.append(
            {
                key: unit.get(key)
                for key in (
                    "unit_id",
                    "scenario_id",
                    "component_id",
                    "objective",
                    "planned_code_targets",
                    "migration_impact",
                )
                if key in unit
            }
            | ({"verification_targets": verification_targets} if verification_targets else {})
        )
    signatures = [
        entry
        for entry in signature_entries
        if str(entry.get("target_path") or "") in relevant_paths | current_paths
    ]
    return {
        "completed_units": relevant,
        "implementation_signature_entries": signatures,
    }


def _units_for_prompt(
    units: list[dict[str, object]],
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    """Drop contract copies the prompt already carries; add each unit's write scope."""

    result = []
    for unit in units:
        item = {key: value for key, value in unit.items() if key not in {"surface_contracts", "boundary_contracts"}}
        if _dict_items(unit.get("test_intents")):
            # Ids let a repair drop one test without resending the others.
            item["test_intents"] = [
                {"id": test_intent_id(test), **test} for test in _dict_items(unit.get("test_intents"))
            ]
        if _dict_items(unit.get("qa_test_intents")):
            item["qa_test_intents"] = [
                {"id": test_intent_id(test), **test}
                for test in _dict_items(unit.get("qa_test_intents"))
            ]
        scope = unit_write_scope(unit, planning_policy) if planning_policy else {}
        if scope:
            item["write_scope"] = scope
        result.append(item)
    return result


def _complete_phase3_json(
    llm_client: Any,
    *,
    system: str,
    prompt: str,
    profile_name: str,
) -> str:
    content, _ = _complete_phase3_json_result(
        llm_client,
        system=system,
        prompt=prompt,
        profile_name=profile_name,
    )
    return content


def _complete_phase3_json_result(
    llm_client: Any,
    *,
    system: str,
    prompt: str,
    profile_name: str,
) -> tuple[str, str]:
    complete_with_metadata = getattr(llm_client, "complete_with_metadata", None)
    if callable(complete_with_metadata):
        settings = getattr(llm_client, "settings", None)
        fallback_max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
        completion = complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=True,
            **phase3_llm_call_options(
                settings,
                profile_name,
                fallback_max_tokens=fallback_max_tokens,
            ),
        )
        return str(completion.content), str(getattr(completion, "finish_reason", "") or "")
    return str(llm_client.complete(system=system, prompt=prompt)), ""


def _component_plan_prompt(
    *,
    run_id: str,
    requirement_packet: str,
    design_artifact: str,
    units: list[dict[str, object]],
    vertical_design_graph: dict[str, object],
    vertical_trace_graph: dict[str, object],
    repo_context: dict[str, object],
    planning_policy: PlanningPathPolicy,
    prompt_directive: LanguagePromptDirective | None,
    verification_policy: VerificationStagePolicy,
    signature_targets: list[dict[str, object]],
    repair_feedback: str,
    previous_review: dict[str, object],
    runtime_config: dict[str, object] | None = None,
    scenario_id: str = "",
    prior_plan_context: dict[str, object] | None = None,
) -> str:
    node_ids = {
        node_id for unit in units for node_id in _string_items(unit.get("design_node_ids"))
    }
    shared_node_ids = {
        design_node_id
        for unit in units
        for entity in _dict_items(unit.get("code_entities"))
        for design_node_id in _string_items(entity.get("design_node_ids"))
    } - node_ids
    design_nodes = {
        str(item.get("id") or ""): item
        for item in _dict_items(vertical_design_graph.get("nodes"))
    }
    shared_code_entity_contracts = [
        {
            key: node.get(key)
            for key in (
                "id",
                "scenario_id",
                "component_id",
                "kind",
                "surface",
                "contract",
                "surface_contract",
            )
            if key in node
        }
        for node_id in sorted(shared_node_ids)
        for node in [_object_dict(design_nodes.get(node_id))]
        if node
    ]
    design_slice = {
        "schema_version": vertical_design_graph.get("schema_version"),
        "artifact_digest": vertical_design_graph.get("artifact_digest"),
        "nodes": [
            item for item in _dict_items(vertical_design_graph.get("nodes"))
            if str(item.get("id") or "") in node_ids
        ],
        "edges": [
            item for item in _dict_items(vertical_design_graph.get("edges"))
            if str(item.get("from") or "") in node_ids or str(item.get("to") or "") in node_ids
        ],
    }
    trace_node_ids = node_ids | {
        ref
        for unit in units
        for ref in [*_string_items(unit.get("requirement_refs")), *_string_items(unit.get("acceptance_refs"))]
    }
    trace_slice = {
        "schema_version": vertical_trace_graph.get("schema_version"),
        "artifact_digest": vertical_trace_graph.get("artifact_digest"),
        "nodes": [
            item for item in _dict_items(vertical_trace_graph.get("nodes"))
            if str(item.get("id") or "") in trace_node_ids
        ],
        "edges": [
            item for item in _dict_items(vertical_trace_graph.get("edges"))
            if str(item.get("from") or "") in trace_node_ids
            and str(item.get("to") or "") in trace_node_ids
        ],
    }
    language = prompt_directive.developer_plan_context() if prompt_directive else {}
    runtime_config_section = (
        "Runtime configuration (Harness-extracted repository facts and policy; read-only):\n"
        + json.dumps(runtime_config, ensure_ascii=False, indent=2)
        + "\n"
        + _RUNTIME_CONFIG_RULES
        if runtime_config
        else ""
    )
    repair = (
        "\nRepair feedback:\n"
        + repair_feedback
        + "\nPrevious review:\n"
        + json.dumps(previous_review, ensure_ascii=False, indent=2)
        if repair_feedback.strip()
        else ""
    )
    repair_rules = (
        """
- This is a repair patch. Return only the units named by the review, and in them only what changes:
  - `planned_code_targets`, `test_intents`, and `qa_test_intents` add to what the unit already
    has; never resend an existing target or test. Drop an existing test by listing its `id` in
    `remove_test_intents` or `remove_qa_test_intents`, and drop a target the review calls unapproved
    or unnecessary by listing its exact path and symbol in `remove_planned_code_targets`. Targets
    seeded from runtime_bindings cannot be removed.
  - `unverified_obligations` entries add to or replace the unit's entries with the same id.
  - Send `objective` only when the review requires it to change; keep its unchanged sentences
    word for word and add the new ones.
""".rstrip()
        if repair_feedback.strip()
        else ""
    )
    browser_qa_active = (
        "browser_e2e" in verification_policy.stage_ids
        and verification_policy.owner_for_stage("browser_e2e") == QA_INTENT_STAGE_OWNER
    )
    browser_qa_rule = (
        "Browser QA is active. Put every UI obligation in at least one `qa_test_intents` "
        "entry and do not mark UI obligations unverified. For each `runtime_input` obligation "
        "whose approved Design source is `external_use_case`, keep the production dependency "
        "external. Every QA intent must declare `data_setup`: use mode `repository` only when "
        "repository evidence names an existing seed or fixture setup, mode `synthetic` when no "
        "such source exists, and `not_required` only when the flow needs no data setup. For a "
        "synthetic setup, list the exact input values and states QA must create deterministically "
        "inside the browser spec. This authorizes test data only: do not add or alter a production "
        "provider, endpoint, entrypoint, global, history state, runtime config, or Design seam to "
        "deliver it. The QA agent will bind the fixture to the public boundary in the implemented "
        "code after Developer execution."
        if browser_qa_active
        else "Browser QA is inactive. Record UI behavior that active stages cannot observe in "
        "`unverified_obligations`."
    )
    scenario_contract = (
        f"This call plans exactly scenario `{scenario_id}`. Return top-level "
        f"`scenario_id: \"{scenario_id}\"` and exactly one update for every listed work unit. "
        "Do not return a work unit from another scenario."
        if scenario_id
        else "This call plans the complete supplied work-unit set."
    )
    scenario_field = f'  "scenario_id": "{scenario_id}",\n' if scenario_id else ""
    return f"""Component-aware Implementation Plan V5

Enrich the Harness-owned work units. Do not add, delete, merge, split or reorder units.
{scenario_contract}
Return JSON only:
{{
{scenario_field}  "summary": "short human summary",
  "repo_evidence": ["bounded evidence"],
  "work_unit_updates": [
    {{
      "unit_id": "one existing WU id",
      "title": "short title",
      "objective": "concrete implementation objective",
      "planned_code_targets": [
        {{"path": "profile-approved repo-relative path", "symbol": "optional qualified symbol"}}
      ],
      "remove_planned_code_targets": [
        {{"path": "exact existing target path", "symbol": "exact existing target symbol"}}
      ],
      "migration_impact": {{
        "status": "required|not_required",
        "reason": "decision grounded in the approved data contract",
        "targets": ["packages/db/prisma/... when required; otherwise an empty array"]
      }},
      "test_intents": [
        {{
          "stage_id": "one stage owned by developer_intent",
          "intent": "verification intent",
          "acceptance_refs": ["AC-..."],
          "target_paths": ["exact profile-approved test path"],
          "obligation_refs": ["ui_obligations ids this test observes"]
        }}
      ],
      "qa_test_intents": [
        {{
          "stage_id": "browser_e2e",
          "intent": "browser-observable acceptance intent for the independent QA agent",
          "acceptance_refs": ["AC-..."],
          "target_paths": ["tests/e2e/<scenario>.spec.ts"],
          "obligation_refs": ["ui_obligations ids observed in the real browser"],
          "data_setup": {{
            "mode": "repository|synthetic|not_required",
            "reason": "why this source mode applies",
            "requirements": ["exact deterministic input or initial state QA needs"]
          }}
        }}
      ],
      "unverified_obligations": [
        {{"id": "ui_obligations id no active stage fully observes", "reason": "what stays unobserved and why"}}
      ],
      "remove_test_intents": ["id of an existing test intent to drop"],
      "remove_qa_test_intents": ["id of an existing QA intent to drop"],
      "repo_evidence_refs": ["evidence refs used for this unit"]
    }}
  ],
  "runtime_config_impact": {{
    "status": "required|not_required",
    "reason": "why runtime configuration is or is not needed; include every cross-workspace package check",
    "changes": [
      {{"kind": "env_var", "unit_id": "existing WU id", "name": "UPPER_SNAKE", "default": "non-secret default", "read_by": "planned source path", "purpose": "why"}},
      {{"kind": "npm_package", "unit_id": "existing WU id", "manifest": "consuming workspace package.json", "package": "npm or sibling workspace package", "version": "exact repository version", "dev": false, "used_by": "planned source path in that workspace", "purpose": "why"}}
    ]
  }},
  "implementation_signature_contract": {{
    "entries": [
      {{
        "target_path": "Harness-owned source path",
        "target_symbol": "Harness-owned function/component or Owner.method",
        "parameters": [
          {{"name": "value", "type": "string", "optional": false, "rest": false}}
        ],
        "return_type": "Promise<OperationResult>",
        "type_parameters": [],
        "async": true
      }}
    ]
  }}
}}

Rules:
- Update every work unit that lacks an exact target or test intent.
- Never return command, argv, cwd, env or timeout fields.
- Use only paths allowed by the immutable planning policy.
- Keep every acceptance ref covered by at least one `test_intents` or `qa_test_intents`
  entry in its owning unit.
- `qa_test_intents` belong to the independent QA agent and never expand Developer write scope.
  Use only the `browser_e2e` stage and exact `tests/e2e/*.spec.ts` paths.
- `data_setup` belongs only to QA. It describes required test data, not how production obtains
  runtime inputs. Synthetic data is created in the QA spec after code exists and must never add
  a production code target, provider, API, global, history-state contract, or runtime setting.
- Stage ownership is authoritative: `test_intents` may use only `developer_intent` stages,
  `qa_test_intents` may use only `qa_intent` stages, and `harness_global` stages must never
  appear in either list because the Harness runs them for the whole implementation.
- {browser_qa_rule}
- A `unit` test intent must target an exact test file. A `typecheck` intent must
  target an exact non-test source file already listed in that work unit's
  planned_code_targets.
- Add explicit test intent for every negative, exception, cancellation, duplicate,
  interruption, and confirmation gate stated by the owning unit's approved contract.
- Data units must decide migration impact. Non-data units must omit `migration_impact`.
- When `migration_impact` is present, `targets` is always an array: use `[]` for
  `not_required` and one or more exact migration paths for `required`.
- When migration is `required`, include a concrete
  `packages/db/prisma/migrations/<id>_<name>/migration.sql` target. Include
  `packages/db/prisma/schema.prisma` in planned_code_targets when the model changes.
- When another data unit already owns the same schema migration, mark a read-only
  consumer `not_required` and explain that it uses the approved shared schema.
- Do not invent Requirement, Design, component, dependency or work-unit IDs.
- Treat the `surface_contract` of every Design node in a unit's design_node_ids and the
  `boundary_contract` of every edge in its design_edge_ids (both in the Vertical Design slice;
  the work units below omit these copies) as immutable approved behavior. For UI work,
  `ui_obligations` lists the contract items each unit must account for.
- Map data source, authentication and runtime attachment to exact code targets. The objective
  says what the unit builds and through which seams; it does not restate the contract, which
  the Vertical Design slice already carries.
- CSS class names without owned style rules do not cover responsive behavior, and static
  markup does not cover interaction or focus; record such items in `unverified_obligations`.
- For every boundary, identify the owner of required auth and input mappings plus the caller
  that supplies them. A completed dependency ID without its component/surface/artifacts is
  not sufficient execution evidence.
- Do not propose a new work unit. Planned targets must stay inside Harness-owned runtime
  wiring, except approved shared-contract files, data schema/migration artifacts and UI
  stylesheets. A unit's `write_scope` lists exactly the files it may target: put new
  helpers, clients and adapters as exported symbols inside those files, never in a new file.
- A UI unit may own stylesheet targets under the planning policy's `ui_stylesheets` root.
  Choose the style mechanism from repository evidence; when the contract needs responsive,
  focus-appearance or visual-token rules and no stylesheet exists, plan one stylesheet next to
  the screen component in the first unit that owns that component (code_entities order) and
  import it there. Later units extend the same file.
- Bind every test intent to a verification stage whose stated capability can observe the
  behavior it claims. Put decisions the contract fixes (state transitions, focus targets,
  announcement text, which controls are blocked) in exported pure functions inside the
  planned targets so the `unit` stage can test them. When behavior needs a DOM or browser that
  no active stage provides, say in the objective that it stays unverified until such a stage
  exists; never claim a test proves it.
- When Web and API cross a boundary and the profile has a contracts component, add an
  exact `packages/contracts/src/...` target to the nearest API work unit. Include the
  shared-contract barrel only when repository evidence requires its export.
- Do not add database, API, or Web barrel/index targets. Harness assigns repo-profile
  package entrypoints to one provider work unit after this enrichment step; leave an
  entrypoint Harness already placed in a unit as it is.
- Do not change dependency or order in prose. Harness serializes units that share code,
  test, contract, schema, or migration artifacts.
- `code_entities` lists, in implementation order, every unit whose Design nodes bind to the
  same code symbol. A later unit extends the code an earlier unit creates: its objective and
  tests add only its own scenario's behavior and must stay compatible with the contracts of
  the other listed design_node_ids, which appear in Shared code-entity contracts. When the
  extension changes how earlier code is called (a new parameter, dependency or response),
  keep the earlier call working (optional parameter, injected dependency with a default) or
  add a test intent here that updates the earlier unit's test file, named by exact path.
- A runtime binding with `attachment_wired_by` is already attached to its owner by that earlier
  unit. Target the owner file when this unit adds something the owner must create, inject or
  pass on (for example a client with a network default that tests replace); otherwise leave
  the owner file untouched.
- `ui_obligations` lists, with stable ids, the states, validations, transitions, feedback,
  accessibility, focus and responsive items of the unit's UI contracts. Account for every id:
  list it in `obligation_refs` of a test intent whose stage can observe it, or in
  `unverified_obligations` with the reason, or in both when a test observes part of it and the
  rest needs a stage the profile does not activate. Roles, aria attributes, accessible names and
  state-dependent markup are observable by static rendering; focus movement, keyboard
  interaction and viewport layout need a stage the profile may not activate. The rule review
  rejects a unit that leaves an id unaccounted for.
{repair_rules}
- Emit one signature entry for every Harness-owned signature target below; in a repair, only
  for targets of the units you update.
- Copy `target_path` and `target_symbol` exactly. Harness owns Design-to-code binding;
  never derive a class/method owner from the legacy Class graph and never invent a target.
- Multiple legacy Design operations may trace to one function/component target. Describe
  the callable signature of that real target once, not one fake method per Design operation.
- Derive parameters and return type from Vertical Design contracts, Requirement behavior,
  repository types, and the target's actual TypeScript role. A React function component
  returns a renderable value; an exported function uses its simple target symbol.
- Never emit design_element_id, symbol_kind, owner_kind, qualified_name, source_path or
  contract_id. Harness binds those identity fields deterministically.

Run id: {run_id}
Planning policy:
{json.dumps(planning_policy.prompt_context(), ensure_ascii=False, indent=2)}
Verification policy:
{json.dumps(verification_policy.prompt_context(), ensure_ascii=False, indent=2)}
{runtime_config_section}Language directive:
{json.dumps(language, ensure_ascii=False, indent=2)}
Harness-owned signature targets:
{json.dumps(
    [
        {
            "target_path": item.get("target_path"),
            "target_symbol": item.get("target_symbol"),
        }
        for item in signature_targets
    ],
    ensure_ascii=False,
    indent=2,
)}
Harness-owned work units:
{json.dumps(_units_for_prompt(units, planning_policy), ensure_ascii=False, indent=2)}
Prior scenario plan context (read-only; preserve compatible shared decisions):
{json.dumps(prior_plan_context or {}, ensure_ascii=False, separators=(",", ":"))}
Shared code-entity contracts (read-only compatibility context, not units to plan):
{json.dumps(shared_code_entity_contracts, ensure_ascii=False, separators=(",", ":"))}
Vertical Design slice:
{json.dumps(design_slice, ensure_ascii=False, separators=(",", ":"))}
Vertical Trace slice:
{json.dumps(trace_slice, ensure_ascii=False, separators=(",", ":"))}

Repository context:
{json.dumps(_repo_context_summary(repo_context), ensure_ascii=False, indent=2)}
Repository evidence:
{_excerpt(str(repo_context.get("evidence") or ""), limit=6000)}
{repair}
""".strip()


def _developer_plan_prompt(
    *,
    run_id: str,
    source_ref: str,
    issue_key: str,
    requirement_packet: str,
    design_artifact: str,
    requirement_design_trace: dict[str, object],
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    repo_context: dict[str, object],
    base_mappings: list[dict[str, object]],
    base_test_intents: list[dict[str, object]],
    target_repo_path: str,
    target_repo_remote: str,
    target_base_branch: str,
    planned_branch: str,
    target_files: list[str],
    test_command: str,
    repair_feedback: str,
    previous_review: dict[str, object],
    canonical_requirement: RequirementModelV2 | None,
    planning_policy: PlanningPathPolicy,
    prompt_directive: LanguagePromptDirective | None,
    verification_policy: VerificationStagePolicy,
) -> str:
    canonical_section = (
        "Canonical Requirement V2 (source of truth; use stable IDs exactly):\n"
        + json.dumps(canonical_requirement.to_dict(), ensure_ascii=False, indent=2)
        if canonical_requirement is not None
        else "Canonical Requirement V2: not enabled for this V1 run."
    )
    repair_mode = bool(repair_feedback.strip())
    requirement_text = _excerpt(requirement_packet, limit=3000 if repair_mode else 6000)
    design_text = _excerpt(design_artifact, limit=3000 if repair_mode else 6000)
    trace_payload = (
        {
            key: requirement_design_trace.get(key, [])
            for key in ("version", "class_trace", "sequence_trace")
        }
        if repair_mode
        else requirement_design_trace
    )
    class_payload = _compact_class_graph(class_graph) if repair_mode else class_graph
    sequence_payload = _compact_sequence_graph(sequence_graph) if repair_mode else sequence_graph
    mapping_payload = _compact_mappings(base_mappings) if repair_mode else base_mappings
    if planning_policy:
        if prompt_directive is None:
            prompt_directive = resolve_language_prompt_directive(
                get_language_profile(planning_policy.language_profile_id)
            )
        if prompt_directive.language_profile_id != planning_policy.language_profile_id:
            raise HarnessError(
                "prompt_directive_mismatch",
                "Phase 3 planning requires a prompt directive matching the planning language profile.",
            )
        example_rule = planning_policy.component_rules[0]
        source_example = f"{example_rule.source_root}/Example{example_rule.source_suffix}"
        test_example = f"{example_rule.source_root}/Example{example_rule.test_suffix}"
        path_rules = (
            "Use only paths accepted by the immutable planning policy below. "
            "Do not choose or change a component for an existing deterministic mapping."
        )
        planning_context = {
            **planning_policy.prompt_context(),
            "language_prompt": prompt_directive.developer_plan_context(),
        }
    else:  # Defensive only; public planning entry points reject missing policy first.
        raise HarnessError(
            "planning_profile_policy_invalid",
            "Developer planning requires an immutable planning policy.",
        )
    if verification_policy:
        if test_command.strip():
            raise HarnessError(
                "verification_stage_policy_mismatch",
                "Profiled Phase 3 planning cannot receive a raw test command.",
            )
        if planning_policy and verification_policy.project_profile_id != planning_policy.project_profile_id:
            raise HarnessError(
                "verification_stage_policy_mismatch",
                "Verification stages do not belong to the frozen planning project profile.",
            )
        stage_example = "unit" if "unit" in verification_policy.stage_ids else verification_policy.stage_ids[0]
        test_intent_schema = json.dumps(
            [
                {
                    "scope": "unit_or_service",
                    "intent": "specific behavior to verify",
                    "stage_id": stage_example,
                }
            ],
            ensure_ascii=False,
        )
        verification_rules = (
            "Every test_intents item must reference exactly one allowed stage_id. "
            "Never emit command, shell, argv, cwd, env, or timeout fields."
        )
        verification_context = json.dumps(
            verification_policy.prompt_context(),
            ensure_ascii=False,
            indent=2,
        )
        verification_run_context = ""
        signature_contract_field = (
            '  "implementation_signature_contract": '
            + json.dumps(
                {
                    "version": "implementation_signature_contract_v1",
                    "entries": [
                        {
                            "design_element_id": "method:Owner.operation",
                            "symbol_kind": "method",
                            "owner_kind": "class|interface|abstract",
                            "qualified_name": "Owner.operation",
                            "parameters": [
                                {
                                    "name": "value",
                                    "type": "string",
                                    "optional": False,
                                    "rest": False,
                                }
                            ],
                            "return_type": "Promise<void>",
                            "type_parameters": ["T extends string"],
                            "async": True,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + ",\n"
        )
        signature_rules = (
            "For every approved method mapping, provide one or more exact implementation "
            "signature entries. Parameter names are evidence; parameter type/order, "
            "optional/rest, return type, type parameters, and async are approved contract. "
            "Use qualified Owner.method names and do not include source_path or contract_id; "
            "the harness binds those deterministically."
        )
    else:  # Defensive only; public planning entry points reject missing policy first.
        raise HarnessError(
            "planning_profile_policy_invalid",
            "Developer planning requires an immutable verification policy.",
        )
    return f"""Developer Implementation Plan v1

You are creating the Phase 3 implementation plan checkpoint for a TraceDev-style SDLC workflow.
Do not write source code. Do not create commits or branches. Your job is to explain exactly how
the approved requirement/design should be implemented in the target repository in Phase 4.

Return JSON only with this shape:
{{
  "summary": "short summary for human reviewer",
  "repo_evidence": ["specific repo evidence from CodeGraph/context, max 8 items"],
  "implementation_steps": [
    {{
      "order": 1,
      "title": "short step title",
      "description": "what Phase 4 should change",
      "files": ["{source_example}"],
      "design_refs": ["class:...", "method:...", "sequence:..."]
    }}
  ],
  "proposed_files": [
    {{
      "path": "{test_example}",
      "action": "create|update|verify",
      "reason": "why this file is needed",
      "related_design_elements": ["class:..."]
    }}
  ],
  "design_mappings": [
    {{
      "design_element_id": "class:...",
      "design_element_type": "class|method|sequence_message",
      "design_element_name": "...",
      "planned_code_target": "{source_example}#method",
      "reason": "concrete implementation reason",
      "test_intent": "how to verify this element"
    }}
  ],
{signature_contract_field}  "test_intents": {test_intent_schema},
  "assumptions": [],
  "open_questions": []
}}

Rules:
- Keep the whole response compact and below 3000 output tokens.
- Do not repeat all base deterministic mappings. The harness already preserves
  them in Python. In `design_mappings`, return only mappings you improve or
  correct using repo evidence. Return [] when the base mappings are good enough.
- `implementation_steps` must be 3-6 high-level steps, not one step per design element.
- `proposed_files` must include every file named by implementation steps or design mappings.
- Keep proposed files grouped by file, not by method.
- `test_intents` must be max 6 focused tests.
- {verification_rules}
- {signature_rules}
- Do not add behavior outside the approved requirement/design.
- {path_rules}
- Use CodeGraph evidence when naming existing classes/files.
- If this is a repair iteration, address the validator feedback directly.
- A mapping with `implementation_status=waived_by_design_adjustment` is intentionally omitted
  from executable code. Never plan that method in a step, prose, test, or direct symbol target.
- During repair, return concise replacements and keep the response below 3000 tokens. Do not
  repeat unchanged design mappings or repository evidence.

Run context:
- run_id: {run_id}
- source_ref: {source_ref}
- issue_key: {issue_key}
- target_repo_path: {target_repo_path}
- target_repo_remote: {target_repo_remote}
- target_base_branch: {target_base_branch}
- planned_branch: {planned_branch}
{verification_run_context}

Target files:
{json.dumps(target_files, ensure_ascii=False, indent=2)}

Immutable planning path policy:
{json.dumps(planning_context, ensure_ascii=False, indent=2)}

Immutable verification stage policy:
{verification_context}

{canonical_section}

Approved requirement packet:
{requirement_text}

Approved design artifact:
{design_text}

Requirement-design trace:
{json.dumps(trace_payload, ensure_ascii=False, separators=(",", ":"))}

Class graph:
{json.dumps(class_payload, ensure_ascii=False, separators=(",", ":"))}

Sequence graph:
{json.dumps(sequence_payload, ensure_ascii=False, separators=(",", ":"))}

Base deterministic mappings:
{json.dumps(mapping_payload, ensure_ascii=False, separators=(",", ":"))}

Base test intents:
{json.dumps(base_test_intents, ensure_ascii=False, indent=2)}

CodeGraph repo context:
{json.dumps(repo_context, ensure_ascii=False, indent=2)}

Repair feedback:
{repair_feedback or "none"}

Previous review:
{json.dumps(previous_review, ensure_ascii=False, indent=2)}
""".strip()


def _parse_developer_plan_payload(raw: str) -> dict[str, object]:
    try:
        parsed = _parse_first_json_object(
            raw,
            expected_keys={
                "summary",
                "repo_evidence",
                "implementation_steps",
                "proposed_files",
                "design_mappings",
                "test_intents",
            },
        )
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan LLM did not return valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan LLM must return a JSON object.",
        )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "repo_evidence": _string_items(parsed.get("repo_evidence")),
        "implementation_steps": _dict_items(parsed.get("implementation_steps")),
        "proposed_files": _dict_items(parsed.get("proposed_files")),
        "design_mappings": _dict_items(parsed.get("design_mappings")),
        "implementation_signature_contract": _object_dict(
            parsed.get("implementation_signature_contract")
        ),
        "test_intents": _dict_items(parsed.get("test_intents")),
        "assumptions": _string_items(parsed.get("assumptions")),
        "open_questions": _string_items(parsed.get("open_questions")),
    }


def _parse_component_plan_payload(
    raw: str,
    *,
    scenario_id: str = "",
    allowed_unit_ids: set[str] | None = None,
    required_unit_ids: set[str] | None = None,
    signature_targets: list[dict[str, object]] | None = None,
    runtime_config_required: bool = False,
    previous_runtime_config: object = None,
) -> dict[str, object]:
    try:
        parsed = _parse_first_json_object(
            raw,
            expected_keys={"summary", "work_unit_updates"},
        )
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan V5 LLM did not return valid component-plan JSON.",
        ) from exc
    issues = _component_plan_payload_issues(
        parsed,
        scenario_id=scenario_id,
        allowed_unit_ids=allowed_unit_ids,
        required_unit_ids=required_unit_ids,
        signature_targets=signature_targets or [],
        runtime_config_required=runtime_config_required,
        previous_runtime_config=previous_runtime_config,
    )
    if issues:
        raise HarnessError(
            "implementation_plan_developer_contract_invalid",
            "Developer.plan V5 output contract invalid: " + "; ".join(issues[:12]),
        )
    return {
        **({"scenario_id": scenario_id} if scenario_id else {}),
        "summary": str(parsed.get("summary") or "").strip(),
        "repo_evidence": _string_items(parsed.get("repo_evidence")),
        "work_unit_updates": _dict_items(parsed.get("work_unit_updates")),
        "implementation_signature_contract": _object_dict(
            parsed.get("implementation_signature_contract")
        ),
        "runtime_config_impact": _object_dict(parsed.get("runtime_config_impact")),
    }


def _component_plan_payload_issues(
    payload: dict[str, object],
    *,
    scenario_id: str,
    allowed_unit_ids: set[str] | None,
    required_unit_ids: set[str] | None,
    signature_targets: list[dict[str, object]],
    runtime_config_required: bool,
    previous_runtime_config: object,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(payload.get("summary"), str):
        issues.append("$.summary must be a string")
    if "repo_evidence" in payload:
        _require_string_list(payload.get("repo_evidence"), "$.repo_evidence", issues)
    if scenario_id and payload.get("scenario_id") != scenario_id:
        issues.append(f"$.scenario_id must equal `{scenario_id}`")
    updates = payload.get("work_unit_updates")
    if not isinstance(updates, list):
        issues.append("$.work_unit_updates must be an array")
        updates = []
    seen: set[str] = set()
    for index, raw_update in enumerate(updates):
        path = f"$.work_unit_updates[{index}]"
        if not isinstance(raw_update, dict):
            issues.append(f"{path} must be an object")
            continue
        unit_id = raw_update.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id.strip():
            issues.append(f"{path}.unit_id must be a non-empty string")
            continue
        unit_id = unit_id.strip()
        if unit_id in seen:
            issues.append(f"{path}.unit_id duplicates `{unit_id}`")
        seen.add(unit_id)
        if allowed_unit_ids is not None and unit_id not in allowed_unit_ids:
            issues.append(f"{path}.unit_id `{unit_id}` is outside this planning slice")
        for field in ("title", "objective"):
            if field in raw_update and not isinstance(raw_update.get(field), str):
                issues.append(f"{path}.{field} must be a string")
        for field in ("planned_code_targets", "remove_planned_code_targets"):
            _require_target_list(raw_update.get(field), f"{path}.{field}", issues, optional=True)
        for field in ("test_intents", "qa_test_intents"):
            _require_test_intent_list(
                raw_update.get(field),
                f"{path}.{field}",
                issues,
                optional=True,
                require_data_setup=field == "qa_test_intents",
            )
        for field in ("remove_test_intents", "remove_qa_test_intents", "repo_evidence_refs"):
            if field in raw_update:
                _require_string_list(raw_update.get(field), f"{path}.{field}", issues)
        if "unverified_obligations" in raw_update:
            rows = raw_update.get("unverified_obligations")
            if not isinstance(rows, list):
                issues.append(f"{path}.unverified_obligations must be an array")
            else:
                for row_index, row in enumerate(rows):
                    row_path = f"{path}.unverified_obligations[{row_index}]"
                    if not isinstance(row, dict):
                        issues.append(f"{row_path} must be an object")
                        continue
                    for field in ("id", "reason"):
                        if not isinstance(row.get(field), str) or not str(row.get(field)).strip():
                            issues.append(f"{row_path}.{field} must be a non-empty string")
        if "migration_impact" in raw_update:
            migration = raw_update.get("migration_impact")
            if not isinstance(migration, dict):
                issues.append(f"{path}.migration_impact must be an object")
            else:
                for field in ("status", "reason"):
                    if not isinstance(migration.get(field), str):
                        issues.append(f"{path}.migration_impact.{field} must be a string")
                _require_string_list(
                    migration.get("targets"), f"{path}.migration_impact.targets", issues
                )
    if required_unit_ids is not None and seen != required_unit_ids:
        missing = sorted(required_unit_ids - seen)
        extra = sorted(seen - required_unit_ids)
        issues.append(
            "$.work_unit_updates must contain the exact slice unit set "
            f"(missing={missing}, extra={extra})"
        )
    _require_signature_contract(
        payload.get("implementation_signature_contract"),
        signature_targets=signature_targets,
        required=required_unit_ids is not None,
        issues=issues,
    )
    _require_runtime_config(
        payload.get("runtime_config_impact"),
        required=runtime_config_required,
        allowed_unit_ids=allowed_unit_ids,
        previous=previous_runtime_config,
        issues=issues,
    )
    return issues


def _require_string_list(value: object, path: str, issues: list[str]) -> None:
    if not isinstance(value, list):
        issues.append(f"{path} must be an array of strings")
        return
    if any(not isinstance(item, str) for item in value):
        issues.append(f"{path} must contain only strings")


def _require_target_list(
    value: object,
    path: str,
    issues: list[str],
    *,
    optional: bool,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, list):
        issues.append(f"{path} must be an array")
        return
    for index, target in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(target, dict):
            issues.append(f"{item_path} must be an object")
            continue
        if not isinstance(target.get("path"), str) or not str(target.get("path")).strip():
            issues.append(f"{item_path}.path must be a non-empty string")
        if "symbol" in target and not isinstance(target.get("symbol"), str):
            issues.append(f"{item_path}.symbol must be a string")


def _require_test_intent_list(
    value: object,
    path: str,
    issues: list[str],
    *,
    optional: bool,
    require_data_setup: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, list):
        issues.append(f"{path} must be an array")
        return
    for index, intent in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(intent, dict):
            issues.append(f"{item_path} must be an object")
            continue
        for field in ("stage_id", "intent"):
            if not isinstance(intent.get(field), str) or not str(intent.get(field)).strip():
                issues.append(f"{item_path}.{field} must be a non-empty string")
        for field in ("acceptance_refs", "target_paths", "obligation_refs"):
            if field in intent:
                _require_string_list(intent.get(field), f"{item_path}.{field}", issues)
        if "target_paths" not in intent:
            issues.append(f"{item_path}.target_paths must be an array of strings")
        if require_data_setup:
            setup = intent.get("data_setup")
            if not isinstance(setup, dict):
                issues.append(f"{item_path}.data_setup must be an object")
                continue
            mode = setup.get("mode")
            if mode not in {"repository", "synthetic", "not_required"}:
                issues.append(
                    f"{item_path}.data_setup.mode must be repository, synthetic, or not_required"
                )
            if not isinstance(setup.get("reason"), str) or not str(setup.get("reason")).strip():
                issues.append(f"{item_path}.data_setup.reason must be a non-empty string")
            requirements = setup.get("requirements")
            _require_string_list(requirements, f"{item_path}.data_setup.requirements", issues)
            if mode in {"repository", "synthetic"} and (
                not isinstance(requirements, list)
                or not any(isinstance(item, str) and item.strip() for item in requirements)
            ):
                issues.append(
                    f"{item_path}.data_setup.requirements must be non-empty for mode `{mode}`"
                )


def _require_signature_contract(
    value: object,
    *,
    signature_targets: list[dict[str, object]],
    required: bool,
    issues: list[str],
) -> None:
    if value is None:
        if required and signature_targets:
            issues.append("$.implementation_signature_contract must be an object")
        return
    if not isinstance(value, dict):
        issues.append("$.implementation_signature_contract must be an object")
        return
    entries = value.get("entries")
    if not isinstance(entries, list):
        issues.append("$.implementation_signature_contract.entries must be an array")
        return
    allowed = {
        (str(item.get("target_path") or ""), str(item.get("target_symbol") or ""))
        for item in signature_targets
    }
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        path = f"$.implementation_signature_contract.entries[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{path} must be an object")
            continue
        raw_key = (entry.get("target_path"), entry.get("target_symbol"))
        key = tuple(str(item) if isinstance(item, str) else "" for item in raw_key)
        seen.add(key)
        if not all(isinstance(item, str) for item in raw_key):
            issues.append(f"{path} target_path and target_symbol must be strings")
        elif allowed and key not in allowed:
            issues.append(f"{path} names a signature target outside this planning slice")
        parameters = entry.get("parameters")
        if not isinstance(parameters, list):
            issues.append(f"{path}.parameters must be an array")
        else:
            for parameter_index, parameter in enumerate(parameters):
                parameter_path = f"{path}.parameters[{parameter_index}]"
                if not isinstance(parameter, dict):
                    issues.append(f"{parameter_path} must be an object")
                    continue
                for field in ("name", "type"):
                    if not isinstance(parameter.get(field), str):
                        issues.append(f"{parameter_path}.{field} must be a string")
                for field in ("optional", "rest"):
                    if not isinstance(parameter.get(field), bool):
                        issues.append(f"{parameter_path}.{field} must be a boolean")
        if not isinstance(entry.get("return_type"), str):
            issues.append(f"{path}.return_type must be a string")
        _require_string_list(entry.get("type_parameters"), f"{path}.type_parameters", issues)
        if "async" in entry and not isinstance(entry.get("async"), bool):
            issues.append(f"{path}.async must be a boolean")
    if required and allowed and seen != allowed:
        issues.append(
            "$.implementation_signature_contract.entries must contain the exact slice target set "
            f"(missing={sorted(allowed - seen)}, extra={sorted(seen - allowed)})"
        )


def _require_runtime_config(
    value: object,
    *,
    required: bool,
    allowed_unit_ids: set[str] | None,
    previous: object,
    issues: list[str],
) -> None:
    if value is None and not required:
        return
    if not isinstance(value, dict):
        issues.append("$.runtime_config_impact must be an object")
        return
    status = value.get("status")
    if status not in {"required", "not_required"}:
        issues.append("$.runtime_config_impact.status must be `required` or `not_required`")
    elif not isinstance(status, str):
        issues.append("$.runtime_config_impact.status must be a string")
    if not isinstance(value.get("reason"), str) or not str(value.get("reason")).strip():
        issues.append("$.runtime_config_impact.reason must be a non-empty string")
    changes = value.get("changes")
    if not isinstance(changes, list):
        issues.append("$.runtime_config_impact.changes must be an array")
        return
    if status == "not_required" and changes:
        issues.append("$.runtime_config_impact.changes must be empty when status is `not_required`")
    if status == "required" and not changes:
        issues.append("$.runtime_config_impact.changes must not be empty when status is `required`")
    for index, change in enumerate(changes):
        path = f"$.runtime_config_impact.changes[{index}]"
        if not isinstance(change, dict):
            issues.append(f"{path} must be an object")
            continue
        if not isinstance(change.get("kind"), str):
            issues.append(f"{path}.kind must be a string")
        if not isinstance(change.get("unit_id"), str):
            issues.append(f"{path}.unit_id must be a string")
        for field, field_value in change.items():
            if field in {"kind", "unit_id"}:
                continue
            if not isinstance(field_value, (str, bool)):
                issues.append(f"{path}.{field} must be a string or boolean")
    if previous is None:
        if allowed_unit_ids is not None:
            foreign = {
                str(change.get("unit_id") or "")
                for change in changes
                if isinstance(change, dict)
            } - allowed_unit_ids
            if foreign:
                issues.append(
                    "$.runtime_config_impact contains changes outside this planning slice: "
                    f"{sorted(foreign)}"
                )
        return
    previous_impact = canonical_runtime_config_impact(previous)
    current_impact = canonical_runtime_config_impact(value)
    previous_identities = {
        _runtime_change_identity(change)
        for change in _dict_items(previous_impact.get("changes"))
    }
    current_changes = {
        _runtime_change_identity(change): change
        for change in _dict_items(current_impact.get("changes"))
    }
    if allowed_unit_ids is not None:
        foreign_new = sorted(
            {
                str(change.get("unit_id") or "")
                for identity, change in current_changes.items()
                if identity not in previous_identities
                and str(change.get("unit_id") or "") not in allowed_unit_ids
            }
        )
        if foreign_new:
            issues.append(
                "$.runtime_config_impact adds changes outside this planning slice: "
                f"{foreign_new}"
            )
    for change in _dict_items(previous_impact.get("changes")):
        unit_id = str(change.get("unit_id") or "")
        if allowed_unit_ids is not None and unit_id in allowed_unit_ids:
            continue
        if current_changes.get(_runtime_change_identity(change)) != change:
            issues.append(
                "$.runtime_config_impact must preserve prior change owned by "
                f"unit `{unit_id}` byte-for-byte"
            )


def _runtime_change_identity(change: dict[str, object]) -> tuple[str, str, str]:
    kind = str(change.get("kind") or "")
    identity = str(
        change.get("name")
        or change.get("package")
        or change.get("service")
        or ""
    )
    return kind, str(change.get("unit_id") or ""), identity


def _merge_implementation_signature_contract(
    *,
    planning_policy: PlanningPathPolicy,
    mappings: list[dict[str, object]],
    class_graph: dict[str, object],
    previous: object,
    patch: object,
    bind_to_mapped_symbol: bool = False,
) -> dict[str, object]:
    """Merge a valid signature patch without dropping untouched prior entries."""

    previous_contract = build_implementation_signature_contract(
        planning_policy=planning_policy,
        mappings=mappings,
        class_graph=class_graph,
        candidate=previous,
        bind_to_mapped_symbol=bind_to_mapped_symbol,
    )
    patch_contract = build_implementation_signature_contract(
        planning_policy=planning_policy,
        mappings=mappings,
        class_graph=class_graph,
        candidate=patch,
        bind_to_mapped_symbol=bind_to_mapped_symbol,
    )
    merged_entries = {
        str(entry.get("design_element_id") or ""): entry
        for entry in _dict_items(previous_contract.get("entries"))
        if str(entry.get("design_element_id") or "")
    }
    for entry in _dict_items(patch_contract.get("entries")):
        design_id = str(entry.get("design_element_id") or "")
        if design_id:
            merged_entries[design_id] = entry
    return build_implementation_signature_contract(
        planning_policy=planning_policy,
        mappings=mappings,
        class_graph=class_graph,
        candidate={"entries": list(merged_entries.values())},
        bind_to_mapped_symbol=bind_to_mapped_symbol,
    )


def _implementation_signature_targets(
    *,
    mappings: list[dict[str, object]],
    class_graph: dict[str, object],
) -> list[dict[str, object]]:
    """Expose real callable targets while keeping legacy Design IDs Harness-owned."""

    owner_kinds = {
        str(item.get("className") or item.get("id") or "").strip():
        str(item.get("type") or "class").strip().lower()
        for item in _dict_items(class_graph.get("nodes"))
        if str(item.get("className") or item.get("id") or "").strip()
    }
    grouped: dict[tuple[str, str], list[str]] = {}
    for mapping in mappings:
        if str(mapping.get("design_element_type") or "") != "method":
            continue
        target = str(mapping.get("planned_code_target") or "").strip().replace("\\", "/")
        path, separator, symbol = target.partition("#")
        design_id = str(mapping.get("design_element_id") or "").strip()
        if separator and path and symbol and design_id:
            grouped.setdefault((path, symbol), []).append(design_id)

    targets: list[dict[str, object]] = []
    for (path, symbol), design_ids in sorted(grouped.items()):
        representative = sorted(set(design_ids))[0]
        design_owner = representative.removeprefix("method:").rsplit(".", 1)[0]
        target_owner = symbol.rsplit(".", 1)[0] if "." in symbol else ""
        owner_kind = owner_kinds.get(target_owner) or owner_kinds.get(design_owner) or "class"
        targets.append(
            {
                "target_path": path,
                "target_symbol": symbol,
                "symbol_kind": "method" if "." in symbol else "function",
                "owner_kind": owner_kind if "." in symbol else "",
                "design_element_ids": sorted(set(design_ids)),
            }
        )
    return targets


def _bind_target_signature_patch(
    *,
    candidate: object,
    signature_targets: list[dict[str, object]],
) -> dict[str, object]:
    """Bind LLM-proposed shapes to the immutable target catalogue."""

    requested = {
        (
            str(item.get("target_path") or "").strip().replace("\\", "/"),
            str(item.get("target_symbol") or "").strip(),
        ): item
        for item in _dict_items(_object_dict(candidate).get("entries"))
    }
    entries: list[dict[str, object]] = []
    for target in signature_targets:
        key = (
            str(target.get("target_path") or ""),
            str(target.get("target_symbol") or ""),
        )
        raw = requested.get(key)
        design_ids = _string_items(target.get("design_element_ids"))
        if not raw or not design_ids:
            continue
        entries.append(
            {
                "design_element_id": design_ids[0],
                "symbol_kind": str(target.get("symbol_kind") or "function"),
                "owner_kind": str(target.get("owner_kind") or ""),
                "qualified_name": key[1],
                "parameters": raw.get("parameters"),
                "return_type": raw.get("return_type"),
                "type_parameters": raw.get("type_parameters"),
                "async": raw.get("async") is True,
            }
        )
    return {"entries": entries}


def _signature_patch_for_repair(
    *,
    llm_payload: dict[str, object],
    developer_feedback: str,
) -> object:
    if "implementation signature" not in developer_feedback.casefold():
        return {}
    return llm_payload.get("implementation_signature_contract")


def _resolve_verification_policy(
    *,
    planning_policy: PlanningPathPolicy | None,
    verification_policy: VerificationStagePolicy | None,
) -> VerificationStagePolicy:
    """Bắt buộc Phase 3 dùng policy đã resolve từ immutable run snapshot."""

    if planning_policy is None or verification_policy is None:
        raise HarnessError(
            "planning_profile_policy_invalid",
            "Developer planning requires immutable planning and verification policies.",
        )
    if verification_policy.project_profile_id != planning_policy.project_profile_id:
        raise HarnessError(
            "verification_stage_policy_mismatch",
            "Verification stages do not belong to the planning project profile.",
        )
    return verification_policy


def _parse_first_json_object(raw: str, *, expected_keys: set[str]) -> dict[str, object]:
    """Parse JSON object top-level mong muốn, bỏ qua object con/prompt echo."""

    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _has_expected_keys(parsed, expected_keys):
        return parsed

    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and _has_expected_keys(candidate, expected_keys):
            return candidate
    raise json.JSONDecodeError("No matching top-level JSON object found", text, 0)


def _has_expected_keys(candidate: dict[str, object], expected_keys: set[str]) -> bool:
    return expected_keys.issubset(candidate.keys())


def _merge_mappings(
    base: list[dict[str, object]],
    llm_items: list[dict[str, object]],
    *,
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    by_id = {str(item.get("design_element_id") or ""): dict(item) for item in base}
    for item in llm_items:
        element_id = str(item.get("design_element_id") or "")
        if not element_id or (planning_policy and element_id not in by_id):
            continue
        current = by_id.get(element_id, {})
        merged = {
            **current,
            **{key: value for key, value in item.items() if str(value).strip()},
        }
        if planning_policy and current:
            merged["planned_code_target"] = current.get("planned_code_target", "")
            merged["component_id"] = current.get("component_id", "")
        by_id[element_id] = merged
    ordered: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in base:
        element_id = str(item.get("design_element_id") or "")
        if element_id in by_id:
            ordered.append(by_id[element_id])
            seen.add(element_id)
    for element_id, item in by_id.items():
        if element_id not in seen:
            ordered.append(item)
    return ordered


def _repo_context_summary(repo_context: dict[str, object]) -> dict[str, object]:
    evidence = str(repo_context.get("evidence") or "")
    return {
        "mode": repo_context.get("mode", ""),
        "status": repo_context.get("status", ""),
        "query": repo_context.get("query", ""),
        "source_files": repo_context.get("source_files", []),
        "evidence_char_count": len(evidence),
    }


def _repo_context_evidence_lines(repo_context: dict[str, object]) -> list[str]:
    evidence = str(repo_context.get("evidence") or "").strip()
    if not evidence:
        return []
    lines = [line.strip() for line in evidence.splitlines() if line.strip()]
    return lines[:8]


def _implementation_steps_from_mappings(
    mappings: list[dict[str, object]],
    *,
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    files = _proposed_files_from_mappings(mappings)
    return [
        {
            "order": 1,
            "title": "Map approved design to code targets",
            "description": "Implement the approved class and sequence behavior using the mapped source/test files.",
            "files": [str(item.get("path") or "") for item in files],
            "design_refs": [str(item.get("design_element_id") or "") for item in mappings],
        },
        {
            "order": 2,
            "title": "Run verification",
            "description": (
                "Add or update tests for the mapped behavior, then run the profile-owned verification stages."
                if planning_policy
                else "Add or update tests for the mapped behavior, then run the configured target repo test command."
            ),
            "files": [
                str(item.get("path") or "")
                for item in files
                if (
                    planning_policy.is_test_path(item.get("path"))
                    if planning_policy
                    else "src/test/" in str(item.get("path") or "")
                )
            ],
            "design_refs": [str(item.get("design_element_id") or "") for item in mappings],
        },
    ]


def _proposed_files_from_mappings(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in mappings:
        target = str(item.get("planned_code_target") or "").split("#", 1)[0]
        if not target:
            continue
        entry = result.setdefault(
            target,
            {
                "path": target,
                "action": "create_or_update",
                "reason": "Needed by approved design mapping.",
                "related_design_elements": [],
            },
        )
        refs = entry.setdefault("related_design_elements", [])
        if isinstance(refs, list):
            refs.append(str(item.get("design_element_id") or ""))
    return list(result.values())


def _canonicalize_implementation_steps(
    steps: list[dict[str, object]],
    *,
    mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy | None,
) -> list[dict[str, object]]:
    """Giữ files hợp lệ và khôi phục deterministic mapping paths sau output LLM."""

    if not planning_policy:
        return steps
    mapping_paths = {
        str(item.get("design_element_id") or ""): planning_policy.normalize_mapping_path(
            str(item.get("planned_code_target") or "").split("#", 1)[0]
        )
        for item in mappings
    }
    result: list[dict[str, object]] = []
    for step in steps:
        item = dict(step)
        files = [
            path
            for raw_path in (item.get("files") if isinstance(item.get("files"), list) else [])
            for path in [planning_policy.normalize_editable_path(raw_path)]
            if path
        ]
        for design_ref in _string_items(item.get("design_refs")):
            path = mapping_paths.get(design_ref, "")
            if path and path not in files:
                files.append(path)
        item["files"] = files
        result.append(item)
    return result


def _canonical_proposed_files(
    *,
    proposed_files: list[dict[str, object]],
    implementation_steps: list[dict[str, object]],
    mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    """Return the executable profile-approved file set as one deterministic union."""

    by_path: dict[str, dict[str, object]] = {}
    for item in proposed_files:
        path = _planning_path(item.get("path"), planning_policy=planning_policy)
        if path:
            by_path[path] = _with_path_component({**item, "path": path}, planning_policy)
    for step in implementation_steps:
        for raw_path in step.get("files", []) if isinstance(step.get("files"), list) else []:
            path = _planning_path(raw_path, planning_policy=planning_policy)
            if path and path not in by_path:
                by_path[path] = _with_path_component({
                    "path": path,
                    "action": "create_or_update",
                    "reason": "Named by an implementation step.",
                    "related_design_elements": _string_items(step.get("design_refs")),
                }, planning_policy)
    for mapping in mappings:
        path = _planning_path(
            str(mapping.get("planned_code_target") or "").split("#", 1)[0],
            planning_policy=planning_policy,
            mapping_only=True,
        )
        if not path:
            continue
        entry = by_path.setdefault(
            path,
            _with_path_component({
                "path": path,
                "action": "create_or_update",
                "reason": "Needed by an approved design mapping.",
                "related_design_elements": [],
            }, planning_policy),
        )
        refs = entry.setdefault("related_design_elements", [])
        element_id = str(mapping.get("design_element_id") or "").strip()
        if isinstance(refs, list) and element_id and element_id not in refs:
            refs.append(element_id)
    return list(by_path.values())


def _planning_path(
    value: object,
    *,
    planning_policy: PlanningPathPolicy | None,
    mapping_only: bool = False,
) -> str:
    if planning_policy:
        return (
            planning_policy.normalize_mapping_path(value)
            if mapping_only
            else planning_policy.normalize_editable_path(value)
        )
    return _java_path(value)


def _with_path_component(
    item: dict[str, object],
    planning_policy: PlanningPathPolicy | None,
) -> dict[str, object]:
    if planning_policy:
        item["component_id"] = planning_policy.component_for_path(item.get("path"))
    return item


def _java_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    return path if path.startswith(("src/main/java/", "src/test/java/")) and path.endswith(".java") else ""


def _ensure_executable_test_files(
    *,
    proposed_files: list[dict[str, object]],
    mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    """Ensure flow and repository behaviors have editable test targets."""

    result = list(proposed_files)
    seen = {str(item.get("path") or "") for item in result}
    class_targets = {
        str(item.get("design_element_name") or ""): str(item.get("planned_code_target") or "").split("#", 1)[0]
        for item in mappings
        if item.get("design_element_type") == "class"
    }
    required: list[tuple[str, str, str]] = []
    controllers = [
        (name, path)
        for name, path in class_targets.items()
        if name.endswith("Controller") or (planning_policy and name.endswith("Boundary"))
    ]
    if controllers and any(item.get("design_element_type") == "sequence_message" for item in mappings):
        name, path = controllers[0]
        flow_base = name.removesuffix("Controller")
        if planning_policy:
            flow_base = flow_base.removesuffix("Boundary")
        flow_name = flow_base + ("Flow" if planning_policy else "FlowTest")
        required.append(
            (
                name,
                _test_path_for_main(path, flow_name, planning_policy=planning_policy),
                "Verify the approved sequence and acceptance flow.",
            )
        )
    repositories = [(name, path) for name, path in class_targets.items() if name.endswith("Repository")]
    if repositories:
        name, path = repositories[0]
        test_name = name if planning_policy else f"{name}Test"
        required.append(
            (
                name,
                _test_path_for_main(path, test_name, planning_policy=planning_policy),
                "Verify repository rules and persistence boundaries.",
            )
        )
    for design_name, path, reason in required:
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(
            _with_path_component({
                "path": path,
                "action": "create_or_update",
                "reason": reason,
                "related_design_elements": [f"class:{design_name}"],
            }, planning_policy)
        )
    return result


def _test_path_for_main(
    main_path: str,
    test_class_name: str,
    *,
    planning_policy: PlanningPathPolicy | None = None,
) -> str:
    if planning_policy:
        return planning_policy.test_path_for_source(main_path, test_class_name)
    path = _java_path(main_path)
    if not path or not path.startswith("src/main/java/"):
        return ""
    parent = path.rsplit("/", 1)[0].replace("src/main/java/", "src/test/java/", 1)
    return f"{parent}/{test_class_name}.java"


def _design_adjustments(
    *,
    class_graph: dict[str, object],
    class_targets: dict[str, str],
    target_repo_path: str,
    requirement_design_trace: dict[str, object],
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    """Propose omission of untraced setters that conflict with existing final fields."""

    if planning_policy:
        return []

    directly_traced = {
        str(link.get("target") or "").strip()
        for key in ("class_trace", "sequence_trace")
        for item in _dict_items(requirement_design_trace.get(key))
        for link in _dict_items(item.get("links"))
        if str(link.get("target") or "").strip()
    }
    root = Path(target_repo_path) if target_repo_path else None
    result: list[dict[str, object]] = []
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        class_file = class_targets.get(class_name, "")
        source = ""
        if root and class_file:
            source_path = root / class_file
            if source_path.exists():
                source = source_path.read_text(encoding="utf-8", errors="replace")
        if not source:
            continue
        for method in _dict_items(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if not re.fullmatch(r"set[A-Z].*", method_name):
                continue
            field_name = method_name[3:4].lower() + method_name[4:]
            if not re.search(rf"\bfinal\b[^;]*\b{re.escape(field_name)}\s*;", source):
                continue
            design_name = f"{class_name}.{method_name}"
            traced = design_name in directly_traced or method_name in directly_traced
            result.append(
                {
                    "design_element_id": f"method:{design_name}",
                    "action": "unresolved" if traced else "omit",
                    "current_contract": f"`{class_name}.{field_name}` is final in the target repository.",
                    "proposed_effective_contract": (
                        f"Preserve immutable `{class_name}` state and constructor assignment; omit `{method_name}`."
                    ),
                    "reason": "A generated setter would conflict with the repository's immutable final-field invariant.",
                    "requirement_trace_refs": [design_name] if traced else [],
                    "requires_human_approval": True,
                    "status": "conflict" if traced else "proposed",
                }
            )
    return result


def _apply_design_adjustments(
    mappings: list[dict[str, object]], adjustments: list[dict[str, object]]
) -> list[dict[str, object]]:
    by_id = {str(item.get("design_element_id") or ""): item for item in adjustments}
    result: list[dict[str, object]] = []
    for mapping in mappings:
        item = dict(mapping)
        adjustment = by_id.get(str(item.get("design_element_id") or ""))
        if adjustment and adjustment.get("action") == "omit":
            item["planned_code_target"] = str(item.get("planned_code_target") or "").split("#", 1)[0]
            item["implementation_status"] = "waived_by_design_adjustment"
            item["adjustment_ref"] = adjustment.get("design_element_id")
            item["trace_policy"] = "design_adjustment"
            item["reason"] = str(adjustment.get("proposed_effective_contract") or adjustment.get("reason") or "")
            item["test_intent"] = "Verify the existing immutable constructor contract remains intact; do not add this setter."
        result.append(item)
    return result


def _apply_design_adjustments_to_steps(
    steps: list[dict[str, object]], adjustments: list[dict[str, object]]
) -> list[dict[str, object]]:
    omitted = [item for item in adjustments if item.get("action") == "omit"]
    if not omitted:
        return steps
    by_class: dict[str, list[str]] = {}
    omitted_ids = {str(item.get("design_element_id") or "") for item in omitted}
    for item in omitted:
        element_id = str(item.get("design_element_id") or "")
        qualified = element_id.split(":", 1)[-1]
        if "." not in qualified:
            continue
        class_name, method_name = qualified.rsplit(".", 1)
        by_class.setdefault(class_name, []).append(method_name)

    result: list[dict[str, object]] = []
    for step in steps:
        item = dict(step)
        refs = _string_items(item.get("design_refs"))
        text = f"{item.get('title') or ''} {item.get('description') or ''}"
        affected = {
            class_name: methods
            for class_name, methods in by_class.items()
            if any(f"method:{class_name}.{method}" in refs or method in text for method in methods)
        }
        item["design_refs"] = [ref for ref in refs if ref not in omitted_ids]
        if affected:
            constraints = []
            for class_name, methods in affected.items():
                constraints.append(
                    f"Preserve the existing immutable `{class_name}` constructor contract and do not add "
                    + ", ".join(f"`{method}`" for method in methods)
                    + "; those methods are omitted by the design adjustment reviewed at this checkpoint."
                )
            item["description"] = " ".join(constraints) + " Keep all other approved design refs in this step unchanged."
        result.append(item)
    return result


def _compact_review(review: dict[str, object]) -> dict[str, object]:
    return {
        "ok": review.get("ok"),
        "summary": review.get("summary"),
        "developer_feedback": review.get("developer_feedback"),
        "issues": _string_items(review.get("issues")) or [
            issue
            for key in (
                "coverage_issues",
                "requirement_coverage_issues",
                "traceability_issues",
                "executability_issues",
                "scope_issues",
                "implementation_quality_issues",
                "test_issues",
            )
            for issue in _string_items(review.get(key))
        ],
    }


def _affected_unit_ids(review: dict[str, object]) -> list[str]:
    direct = _string_items(review.get("affected_unit_ids"))
    if direct:
        return sorted(set(direct))
    text = "\n".join(
        _string_items(review.get("issues"))
        + [
            issue
            for key in (
                "coverage_issues",
                "requirement_coverage_issues",
                "traceability_issues",
                "executability_issues",
                "scope_issues",
                "implementation_quality_issues",
                "test_issues",
            )
            for issue in _string_items(review.get(key))
        ]
    )
    return sorted(set(re.findall(r"\bWU-[A-F0-9]{12}\b", text)))


def _repair_candidate(plan: dict[str, object]) -> dict[str, object]:
    return {
        "implementation_steps": _dict_items(plan.get("implementation_steps")),
        "proposed_files": _dict_items(plan.get("proposed_files")),
        "design_adjustments": _dict_items(plan.get("design_adjustments")),
        "implementation_signature_contract": _object_dict(
            plan.get("implementation_signature_contract")
        ),
        "test_intents": _dict_items(plan.get("test_intents")),
    }


def _compact_mappings(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    keys = (
        "design_element_id",
        "design_element_type",
        "design_element_name",
        "planned_code_target",
        "message_type",
        "trace_policy",
        "implementation_status",
        "adjustment_ref",
    )
    return [{key: item[key] for key in keys if key in item} for item in mappings]


def _compact_class_graph(graph: dict[str, object]) -> dict[str, object]:
    return {
        "nodes": [
            {
                "className": node.get("className") or node.get("id"),
                "methods": [method.get("name") for method in _dict_items(node.get("methods"))],
            }
            for node in _dict_items(graph.get("nodes"))
        ]
    }


def _compact_sequence_graph(graph: dict[str, object]) -> dict[str, object]:
    return {
        "sequenceDiagrams": [
            {
                "id": diagram.get("id"),
                "messages": [
                    {
                        "from": message.get("from"),
                        "to": message.get("to"),
                        "message": message.get("message"),
                        "type": message.get("type"),
                    }
                    for message in _dict_items(diagram.get("messages"))
                ],
            }
            for diagram in _dict_items(graph.get("sequenceDiagrams"))
        ]
    }


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _requirement_coverage(
    *,
    requirement_graph: dict[str, object],
    requirement_design_trace: dict[str, object],
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Derive canonical requirement -> approved design -> code/test coverage in Python."""

    mapping_by_name: dict[str, list[dict[str, object]]] = {}
    for mapping in mappings:
        name = str(mapping.get("design_element_name") or "").strip()
        if name:
            mapping_by_name.setdefault(name, []).append(mapping)

    trace_targets: dict[str, list[str]] = {}
    for trace_key in ("class_trace", "sequence_trace"):
        for item in _dict_items(requirement_design_trace.get(trace_key)):
            requirement_id = str(item.get("requirement") or "").strip()
            if not requirement_id:
                continue
            targets = trace_targets.setdefault(requirement_id, [])
            for link in _dict_items(item.get("links")):
                target = str(link.get("target") or "").strip()
                if target and target not in targets:
                    targets.append(target)

    coverage: list[dict[str, object]] = []
    for requirement in _dict_items(requirement_graph.get("requirements")):
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        targets = trace_targets.get(requirement_id, [])
        resolved: list[dict[str, object]] = []
        for target in targets:
            candidates = _mappings_for_trace_target(target=target, mapping_by_name=mapping_by_name)
            for mapping in candidates:
                if mapping not in resolved:
                    resolved.append(mapping)
        coverage.append(
            {
                "requirement_id": requirement_id,
                "content": str(requirement.get("content") or "").strip(),
                "design_targets": targets,
                "design_element_ids": _unique_strings(
                    str(item.get("design_element_id") or "") for item in resolved
                ),
                "planned_code_targets": _unique_strings(
                    str(item.get("planned_code_target") or "") for item in resolved
                ),
                "test_intents": _unique_strings(str(item.get("test_intent") or "") for item in resolved),
            }
        )
    return coverage


def _merge_requirement_coverage(
    component_coverage: list[dict[str, object]],
    canonical_coverage: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Keep direct Vertical refs and backfill canonical IDs needed by Phase 4."""

    direct = {
        str(item.get("requirement_id") or ""): item
        for item in component_coverage
        if str(item.get("requirement_id") or "")
    }
    result: list[dict[str, object]] = []
    for item in canonical_coverage:
        requirement_id = str(item.get("requirement_id") or "")
        vertical = direct.get(requirement_id)
        result.append(
            {
                **item,
                **(
                    {
                        "vertical_design_node_ids": _string_items(
                            vertical.get("design_element_ids")
                        ),
                        "work_unit_ids": _string_items(vertical.get("work_unit_ids")),
                    }
                    if vertical
                    else {}
                ),
            }
        )
    known = {str(item.get("requirement_id") or "") for item in result}
    result.extend(
        item
        for item in component_coverage
        if str(item.get("requirement_id") or "") not in known
    )
    return result


def _mappings_for_trace_target(
    *,
    target: str,
    mapping_by_name: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    candidates = list(mapping_by_name.get(target, []))
    candidates.extend(
        mapping
        for name, named_mappings in mapping_by_name.items()
        if name.endswith(f": {target}")
        for mapping in named_mappings
    )
    # Class trace may point to an approved attribute. Phase 3 plans at class/file
    # granularity, so resolve `Class.attribute` through its owning class mapping.
    if "." in target:
        owner = target.split(".", 1)[0]
        candidates.extend(mapping_by_name.get(owner, []))
    return candidates


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _design_mappings(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    target_files: list[str],
    target_repo_path: str = "",
    requirement_design_trace: dict[str, object] | None = None,
    planning_policy: PlanningPathPolicy | None = None,
) -> list[dict[str, object]]:
    mappings: list[dict[str, object]] = []
    class_targets = _class_target_files(
        class_graph=class_graph,
        target_files=target_files,
        planning_policy=planning_policy,
    )
    class_methods = _class_methods(class_graph)

    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        class_file = class_targets.get(class_name) or _default_target_file(
            class_name,
            node=node,
            planning_policy=planning_policy,
        )
        component = (
            planning_policy.component_for_path(class_file, mapping_only=True)
            if planning_policy
            else ""
        )
        mappings.append(
            {
                "design_element_id": f"class:{class_name}",
                "design_element_type": "class",
                "design_element_name": class_name,
                "planned_code_target": class_file,
                **({"component_id": component} if component else {}),
                "reason": f"Implement or update class `{class_name}` from the approved class diagram.",
                "test_intent": f"Cover behavior exposed through `{class_name}` in unit or flow tests.",
            }
        )
        for method in _dict_items(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if not method_name:
                continue
            mappings.append(
                {
                    "design_element_id": f"method:{class_name}.{method_name}",
                    "design_element_type": "method",
                    "design_element_name": f"{class_name}.{method_name}",
                    "planned_code_target": f"{class_file}#{method_name}",
                    **({"component_id": component} if component else {}),
                    "reason": f"Implement method `{class_name}.{method_name}` from the approved class diagram.",
                    "test_intent": f"Exercise `{method_name}` through the primary use-case flow or direct unit test.",
                }
            )

    for diagram in _dict_items(sequence_graph.get("sequenceDiagrams")):
        diagram_id = str(diagram.get("id") or "sequence_diagram").strip()
        participant_classes = _sequence_participant_classes(diagram=diagram, class_targets=class_targets)
        for index, message in enumerate(_dict_items(diagram.get("messages")), start=1):
            source = str(message.get("from") or "").strip()
            target = str(message.get("to") or "").strip()
            text = str(message.get("message") or "").strip()
            if not text:
                continue
            message_type = str(message.get("type") or "synchronous").strip().lower()
            code_target, trace_policy = _sequence_message_target(
                message_text=text,
                source=source,
                target=target,
                class_targets=class_targets,
                class_methods=class_methods,
                participant_classes=participant_classes,
                message_type=message_type,
                planning_policy=planning_policy,
            )
            component = (
                planning_policy.component_for_path(code_target.split("#", 1)[0], mapping_only=True)
                if planning_policy
                else ""
            )
            mappings.append(
                {
                    "design_element_id": f"sequence:{diagram_id}:{index}",
                    "design_element_type": "sequence_message",
                    "design_element_name": f"{source}->{target}: {text}",
                    "planned_code_target": code_target,
                    **({"component_id": component} if component else {}),
                    "message_type": message_type,
                    "trace_policy": trace_policy,
                    "source_message": text,
                    "reason": "Preserve the approved sequence interaction in the implementation flow.",
                    "test_intent": f"Verify sequence step `{text}` through an integration or service-level test.",
                }
            )
    return mappings


def _test_intents(
    *,
    mappings: list[dict[str, object]],
    test_command: str,
    verification_policy: VerificationStagePolicy | None = None,
) -> list[dict[str, str]]:
    methods = [
        str(item.get("design_element_name") or "")
        for item in mappings
        if item.get("design_element_type") == "method"
    ]
    primary_methods = ", ".join(methods[:5]) if methods else "primary use-case behavior"
    if verification_policy:
        primary_stage = "unit" if "unit" in verification_policy.stage_ids else verification_policy.stage_ids[0]
        regression_stage = (
            "typecheck"
            if "typecheck" in verification_policy.stage_ids
            else next(
                (stage_id for stage_id in verification_policy.stage_ids if stage_id != primary_stage),
                primary_stage,
            )
        )
        return [
            {
                "scope": "unit_or_service",
                "intent": f"Validate {primary_methods}.",
                "stage_id": primary_stage,
            },
            {
                "scope": "regression",
                "intent": "Run profile-owned static and regression verification after Phase 4 changes.",
                "stage_id": regression_stage,
            },
        ]
    return [
        {
            "scope": "unit_or_service",
            "intent": f"Validate {primary_methods}.",
            "command": test_command,
        },
        {
            "scope": "regression",
            "intent": "Run the target repo test command after Phase 4 code changes.",
            "command": test_command,
        },
    ]


def _class_target_files(
    *,
    class_graph: dict[str, object],
    target_files: list[str],
    planning_policy: PlanningPathPolicy | None = None,
) -> dict[str, str]:
    if planning_policy:
        result: dict[str, str] = {}
        for node in _dict_items(class_graph.get("nodes")):
            class_name = str(node.get("className") or node.get("id") or "").strip()
            if class_name:
                result[class_name], _ = planning_policy.resolve_class_target(
                    node=node,
                    target_files=target_files,
                )
        return result
    existing = {Path(path).stem: path for path in target_files if path.endswith(".java")}
    result: dict[str, str] = {}
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if class_name:
            result[class_name] = existing.get(class_name, _default_java_file(class_name))
    return result


def _default_java_file(class_name: str) -> str:
    return f"src/main/java/com/sdlc/etour/heritage/{class_name}.java"


def _default_target_file(
    class_name: str,
    *,
    node: dict[str, object],
    planning_policy: PlanningPathPolicy | None,
) -> str:
    if not planning_policy:
        return _default_java_file(class_name)
    path, _ = planning_policy.resolve_class_target(node=node, target_files=[])
    return path


def _sequence_message_target(
    *,
    message_text: str,
    source: str,
    target: str,
    class_targets: dict[str, str],
    class_methods: dict[str, set[str]],
    participant_classes: dict[str, str],
    message_type: str,
    planning_policy: PlanningPathPolicy | None = None,
) -> tuple[str, str]:
    method_name = _message_method_name(message_text)
    source_class = participant_classes.get(source) or _participant_to_class(source, class_targets)
    target_class = participant_classes.get(target) or _participant_to_class(target, class_targets)
    normalized_type = message_type or "synchronous"

    if normalized_type == "return":
        owner = source_class or target_class
        if not owner:
            return "", "return_behavior"
        code_file = _class_file_for_owner(owner, class_targets, planning_policy)
        if method_name and method_name in class_methods.get(owner, set()):
            return f"{code_file}#{method_name}", "direct_symbol"
        policy = "semantic_side_effect" if method_name.lower().startswith(("display", "show", "notify")) else "return_behavior"
        return code_file, policy

    if normalized_type in {"lost_or_exception", "exception", "lost"}:
        owner = source_class or target_class
        if not owner:
            return "", "exception_path"
        return _class_file_for_owner(owner, class_targets, planning_policy), "exception_path"

    owner = target_class or source_class
    if not owner:
        return "", "boundary_interaction"
    code_file = _class_file_for_owner(owner, class_targets, planning_policy)
    if method_name and method_name in class_methods.get(owner, set()):
        return f"{code_file}#{method_name}", "direct_symbol"
    return code_file, "boundary_interaction"


def _class_file_for_owner(
    owner: str,
    class_targets: dict[str, str],
    planning_policy: PlanningPathPolicy | None,
) -> str:
    if owner in class_targets:
        return class_targets[owner]
    return "" if planning_policy else _default_java_file(owner)


def _message_method_name(message_text: str) -> str:
    candidate = message_text.split("(", 1)[0].strip().replace(" ", "")
    return candidate if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", candidate) else ""


def _class_methods(class_graph: dict[str, object]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        result[class_name] = {
            str(method.get("name") or "").strip()
            for method in _dict_items(node.get("methods"))
            if str(method.get("name") or "").strip()
        }
    return result


def _sequence_participant_classes(
    *, diagram: dict[str, object], class_targets: dict[str, str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in _dict_items(diagram.get("nodes")):
        participant_id = str(node.get("id") or "").strip()
        label = str(node.get("label") or node.get("name") or participant_id).strip()
        class_name = _participant_to_class(label, class_targets) or _participant_to_class(participant_id, class_targets)
        if participant_id and class_name:
            result[participant_id] = class_name
    return result


def _participant_to_class(participant: str, class_targets: dict[str, str]) -> str:
    normalized = participant.lower()
    for class_name in class_targets:
        lowered = class_name.lower()
        if normalized == lowered or normalized in lowered or lowered.endswith(normalized):
            return class_name
    return ""


def _markdown_plan(
    *,
    structured: dict[str, object],
    requirement_packet: str,
    design_artifact: str,
) -> str:
    repo = _object_dict(structured.get("target_repository"))
    mappings = _dict_items(structured.get("design_mappings"))
    requirement_coverage = _dict_items(structured.get("requirement_coverage"))
    test_intents = _dict_items(structured.get("test_intents"))
    verification_stage_ids = _string_items(structured.get("verification_stage_ids"))
    implementation_steps = _dict_items(structured.get("implementation_steps"))
    work_units = _dict_items(structured.get("work_units"))
    proposed_files = _dict_items(structured.get("proposed_files"))
    repo_evidence = [str(item) for item in structured.get("repo_evidence", []) if str(item).strip()]
    assumptions = [str(item) for item in structured.get("assumptions", []) if str(item).strip()]
    open_questions = [str(item) for item in structured.get("open_questions", []) if str(item).strip()]
    out_of_scope = [str(item) for item in structured.get("out_of_scope", []) if str(item).strip()]
    signature_contract = _object_dict(structured.get("implementation_signature_contract"))
    signature_entries = _dict_items(signature_contract.get("entries"))
    version_text = str(structured.get("version") or "")
    plan_label = (
        "V5"
        if version_text.endswith("_v5")
        else "V4"
        if version_text.endswith("_v4")
        else "V3"
        if version_text.endswith("_v3")
        else "V2"
        if version_text.endswith("_v2")
        else "V1"
    )

    lines = [
        f"# Implementation Plan {plan_label}",
        "",
        "## Run Context",
        "",
        f"- Run id: `{structured.get('run_id', '')}`",
        f"- Source ref: `{structured.get('source_ref', '')}`",
        f"- Issue key: `{structured.get('issue_key', '')}`",
        "",
        "## Target Repository",
        "",
        f"- Local path: `{repo.get('local_path', '')}`",
        f"- Remote: `{repo.get('remote', '')}`",
        f"- Base branch: `{repo.get('base_branch', '')}`",
        f"- Planned branch: `{repo.get('planned_branch', '')}`",
        f"- Workspace mode: `{repo.get('workspace_mode', '')}`",
        f"- Base SHA: `{repo.get('base_sha', '')}`",
        "",
        "## Repo Evidence",
        "",
    ]
    if repo_evidence:
        for item in repo_evidence[:12]:
            lines.append(f"- {item}")
    else:
        lines.append("- No CodeGraph evidence was attached.")

    if work_units:
        lines.extend(["", "## Component Work Units", ""])
        for unit in work_units:
            targets = ", ".join(
                f"`{item.get('path')}`"
                + (f"#{item.get('symbol')}" if str(item.get("symbol") or "") else "")
                for item in _dict_items(unit.get("planned_code_targets"))
                if str(item.get("path") or "")
            )
            dependencies = ", ".join(
                f"`{item}`" for item in _string_items(unit.get("depends_on"))
            )
            acceptance = ", ".join(
                f"`{item}`" for item in _string_items(unit.get("acceptance_refs"))
            )
            lines.extend(
                [
                    f"### {unit.get('order', '-')}. {unit.get('title') or unit.get('unit_id')}",
                    "",
                    f"- Unit: `{unit.get('unit_id', '')}`",
                    f"- Scenario / component: `{unit.get('scenario_id', '')}` / `{unit.get('component_id', '')}`",
                    f"- Objective: {unit.get('objective') or 'n/a'}",
                    f"- Depends on: {dependencies or 'none'}",
                    f"- Acceptance coverage: {acceptance or 'none'}",
                    f"- Code targets: {targets or 'n/a'}",
                ]
            )
            migration = _object_dict(unit.get("migration_impact"))
            if migration:
                lines.append(
                    f"- Migration: `{migration.get('status') or 'undecided'}` — "
                    f"{migration.get('reason') or 'no reason supplied'}"
                )
            for test in _dict_items(unit.get("test_intents")):
                lines.append(
                    f"- Test `{test.get('stage_id', '')}`: {test.get('intent', '')}"
                )
            for item in _dict_items(unit.get("unverified_obligations")):
                lines.append(f"- Unverified `{item.get('id', '')}`: {item.get('reason', '')}")
            for binding in _dict_items(unit.get("surface_contracts")):
                if str(binding.get("surface") or "") != "ui":
                    continue
                experience = _object_dict(
                    _object_dict(binding.get("surface_contract")).get("experience_contract")
                )
                if not experience:
                    continue
                structure = "; ".join(_string_items(experience.get("component_structure")))
                accessibility = "; ".join(_string_items(experience.get("accessibility")))
                responsive = "; ".join(_string_items(experience.get("responsive")))
                focus_flow = "; ".join(_string_items(experience.get("focus_flow")))
                feedback = "; ".join(
                    f"{item.get('trigger')}: {item.get('response')}"
                    for item in _dict_items(experience.get("feedback"))
                )
                lines.extend(
                    [
                        f"- UI structure: {structure or 'n/a'}",
                        f"- UI feedback: {feedback or 'n/a'}",
                        f"- Accessibility: {accessibility or 'n/a'}",
                        f"- Responsive: {responsive or 'n/a'}",
                        f"- Focus flow: {focus_flow or 'n/a'}",
                    ]
                )

    runtime_config = structured.get("runtime_config_impact")
    if isinstance(runtime_config, dict):
        lines.extend(
            [
                "",
                "## Runtime Configuration",
                "",
                f"- Decision: `{runtime_config.get('status') or 'undecided'}` — "
                f"{runtime_config.get('reason') or 'n/a'}",
            ]
        )
        for change in _dict_items(runtime_config.get("changes")):
            details = ", ".join(
                f"{key}={value}"
                for key, value in change.items()
                if key not in {"kind", "unit_id"} and value != ""
            )
            lines.append(f"- `{change.get('kind')}` in `{change.get('unit_id')}`: {details}")
    lines.extend(
        [
            "",
            "## Implementation Steps",
            "",
        ]
    )
    for item in implementation_steps:
        files = ", ".join(f"`{path}`" for path in item.get("files", []) if str(path).strip())
        refs = ", ".join(f"`{ref}`" for ref in item.get("design_refs", []) if str(ref).strip())
        lines.append(
            f"{item.get('order', '-')}. **{item.get('title', 'Implementation step')}**: "
            f"{item.get('description', '')} Files: {files or 'n/a'}. Design refs: {refs or 'n/a'}."
        )

    lines.extend(
        [
            "",
            "## Proposed Files",
            "",
            "| File | Action | Reason | Design refs |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in proposed_files:
        refs = ", ".join(str(ref) for ref in item.get("related_design_elements", []) if str(ref).strip())
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(f"`{item.get('path') or ''}`"),
                    _table_cell(str(item.get("action") or "")),
                    _table_cell(str(item.get("reason") or "")),
                    _table_cell(refs),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
        "## Design To Code Mapping",
        "",
        "| Design element | Type | Planned code target | Reason | Test intent |",
        "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in mappings:
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(str(item.get("design_element_name") or "")),
                    _table_cell(str(item.get("design_element_type") or "")),
                    _table_cell(f"`{item.get('planned_code_target') or ''}`"),
                    _table_cell(str(item.get("reason") or "")),
                    _table_cell(str(item.get("test_intent") or "")),
                ]
            )
            + " |"
        )

    if signature_entries:
        lines.extend(
            [
                "",
                "## Approved Implementation Signatures",
                "",
                f"- Contract: `{signature_contract.get('version', '')}`",
                f"- Digest: `{signature_contract.get('digest', '')}`",
                "",
                "| Design element | Source | Qualified symbol | Signature |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in signature_entries:
            params = ", ".join(
                (
                    ("..." if param.get("rest") is True else "")
                    + str(param.get("name") or "")
                    + ("?" if param.get("optional") is True else "")
                    + ": "
                    + str(param.get("type") or "")
                )
                for param in _dict_items(item.get("parameters"))
            )
            type_parameters = _string_items(item.get("type_parameters"))
            generic = f"<{', '.join(type_parameters)}>" if type_parameters else ""
            async_prefix = "async " if item.get("async") is True else ""
            signature = (
                f"{async_prefix}{item.get('qualified_name', '')}{generic}"
                f"({params}): {item.get('return_type', '')}"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _table_cell(str(item.get("design_element_id") or "")),
                        _table_cell(str(item.get("source_path") or "")),
                        _table_cell(str(item.get("qualified_name") or "")),
                        _table_cell(signature),
                    ]
                )
                + " |"
            )

    if requirement_coverage:
        lines.extend(
            [
                "",
                "## Requirement Coverage",
                "",
                "| Requirement | Content | Approved design targets | Planned code targets | Test intents |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in requirement_coverage:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _table_cell(str(item.get("requirement_id") or "")),
                        _table_cell(str(item.get("content") or "")),
                        _table_cell(", ".join(_string_items(item.get("design_targets")))),
                        _table_cell(", ".join(_string_items(item.get("planned_code_targets")))),
                        _table_cell("; ".join(_string_items(item.get("test_intents")))),
                    ]
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Verification Plan" if verification_stage_ids else "## Test Intent",
            "",
        ]
    )
    if verification_stage_ids:
        lines.append(
            "- Ordered profile stages: "
            + ", ".join(f"`{stage_id}`" for stage_id in verification_stage_ids)
            + "."
        )
    for item in test_intents:
        stage_id = str(item.get("stage_id") or "").strip()
        target_label = ", ".join(
            f"`{path}`" for path in _string_items(item.get("target_paths"))
        ) or str(item.get("scope") or "Unbound target")
        if stage_id:
            lines.append(f"- {target_label}: {item.get('intent')} Stage `{stage_id}`.")
        else:
            lines.append(f"- {target_label}: {item.get('intent')} Run `{item.get('command')}`.")

    lines.extend(["", "## Assumptions / Open Questions", ""])
    for item in assumptions:
        lines.append(f"- Assumption: {item}")
    for item in open_questions:
        lines.append(f"- Open question: {item}")

    lines.extend(["", "## Out Of Scope", ""])
    for item in out_of_scope:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Approved Requirement Summary",
            "",
            _excerpt(requirement_packet),
            "",
            "## Approved Design Summary",
            "",
            _excerpt(design_artifact),
            "",
        ]
    )
    return "\n".join(lines)


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _object_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _excerpt(content: str, *, limit: int = 1400) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n... truncated for implementation planning ..."
