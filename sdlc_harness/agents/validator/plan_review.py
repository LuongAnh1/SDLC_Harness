"""Validator.review_plan cho Phase 3 implementation plan checkpoint.

Reviewer này chạy theo hai lớp: rule hard gate kiểm graph/schema bằng code, rồi
LLM review kiểm semantic gap giữa requirement/design/trace và implementation
plan. Mục tiêu là chặn kế hoạch thiếu coverage, bịa design target hoặc chưa đủ
dữ kiện để Phase 4 sửa code an toàn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from ...core.config import phase3_llm_call_options
from ...core.errors import HarnessError
from ...llm.client import strip_code_fence
from ...profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    LanguagePromptDirective,
    PlanningPathPolicy,
    VerificationStagePolicy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
    profiled_plan_contract_issues,
    resolve_language_prompt_directive,
)


def _expected_design_elements(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
) -> dict[str, dict[str, str]]:
    expected: dict[str, dict[str, str]] = {}
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        expected[f"class:{class_name}"] = {"type": "class", "name": class_name}
        for method in _dict_items(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if method_name:
                full_name = f"{class_name}.{method_name}"
                expected[f"method:{full_name}"] = {"type": "method", "name": full_name}

    for diagram in _dict_items(sequence_graph.get("sequenceDiagrams")):
        diagram_id = str(diagram.get("id") or "sequence_diagram").strip()
        for index, message in enumerate(_dict_items(diagram.get("messages")), start=1):
            source = str(message.get("from") or "").strip()
            target = str(message.get("to") or "").strip()
            text = str(message.get("message") or "").strip()
            if text:
                name = f"{source}->{target}: {text}"
                expected[f"sequence:{diagram_id}:{index}"] = {
                    "type": "sequence_message",
                    "name": name,
                    "message_type": str(message.get("type") or "synchronous").strip().lower(),
                    "message": text,
                }
    return expected


def _traceability_issues(
    *,
    requirement_design_trace: dict[str, object],
    mapped_names: set[str],
    allowed_names: set[str],
) -> list[str]:
    issues: list[str] = []
    for trace_key in ("class_trace", "sequence_trace"):
        for item in _dict_items(requirement_design_trace.get(trace_key)):
            requirement = str(item.get("requirement") or "").strip()
            for link in _dict_items(item.get("links")):
                target = str(link.get("target") or "").strip()
                if not target:
                    continue
                if target in allowed_names and target not in mapped_names:
                    issues.append(f"Trace target `{target}` for {requirement} is not reflected in the plan.")
    return issues


def _requirement_coverage_issues(
    *,
    structured_plan: dict[str, object],
    requirement_graph: dict[str, object],
    requirement_design_trace: dict[str, object],
) -> list[str]:
    """Hard-gate the canonical V2 requirement coverage produced by Python."""

    issues: list[str] = []
    contract = structured_plan.get("requirement_contract")
    contract = contract if isinstance(contract, dict) else {}
    if contract.get("version") != "v2":
        issues.append("Structured plan requirement contract version must be v2.")
    if contract.get("revision") != requirement_graph.get("revision"):
        issues.append("Structured plan requirement revision does not match the canonical graph.")
    artifact_refs = contract.get("artifact_refs")
    artifact_refs = artifact_refs if isinstance(artifact_refs, dict) else {}
    for key in ("requirement_model", "requirement_packet", "formal_use_case"):
        if not str(artifact_refs.get(key) or "").strip():
            issues.append(f"Structured plan requirement contract is missing artifact ref: {key}")
    requirements = _dict_items(requirement_graph.get("requirements"))
    expected_ids = [str(item.get("id") or "").strip() for item in requirements]
    expected_ids = [item for item in expected_ids if item]
    expected_set = set(expected_ids)
    coverage = _dict_items(structured_plan.get("requirement_coverage"))
    actual_ids = [str(item.get("requirement_id") or "").strip() for item in coverage]
    actual_set = {item for item in actual_ids if item}

    if len(actual_ids) != len(actual_set):
        issues.append("Requirement coverage contains duplicate requirement IDs.")
    for requirement_id in sorted(expected_set - actual_set):
        issues.append(f"Missing requirement coverage for canonical ID: {requirement_id}")
    for requirement_id in sorted(actual_set - expected_set):
        issues.append(f"Requirement coverage invented unknown canonical ID: {requirement_id}")

    allowed_targets: dict[str, set[str]] = {requirement_id: set() for requirement_id in expected_set}
    for trace_key in ("class_trace", "sequence_trace"):
        for trace_item in _dict_items(requirement_design_trace.get(trace_key)):
            requirement_id = str(trace_item.get("requirement") or "").strip()
            if requirement_id not in allowed_targets:
                continue
            for link in _dict_items(trace_item.get("links")):
                target = str(link.get("target") or "").strip()
                if target:
                    allowed_targets[requirement_id].add(target)

    for item in coverage:
        requirement_id = str(item.get("requirement_id") or "").strip()
        if requirement_id not in expected_set:
            continue
        targets = set(_string_items(item.get("design_targets")))
        if targets != allowed_targets[requirement_id]:
            extra = sorted(targets - allowed_targets[requirement_id])
            missing = sorted(allowed_targets[requirement_id] - targets)
            if extra:
                issues.append(
                    f"Requirement {requirement_id} references design targets outside its approved trace: {extra}"
                )
            if missing:
                issues.append(f"Requirement {requirement_id} omits approved design targets: {missing}")
        if not _string_items(item.get("design_element_ids")):
            issues.append(f"Requirement {requirement_id} has no resolved design element IDs.")
        if not _string_items(item.get("planned_code_targets")):
            issues.append(f"Requirement {requirement_id} has no planned code target.")
        if not _string_items(item.get("test_intents")):
            issues.append(f"Requirement {requirement_id} has no test intent.")
    return issues


def _component_requirement_coverage_issues(
    *,
    structured_plan: dict[str, object],
    requirement_graph: dict[str, object],
) -> list[str]:
    """Validate exact canonical IDs while Vertical Trace owns V4 design identity."""

    issues: list[str] = []
    contract = structured_plan.get("requirement_contract")
    contract = contract if isinstance(contract, dict) else {}
    if contract.get("version") != "v2":
        issues.append("Structured plan requirement contract version must be v2.")
    if contract.get("revision") != requirement_graph.get("revision"):
        issues.append("Structured plan requirement revision does not match the canonical graph.")
    artifact_refs = contract.get("artifact_refs")
    artifact_refs = artifact_refs if isinstance(artifact_refs, dict) else {}
    for key in ("requirement_model", "requirement_packet", "formal_use_case"):
        if not str(artifact_refs.get(key) or "").strip():
            issues.append(f"Structured plan requirement contract is missing artifact ref: {key}")
    expected = {
        str(item.get("id") or "").strip(): str(item.get("content") or "").strip()
        for item in _dict_items(requirement_graph.get("requirements"))
        if str(item.get("id") or "").strip()
    }
    coverage = _dict_items(structured_plan.get("requirement_coverage"))
    actual_ids = [str(item.get("requirement_id") or "").strip() for item in coverage]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        issues.append("Component-aware plan must cover each canonical requirement ID exactly once.")
    allowed_design_ids = {
        str(item.get("design_element_id") or "")
        for item in _dict_items(structured_plan.get("design_mappings"))
        if str(item.get("design_element_id") or "")
    }
    for item in coverage:
        requirement_id = str(item.get("requirement_id") or "").strip()
        if requirement_id not in expected:
            continue
        if str(item.get("content") or "").strip() != expected[requirement_id]:
            issues.append(f"Requirement {requirement_id} coverage content is stale.")
        design_ids = set(_string_items(item.get("design_element_ids")))
        if not design_ids or not design_ids.issubset(allowed_design_ids):
            issues.append(f"Requirement {requirement_id} has missing or unapproved design element IDs.")
        if not _string_items(item.get("planned_code_targets")):
            issues.append(f"Requirement {requirement_id} has no planned code target.")
        if not _string_items(item.get("test_intents")):
            issues.append(f"Requirement {requirement_id} has no test intent.")
    return issues


def _executability_issues(
    *,
    structured_plan: dict[str, object],
    mappings: list[dict[str, Any]],
    class_graph: dict[str, object] | None = None,
    planning_policy: PlanningPathPolicy | None = None,
    verification_policy: VerificationStagePolicy | None = None,
) -> list[str]:
    issues: list[str] = []
    repo = structured_plan.get("target_repository")
    if not isinstance(repo, dict):
        issues.append("Target repository block is missing.")
        repo = {}
    for key in ("local_path", "remote", "base_branch", "planned_branch"):
        if not str(repo.get(key) or "").strip():
            issues.append(f"Target repository `{key}` is missing.")

    if planning_policy and not verification_policy:
        planning_profile = structured_plan.get("planning_profile")
        planning_profile = planning_profile if isinstance(planning_profile, dict) else {}
        if planning_profile != planning_policy.metadata():
            issues.append("Structured plan planning_profile does not match the frozen run profile.")

    if not mappings:
        issues.append("Implementation plan has no design mappings.")
    for item in mappings:
        target = str(item.get("planned_code_target") or "").strip()
        if not target:
            issues.append(f"Mapping `{item.get('design_element_id')}` has no planned code target.")
            continue
        path = target.split("#", 1)[0]
        normalized = _normalized_plan_path(path, planning_policy=planning_policy, mapping_only=True)
        if not normalized:
            issues.append(f"Planned code target is outside approved planning roots: {target}")
        elif planning_policy:
            component_id = str(item.get("component_id") or "").strip()
            expected_component = planning_policy.component_for_path(normalized, mapping_only=True)
            if component_id != expected_component:
                issues.append(
                    f"Mapping component does not match target path: {item.get('design_element_id')} "
                    f"expects `{expected_component}`, got `{component_id or 'missing'}`."
                )
        if not str(item.get("test_intent") or "").strip():
            issues.append(f"Mapping `{item.get('design_element_id')}` has no test intent.")

    test_intents = _dict_items(structured_plan.get("test_intents"))
    if verification_policy:
        if planning_policy is None:
            issues.append("Profiled implementation plan is missing its planning path policy.")
        else:
            issues.extend(
                profiled_plan_contract_issues(
                    structured_plan,
                    planning_policy=planning_policy,
                    verification_policy=verification_policy,
                    class_graph=class_graph,
                )
            )
    else:
        if not test_intents:
            issues.append("Implementation plan has no test intent block.")
        for item in test_intents:
            if not str(item.get("command") or "").strip():
                issues.append("A test intent is missing the command.")

    proposed = _dict_items(structured_plan.get("proposed_files"))
    proposed_paths = {
        _normalized_plan_path(item.get("path"), planning_policy=planning_policy)
        for item in proposed
    }
    proposed_paths.discard("")
    for item in proposed:
        path = _normalized_plan_path(item.get("path"), planning_policy=planning_policy)
        if not path:
            issues.append(f"Proposed file is outside approved planning roots: {item.get('path')}")
        elif planning_policy:
            component_id = str(item.get("component_id") or "").strip()
            expected_component = planning_policy.component_for_path(path)
            if component_id != expected_component:
                issues.append(
                    f"Proposed file component does not match its path: {path} "
                    f"expects `{expected_component}`, got `{component_id or 'missing'}`."
                )
    step_paths: set[str] = set()
    for step in _dict_items(structured_plan.get("implementation_steps")):
        raw_files = step.get("files") if isinstance(step.get("files"), list) else []
        for raw_path in raw_files:
            path = _normalized_plan_path(raw_path, planning_policy=planning_policy)
            if not path:
                issues.append(f"Implementation step file is outside approved planning roots: {raw_path}")
                continue
            step_paths.add(path)
    for path in sorted(step_paths - proposed_paths):
        issues.append(f"Implementation step file is missing from proposed_files: {path}")
    mapping_paths = {
        _normalized_plan_path(
            str(item.get("planned_code_target") or "").split("#", 1)[0],
            planning_policy=planning_policy,
            mapping_only=True,
        )
        for item in mappings
    }
    mapping_paths.discard("")
    for path in sorted(mapping_paths - proposed_paths):
        issues.append(f"Design mapping file is missing from proposed_files: {path}")

    class_targets = {
        str(item.get("design_element_name") or ""): str(item.get("planned_code_target") or "").split("#", 1)[0]
        for item in mappings
        if item.get("design_element_type") == "class"
    }
    if any(item.get("design_element_type") == "sequence_message" for item in mappings):
        controller = next(
            (
                (name, path)
                for name, path in class_targets.items()
                if name.endswith("Controller") or (planning_policy and name.endswith("Boundary"))
            ),
            None,
        )
        if controller:
            name, main_path = controller
            if planning_policy:
                test_stem = name.removesuffix("Controller").removesuffix("Boundary") + "Flow"
                expected_flow_test = planning_policy.test_path_for_source(main_path, test_stem)
            else:
                parent = main_path.rsplit("/", 1)[0].replace("src/main/java/", "src/test/java/", 1)
                expected_flow_test = f"{parent}/{name.removesuffix('Controller')}FlowTest.java"
            if expected_flow_test not in proposed_paths:
                issues.append(f"Approved sequence flow has no editable flow-test path: {expected_flow_test}")

    if planning_policy:
        repository = next(
            ((name, path) for name, path in class_targets.items() if name.endswith("Repository")),
            None,
        )
        if repository:
            name, main_path = repository
            expected_repository_test = planning_policy.test_path_for_source(main_path, name)
            if expected_repository_test not in proposed_paths:
                issues.append(
                    f"Approved repository has no editable test path: {expected_repository_test}"
                )

    for adjustment in _dict_items(structured_plan.get("design_adjustments")):
        if adjustment.get("action") == "unresolved" or adjustment.get("status") == "conflict":
            issues.append(
                f"Design/repository conflict is unresolved: {adjustment.get('design_element_id')}"
            )
    return issues


def _resolve_verification_policy(
    *,
    planning_policy: PlanningPathPolicy | None,
    verification_policy: VerificationStagePolicy | None,
) -> VerificationStagePolicy | None:
    if planning_policy and verification_policy is None:
        verification_policy = build_verification_stage_policy(
            project=get_project_profile(planning_policy.project_profile_id)
        )
    if (
        planning_policy
        and verification_policy
        and verification_policy.project_profile_id != planning_policy.project_profile_id
    ):
        raise HarnessError(
            "verification_stage_policy_mismatch",
            "Verification stages do not belong to the planning project profile.",
        )
    return verification_policy


def _mapping_contract_issues(
    *,
    mappings: list[dict[str, Any]],
    expected: dict[str, dict[str, str]],
    class_graph: dict[str, object],
    planning_policy: PlanningPathPolicy | None = None,
) -> list[str]:
    issues: list[str] = []
    ids = [str(item.get("design_element_id") or "").strip() for item in mappings]
    if len([item for item in ids if item]) != len(set(item for item in ids if item)):
        issues.append("Implementation plan contains duplicate design mapping IDs.")
    class_methods: dict[str, set[str]] = {}
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        class_methods[class_name] = {
            str(method.get("name") or "").strip()
            for method in _dict_items(node.get("methods"))
            if str(method.get("name") or "").strip()
        }
    allowed_policies = {
        "synchronous": {"direct_symbol", "boundary_interaction"},
        "return": {"direct_symbol", "return_behavior", "semantic_side_effect"},
        "lost_or_exception": {"exception_path"},
        "exception": {"exception_path"},
        "lost": {"exception_path"},
    }
    for mapping in mappings:
        element_id = str(mapping.get("design_element_id") or "").strip()
        expected_item = expected.get(element_id)
        if not expected_item:
            continue
        if str(mapping.get("design_element_type") or "") != expected_item["type"]:
            issues.append(f"Mapping type does not match approved graph: {element_id}")
        if str(mapping.get("design_element_name") or "") != expected_item["name"]:
            issues.append(f"Mapping name does not match approved graph: {element_id}")
        if expected_item["type"] == "method":
            if mapping.get("implementation_status") == "waived_by_design_adjustment":
                continue
            target = str(mapping.get("planned_code_target") or "").strip()
            if "#" not in target:
                issues.append(f"Method mapping does not target an approved method symbol: {element_id}")
            else:
                path, symbol = target.split("#", 1)
                owner = _symbol_owner(path, planning_policy=planning_policy)
                if symbol not in class_methods.get(owner, set()):
                    issues.append(f"Method target is not an approved class method: {target}")
            continue
        if expected_item["type"] != "sequence_message":
            continue
        message_type = str(mapping.get("message_type") or "").strip().lower()
        trace_policy = str(mapping.get("trace_policy") or "").strip()
        if message_type != expected_item.get("message_type"):
            issues.append(f"Sequence message type does not match approved graph: {element_id}")
        if trace_policy not in allowed_policies.get(message_type, set()):
            issues.append(f"Sequence trace policy `{trace_policy}` is invalid for {message_type}: {element_id}")
        target = str(mapping.get("planned_code_target") or "").strip()
        if "#" not in target or trace_policy != "direct_symbol":
            continue
        path, symbol = target.split("#", 1)
        owner = _symbol_owner(path, planning_policy=planning_policy)
        if symbol not in class_methods.get(owner, set()):
            issues.append(f"Direct sequence target is not an approved class method: {target}")
    return issues


def _normalized_java_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    return path if path.startswith(("src/main/java/", "src/test/java/")) and path.endswith(".java") else ""


def _normalized_plan_path(
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
    return _normalized_java_path(value)


def _symbol_owner(path: str, *, planning_policy: PlanningPathPolicy | None) -> str:
    if planning_policy:
        return planning_policy.symbol_owner(path)
    return path.rsplit("/", 1)[-1].removesuffix(".java")


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]

@dataclass(frozen=True)
class ImplementationPlanReview:
    """Quality report của Validator.review_plan."""

    ok: bool
    mode: str
    coverage_issues: list[str] = field(default_factory=list)
    requirement_coverage_issues: list[str] = field(default_factory=list)
    traceability_issues: list[str] = field(default_factory=list)
    executability_issues: list[str] = field(default_factory=list)
    scope_issues: list[str] = field(default_factory=list)
    implementation_quality_issues: list[str] = field(default_factory=list)
    test_issues: list[str] = field(default_factory=list)
    summary: str = ""
    developer_feedback: str = ""
    rule_review: dict[str, object] | None = None
    llm_review: dict[str, object] | None = None
    repair_iterations: int = 0
    requirement_contract_version: str = "v2"

    @property
    def issues(self) -> list[str]:
        return [
            *self.coverage_issues,
            *self.requirement_coverage_issues,
            *self.traceability_issues,
            *self.executability_issues,
            *self.scope_issues,
            *self.implementation_quality_issues,
            *self.test_issues,
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "coverage_issues": self.coverage_issues,
            "requirement_coverage_issues": self.requirement_coverage_issues,
            "traceability_issues": self.traceability_issues,
            "executability_issues": self.executability_issues,
            "scope_issues": self.scope_issues,
            "implementation_quality_issues": self.implementation_quality_issues,
            "test_issues": self.test_issues,
            "summary": self.summary,
            "developer_feedback": self.developer_feedback,
            "rule_review": self.rule_review or {},
            "llm_review": self.llm_review or {},
            "repair_iterations": self.repair_iterations,
            "requirement_contract_version": self.requirement_contract_version,
            "issue_count": len(self.issues),
        }


class ImplementationPlanValidator:
    """Kiểm plan bằng rule hard gate và LLM semantic review."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def review(
        self,
        *,
        structured_plan: dict[str, object],
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        requirement_packet: str = "",
        design_artifact: str = "",
        llm_client: Any | None = None,
        repair_iterations: int = 0,
        requirement_graph: dict[str, object] | None = None,
        requirement_contract_version: str = "v2",
        canonical_requirement: dict[str, object] | None = None,
        planning_policy: PlanningPathPolicy | None = None,
        prompt_directive: LanguagePromptDirective | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        vertical_design_graph: dict[str, object] | None = None,
        vertical_trace_graph: dict[str, object] | None = None,
        repo_landscape: dict[str, object] | None = None,
        component_plan_inputs: dict[str, object] | None = None,
    ) -> ImplementationPlanReview:
        """Đối chiếu structured plan với graph, rồi kiểm ngữ nghĩa bằng LLM nếu có."""

        rule_review = self.review_rules(
            structured_plan=structured_plan,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            requirement_graph=requirement_graph,
            requirement_contract_version=requirement_contract_version,
            planning_policy=planning_policy,
            verification_policy=verification_policy,
            vertical_design_graph=vertical_design_graph,
            vertical_trace_graph=vertical_trace_graph,
            repo_landscape=repo_landscape,
            component_plan_inputs=component_plan_inputs,
        )
        client = llm_client or self.llm_client
        if not client or not requirement_packet.strip() or not design_artifact.strip():
            return rule_review
        if not rule_review.ok:
            return _hybrid_review(
                rule_review=rule_review,
                llm_payload={"skipped": True, "reason": "rule_review_failed"},
                repair_iterations=repair_iterations,
            )

        llm_payload = self.review_llm(
            structured_plan=structured_plan,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            rule_review=rule_review,
            llm_client=client,
            canonical_requirement=canonical_requirement,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
            vertical_design_graph=vertical_design_graph,
            vertical_trace_graph=vertical_trace_graph,
        )
        return _hybrid_review(
            rule_review=rule_review,
            llm_payload=llm_payload,
            repair_iterations=repair_iterations,
        )

    def review_rules(
        self,
        *,
        structured_plan: dict[str, object],
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        requirement_graph: dict[str, object] | None = None,
        requirement_contract_version: str = "v2",
        planning_policy: PlanningPathPolicy | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        vertical_design_graph: dict[str, object] | None = None,
        vertical_trace_graph: dict[str, object] | None = None,
        repo_landscape: dict[str, object] | None = None,
        component_plan_inputs: dict[str, object] | None = None,
    ) -> ImplementationPlanReview:
        """Hard gate: kiểm coverage/traceability/executability/scope bằng graph."""

        verification_policy = _resolve_verification_policy(
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        if structured_plan.get("version") == COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION:
            requirement_coverage_issues = (
                _component_requirement_coverage_issues(
                    structured_plan=structured_plan,
                    requirement_graph=requirement_graph or {},
                )
                if requirement_contract_version == "v2"
                else []
            )
            if planning_policy is None or verification_policy is None:
                issues = ["Component-aware plan requires frozen planning and verification policies."]
            else:
                from ..developer.component_plan import (
                    build_component_work_units,
                    component_plan_issues,
                )

                expected_units = build_component_work_units(
                    design_graph=vertical_design_graph or {},
                    trace_graph=vertical_trace_graph or {},
                    repo_landscape=repo_landscape or {},
                    planning_policy=planning_policy,
                )
                issues = component_plan_issues(
                    structured_plan=structured_plan,
                    expected_units=expected_units,
                    expected_inputs=component_plan_inputs or {},
                    planning_policy=planning_policy,
                    verification_stage_ids=list(verification_policy.stage_ids),
                    design_graph=vertical_design_graph or {},
                    trace_graph=vertical_trace_graph or {},
                    repo_landscape=repo_landscape or {},
                )
                issues.extend(
                    profiled_plan_contract_issues(
                        structured_plan,
                        planning_policy=planning_policy,
                        verification_policy=verification_policy,
                        class_graph=class_graph,
                    )
                )
            repo = structured_plan.get("target_repository")
            if not isinstance(repo, dict):
                issues.append("Target repository block is missing.")
            else:
                for key in ("local_path", "remote", "base_branch", "planned_branch"):
                    if not str(repo.get(key) or "").strip():
                        issues.append(f"Target repository `{key}` is missing.")
            issues = list(dict.fromkeys(issues))
            ok = not issues and not requirement_coverage_issues
            return ImplementationPlanReview(
                ok=ok,
                mode="implementation_plan_rule_review_v4",
                requirement_coverage_issues=requirement_coverage_issues,
                executability_issues=issues,
                summary="Rule review passed." if ok else "Rule review found blocking issues.",
                requirement_contract_version=requirement_contract_version,
            )
        expected = _expected_design_elements(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
        )
        mappings = _dict_items(structured_plan.get("design_mappings"))
        mapped_ids = {str(item.get("design_element_id") or "") for item in mappings}
        mapped_names = {str(item.get("design_element_name") or "") for item in mappings}
        allowed_ids = set(expected)
        allowed_names = {item["name"] for item in expected.values()}

        coverage_issues = [
            f"Missing implementation mapping for design element: {item['name']} ({element_id})"
            for element_id, item in expected.items()
            if element_id not in mapped_ids
        ]
        traceability_issues = _traceability_issues(
            requirement_design_trace=requirement_design_trace,
            mapped_names=mapped_names,
            allowed_names=allowed_names,
        )
        executability_issues = _executability_issues(
            structured_plan=structured_plan,
            mappings=mappings,
            class_graph=class_graph,
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
        executability_issues.extend(
            _mapping_contract_issues(
                mappings=mappings,
                expected=expected,
                class_graph=class_graph,
                planning_policy=planning_policy,
            )
        )
        scope_issues = [
            f"Plan references design element outside approved graph: {element_id}"
            for element_id in sorted(mapped_ids)
            if element_id and element_id not in allowed_ids
        ]
        requirement_coverage_issues = (
            _requirement_coverage_issues(
                structured_plan=structured_plan,
                requirement_graph=requirement_graph or {},
                requirement_design_trace=requirement_design_trace,
            )
            if requirement_contract_version == "v2"
            else []
        )
        ok = not (
            coverage_issues
            or requirement_coverage_issues
            or traceability_issues
            or executability_issues
            or scope_issues
        )
        return ImplementationPlanReview(
            ok=ok,
            mode=f"implementation_plan_rule_review_{requirement_contract_version}",
            coverage_issues=coverage_issues,
            requirement_coverage_issues=requirement_coverage_issues,
            traceability_issues=traceability_issues,
            executability_issues=executability_issues,
            scope_issues=scope_issues,
            summary="Rule review passed." if ok else "Rule review found blocking issues.",
            requirement_contract_version=requirement_contract_version,
        )

    def review_llm(
        self,
        *,
        structured_plan: dict[str, object],
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        rule_review: ImplementationPlanReview,
        llm_client: Any | None = None,
        canonical_requirement: dict[str, object] | None = None,
        planning_policy: PlanningPathPolicy | None = None,
        prompt_directive: LanguagePromptDirective | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        vertical_design_graph: dict[str, object] | None = None,
        vertical_trace_graph: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Nhờ LLM đánh giá plan theo ngữ nghĩa requirement/design đã duyệt."""

        client = llm_client or self.llm_client
        if not client:
            raise HarnessError(
                "implementation_plan_llm_review_failed",
                "LLM client is required for implementation plan content review.",
            )
        prompt = _llm_review_prompt(
            structured_plan=structured_plan,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            rule_review=rule_review,
            canonical_requirement=canonical_requirement,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
            vertical_design_graph=vertical_design_graph,
            vertical_trace_graph=vertical_trace_graph,
        )
        system = (
            "You are SDLC Harness Validator.review_plan. Return JSON only. "
            "Be strict about requirement/design coverage, implementation clarity, and tests."
        )
        complete_with_metadata = getattr(client, "complete_with_metadata", None)
        if callable(complete_with_metadata):
            settings = getattr(client, "settings", None)
            fallback_max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
            completion = complete_with_metadata(
                system=system,
                prompt=prompt,
                json_mode=True,
                **phase3_llm_call_options(
                    settings,
                    "plan_validator",
                    fallback_max_tokens=fallback_max_tokens,
                ),
            )
            raw = str(completion.content)
        else:
            raw = client.complete(system=system, prompt=prompt)
        try:
            return _parse_llm_review_payload(raw)
        except HarnessError as exc:
            return {
                "ok": False,
                "summary": "Implementation plan LLM review did not return parseable JSON.",
                "coverage_issues": [],
                "requirement_coverage_issues": [],
                "traceability_issues": [],
                "implementation_quality_issues": [exc.message],
                "test_issues": [],
                "scope_issues": [],
                "developer_feedback": (
                    "Rewrite the implementation plan with clearer implementation steps, code targets, "
                    "and test intents so the next review can evaluate it."
                ),
                "parse_error": exc.message,
            }


def _hybrid_review(
    *,
    rule_review: ImplementationPlanReview,
    llm_payload: dict[str, object],
    repair_iterations: int,
) -> ImplementationPlanReview:
    coverage = [
        *rule_review.coverage_issues,
        *_string_items(llm_payload.get("coverage_issues")),
    ]
    requirement_coverage = [
        *rule_review.requirement_coverage_issues,
        *_string_items(llm_payload.get("requirement_coverage_issues")),
    ]
    traceability = [
        *rule_review.traceability_issues,
        *_string_items(llm_payload.get("traceability_issues")),
    ]
    executability = list(rule_review.executability_issues)
    scope = [
        *rule_review.scope_issues,
        *_string_items(llm_payload.get("scope_issues")),
    ]
    implementation_quality = _string_items(llm_payload.get("implementation_quality_issues"))
    test_issues = _string_items(llm_payload.get("test_issues"))
    skipped = bool(llm_payload.get("skipped"))
    llm_ok = bool(llm_payload.get("ok")) if not skipped else False
    ok = rule_review.ok and llm_ok and not (
        coverage
        or requirement_coverage
        or traceability
        or executability
        or scope
        or implementation_quality
        or test_issues
    )
    return ImplementationPlanReview(
        ok=ok,
        mode=f"implementation_plan_review_hybrid_{rule_review.requirement_contract_version}",
        coverage_issues=coverage,
        requirement_coverage_issues=requirement_coverage,
        traceability_issues=traceability,
        executability_issues=executability,
        scope_issues=scope,
        implementation_quality_issues=implementation_quality,
        test_issues=test_issues,
        summary=str(llm_payload.get("summary") or rule_review.summary).strip(),
        developer_feedback=str(llm_payload.get("developer_feedback") or "").strip(),
        rule_review=rule_review.to_dict(),
        llm_review=llm_payload,
        repair_iterations=repair_iterations,
        requirement_contract_version=rule_review.requirement_contract_version,
    )


def _llm_review_prompt(
    *,
    structured_plan: dict[str, object],
    requirement_packet: str,
    design_artifact: str,
    requirement_design_trace: dict[str, object],
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    rule_review: ImplementationPlanReview,
    canonical_requirement: dict[str, object] | None = None,
    planning_policy: PlanningPathPolicy | None = None,
    prompt_directive: LanguagePromptDirective | None = None,
    verification_policy: VerificationStagePolicy | None = None,
    vertical_design_graph: dict[str, object] | None = None,
    vertical_trace_graph: dict[str, object] | None = None,
) -> str:
    verification_policy = _resolve_verification_policy(
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )
    canonical_section = (
        "Canonical Requirement V2 (source of truth; stable IDs are authoritative):\n"
        + json.dumps(canonical_requirement, ensure_ascii=False, indent=2)
        if canonical_requirement
        else "Canonical Requirement V2: not enabled for this V1 run."
    )
    compact_plan = {
        key: value
        for key, value in structured_plan.items()
        if key not in {"requirement_coverage", "repair_history", "repo_context"}
    }
    compact_trace = {
        key: requirement_design_trace.get(key, [])
        for key in ("version", "class_trace", "sequence_trace")
    }
    vertical_context = {
        "design": {
            key: (vertical_design_graph or {}).get(key)
            for key in ("schema_version", "artifact_digest", "nodes", "edges")
        },
        "trace": {
            key: (vertical_trace_graph or {}).get(key)
            for key in ("schema_version", "artifact_digest", "nodes", "edges")
        },
    }
    if planning_policy:
        if prompt_directive is None:
            prompt_directive = resolve_language_prompt_directive(
                get_language_profile(planning_policy.language_profile_id)
            )
        if prompt_directive.language_profile_id != planning_policy.language_profile_id:
            raise HarnessError(
                "prompt_directive_mismatch",
                "Phase 3 review requires a prompt directive matching the planning language profile.",
            )
        language_context = prompt_directive.validator_review_context()
    else:
        language_context = {"prompt_directive_id": "legacy_compatibility"}
    verification_context = (
        verification_policy.prompt_context()
        if verification_policy
        else {"policy_version": "legacy_command_compatibility_v1"}
    )
    v4_boundaries = (
        """
Component-aware V4 boundaries:
- Vertical Design nodes define the complete approved work-unit/component scope.
  Do not invent a new business component or work unit. Supporting artifacts in an
  existing repository component are allowed when repo evidence makes them necessary
  for an executable approved flow.
- Work-unit identity, membership, dependency, and order are Harness-owned. Do not
  ask Developer.plan to change them. Review only Planner-owned objectives, targets,
  migration decisions, tests, evidence, and signature content.
- Compatibility fields are projections, not a second plan. Fail them only when they
  contradict canonical work-unit targets or signatures; feedback must be repairable
  through Planner-owned fields.
- A `unit` intent targets a test file. A `typecheck` intent targets a non-test source
  file already owned by the same work unit.
- The legacy Class graph supplies signature trace identity, not callable type authority.
  Require parameters and returns that make the approved Vertical Design behavior
  callable and testable; reject placeholder-only `(): void` contracts when the flow
  accepts input, returns data/status, persists identity, or renders UI.
- When repo evidence shows an existing shared-contract, client-adapter, or migration
  convention, require the plan to use it if otherwise Web/API/data boundaries would
  have to invent incompatible shapes. Attach supporting files to an approved unit;
  require upstream Design change only when no approved unit can own them.
- For every UI surface, verify that the objective, exact code targets, and test intents
  cover every non-empty experience-contract category: component structure, feedback,
  accessibility, responsive behavior, and focus flow. CSS class names without owned or
  verified style rules do not implement responsive behavior; static markup alone does not
  verify interaction or focus navigation.
- Verify that required boundary auth/input mappings and runtime attachments have an explicit
  owner/caller target. A dependency ID without its component, surfaces, and produced artifacts
  is not sufficient evidence that downstream wiring is executable.
- A required migration needs a concrete migration artifact, not only a schema file.
  Only approved Requirement/Vertical Design behavior and established repo conventions
  may create a blocker.
""".strip()
        if structured_plan.get("version") == COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
        else ""
    )
    return f"""Implementation Plan LLM Review v1

You must compare the implementation plan against the approved requirement and design.
The deterministic rule review already passed basic graph/schema checks. Your job is
to catch semantic gaps that rules cannot see.

Return JSON only with this shape:
{{
  "ok": true,
  "summary": "short review summary",
  "coverage_issues": [],
  "requirement_coverage_issues": [],
  "traceability_issues": [],
  "implementation_quality_issues": [],
  "test_issues": [],
  "scope_issues": [],
  "developer_feedback": "actionable feedback for Developer.plan if repair is needed"
}}

Fail the review when:
- the plan does not explain how approved design elements will become code;
- the plan drops important requirement/design behavior;
- the test plan is too vague for Phase 4 to verify the use case;
- the plan adds out-of-scope behavior not present in the approved design.

Review boundaries:
- An untraced `design_adjustment` with action `omit`, a waived mapping, and
  `requires_human_approval=true` is a proposal for the current human plan checkpoint.
  Treat it as internally resolved when steps do not implement the omitted element;
  the next human approval freezes it into the effective design contract.
- Judge concurrency, transaction, rollback, authentication, and infrastructure only
  when the approved requirement/design explicitly defines those semantics. Do not
  invent a broader system contract as a reason to reject the plan.

{v4_boundaries}

Language prompt directive:
{json.dumps(language_context, ensure_ascii=False, indent=2)}

Verification stage policy:
{json.dumps(verification_context, ensure_ascii=False, indent=2)}

Approved requirement packet:
{_excerpt(requirement_packet, limit=6000)}

{canonical_section}

Approved design artifact:
{_excerpt(design_artifact, limit=6000)}

Requirement-design trace JSON:
{json.dumps(compact_trace, ensure_ascii=False, separators=(",", ":"))}

Vertical Design/Trace JSON (authoritative for V4 plans):
{json.dumps(vertical_context, ensure_ascii=False, separators=(",", ":"))}

Class graph JSON:
{json.dumps(class_graph, ensure_ascii=False, separators=(",", ":"))}

Sequence graph JSON:
{json.dumps(sequence_graph, ensure_ascii=False, separators=(",", ":"))}

Structured implementation plan JSON:
{json.dumps(compact_plan, ensure_ascii=False, separators=(",", ":"))}

Rule review JSON:
{json.dumps(rule_review.to_dict(), ensure_ascii=False, indent=2)}
""".strip()


def _parse_llm_review_payload(raw: str) -> dict[str, object]:
    try:
        parsed = _parse_first_json_object(raw, expected_keys={"ok", "summary", "developer_feedback"})
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "implementation_plan_llm_review_failed",
            "Implementation plan LLM review did not return valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError(
            "implementation_plan_llm_review_failed",
            "Implementation plan LLM review must return a JSON object.",
        )
    payload = {
        "ok": parsed.get("ok") if isinstance(parsed.get("ok"), bool) else False,
        "summary": str(parsed.get("summary") or "").strip(),
        "coverage_issues": _string_items(parsed.get("coverage_issues")),
        "requirement_coverage_issues": _string_items(parsed.get("requirement_coverage_issues")),
        "traceability_issues": _string_items(parsed.get("traceability_issues")),
        "implementation_quality_issues": _string_items(parsed.get("implementation_quality_issues")),
        "test_issues": _string_items(parsed.get("test_issues")),
        "scope_issues": _string_items(parsed.get("scope_issues")),
        "developer_feedback": str(parsed.get("developer_feedback") or "").strip(),
    }
    if payload["ok"] and any(
        payload[key]
        for key in (
            "coverage_issues",
            "requirement_coverage_issues",
            "traceability_issues",
            "implementation_quality_issues",
            "test_issues",
            "scope_issues",
        )
    ):
        payload["ok"] = False
    if not payload["ok"] and not any(
        payload[key]
        for key in (
            "coverage_issues",
            "requirement_coverage_issues",
            "traceability_issues",
            "implementation_quality_issues",
            "test_issues",
            "scope_issues",
        )
    ):
        payload["implementation_quality_issues"] = ["LLM review rejected the plan without issue details."]
    if not payload["summary"]:
        payload["summary"] = "LLM review did not provide a summary."
    return payload


def _parse_first_json_object(raw: str, *, expected_keys: set[str]) -> dict[str, object]:
    """Parse JSON object đúng schema mong muốn trong output LLM có thể lẫn prose."""

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


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _excerpt(content: str, *, limit: int) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n... truncated for implementation plan review ..."
