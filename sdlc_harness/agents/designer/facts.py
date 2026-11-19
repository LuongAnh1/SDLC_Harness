"""Design facts shared by the Designer tool loop and its prompt (scenarios, surfaces, UI/UX guidance)."""

from __future__ import annotations

from ...core.coerce import dict_copies as _dicts


OBSERVATION_ACTION_LIMIT = 3


def _feature_guidance_satisfied(graph: dict[str, object]) -> bool:
    """Under UI contract v2 one feature-level guidance pass is enough; later sessions (repairs) reuse its refs."""

    return any(
        _strings(_dict(_dict(node.get("surface_contract")).get("experience_contract")).get("guidance_refs"))
        for node in _dicts(graph.get("nodes"))
        if str(node.get("surface") or "") == "ui"
    )


def _ui_guidance_required(
    *,
    active_partition: dict[str, object] | None,
    repair_task: dict[str, object] | None,
    graph: dict[str, object],
) -> bool:
    if active_partition is not None:
        return "ui" in _strings(active_partition.get("missing_surfaces")) or any(
            str(item.get("surface") or "") == "ui"
            for item in _dicts(active_partition.get("surface_work"))
        )
    codes = {
        str(item.get("code") or item.get("category") or "")
        for item in _dicts((repair_task or {}).get("findings"))
    }
    ui_repair = any(
        code.startswith("ui_")
        or code in {"surface_contract_missing", "accessibility", "interaction_feedback", "responsive_attachment"}
        for code in codes
    )
    return ui_repair and any(
        str(item.get("surface") or "") == "ui" for item in _dicts((repair_task or {}).get("nodes"))
    )


def _uiux_guidance_status(
    active_guidance: list[dict[str, object]],
    *,
    source_inspected: bool,
    delta_required: bool = False,
) -> dict[str, object]:
    read_actions = [{"action": "repo.read"}, {"action": "codegraph.query"}]

    def lane(mode: str, selector: str) -> dict[str, object]:
        attempts = [
            item
            for item in active_guidance
            if str(item.get("mode") or "") == mode and str(item.get("selector") or "") == selector
        ]
        return {
            "mode": mode,
            "selector": selector,
            "attempts": len(attempts),
            "max_attempts": 2,
            "retry_remaining": max(0, 2 - len(attempts)),
            "latest_result_count": int(attempts[-1].get("count") or 0) if attempts else 0,
            "has_evidence": any(_strings(item.get("evidence_refs")) for item in attempts),
        }

    domain = lane("domain", "ux")
    stack = lane("stack", "react")
    required_next: dict[str, object] | None = None
    allowed_next: list[dict[str, object]] = []
    blocked_reason = ""
    if not source_inspected:
        allowed_next = read_actions
    elif not bool(domain["has_evidence"]):
        if int(domain["retry_remaining"]) > 0:
            required_next = {
                "action": "uiux.search_guidance",
                "arguments": {"mode": "domain", "domain": "ux"},
            }
        else:
            blocked_reason = "domain=ux guidance produced no usable evidence after two attempts"
    elif int(domain["attempts"]) == 1 and int(stack["attempts"]) == 0:
        allowed_next = [
            {"action": "uiux.search_guidance", "arguments": {"mode": "domain", "domain": "ux"}},
            {"action": "uiux.search_guidance", "arguments": {"mode": "stack", "stack": "react"}},
        ]
    elif not bool(stack["has_evidence"]):
        if int(stack["retry_remaining"]) > 0:
            required_next = {
                "action": "uiux.search_guidance",
                "arguments": {"mode": "stack", "stack": "react"},
            }
        else:
            blocked_reason = "stack=react guidance produced no usable evidence after two attempts"
    elif delta_required:
        required_next = {"action": "design.propose_delta", "arguments": {}}
    # Guidance done: the Designer may still read source it has not seen (observation_policy bounds it) or propose.
    elif int(stack["attempts"]) == 1:
        allowed_next = [
            {"action": "uiux.search_guidance", "arguments": {"mode": "stack", "stack": "react"}},
            *read_actions,
            {"action": "design.propose_delta"},
        ]
    else:
        allowed_next = [*read_actions, {"action": "design.propose_delta"}]
    return {
        "source_inspected": source_inspected,
        "domain": domain,
        "stack": stack,
        "required_next_action": required_next,
        "allowed_next_actions": allowed_next,
        "retry_policy": "Retry the same selector once only when its result is empty or off-topic.",
        "blocked_reason": blocked_reason,
    }


def _uiux_call_allowed(status: dict[str, object], *, mode: str, selector: str) -> bool:
    """A guidance call must match required_next_action, or an allowed_next_actions entry when none is required."""

    required = _dict(status.get("required_next_action"))
    candidates = [required] if required else [_dict(item) for item in status.get("allowed_next_actions") or []]
    for item in candidates:
        arguments = _dict(item.get("arguments"))
        if (
            str(item.get("action") or "") == "uiux.search_guidance"
            and str(arguments.get("mode") or "") == mode
            and str(arguments.get("domain") or arguments.get("stack") or "") == selector
        ):
            return True
    return False


def _required_surface_set(scenario: dict[str, object]) -> set[str]:
    surfaces = _dict(scenario.get("surfaces"))
    return {
        str(surface)
        for surface, value in surfaces.items()
        if isinstance(value, dict) and str(value.get("status") or "") == "required"
    }


def _scenarios(requirement_model: dict[str, object]) -> list[dict[str, object]]:
    contract = _dict(requirement_model.get("scenario_contract"))
    return _dicts(contract.get("scenarios"))


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
