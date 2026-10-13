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

    @property
    def issues(self) -> list[str]:
        return [
            *self.coverage_issues,
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
    ) -> ImplementationPlanReview:
        """Đối chiếu structured plan với graph, rồi kiểm ngữ nghĩa bằng LLM nếu có."""

        rule_review = self.review_rules(
            structured_plan=structured_plan,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
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
        ok = not coverage_issues and not traceability_issues and not executability_issues and not scope_issues
        return ImplementationPlanReview(
            ok=ok,
            mode="implementation_plan_rule_review_v1",
            coverage_issues=coverage_issues,
            traceability_issues=traceability_issues,
            executability_issues=executability_issues,
            scope_issues=scope_issues,
            summary="Rule review passed." if ok else "Rule review found blocking issues.",
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
        coverage or traceability or executability or scope or implementation_quality or test_issues
    )
    return ImplementationPlanReview(
        ok=ok,
        mode="implementation_plan_review_hybrid_v1",
        coverage_issues=coverage,
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
) -> str:
    return f"""Implementation Plan LLM Review v1

You must compare the implementation plan against the approved requirement and design.
The deterministic rule review already passed basic graph/schema checks. Your job is
to catch semantic gaps that rules cannot see.

Return JSON only with this shape:
{{
  "ok": true,
  "summary": "short review summary",
  "coverage_issues": [],
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
