"""Parser PlantUML tối giản cho artifact thiết kế Phase 2.

TraceDev chuyển class/sequence diagram thành graph trước khi Validator kiểm
traceability. Module này làm lát đầu của bước đó: chỉ parse những cú pháp
PlantUML mà prompt Designer yêu cầu, đủ để workflow có dữ liệu máy đọc được và
fail rõ khi LLM trả thiếu diagram.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from ...core.errors import HarnessError

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


@dataclass(frozen=True)
class ParsedDesign:
    """Graph class/sequence đã parse được từ design artifact."""

    class_graph: dict[str, object]
    sequence_graph: dict[str, object]


def parse_design_model(design_model: str) -> ParsedDesign:
    """Parse design model PlantUML và fail rõ nếu thiếu class hoặc sequence."""

    class_diagram, sequence_diagrams = extract_class_sequence_diagrams(design_model)
    if not class_diagram:
        raise HarnessError("design_parser_failed", "Design artifact does not contain a class diagram.")
    if not sequence_diagrams:
        raise HarnessError("design_parser_failed", "Design artifact does not contain a sequence diagram.")

    class_graph = ClassDiagramParser().parse(class_diagram)
    sequence_graph = SequenceDiagramParser().parse(sequence_diagrams)
    _validate_graphs(class_graph=class_graph, sequence_graph=sequence_graph)
    return ParsedDesign(class_graph=class_graph, sequence_graph=sequence_graph)


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


class ClassDiagramParser:
    """Parse subset PlantUML class diagram thành nodes/relationships."""

    def parse(self, class_diagram: str) -> dict[str, object]:
        nodes: list[dict[str, object]] = []
        relationships: list[dict[str, str]] = []
        others: list[str] = []
        current_node: dict[str, object] | None = None

        for raw_line in class_diagram.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("'") or line.startswith("@"):
                continue
            if line == "}":
                current_node = None
                continue

            class_match = _CLASS_DECL_RE.match(line)
            if class_match:
                kind, name, stereotype = class_match.groups()
                current_node = {
                    "id": name,
                    "type": kind,
                    "className": name,
                    "stereotype": stereotype or "",
                    "attributes": [],
                    "methods": [],
                }
                nodes.append(current_node)
                if not line.endswith("{"):
                    current_node = None
                continue

            relation_match = _RELATION_RE.match(line)
            if relation_match and current_node is None:
                source, relation, target = relation_match.groups()
                relationships.append({"from": source, "to": target, "relation": relation})
                continue

            if current_node is not None:
                method_match = _METHOD_RE.match(line)
                if method_match:
                    visibility, name, parameters, return_type = method_match.groups()
                    current_node["methods"].append(
                        {
                            "visibility": visibility,
                            "name": name,
                            "parameters": _parse_parameters(parameters),
                            "returnType": (return_type or "void").strip(),
                        }
                    )
                    continue
                field_match = _FIELD_RE.match(line)
                if field_match:
                    visibility, name, field_type = field_match.groups()
                    current_node["attributes"].append(
                        {"visibility": visibility, "name": name, "type": field_type.strip()}
                    )
                    continue

            others.append(line)

        return {
            "version": "class_diagram_graph_v1",
            "nodes": nodes,
            "relationships": relationships,
            "others": others,
        }


class SequenceDiagramParser:
    """Parse subset PlantUML sequence diagram thành lifeline/message graph."""

    def parse(self, sequence_diagrams: list[str]) -> dict[str, object]:
        diagrams: list[dict[str, object]] = []
        for index, diagram in enumerate(sequence_diagrams, start=1):
            nodes: dict[str, dict[str, str]] = {}
            messages: list[dict[str, str]] = []
            control_flows: list[dict[str, str]] = []
            control_stack: list[str] = []

            for raw_line in diagram.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("'") or line.startswith("@"):
                    continue
                lowered = line.lower()
                if lowered.startswith(("title", "autonumber", "activate", "deactivate", "hide", "skinparam")):
                    continue

                participant_match = _PARTICIPANT_RE.match(line)
                if participant_match and not participant_match.group(2).lstrip().startswith(("-", ".")):
                    participant_type, raw_name = participant_match.groups()
                    node_id, label = _parse_participant(raw_name)
                    nodes[node_id] = {"id": node_id, "type": participant_type.lower(), "label": label}
                    continue

                first_word = lowered.split(" ", 1)[0]
                if first_word in _CONTROL_START:
                    condition = line.split(" ", 1)[1].strip() if " " in line else ""
                    control_stack.append(first_word)
                    control_flows.append({"type": first_word, "condition": condition})
                    continue
                if first_word == "else":
                    control_flows.append({"type": "else", "condition": line.split(" ", 1)[1].strip() if " " in line else ""})
                    continue
                if first_word == "end":
                    if control_stack:
                        control_stack.pop()
                    control_flows.append({"type": "end", "condition": ""})
                    continue
                if lowered.startswith("note "):
                    control_flows.append({"type": "note", "condition": line})
                    continue

                message_match = _MESSAGE_RE.match(line)
                if message_match:
                    source, arrow, target, message = message_match.groups()
                    nodes.setdefault(source, {"id": source, "type": "participant", "label": source})
                    nodes.setdefault(target, {"id": target, "type": "participant", "label": target})
                    messages.append(
                        {
                            "from": source,
                            "to": target,
                            "arrow": arrow,
                            "type": _message_type(arrow),
                            "message": message.strip(),
                        }
                    )

            diagrams.append(
                {
                    "id": f"sequence_diagram_{index}",
                    "nodes": list(nodes.values()),
                    "messages": messages,
                    "controlFlows": control_flows,
                }
            )

        return {"version": "sequence_diagram_graph_v1", "sequenceDiagrams": diagrams}


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


def _validate_graphs(*, class_graph: dict[str, object], sequence_graph: dict[str, object]) -> None:
    nodes = class_graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise HarnessError("design_parser_failed", "Class diagram did not produce class nodes.")

    diagrams = sequence_graph.get("sequenceDiagrams")
    if not isinstance(diagrams, list) or not diagrams:
        raise HarnessError("design_parser_failed", "Sequence diagram did not produce graph data.")

    has_sequence_nodes = False
    has_messages = False
    for diagram in diagrams:
        if not isinstance(diagram, dict):
            continue
        if diagram.get("nodes"):
            has_sequence_nodes = True
        if diagram.get("messages"):
            has_messages = True
    if not has_sequence_nodes:
        raise HarnessError("design_parser_failed", "Sequence diagram did not produce participants.")
    if not has_messages:
        raise HarnessError("design_parser_failed", "Sequence diagram did not produce messages.")
