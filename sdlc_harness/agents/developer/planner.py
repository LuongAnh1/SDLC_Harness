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
from ...core.errors import HarnessError
from ...llm.client import strip_code_fence
from ...profiles import (
    LanguagePromptDirective,
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PlanningPathPolicy,
    VerificationStagePolicy,
    build_implementation_signature_contract,
    get_language_profile,
    resolve_language_prompt_directive,
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
        raw = self.llm_client.complete(
            system=(
                "You are SDLC Harness Developer.plan. Return JSON only. "
                "Create a concrete implementation plan, but do not write code."
            ),
            prompt=prompt,
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
    ) -> ImplementationPlanDraft:
        """Tạo plan V1 từ graph đã duyệt để checkpoint không còn prose mơ hồ."""

        verification_policy = _resolve_verification_policy(
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        assert planning_policy is not None
        repo_context = repo_context or {}
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
    ) -> ImplementationPlanDraft:
        """Apply a repair patch to the previous candidate without rebuilding it from scratch."""

        verification_policy = _resolve_verification_policy(
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        assert planning_policy is not None
        previous = dict(previous_draft.structured)
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


def _merge_implementation_signature_contract(
    *,
    planning_policy: PlanningPathPolicy,
    mappings: list[dict[str, object]],
    class_graph: dict[str, object],
    previous: object,
    patch: object,
) -> dict[str, object]:
    """Merge a valid signature patch without dropping untouched prior entries."""

    previous_contract = build_implementation_signature_contract(
        planning_policy=planning_policy,
        mappings=mappings,
        class_graph=class_graph,
        candidate=previous,
    )
    patch_contract = build_implementation_signature_contract(
        planning_policy=planning_policy,
        mappings=mappings,
        class_graph=class_graph,
        candidate=patch,
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
    )


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
    proposed_files = _dict_items(structured.get("proposed_files"))
    repo_evidence = [str(item) for item in structured.get("repo_evidence", []) if str(item).strip()]
    assumptions = [str(item) for item in structured.get("assumptions", []) if str(item).strip()]
    open_questions = [str(item) for item in structured.get("open_questions", []) if str(item).strip()]
    out_of_scope = [str(item) for item in structured.get("out_of_scope", []) if str(item).strip()]
    signature_contract = _object_dict(structured.get("implementation_signature_contract"))
    signature_entries = _dict_items(signature_contract.get("entries"))
    version_text = str(structured.get("version") or "")
    plan_label = "V3" if version_text.endswith("_v3") else "V2" if version_text.endswith("_v2") else "V1"

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
        if stage_id:
            lines.append(f"- {item.get('scope')}: {item.get('intent')} Stage `{stage_id}`.")
        else:
            lines.append(f"- {item.get('scope')}: {item.get('intent')} Run `{item.get('command')}`.")

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
