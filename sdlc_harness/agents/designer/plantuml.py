"""Parser PlantUML tối giản cho artifact thiết kế Phase 2.

TraceDev chuyển class/sequence diagram thành graph trước khi Validator kiểm
traceability. Module này làm lát đầu của bước đó: chỉ parse những cú pháp
PlantUML mà prompt Designer yêu cầu, đủ để workflow có dữ liệu máy đọc được và
fail rõ khi LLM trả thiếu diagram.
"""

from __future__ import annotations

import re


_CLASS_DECL_RE = re.compile(r"^\s*(class|interface|abstract|enum)\s+([A-Za-z_][\w]*)(?:\s+<<(.+?)>>)?\s*\{?\s*$")
_FIELD_RE = re.compile(r"^\s*([+\-#])\s*([A-Za-z_][\w]*)\s*:\s*([^()]+?)\s*$")
_METHOD_RE = re.compile(r"^\s*([+\-#])\s*([A-Za-z_][\w]*)\s*\((.*?)\)\s*(?::\s*(.+?))?\s*$")
_RELATION_RE = re.compile(r"^\s*([A-Za-z_][\w]*)\s+([.ox*<|}\-]+(?:>|[.ox*<|}\-]+)?)\s+([A-Za-z_][\w]*)")
_PARTICIPANT_RE = re.compile(
    r'^\s*(actor|participant|boundary|control|utility|database|entity|collections|queue|node|cloud|storage)\s+(.+?)\s*$',
    re.IGNORECASE,
)
_MESSAGE_RE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*([-]+>>?|-->|--x|->x)\s*([A-Za-z_][\w]*)\s*:\s*(.+?)\s*$")
_CONTROL_START = {"alt", "opt", "loop", "group", "break", "par", "critical"}


def extract_class_sequence_diagrams(design_model: str) -> tuple[str, list[str]]:
    """Tách block PlantUML đầu tiên làm class diagram và các block sau làm sequence."""

    blocks = re.findall(r"@startuml\b.*?@enduml", design_model, flags=re.IGNORECASE | re.DOTALL)
    if not blocks:
        return "", []

    class_block = ""
    sequence_blocks: list[str] = []
    in_sequence_section = False
    for block in blocks:
        before = design_model[: design_model.find(block)]
        tail = before[-300:].lower()
        looks_sequence = "sequence diagram" in tail or re.search(
            r"\b(actor|participant|boundary|control|database)\b",
            block,
            flags=re.IGNORECASE,
        )
        looks_class = "class diagram" in tail or re.search(
            r"^\s*(class|interface|abstract|enum)\s+",
            block,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if looks_sequence or in_sequence_section:
            sequence_blocks.append(block)
            in_sequence_section = True
        elif looks_class and not class_block:
            class_block = block
        elif not class_block:
            class_block = block
        else:
            sequence_blocks.append(block)

    if not class_block and blocks:
        class_block = blocks[0]
        sequence_blocks = blocks[1:]
    return class_block, sequence_blocks


def _parse_parameters(raw: str) -> list[dict[str, str]]:
    parameters: list[dict[str, str]] = []
    if not raw.strip():
        return parameters
    for item in raw.split(","):
        part = item.strip()
        if not part:
            continue
        if ":" in part:
            name, param_type = part.split(":", 1)
            parameters.append({"name": name.strip(), "type": param_type.strip()})
        else:
            parameters.append({"name": part, "type": ""})
    return parameters


def _parse_participant(raw_name: str) -> tuple[str, str]:
    raw = raw_name.strip()
    alias_match = re.match(r'"(.+?)"\s+as\s+([A-Za-z_][\w]*)$', raw)
    if alias_match:
        label, node_id = alias_match.groups()
        return node_id, label
    alias_match = re.match(r"(.+?)\s+as\s+([A-Za-z_][\w]*)$", raw)
    if alias_match:
        label, node_id = alias_match.groups()
        return node_id, label.strip('" ')
    cleaned = raw.strip('"')
    return cleaned.replace(" ", "_"), cleaned


def _message_type(arrow: str) -> str:
    if "x" in arrow:
        return "lost_or_exception"
    if arrow.startswith("-->"):
        return "return"
    if arrow.endswith(">>"):
        return "asynchronous"
    return "synchronous"

