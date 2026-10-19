"""Các projection tất định cho Requirement Model V2."""

from __future__ import annotations

from typing import Any, Iterable

from .model_v2 import RequirementModelV2


def render_requirement_packet_v2(model: RequirementModelV2) -> str:
    data = model.to_dict()
    use_case = data["use_case"]
    flows = data["flows"]
    lines = [
        f"# Requirement Packet V2 R{data['revision']}",
        "",
        f"- Run id: `{data['run_id']}`",
        f"- Source: `{data['source'].get('source_ref', '')}`",
        f"- Use case: `{use_case.get('id', '')}` {use_case.get('name', '')}",
        f"- Validation: `{data['validation'].get('status', '')}`",
        "",
    ]
    _packet_statement_section(lines, "Goal", [use_case.get("goal")])
    _packet_actor_section(lines, use_case.get("actors"))
    _packet_statement_section(lines, "Preconditions", data["preconditions"])
    _packet_main_flow(lines, flows.get("main"))
    _packet_branch_flows(lines, "Alternative Flows", flows.get("alternatives"))
    _packet_branch_flows(lines, "Exception Flows", flows.get("exceptions"))
    _packet_statement_section(lines, "Postconditions", data["postconditions"])
    _packet_statement_section(lines, "Business Rules", data["business_rules"])
    _packet_acceptance_section(lines, data["acceptance_criteria"])
    _packet_statement_section(lines, "Quality / Non-functional Requirements", data["quality_requirements"])
    _packet_assumption_section(lines, data["assumptions"])
    _packet_question_section(lines, data["open_questions"])
    _packet_decision_section(lines, data["decisions"])
    _packet_evidence_section(lines, data["evidence"])
    return "\n".join(lines).rstrip() + "\n"


def render_formal_use_case_v2(model: RequirementModelV2) -> str:
    data = model.to_dict()
    use_case = data["use_case"]
    flows = data["flows"]
    lines = [f"UsecaseName: {use_case.get('name', '')}", "Goal:"]
    _formal_simple(lines, [use_case.get("goal")])
    lines.append("Actors:")
    actors = _dict_items(use_case.get("actors"))
    lines.extend([f"[{item.get('id', '')}] {_content(item, 'name')}" for item in actors] or ["[NONE]"])
    lines.append("Entry Conditions:")
    _formal_simple(lines, data["preconditions"])
    lines.append("Main Flow:")
    main = _dict_items(flows.get("main"))
    if main:
        for item in main:
            action = _content(item, "action")
            actor = str(item.get("actor") or "").strip()
            target = str(item.get("target") or "").strip()
            text = " ".join(part for part in (actor, action, target) if part).strip()
            lines.append(f"[{item.get('id', '')}] {text}")
    else:
        lines.append("[NONE]")
    lines.append("Alternative Flows:")
    _formal_branches(lines, flows.get("alternatives"))
    lines.append("Exception Flows:")
    _formal_branches(lines, flows.get("exceptions"))
    lines.append("Exit Conditions:")
    _formal_simple(lines, data["postconditions"])
    lines.append("Business Rules:")
    _formal_simple(lines, data["business_rules"])
    lines.append("Quality Requirements:")
    _formal_simple(lines, data["quality_requirements"])
    lines.append("Open Questions:")
    questions = _dict_items(data["open_questions"])
    if questions:
        for item in questions:
            label = "BLOCKING" if item.get("effective_blocking") is True else "NON-BLOCKING"
            lines.append(f"[{item.get('id', '')}] {label} | {item.get('category', '')} | {item.get('question', '')}")
    else:
        lines.append("[NONE]")
    return "\n".join(lines).rstrip() + "\n"


def _packet_statement_section(lines: list[str], title: str, items: Any) -> None:
    lines.extend([f"## {title}", ""])
    values = _dict_items(items)
    if not values:
        lines.extend(["- none", ""])
        return
    for item in values:
        suffix = _status_suffix(item)
        lines.append(f"- [{item.get('id', '')}] {_content(item, 'text')}{suffix}")
    lines.append("")


def _packet_actor_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Actors", ""])
    values = _dict_items(items)
    lines.extend([f"- [{item.get('id', '')}] {_content(item, 'name')}{_status_suffix(item)}" for item in values] or ["- none"])
    lines.append("")


def _packet_main_flow(lines: list[str], items: Any) -> None:
    lines.extend(["## Main Flow", ""])
    values = _dict_items(items)
    for item in values:
        target = f" {item.get('target')}" if item.get("target") else ""
        lines.append(f"- [{item.get('id', '')}] {item.get('actor', '')} {_content(item, 'action')}{target}".rstrip())
    if not values:
        lines.append("- none")
    lines.append("")


def _packet_branch_flows(lines: list[str], title: str, items: Any) -> None:
    lines.extend([f"## {title}", ""])
    values = _dict_items(items)
    if not values:
        lines.extend(["- none", ""])
        return
    for item in values:
        item_id = str(item.get("id") or "")
        lines.append(f"- [{item_id}] Trigger: {item.get('trigger', '')}")
        steps = [str(step).strip() for step in item.get("steps", []) if str(step).strip()]
        for index, step in enumerate(steps, start=1):
            lines.append(f"  - [{item_id}.{index}] {step}")
        if not steps:
            lines.append(f"  - [{item_id}.1] {_unknown(item)}")
        outcome = str(item.get("outcome") or "").strip() or _unknown(item)
        lines.append(f"  - [{item_id}.OUTCOME] {outcome}")
    lines.append("")


def _packet_acceptance_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Acceptance Criteria", ""])
    values = _dict_items(items)
    for item in values:
        verifies = ", ".join(str(ref) for ref in item.get("verifies", [])) or "none"
        lines.append(f"- [{item.get('id', '')}] {_content(item, 'text')}{_status_suffix(item)} (verifies: {verifies})")
    if not values:
        lines.append("- none")
    lines.append("")


def _packet_assumption_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Assumptions", ""])
    values = _dict_items(items)
    for item in values:
        affects = ", ".join(str(ref) for ref in item.get("affects", [])) or "none"
        lines.append(f"- [{item.get('id', '')}] {_content(item, 'text')} (reason: {item.get('reason', '')}; affects: {affects})")
    if not values:
        lines.append("- none")
    lines.append("")


def _packet_question_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Open Questions", ""])
    values = _dict_items(items)
    for item in values:
        label = "BLOCKING" if item.get("effective_blocking") is True else "NON-BLOCKING"
        affects = ", ".join(str(ref) for ref in item.get("affects", [])) or "none"
        lines.append(f"- [{item.get('id', '')}] {label} | {item.get('category', '')} | {item.get('question', '')} (affects: {affects}; status: {item.get('status', '')})")
    if not values:
        lines.append("- none")
    lines.append("")


def _packet_decision_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Human Decisions", ""])
    values = _dict_items(items)
    for item in values:
        lines.append(f"- [{item.get('id', '')}] {item.get('question_id', '')}: {item.get('answer', '')} (status: {item.get('status', '')}; by: {item.get('answered_by', '')}; at: {item.get('answered_at', '')})")
    if not values:
        lines.append("- none")
    lines.append("")


def _packet_evidence_section(lines: list[str], items: Any) -> None:
    lines.extend(["## Source Evidence", ""])
    values = _dict_items(items)
    for item in values:
        quote = str(item.get("quote") or "").replace("\n", " ").strip()
        lines.append(f"- [{item.get('id', '')}] `{item.get('source_ref', '')}` / {item.get('location', '')}: {quote}")
    if not values:
        lines.append("- none")
    lines.append("")


def _formal_simple(lines: list[str], items: Any) -> None:
    values = _dict_items(items)
    lines.extend([f"[{item.get('id', '')}] {_content(item, 'text')}" for item in values] or ["[NONE]"])


def _formal_branches(lines: list[str], items: Any) -> None:
    values = _dict_items(items)
    if not values:
        lines.append("[NONE]")
        return
    for item in values:
        item_id = str(item.get("id") or "")
        lines.append(f"[{item_id}] Trigger: {item.get('trigger', '')}")
        steps = [str(step).strip() for step in item.get("steps", []) if str(step).strip()]
        if steps:
            lines.extend(f"[{item_id}.{index}] {step}" for index, step in enumerate(steps, start=1))
        else:
            lines.append(f"[{item_id}.1] {_unknown(item)}")
        outcome = str(item.get("outcome") or "").strip() or _unknown(item)
        lines.append(f"[{item_id}.OUTCOME] {outcome}")


def _content(item: dict[str, Any], field: str) -> str:
    value = str(item.get(field) or "").strip()
    if value:
        return value
    return _unknown(item)


def _unknown(item: dict[str, Any]) -> str:
    refs = [str(ref) for ref in item.get("open_question_refs", []) if str(ref).strip()]
    return f"UNKNOWN_FROM_SOURCE [{refs[0]}]" if refs else "UNKNOWN_FROM_SOURCE"


def _status_suffix(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    return f" _(status: {status})_" if status else ""


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
