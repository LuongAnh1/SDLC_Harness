"""Validator traceability cho Phase 2.

Module này là lát đầu của Validator theo tinh thần TraceDev: requirement đã
duyệt được chuyển thành graph máy đọc được, class/sequence graph từ Designer
được dùng làm miền target hợp lệ, rồi LLM chỉ được phép tạo link vào các target
đã tồn tại. Repair loop sẽ dùng missing links ở lát sau.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable

from ..requirements import RequirementModelV2
from ...core.errors import HarnessError
from ...llm.client import LlmClient, LlmCompletion, strip_code_fence

_PROMPT_DIR = Path(__file__).with_name("prompts")
_CLASS_TRACE_PROMPT_PATH = _PROMPT_DIR / "traceability_class.txt"
_SEQUENCE_TRACE_PROMPT_PATH = _PROMPT_DIR / "traceability_sequence.txt"

_REQUIREMENT_SECTIONS = {
    "Preconditions": "PRE",
    "Main Flow": "MAIN",
    "Alternative / Exception Flows": "ALT",
    "Postconditions": "POST",
    "Acceptance Criteria": "AC",
    "Quality / Non-functional Requirements": "NFR",
}


@dataclass(frozen=True)
class TraceabilityResult:
    """Kết quả traceability đã normalize và đã tính missing links."""

    requirement_graph: dict[str, object]
    class_trace: list[dict[str, object]]
    sequence_trace: list[dict[str, object]]
    missing_links: dict[str, list[dict[str, str]]]
    mode: str = "traceability_v1"

    @property
    def ok(self) -> bool:
        return not self.missing_links["class_diagram"] and not self.missing_links["sequence_diagram"]


class ValidatorAgent:
    """Vai Validator tạo trace requirement-design ở Phase 2."""

    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def construct_traceability(
        self,
        *,
        requirement_packet: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        canonical_requirement: RequirementModelV2 | None = None,
        attempt_recorder: Callable[[dict[str, object]], None] | None = None,
    ) -> TraceabilityResult:
        """Tạo requirement graph, trace class/sequence và missing-link report."""

        requirement_graph = (
            build_requirement_graph_v2(canonical_requirement)
            if canonical_requirement is not None
            else build_requirement_graph(requirement_packet)
        )
        requirements = requirement_graph["requirements"]
        requirement_json = json.dumps(requirements, ensure_ascii=False, indent=2)
        requirement_ids = {str(item["id"]) for item in requirements}

        class_trace = self._trace_class(
            requirement_json=requirement_json,
            class_graph=class_graph,
            requirement_ids=requirement_ids,
            attempt_recorder=attempt_recorder,
        )
        sequence_trace = self._trace_sequence(
            requirement_json=requirement_json,
            sequence_graph=sequence_graph,
            requirement_ids=requirement_ids,
            attempt_recorder=attempt_recorder,
        )

        class_trace = _normalize_trace(
            trace=class_trace,
            requirement_graph=requirement_graph,
            allowed_targets=_class_targets(class_graph),
            diagram_name="class diagram",
        )
        sequence_trace = _normalize_trace(
            trace=sequence_trace,
            requirement_graph=requirement_graph,
            allowed_targets=_sequence_targets(sequence_graph),
            diagram_name="sequence diagram",
        )
        missing_links = compute_missing_links(
            requirement_graph=requirement_graph,
            class_trace=class_trace,
            sequence_trace=sequence_trace,
        )
        return TraceabilityResult(
            requirement_graph=requirement_graph,
            class_trace=class_trace,
            sequence_trace=sequence_trace,
            missing_links=missing_links,
            mode="traceability_v2" if canonical_requirement is not None else "traceability_v1",
        )

    def _trace_class(
        self,
        *,
        requirement_json: str,
        class_graph: dict[str, object],
        requirement_ids: set[str],
        attempt_recorder: Callable[[dict[str, object]], None] | None,
    ) -> list[dict[str, object]]:
        prompt = _CLASS_TRACE_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{requirement}", requirement_json)
        prompt = prompt.replace("{class_diagram_json}", json.dumps(class_graph, ensure_ascii=False, indent=2))
        return self._generate_trace(
            diagram_name="class diagram",
            system="You are Validator.traceability_class. Return exactly one JSON object.",
            prompt=prompt,
            requirement_ids=requirement_ids,
            attempt_recorder=attempt_recorder,
        )

    def _trace_sequence(
        self,
        *,
        requirement_json: str,
        sequence_graph: dict[str, object],
        requirement_ids: set[str],
        attempt_recorder: Callable[[dict[str, object]], None] | None,
    ) -> list[dict[str, object]]:
        prompt = _SEQUENCE_TRACE_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{requirement}", requirement_json)
        prompt = prompt.replace("{sequence_diagram_json}", json.dumps(sequence_graph, ensure_ascii=False, indent=2))
        return self._generate_trace(
            diagram_name="sequence diagram",
            system="You are Validator.traceability_sequence. Return exactly one JSON object.",
            prompt=prompt,
            requirement_ids=requirement_ids,
            attempt_recorder=attempt_recorder,
        )

    def _generate_trace(
        self,
        *,
        diagram_name: str,
        system: str,
        prompt: str,
        requirement_ids: set[str],
        attempt_recorder: Callable[[dict[str, object]], None] | None,
    ) -> list[dict[str, object]]:
        """Generate strict structured traceability and retry one malformed response."""

        last_error: HarnessError | None = None
        for attempt in (1, 2):
            attempt_prompt = prompt
            if last_error is not None:
                attempt_prompt = (
                    f"{prompt.rstrip()}\n\n"
                    "## Contract recovery\n"
                    f"The previous response was rejected: {last_error.message}\n"
                    "Return one complete JSON object with top-level key `traceability`. "
                    "Every item must contain a non-empty requirement ID copied from the input and a links array. "
                    "Keep each reason to one short sentence and include only directly supporting targets."
                )
            completion = _complete_trace_request(
                self.llm_client,
                system=system,
                prompt=attempt_prompt,
            )
            try:
                if completion.finish_reason.strip().lower() == "length":
                    raise HarnessError(
                        "validator_traceability_failed",
                        f"Validator {diagram_name} trace output was truncated by the model output limit.",
                    )
                parsed = _parse_trace_json(
                    completion.content,
                    diagram_name=diagram_name,
                    expected_requirement_ids=requirement_ids,
                )
            except HarnessError as exc:
                last_error = exc
                _record_trace_attempt(
                    attempt_recorder,
                    diagram_name=diagram_name,
                    attempt=attempt,
                    completion=completion,
                    parse_status="rejected",
                    error=exc.message,
                )
                continue
            _record_trace_attempt(
                attempt_recorder,
                diagram_name=diagram_name,
                attempt=attempt,
                completion=completion,
                parse_status="parsed",
                error="",
            )
            return parsed

        assert last_error is not None
        raise HarnessError(
            "validator_traceability_failed",
            f"Validator {diagram_name} trace failed its JSON contract after 2 attempts: {last_error.message}",
        )


def _complete_trace_request(client: Any, *, system: str, prompt: str) -> LlmCompletion:
    complete_with_metadata = getattr(client, "complete_with_metadata", None)
    if callable(complete_with_metadata):
        settings = getattr(client, "settings", None)
        max_tokens = getattr(settings, "llm_structured_max_tokens", None)
        return complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=True,
            max_tokens=max_tokens,
        )
    return LlmCompletion(content=str(client.complete(system=system, prompt=prompt)))


def _record_trace_attempt(
    recorder: Callable[[dict[str, object]], None] | None,
    *,
    diagram_name: str,
    attempt: int,
    completion: LlmCompletion,
    parse_status: str,
    error: str,
) -> None:
    if recorder is None:
        return
    recorder(
        {
            "diagram": diagram_name,
            "attempt": attempt,
            "raw_response": completion.content,
            "finish_reason": completion.finish_reason,
            "usage": completion.usage,
            "parse_status": parse_status,
            "error": error,
        }
    )


def build_requirement_graph(requirement_packet: str) -> dict[str, object]:
    """Chuyển requirement packet markdown thành requirement graph tối thiểu."""

    sections = _extract_sections(requirement_packet)
    requirements: list[dict[str, str]] = []
    for section, prefix in _REQUIREMENT_SECTIONS.items():
        items = _section_items(sections.get(section, ""))
        for index, item in enumerate(items, start=1):
            requirements.append(
                {
                    "id": f"REQ-{prefix}-{index:03d}",
                    "section": section,
                    "content": item,
                }
            )

    if not requirements:
        raise HarnessError(
            "validator_traceability_failed",
            "Requirement packet did not contain traceable requirement items.",
        )

    return {
        "version": "requirement_graph_v1",
        "requirements": requirements,
        "count": len(requirements),
    }


def build_requirement_graph_v2(model: RequirementModelV2) -> dict[str, object]:
    """Build traceability input from canonical V2 items while preserving stable IDs."""

    requirements: list[dict[str, str]] = []
    confirmed_statuses = {"explicit", "human_decision"}
    for section, items in (("Preconditions", model.preconditions),):
        for item in items:
            if item.get("status") not in confirmed_statuses:
                continue
            _append_v2_requirement(requirements, section=section, item=item, content=str(item.get("text") or ""))

    flows = model.flows
    for item in flows.get("main", []):
        if not isinstance(item, dict):
            continue
        content = " ".join(
            part
            for part in (
                str(item.get("actor") or ""),
                str(item.get("action") or ""),
                str(item.get("target") or ""),
            )
            if part
        )
        _append_v2_requirement(requirements, section="Main Flow", item=item, content=content)
    for group, section in (("alternatives", "Alternative Flows"), ("exceptions", "Exception Flows")):
        for item in flows.get(group, []):
            if not isinstance(item, dict):
                continue
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            content = " ".join(
                part
                for part in (
                    str(item.get("trigger") or ""),
                    " ".join(str(step) for step in steps),
                    str(item.get("outcome") or ""),
                )
                if part
            )
            _append_v2_requirement(requirements, section=section, item=item, content=content)

    for section, items in (
        ("Postconditions", model.postconditions),
        ("Business Rules", model.business_rules),
        ("Acceptance Criteria", model.acceptance_criteria),
        ("Quality Requirements", model.quality_requirements),
    ):
        for item in items:
            if item.get("status") not in confirmed_statuses:
                continue
            _append_v2_requirement(requirements, section=section, item=item, content=str(item.get("text") or ""))

    if not requirements:
        raise HarnessError(
            "validator_traceability_failed",
            "Canonical Requirement V2 did not contain confirmed traceable requirement items.",
            run_id=model.run_id,
        )
    return {
        "version": "requirement_graph_v2",
        "requirement_contract_version": "v2",
        "schema_version": model.schema_version,
        "revision": model.revision,
        "requirements": requirements,
        "count": len(requirements),
    }


def _append_v2_requirement(
    requirements: list[dict[str, str]],
    *,
    section: str,
    item: dict[str, Any],
    content: str,
) -> None:
    requirement_id = str(item.get("id") or "").strip()
    content = content.strip()
    if requirement_id and content:
        requirements.append({"id": requirement_id, "section": section, "content": content})


def compute_missing_links(
    *,
    requirement_graph: dict[str, object],
    class_trace: list[dict[str, object]],
    sequence_trace: list[dict[str, object]],
) -> dict[str, list[dict[str, str]]]:
    """Tính requirement nào chưa có link ở class hoặc sequence diagram."""

    requirements = _requirements_by_id(requirement_graph)
    class_ids = _linked_requirement_ids(class_trace)
    sequence_ids = _linked_requirement_ids(sequence_trace)
    return {
        "class_diagram": [
            {"requirement": req_id, "content": item["content"]}
            for req_id, item in requirements.items()
            if req_id not in class_ids
        ],
        "sequence_diagram": [
            {"requirement": req_id, "content": item["content"]}
            for req_id, item in requirements.items()
            if req_id not in sequence_ids
        ],
    }


def _parse_trace_json(
    raw: str,
    *,
    diagram_name: str,
    expected_requirement_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Parse only the complete outer payload; never salvage a nested links array."""

    try:
        parsed: object = json.loads(strip_code_fence(raw).strip())
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "validator_traceability_failed",
            f"Validator {diagram_name} trace output is not one complete JSON payload.",
        ) from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("traceability")
    if not isinstance(parsed, list):
        raise HarnessError(
            "validator_traceability_failed",
            f"Validator {diagram_name} trace output must contain a top-level traceability array.",
        )

    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace item {index} must be an object.",
            )
        requirement = item.get("requirement")
        req_id = requirement.strip() if isinstance(requirement, str) else ""
        if not req_id:
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace item {index} has a blank or missing requirement ID.",
            )
        if expected_requirement_ids is not None and req_id not in expected_requirement_ids:
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace references unknown requirement: {req_id}",
            )
        if req_id in seen:
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace repeats requirement: {req_id}",
            )
        links = item.get("links")
        if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace item {req_id} must contain an array of link objects.",
            )
        seen.add(req_id)
        items.append(item)
    return items


def _normalize_trace(
    *,
    trace: list[dict[str, object]],
    requirement_graph: dict[str, object],
    allowed_targets: set[str],
    diagram_name: str,
) -> list[dict[str, object]]:
    requirements = _requirements_by_id(requirement_graph)
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()

    for item in trace:
        req_id = str(item.get("requirement", "")).strip()
        if req_id not in requirements:
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace references unknown requirement: {req_id}",
            )
        seen.add(req_id)
        links: list[dict[str, str]] = []
        raw_links = item.get("links", [])
        if isinstance(raw_links, list):
            for raw_link in raw_links:
                if not isinstance(raw_link, dict):
                    continue
                target = str(raw_link.get("target", "")).strip()
                if not target:
                    continue
                if target not in allowed_targets:
                    coerced_target = _coerce_sequence_target(
                        target=target,
                        allowed_targets=allowed_targets,
                        diagram_name=diagram_name,
                    )
                    if coerced_target:
                        target = coerced_target
                    else:
                        raise HarnessError(
                            "validator_traceability_failed",
                            f"Validator {diagram_name} trace invented target: {target}",
                        )
                links.append(
                    {
                        "target": target,
                        "reason": str(raw_link.get("reason", "")).strip(),
                    }
                )
        normalized.append(
            {
                "requirement": req_id,
                "content": requirements[req_id]["content"],
                "links": links,
            }
        )

    for req_id, requirement in requirements.items():
        if req_id not in seen:
            normalized.append({"requirement": req_id, "content": requirement["content"], "links": []})
    return normalized


def _extract_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if line.startswith("# "):
            current = None
            continue
        if current:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _section_items(section_text: str) -> list[str]:
    items: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or line.startswith("#"):
            continue
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.lstrip("- ").strip()
        if line:
            items.append(line)
    return items


def _requirements_by_id(requirement_graph: dict[str, object]) -> dict[str, dict[str, str]]:
    requirements = requirement_graph.get("requirements", [])
    if not isinstance(requirements, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for item in requirements:
        if not isinstance(item, dict):
            continue
        req_id = str(item.get("id", "")).strip()
        if req_id:
            result[req_id] = {
                "id": req_id,
                "section": str(item.get("section", "")),
                "content": str(item.get("content", "")),
            }
    return result


def _linked_requirement_ids(trace: list[dict[str, object]]) -> set[str]:
    linked: set[str] = set()
    for item in trace:
        links = item.get("links", [])
        if isinstance(links, list) and links:
            linked.add(str(item.get("requirement", "")))
    return linked


def _class_targets(class_graph: dict[str, object]) -> set[str]:
    targets: set[str] = set()
    nodes = class_graph.get("nodes", [])
    if not isinstance(nodes, list):
        return targets
    for node in nodes:
        if not isinstance(node, dict):
            continue
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        targets.add(class_name)
        methods = node.get("methods", [])
        if isinstance(methods, list):
            for method in methods:
                if isinstance(method, dict):
                    method_name = str(method.get("name", "")).strip()
                else:
                    method_name = str(method).strip()
                if method_name:
                    targets.add(f"{class_name}.{method_name}")
        attributes = node.get("attributes", [])
        if isinstance(attributes, list):
            for attribute in attributes:
                if isinstance(attribute, dict):
                    attribute_name = str(attribute.get("name", "")).strip()
                else:
                    attribute_name = str(attribute).strip()
                if attribute_name:
                    targets.add(f"{class_name}.{attribute_name}")
    return targets


def _sequence_targets(sequence_graph: dict[str, object]) -> set[str]:
    targets: set[str] = set()
    diagrams = sequence_graph.get("sequenceDiagrams", [])
    if not isinstance(diagrams, list):
        return targets
    for diagram in diagrams:
        if not isinstance(diagram, dict):
            continue
        nodes = diagram.get("nodes", [])
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    node_id = str(node.get("id", "")).strip()
                    label = str(node.get("label", "")).strip()
                    if node_id:
                        targets.add(node_id)
                    if label:
                        targets.add(label)
        messages = diagram.get("messages", [])
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                source = str(message.get("from", "")).strip()
                target = str(message.get("to", "")).strip()
                text = str(message.get("message", "")).strip()
                arrow = str(message.get("arrow", "")).strip()
                if text:
                    targets.add(text)
                if source and target and text:
                    targets.add(f"{source}->{target}: {text}")
                    targets.add(f"{source} -> {target}: {text}")
                    if arrow:
                        targets.add(f"{source}{arrow}{target}: {text}")
                        targets.add(f"{source} {arrow} {target}: {text}")
        control_flows = diagram.get("controlFlows", [])
        if isinstance(control_flows, list):
            for flow in control_flows:
                if not isinstance(flow, dict):
                    continue
                flow_type = str(flow.get("type", "")).strip().lower()
                condition = str(flow.get("condition", "")).strip()
                if condition:
                    targets.add(condition)
                    if flow_type:
                        targets.add(f"{flow_type} {condition}")
                    if flow_type == "note" and ":" in condition:
                        note_text = condition.split(":", 1)[1].strip()
                        if note_text:
                            targets.add(note_text)
    return targets


def _coerce_sequence_target(*, target: str, allowed_targets: set[str], diagram_name: str) -> str:
    """Cho phép LLM trace tới control-flow/note có thật trong sequence graph.

    DeepSeek đôi khi trả target là toàn bộ note nhiều dòng hoặc phần text ngắn
    của một nhánh `alt/else/opt`. Nếu phần đó đã nằm trong control-flow parser
    đọc được, normalize về target đã parse thay vì fail cứng.
    """

    if diagram_name != "sequence diagram":
        return ""
    normalized = target.strip()
    normalized_compact = _compact_text(normalized)
    first_line = normalized.splitlines()[0].strip().lower()
    if normalized.lower().startswith("note "):
        for candidate in allowed_targets:
            candidate_text = candidate.strip()
            candidate_lower = candidate_text.lower()
            if candidate_lower.startswith("note ") and (
                candidate_lower == first_line
                or candidate_lower.startswith(first_line)
                or first_line.startswith(candidate_lower)
            ):
                return candidate_text
    for candidate in allowed_targets:
        candidate_text = candidate.strip()
        candidate_lower = candidate_text.lower()
        candidate_compact = _compact_text(candidate_text)
        if len(normalized_compact) >= 12 and (
            normalized_compact in candidate_compact
            or candidate_compact in normalized_compact
        ):
            return candidate_text
    return ""


def _compact_text(text: str) -> str:
    """Chuẩn hóa text để so khớp mềm các condition ngắn của PlantUML."""

    return " ".join(text.lower().split())
