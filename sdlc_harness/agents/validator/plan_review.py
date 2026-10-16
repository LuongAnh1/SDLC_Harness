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

from ...core.errors import HarnessError
from ...llm.client import strip_code_fence


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
                expected[f"sequence:{diagram_id}:{index}"] = {"type": "sequence_message", "name": name}
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


def _executability_issues(
    *,
    structured_plan: dict[str, object],
    mappings: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    repo = structured_plan.get("target_repository")
    if not isinstance(repo, dict):
        issues.append("Target repository block is missing.")
        repo = {}
    for key in ("local_path", "remote", "base_branch", "planned_branch"):
        if not str(repo.get(key) or "").strip():
            issues.append(f"Target repository `{key}` is missing.")

    if not mappings:
        issues.append("Implementation plan has no design mappings.")
    for item in mappings:
        target = str(item.get("planned_code_target") or "").strip()
        if not target:
            issues.append(f"Mapping `{item.get('design_element_id')}` has no planned code target.")
            continue
        path = target.split("#", 1)[0]
        if not path.startswith(("src/main/java/", "src/test/java/")):
            issues.append(f"Planned code target is outside Java source/test roots: {target}")
        if not str(item.get("test_intent") or "").strip():
            issues.append(f"Mapping `{item.get('design_element_id')}` has no test intent.")

    test_intents = _dict_items(structured_plan.get("test_intents"))
    if not test_intents:
        issues.append("Implementation plan has no test intent block.")
    for item in test_intents:
        if not str(item.get("command") or "").strip():
            issues.append("A test intent is missing the command.")
    return issues


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
    requirement_contract_version: str = "v1"

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
        requirement_contract_version: str = "v1",
        canonical_requirement: dict[str, object] | None = None,
    ) -> ImplementationPlanReview:
        """Đối chiếu structured plan với graph, rồi kiểm ngữ nghĩa bằng LLM nếu có."""

        rule_review = self.review_rules(
            structured_plan=structured_plan,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            requirement_graph=requirement_graph,
            requirement_contract_version=requirement_contract_version,
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
        requirement_contract_version: str = "v1",
    ) -> ImplementationPlanReview:
        """Hard gate: kiểm coverage/traceability/executability/scope bằng graph."""

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
        )
        raw = client.complete(
            system=(
                "You are SDLC Harness Validator.review_plan. Return JSON only. "
                "Be strict about requirement/design coverage, implementation clarity, and tests."
            ),
            prompt=prompt,
        )
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
) -> str:
    canonical_section = (
        "Canonical Requirement V2 (source of truth; stable IDs are authoritative):\n"
        + json.dumps(canonical_requirement, ensure_ascii=False, indent=2)
        if canonical_requirement
        else "Canonical Requirement V2: not enabled for this V1 run."
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

Approved requirement packet:
{_excerpt(requirement_packet, limit=6000)}

{canonical_section}

Approved design artifact:
{_excerpt(design_artifact, limit=6000)}

Requirement-design trace JSON:
{json.dumps(requirement_design_trace, ensure_ascii=False, indent=2)}

Class graph JSON:
{json.dumps(class_graph, ensure_ascii=False, indent=2)}

Sequence graph JSON:
{json.dumps(sequence_graph, ensure_ascii=False, indent=2)}

Structured implementation plan JSON:
{json.dumps(structured_plan, ensure_ascii=False, indent=2)}

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
        "ok": bool(parsed.get("ok")),
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
    return bool(expected_keys.intersection(candidate.keys()))


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _excerpt(content: str, *, limit: int) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n... truncated for implementation plan review ..."
