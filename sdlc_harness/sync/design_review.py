"""Build a concise, human-first review model from canonical Design artifacts."""

from __future__ import annotations

from collections import Counter


HUMAN_DESIGN_REVIEW_VERSION = "human_design_review_v1"


def build_human_design_review(
    *,
    requirement_model: dict[str, object],
    component_view: dict[str, object],
    surface_view: dict[str, object],
    validation_report: dict[str, object],
) -> dict[str, object]:
    """Project machine-oriented artifacts into a short reviewer narrative."""

    use_case = _dict(requirement_model.get("use_case"))
    goal = _dict(use_case.get("goal"))
    actors = {
        str(item.get("id") or ""): str(item.get("name") or item.get("id") or "")
        for item in _dicts(use_case.get("actors"))
    }
    flows = _dict(requirement_model.get("flows"))
    main_flow = [
        {
            "id": str(item.get("id") or ""),
            "actor": actors.get(str(item.get("actor") or ""), str(item.get("actor") or "System")),
            "action": str(item.get("action") or ""),
        }
        for item in _dicts(flows.get("main"))
    ]

    scenario_contract = _dict(requirement_model.get("scenario_contract"))
    scenarios = _dicts(scenario_contract.get("scenarios"))
    surface_items = [
        *_dicts(surface_view.get("ui")),
        *_dicts(surface_view.get("api")),
        *_dicts(surface_view.get("business_rules")),
        *_dicts(surface_view.get("data")),
        *_dicts(surface_view.get("interactions")),
    ]
    exception_scenarios: list[dict[str, object]] = []
    for scenario in scenarios:
        if str(scenario.get("kind") or "") == "main":
            continue
        scenario_id = str(scenario.get("id") or "")
        related = [item for item in surface_items if str(item.get("scenario_id") or "") == scenario_id]
        outcomes: list[str] = []
        for item in related:
            contract = _dict(item.get("surface_contract"))
            outcomes.extend(_strings(contract.get("outcomes")))
            outcomes.extend(
                str(response.get("outcome") or "")
                for response in _dicts(contract.get("responses"))
                if str(response.get("outcome") or "")
            )
        exception_scenarios.append(
            {
                "id": scenario_id,
                "name": str(scenario.get("name") or scenario_id),
                "kind": str(scenario.get("kind") or "alternative"),
                "summary": str((related[0] if related else {}).get("contract") or ""),
                "outcomes": _unique(outcomes)[:3],
            }
        )

    component_changes: list[dict[str, object]] = []
    for component in _dicts(component_view.get("components")):
        nodes = _dicts(component.get("nodes"))
        dispositions = Counter(str(item.get("disposition") or "unknown") for item in nodes)
        component_changes.append(
            {
                "component_id": str(component.get("component_id") or ""),
                "label": _component_label(str(component.get("component_id") or "")),
                "change_summary": ", ".join(
                    f"{count} {name}" for name, count in sorted(dispositions.items())
                ),
                "elements": [str(item.get("name") or "") for item in nodes if str(item.get("name") or "")],
            }
        )

    api_contracts: list[dict[str, object]] = []
    for item in _dicts(surface_view.get("api")):
        contract = _dict(item.get("surface_contract"))
        api_contracts.append(
            {
                "scenario_id": str(item.get("scenario_id") or ""),
                "name": str(item.get("name") or ""),
                "endpoint": f"{contract.get('method', '')} {contract.get('path', '')}".strip(),
                "outcomes": [
                    f"{entry.get('status', '')}: {entry.get('outcome', '')}".strip(": ")
                    for entry in _dicts(contract.get("responses"))
                ],
                "authorization": str(contract.get("authorization") or ""),
            }
        )

    business_rules: list[dict[str, object]] = []
    for item in _dicts(surface_view.get("business_rules")):
        contract = _dict(item.get("surface_contract"))
        business_rules.append(
            {
                "scenario_id": str(item.get("scenario_id") or ""),
                "name": str(item.get("name") or ""),
                "decision": str(contract.get("decision") or ""),
            }
        )

    data_changes: list[dict[str, object]] = []
    for item in _dicts(surface_view.get("data")):
        contract = _dict(item.get("surface_contract"))
        data_changes.append(
            {
                "scenario_id": str(item.get("scenario_id") or ""),
                "entity": str(contract.get("entity") or item.get("name") or ""),
                "operation": str(contract.get("operation") or ""),
                "migration": str(contract.get("migration_intent") or ""),
            }
        )

    findings = [
        {
            "id": str(item.get("id") or ""),
            "severity": str(item.get("severity") or "warning"),
            "message": str(item.get("message") or ""),
            "expected_change": str(item.get("expected_change") or ""),
        }
        for item in [
            *_dicts(validation_report.get("blocking_findings")),
            *_dicts(validation_report.get("warnings")),
        ]
    ]

    return {
        "schema_version": HUMAN_DESIGN_REVIEW_VERSION,
        "use_case": {
            "id": str(use_case.get("id") or ""),
            "name": str(use_case.get("name") or ""),
            "goal": str(goal.get("text") or ""),
            "actors": list(actors.values()),
        },
        "status": {
            "blocking_count": int(validation_report.get("blocking_count") or 0),
            "warning_count": int(validation_report.get("warning_count") or 0),
        },
        "scope": {
            "component_count": len(component_changes),
            "scenario_count": len(scenarios),
            "ui_count": len(_dicts(surface_view.get("ui"))),
            "api_count": len(api_contracts),
            "business_rule_count": len(business_rules),
            "data_count": len(data_changes),
        },
        "main_flow": main_flow,
        "exception_scenarios": exception_scenarios,
        "component_changes": component_changes,
        "api_contracts": api_contracts,
        "business_rules": business_rules,
        "data_changes": data_changes,
        "review_findings": findings,
    }


def _component_label(component_id: str) -> str:
    return {
        "web": "Giao diện web",
        "api": "Dịch vụ API",
        "database": "Cơ sở dữ liệu",
    }.get(component_id, component_id or "Thành phần chưa đặt tên")


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
