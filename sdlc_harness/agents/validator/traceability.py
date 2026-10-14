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
from typing import Any

from ...core.errors import HarnessError
from ...llm.client import LlmClient
from ...llm.json_utils import parse_first_json_array, parse_first_json_object

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
    ) -> TraceabilityResult:
        """Tạo requirement graph, trace class/sequence và missing-link report."""

        requirement_graph = build_requirement_graph(requirement_packet)
        requirements = requirement_graph["requirements"]
        requirement_json = json.dumps(requirements, ensure_ascii=False, indent=2)

        class_trace = self._trace_class(
            requirement_json=requirement_json,
            class_graph=class_graph,
        )
        sequence_trace = self._trace_sequence(
            requirement_json=requirement_json,
            sequence_graph=sequence_graph,
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
        )

    def _trace_class(self, *, requirement_json: str, class_graph: dict[str, object]) -> list[dict[str, object]]:
        prompt = _CLASS_TRACE_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{requirement}", requirement_json)
        prompt = prompt.replace("{class_diagram_json}", json.dumps(class_graph, ensure_ascii=False, indent=2))
        raw = self.llm_client.complete(
            system="You are Validator.traceability_class. Return only JSON.",
            prompt=prompt,
        )
        return _parse_trace_json(raw, diagram_name="class diagram")

    def _trace_sequence(self, *, requirement_json: str, sequence_graph: dict[str, object]) -> list[dict[str, object]]:
        prompt = _SEQUENCE_TRACE_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{requirement}", requirement_json)
        prompt = prompt.replace("{sequence_diagram_json}", json.dumps(sequence_graph, ensure_ascii=False, indent=2))
        raw = self.llm_client.complete(
            system="You are Validator.traceability_sequence. Return only JSON.",
            prompt=prompt,
        )
        return _parse_trace_json(raw, diagram_name="sequence diagram")


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


def _parse_trace_json(raw: str, *, diagram_name: str) -> list[dict[str, object]]:
    try:
        parsed: object = parse_first_json_object(raw, expected_keys={"traceability", "items", "results"})
    except json.JSONDecodeError:
        try:
            parsed = parse_first_json_array(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "validator_traceability_failed",
                f"Validator {diagram_name} trace output is not valid JSON.",
            ) from exc

    if isinstance(parsed, dict):
        for key in ("traceability", "items", "results"):
            value = parsed.get(key)
            if isinstance(value, list):
                parsed = value
                break
    if not isinstance(parsed, list):
        raise HarnessError(
            "validator_traceability_failed",
            f"Validator {diagram_name} trace output must be a JSON array.",
        )
    return [item for item in parsed if isinstance(item, dict)]


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
