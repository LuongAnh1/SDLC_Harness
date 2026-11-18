"""Deterministic fake LLM responses used by tests and local fake mode."""

from __future__ import annotations

import hashlib
import json
import re

def fake_completion(prompt: str) -> str:
    """Fake output theo loại prompt để test không gọi API thật."""

    # Closure prompts embed repository source, so match them first by their exact JSON marker.
    if prompt.startswith('{\n  "prompt_kind": "Runtime Closure Planner v1"'):
        return fake_runtime_closure_plan(prompt)
    if prompt.startswith(
        ('{\n  "prompt_kind": "Runtime Closure Contract Review v1"', '{\n  "prompt_kind": "Runtime Closure Code Review v1"')
    ):
        return fake_runtime_closure_review(prompt)
    if prompt.startswith('{\n  "prompt_kind": "Browser Journey Planner v1"'):
        return fake_browser_journey_plan(prompt)
    if prompt.startswith('{\n  "prompt_kind": "UI Polish Planner v1"'):
        return fake_ui_polish_plan(prompt)
    if prompt.startswith('{\n  "prompt_kind": "UI Polish Contract Review v1"'):
        return fake_ui_polish_review(prompt)
    if prompt.startswith('{"prompt_kind": "UI Polish Result Review v1"'):
        return fake_ui_polish_review(prompt)
    if prompt.startswith('{\n  "prompt_kind": "Browser QA Scenario Review v1"'):
        return fake_browser_qa_review(prompt)
    if prompt.startswith('{\n  "prompt_kind": "Browser Journey Execution Review v1"'):
        return fake_browser_journey_review(prompt)
    if "# Requirement Source Tool Loop" in prompt:
        return fake_requirement_source_action(prompt)
    if "Requirement Model V2 Structured Output" in prompt or "Requirement Model V2 Bounded Repair" in prompt:
        return fake_requirement_model_v2(prompt)
    if (
        "Design Semantic Scenario Review v2" in prompt
        or "Design Semantic Runtime Review v2" in prompt
        or "Design Semantic Global Review v2" in prompt
    ):
        return fake_design_semantic_specialist_review(prompt)
    if "Design Semantic Coherence Review v2" in prompt:
        return fake_design_semantic_coherence_review()
    if "Design Semantic Adjudication v2" in prompt:
        return fake_design_semantic_adjudication(prompt)
    if "Progressive Semantic Claim Review" in prompt:
        return fake_progressive_semantic_review(prompt)
    if "Progressive Targeted Repair Verification" in prompt:
        return fake_progressive_targeted_review(prompt)
    if "Progressive Bounded Semantic Adjudication" in prompt:
        return fake_progressive_adjudication(prompt)
    if "Component-aware Implementation Plan V4" in prompt or "Component-aware Implementation Plan V5" in prompt:
        return fake_component_aware_implementation_plan(prompt)
    if "Implementation Plan LLM Review v1" in prompt or "Implementation Plan LLM Review V5" in prompt:
        return fake_implementation_plan_review(prompt)
    if "Developer Implementation Plan v1" in prompt:
        return fake_developer_implementation_plan(prompt)
    if "Scenario + Boundary Code Validation v1" in prompt:
        return fake_code_validation_task(prompt)
    if "Code Trace LLM Review v1" in prompt:
        return fake_code_trace_review(prompt)
    return fake_refined_requirement(prompt)


def _closure_prompt_payload(prompt: str) -> dict[str, object]:
    # A contract retry appends plain text after the JSON prompt.
    payload, _ = json.JSONDecoder().raw_decode(prompt)
    return payload if isinstance(payload, dict) else {}


def fake_runtime_closure_plan(prompt: str) -> str:
    """Resolve every input generically so the contract passes G1-G15 in fake mode."""

    payload = _closure_prompt_payload(prompt)
    context = payload.get("closure_context") or {}
    policy = payload.get("path_policy") or {}
    roots = policy.get("component_editable_roots") or {}
    browser_component = str(policy.get("browser_test_component_id") or "e2e")
    browser_root = str(policy.get("browser_test_root") or "tests/e2e")
    inputs = [item for item in context.get("runtime_inputs") or [] if isinstance(item, dict)]

    def component_of(path: str) -> str:
        matches = [
            (len(root), component)
            for component, component_roots in roots.items()
            for root in component_roots
            if path.startswith(f"{root}/")
        ]
        return max(matches)[1] if matches else next(iter(roots), "")

    units: dict[str, dict[str, object]] = {}

    def unit_for(component: str) -> dict[str, object]:
        return units.setdefault(
            component,
            {
                "key": f"{component}-closure",
                "component_id": component,
                "objective": f"Provide approved runtime inputs owned by {component}.",
                "depends_on": [],
                "resolves_input_ids": [],
                "write_paths": [],
                "test_target_paths": [],
                "required_stage_ids": ["typecheck"],
                "test_intents": [],
                "context_paths": [],
            },
        )

    resolutions: list[dict[str, object]] = []
    demo_inputs: list[str] = []
    stand_ins: list[dict[str, object]] = []
    for index, item in enumerate(inputs, start=1):
        input_id = str(item.get("input_id") or "")
        source = item.get("design_source")
        env = item.get("env") if isinstance(item.get("env"), dict) else None
        if env:
            resolutions.append(
                {
                    "input_id": input_id,
                    "kind": "existing_runtime",
                    "provider": {"kind": "env_var", "env_key": env["env_key"], "read_by": env["read_by"], "change_ref": "plan"},
                    "decision_ref": None,
                    "reason": "Production keeps the approved environment default.",
                }
            )
            module = f"{browser_root}/support/closure-stand-in-{index}.ts"
            unit = unit_for(browser_component)
            unit["write_paths"].append(module)
            unit["resolves_input_ids"].append(input_id)
            stand_ins.append(
                {
                    "stand_in_id": f"VS-{len(stand_ins) + 1}",
                    "for_input_id": input_id,
                    "decision_ref": "",
                    "module": module,
                    "start_export": f"startClosureStandIn{index}",
                    "env_key": env["env_key"],
                }
            )
            continue
        if source == "existing_runtime":
            resolutions.append(
                {
                    "input_id": input_id,
                    "kind": "existing_runtime",
                    "provider": {"kind": "symbol", "ref": item.get("provided_by")},
                    "decision_ref": None,
                    "reason": "The provider already exists.",
                }
            )
            continue
        component = component_of(str(item.get("consumer") or "").split("#", 1)[0])
        root = (roots.get(component) or [""])[0]
        path = f"{root}/closure-provider-{index}.ts"
        decision = None
        if source == "demo_fixture":
            decision = (item.get("design_decision_refs") or [None])[0]
        elif source != "new_boundary":
            demo_inputs.append(input_id)
            decision = "CD-1"
        resolutions.append(
            {
                "input_id": input_id,
                "kind": "new_boundary" if source == "new_boundary" else "approved_demo_fixture",
                "provider": {"kind": "symbol", "ref": f"{path}#closureProvider{index}"},
                "decision_ref": decision,
                "reason": "Closure writes the provider.",
            }
        )
        unit = unit_for(component)
        unit["write_paths"].append(path)
        unit["resolves_input_ids"].append(input_id)

    decisions: list[dict[str, object]] = []
    if demo_inputs:
        decisions.append(
            {"id": "CD-1", "input_ids": demo_inputs, "statement": "Serve demo data locally.", "scope": "demo_and_verification_only"}
        )
    if stand_ins:
        stand_in_decision = f"CD-{len(decisions) + 1}"
        decisions.append(
            {
                "id": stand_in_decision,
                "input_ids": [item["for_input_id"] for item in stand_ins],
                "statement": "Use a local stand-in for the external system during verification only.",
                "scope": "demo_and_verification_only",
            }
        )
        for item in stand_ins:
            item["decision_ref"] = stand_in_decision

    probes = []
    for index, scenario_id in enumerate(str(item) for item in context.get("scenario_ids") or []):
        proves = [str(item["input_id"]) for item in inputs if scenario_id in (item.get("scenario_ids") or [])]
        if index == 0:
            proves += [str(item["input_id"]) for item in inputs if not item.get("scenario_ids")]
        probes.append(
            {
                "probe_id": f"RP-{index + 1}",
                "scenario_ids": [scenario_id],
                "service": "api",
                "method": "GET",
                "path": "/health",
                "headers_from_input_ids": [],
                "expect_status": 200,
                "expect_json_array_min_items": None,
                "proves_input_ids": proves,
            }
        )
    return json.dumps(
        {
            "resolutions": resolutions,
            "closure_declared_inputs": [],
            "demo_decisions": decisions,
            "closure_units": list(units.values()),
            "runtime_config_changes": [],
            "runtime_probes": probes,
            "verification_stand_ins": stand_ins,
            "unresolved": [],
        },
        ensure_ascii=False,
    )


def fake_browser_journey_plan(prompt: str) -> str:
    """One `goto /` view per runnable scenario citing that scenario's obligations; passes J1-J6."""

    payload = _closure_prompt_payload(prompt)
    obligations = [item for item in payload.get("obligations") or [] if isinstance(item, dict)]
    views = []
    cited: set[str] = set()
    for index, scenario_id in enumerate(str(item) for item in payload.get("runnable_scenario_ids") or []):
        refs = [
            str(item["obligation_id"])
            for item in obligations
            if scenario_id in (item.get("scenario_ids") or []) and str(item["obligation_id"]) not in cited
        ]
        cited.update(refs)
        nodes = sorted(
            {str(item["design_node_id"]) for item in obligations if str(item["obligation_id"]) in refs}
        ) or list((payload.get("scenario_design_nodes") or {}).get(scenario_id) or [])[:1]
        views.append(
            {
                "key": f"scenario-{index + 1}",
                "scenario_id": scenario_id,
                "design_node_ids": nodes,
                "obligation_refs": refs,
                "steps": [{"action": "goto", "path": "/"}],
            }
        )
    return json.dumps({"views": views, "not_reachable": [], "observations": []}, ensure_ascii=False)


def fake_runtime_closure_review(prompt: str) -> str:
    contract = _closure_prompt_payload(prompt).get("contract") or {}
    return json.dumps(
        {
            "summary": "Fake closure review passed.",
            "reviewed_input_ids": [
                str(item.get("input_id") or "") for item in contract.get("runtime_inputs") or [] if isinstance(item, dict)
            ],
            "findings": [],
        }
    )


def fake_code_validation_task(prompt: str) -> str:
    payload = json.loads(prompt)
    contract = payload.get("task_contract") if isinstance(payload, dict) else {}
    contract = contract if isinstance(contract, dict) else {}
    return json.dumps(
        {
            "task_id": contract.get("task_id", ""),
            "reviewed_claim_ids": list(contract.get("authority_claim_ids") or []),
            "reviewed_design_ids": list(contract.get("required_reviewed_design_ids") or []),
            "reviewed_boundary_contract_ids": list(
                contract.get("required_reviewed_boundary_contract_ids") or []
            ),
            "claim_assessments": [
                {"claim_id": claim_id, "verdict": "pass", "finding_ids": []}
                for claim_id in list(contract.get("authority_claim_ids") or [])
            ],
            "ok": True,
            "summary": "Fake Scenario + Boundary task passed.",
            "findings": [],
            "observations": [],
        },
        ensure_ascii=False,
    )


def fake_ui_polish_plan(prompt: str) -> str:
    """One presentation-only unit over the first eligible UI path."""

    payload = _closure_prompt_payload(prompt)
    context = payload.get("planner_context") if isinstance(payload.get("planner_context"), dict) else {}
    candidates = [str(item) for item in context.get("candidate_paths") or [] if str(item)]
    design_ids = [str(item) for item in context.get("design_node_ids") or [] if str(item)]
    tokens = [str(item) for item in context.get("profile_token_keys") or [] if str(item)]
    refs = [*(design_ids[:1]), *(tokens[:1])]
    return json.dumps(
        {
            "authority": {
                "visual_profile_ref": context.get("visual_profile_ref") or {},
                "design_node_ids": design_ids,
            },
            "polish_units": [
                {
                    "key": "app-shell-polish",
                    "objective": "Improve visual hierarchy, spacing and shell consistency without changing behavior.",
                    "write_paths": candidates[:1],
                    "new_paths": [],
                    "depends_on": [],
                    "authority_refs": refs,
                }
            ],
            "out_of_scope": ["Behavior, data flow, copy and accessibility semantics remain unchanged."],
        },
        ensure_ascii=False,
    )


def fake_ui_polish_review(prompt: str) -> str:
    payload = _closure_prompt_payload(prompt)
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    unit_ids = [str(item.get("unit_id")) for item in contract.get("polish_units") or [] if isinstance(item, dict)]
    return json.dumps(
        {
            "summary": "Fake UI Polish contract stays presentation-only and follows the approved authority.",
            "reviewed_unit_ids": unit_ids,
            "findings": [],
            "observations": [],
        },
        ensure_ascii=False,
    )


def fake_browser_qa_review(prompt: str) -> str:
    payload = _closure_prompt_payload(prompt)
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else {}
    obligations = [item for item in scenario.get("obligations") or [] if isinstance(item, dict)]
    evidence = payload.get("review_context") if isinstance(payload.get("review_context"), dict) else {}
    refs = [
        str(item.get("evidence_ref") or "")
        for item in evidence.get("evidence") or []
        if isinstance(item, dict) and str(item.get("evidence_ref") or "")
    ]
    return json.dumps(
        {
            "summary": "Fake Browser QA scenario review passed.",
            "reviewed_obligation_ids": [str(item.get("obligation_id") or "") for item in obligations],
            "verdicts": [
                {
                    "obligation_id": str(item.get("obligation_id") or ""),
                    "verdict": "satisfied",
                    "evidence_refs": refs[:1],
                }
                for item in obligations
            ],
            "findings": [],
            "observations": [],
        },
        ensure_ascii=False,
    )


def fake_browser_journey_review(prompt: str) -> str:
    payload = _closure_prompt_payload(prompt)
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else {}
    views = [item for item in scenario.get("views") or [] if isinstance(item, dict)]
    not_reachable = [item for item in scenario.get("not_reachable") or [] if isinstance(item, dict)]
    context = payload.get("review_context") if isinstance(payload.get("review_context"), dict) else {}
    evidence = [item for item in context.get("evidence") or [] if isinstance(item, dict)]
    return json.dumps(
        {
            "summary": "Fake browser journey execution review passed.",
            "reviewed_view_ids": [str(item.get("view_id") or "") for item in views],
            "reviewed_not_reachable_obligation_ids": [
                str(item.get("obligation_id") or "") for item in not_reachable
            ],
            "view_evidence": [
                {
                    "view_id": str(view.get("view_id") or ""),
                    "evidence_refs": [
                        str(item.get("evidence_ref") or "")
                        for item in evidence
                        if str(item.get("view_id") or "") == str(view.get("view_id") or "")
                        and str(item.get("evidence_ref") or "")
                    ][:1],
                }
                for view in views
            ],
            "obligation_verdicts": fake_journey_obligation_verdicts(views, evidence),
            "not_reachable_verdicts": [
                {"obligation_id": str(item.get("obligation_id") or ""), "verdict": "justified"}
                for item in not_reachable
            ],
            "findings": [],
            "observations": [],
        },
        ensure_ascii=False,
    )


def fake_journey_obligation_verdicts(views: list[dict], evidence: list[dict]) -> list[dict[str, object]]:
    """One `satisfied` verdict per obligation the views cite, on the first capture of a view that cites it."""

    first_ref: dict[str, str] = {}
    for item in evidence:
        first_ref.setdefault(str(item.get("view_id") or ""), str(item.get("evidence_ref") or ""))
    cited: dict[str, str] = {}
    for view in views:
        for obligation_id in view.get("obligation_refs") or []:
            cited.setdefault(str(obligation_id), first_ref.get(str(view.get("view_id") or ""), ""))
    return [
        {"obligation_id": obligation_id, "verdict": "satisfied", "evidence_refs": [ref]}
        for obligation_id, ref in sorted(cited.items())
    ]


def fake_requirement_source_action(prompt: str) -> str:
    """Đi theo list -> read -> complete để fake mode kiểm thử đúng tool-loop thật."""

    marker_start = "TOOL_STATE_JSON_BEGIN\n"
    marker_end = "\nTOOL_STATE_JSON_END"
    try:
        raw_state = prompt.split(marker_start, 1)[1].split(marker_end, 1)[0]
        state = json.loads(raw_state)
    except (IndexError, json.JSONDecodeError):
        return json.dumps({"action": "source.list", "arguments": {}, "summary": "List allowed sources."})
    history = state.get("history") if isinstance(state, dict) else []
    entries = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    listed = next((item for item in entries if item.get("action") == "source.list"), None)
    if listed is None:
        return json.dumps({"action": "source.list", "arguments": {}, "summary": "List allowed sources."})
    read_entry = next(
        (
            item
            for item in entries
            if item.get("action") in {"source.read", "source.search"}
            and isinstance(item.get("result"), dict)
            and item["result"].get("ok") is True
        ),
        None,
    )
    if read_entry is None:
        result = listed.get("result") if isinstance(listed.get("result"), dict) else {}
        sources = result.get("sources") if isinstance(result, dict) else []
        source = next((item for item in sources if isinstance(item, dict)), {}) if isinstance(sources, list) else {}
        return json.dumps(
            {
                "action": "source.read",
                "arguments": {
                    "source_id": source.get("source_id", "SRC-1"),
                    "start_line": 1,
                    "line_count": 200,
                },
                "summary": "Read the immutable source snapshot.",
            }
        )
    result = read_entry.get("result") if isinstance(read_entry.get("result"), dict) else {}
    observation_id = str(result.get("observation_id") or "OBS-1")
    source_ref = str(result.get("source_ref") or "fake:source")
    source_text = str(result.get("text") or "Fake requirement source.")
    use_case_match = re.search(r"Use case:\s*`([^`]+)`", prompt)
    use_case = use_case_match.group(1).strip() if use_case_match else "FakeUseCase"
    synthetic_prompt = (
        f"Source ref: `{source_ref}`\nUse case: `{use_case}`\n"
        f"## Source snapshot\n{source_text}\n## End source snapshot"
    )
    model = json.loads(fake_requirement_model_v2(synthetic_prompt))
    for evidence in model.get("evidence", []):
        if isinstance(evidence, dict):
            evidence["observation_ref"] = observation_id
    return json.dumps(
        {
            "action": "complete",
            "arguments": {"requirement_model": model},
            "summary": "Build the grounded Requirement Model V2.",
        },
        ensure_ascii=False,
    )


def fake_progressive_semantic_review(prompt: str) -> str:
    """Return one satisfied assessment for every immutable experiment claim."""

    context = _fake_progressive_context(prompt)
    claims = context.get("claims") if isinstance(context, dict) else []
    assessments = []
    for item in claims if isinstance(claims, list) else []:
        if not isinstance(item, dict):
            continue
        refs = item.get("allowed_design_refs")
        assessments.append(
            {
                "claim_id": str(item.get("claim_id") or ""),
                "verdict": "satisfied",
                "category": "none",
                "message": "Covered by the traced fake design.",
                "expected_change": "",
                "design_refs": list(refs[:1]) if isinstance(refs, list) else [],
            }
        )
    return json.dumps({"assessments": assessments}, ensure_ascii=False)


def fake_progressive_targeted_review(prompt: str) -> str:
    context = _fake_progressive_context(prompt)
    candidates = context.get("candidates") if isinstance(context, dict) else []
    return json.dumps(
        {
            "results": [
                {
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "verdict": "resolved",
                    "evidence_refs": list(item.get("design_refs") or [])[:1],
                    "reason": "Resolved in the fake revised design.",
                }
                for item in candidates if isinstance(item, dict)
            ]
        },
        ensure_ascii=False,
    )


def fake_progressive_adjudication(prompt: str) -> str:
    context = _fake_progressive_context(prompt)
    candidates = context.get("candidates") if isinstance(context, dict) else []
    return json.dumps(
        {
            "decisions": [
                {
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "decision": "refuted",
                    "requirement_refs": [str(item.get("requirement_id") or "")],
                    "design_refs": list(item.get("design_refs") or []),
                    "reason": "The minority observation is not supported by the fake fixtures.",
                }
                for item in candidates if isinstance(item, dict)
            ]
        },
        ensure_ascii=False,
    )


def _fake_progressive_context(prompt: str) -> dict[str, object]:
    marker = "INPUT:\n"
    if marker not in prompt:
        return {}
    try:
        value = json.loads(prompt.split(marker, 1)[1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def fake_requirement_model_v2(prompt: str) -> str:
    """Sinh Requirement Model V2 tối thiểu, có evidence từ source snapshot."""

    source_ref_match = re.search(r"Source ref:\s*`([^`]+)`", prompt)
    source_ref = source_ref_match.group(1).strip() if source_ref_match else "fake:source"
    use_case_match = re.search(r"Use case:\s*`([^`]+)`", prompt)
    use_case_name = use_case_match.group(1).strip() if use_case_match else "FakeUseCase"
    marker = "## Source snapshot"
    source_text = prompt.split(marker, 1)[1] if marker in prompt else prompt
    source_text = source_text.split("\n## ", 1)[0].strip()
    evidence_quote = next((line.strip() for line in source_text.splitlines() if line.strip()), "Fake source requirement.")
    payload = {
        "schema_version": "requirement_model_v2",
        "revision": 1,
        "run_id": "fake_run",
        "source": {
            "source_id": "SRC-1",
            "source_ref": source_ref,
            "artifact_ref": "fake_source_snapshot",
            "fingerprint": "fake_fingerprint",
        },
        "provenance_ledger": {
            "schema_version": "requirement_provenance_v1",
            "sources": [
                {
                    "id": "SRC-1",
                    "type": "dataset",
                    "source_ref": source_ref,
                    "locator": source_ref,
                    "revision": None,
                    "artifact_ref": "fake_source_snapshot",
                    "fingerprint": "fake_fingerprint",
                }
            ],
        },
        "use_case": {
            "id": "UC-1",
            "name": use_case_name,
            "goal": {
                "id": "GOAL-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            },
            "actors": [],
        },
        "scenario_contract": {
            "schema_version": "requirement_scenario_surface_v1",
            "scenarios": [
                {
                    "id": "SC-1",
                    "name": "Complete the source-described behavior",
                    "kind": "main",
                    "actor_refs": [],
                    "requirement_refs": ["MF-1"],
                    "acceptance_refs": ["AC-1"],
                    "evidence_refs": ["E-1"],
                    "open_question_refs": [],
                    "surfaces": {
                        "ui": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish a user interface contract.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                        "api": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish an API contract.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                        "business_rule": {
                            "status": "required",
                            "reason": "The source-described behavior must be implemented and verified.",
                            "requirement_refs": ["MF-1", "AC-1"],
                            "evidence_refs": ["E-1"],
                            "open_question_refs": [],
                        },
                        "data": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish persisted state.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                    },
                }
            ],
        },
        "preconditions": [
            {
                "id": "PRE-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            }
        ],
        "flows": {
            "main": [
                {
                    "id": "MF-1",
                    "actor": "System",
                    "action": evidence_quote,
                    "target": None,
                    "evidence_refs": ["E-1"],
                    "open_question_refs": [],
                }
            ],
            "alternatives": [],
            "exceptions": [],
        },
        "postconditions": [],
        "business_rules": [],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
                "verifies": ["MF-1"],
            }
        ],
        "quality_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "decisions": [],
        "evidence": [
            {
                "id": "E-1",
                "source_id": "SRC-1",
                "location": "source snapshot",
                "quote": evidence_quote,
            }
        ],
        "validation": {"status": "valid", "errors": []},
    }
    return json.dumps(payload, ensure_ascii=False)


def fake_refined_requirement(prompt: str) -> str:
    """Requirement projection V2 ổn định cho các test downstream độc lập."""

    return """# Requirement Packet V2

## Goal
Cho phép Agency Operator xóa một cultural heritage object đã chọn trong hệ thống eTour.

## Actors
- Agency Operator
- eTour System

## Preconditions
- Agency Operator đã đăng nhập.
- Agency Operator đã tìm và chọn một cultural heritage object từ kết quả SearchCulturalHeritage.

## Main Flow
1. Agency Operator mở danh sách cultural heritage object từ kết quả tìm kiếm.
2. Agency Operator chọn một cultural heritage object.
3. Agency Operator kích hoạt chức năng xóa.
4. Hệ thống yêu cầu xác nhận thao tác xóa.
5. Agency Operator xác nhận thao tác.
6. Hệ thống xóa cultural heritage object đã chọn.
7. Hệ thống thông báo thao tác xóa thành công.

## Alternative / Exception Flows
- Agency Operator có thể hủy thao tác trước khi xác nhận.
- Kết nối tới server eTour có thể bị gián đoạn trong quá trình thao tác.

## Postconditions
- Cultural heritage object đã chọn không còn khả dụng trong hệ thống sau khi xóa thành công.
- Hệ thống hiển thị thông báo xóa thành công.

## Acceptance Criteria
- Khi Agency Operator xác nhận, hệ thống xóa cultural heritage object đã chọn.
- Khi xóa thành công, hệ thống hiển thị thông báo thành công.

## Quality / Non-functional Requirements
- Hệ thống chặn nhiều lần gửi xác nhận trong khi thao tác xóa/hủy đang được xử lý.

## Open Questions
- UC1 chưa nêu rõ quyền chi tiết của Agency Operator ngoài trạng thái đã đăng nhập.
- UC1 chưa mô tả kết quả mong muốn nếu mất kết nối tới server eTour giữa lúc xóa.
- UC1 chưa mô tả nội dung thông báo khi người dùng hủy thao tác.

## Source Evidence
- "Delete a cultural object in the system."
- "The agency has logged."
- "Asks confirmation" và "Confirm the operation".
- "The system shall notify the successful elimination".
- "Operator Agency cancels the operation."
- "Interruption of connection to the server ETOUR."
""".strip()


def fake_implementation_plan_review(prompt: str = "") -> str:
    """LLM review giả lập cho Phase 3 implementation plan."""

    if '"findings": [' in prompt:
        plan = _json_object_after_marker(prompt, "Structured implementation plan JSON:\n")
        reviewed_design_ids = sorted(
            {
                str(design_id)
                for unit in plan.get("work_units", [])
                if isinstance(unit, dict)
                for field in ("design_node_ids", "design_edge_ids")
                for design_id in unit.get(field, [])
                if str(design_id).strip()
            }
        )
        return json.dumps(
            {
                "summary": (
                    "Implementation plan covers the approved requirement/design graph, "
                    "keeps Phase 3 read-only, and gives Phase 4 enough code/test targets."
                ),
                "reviewed_design_ids": reviewed_design_ids,
                "findings": [],
            },
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "ok": True,
            "summary": (
                "Implementation plan covers the approved requirement/design graph, "
                "keeps Phase 3 read-only, and gives Phase 4 enough code/test targets."
            ),
            "coverage_issues": [],
            "traceability_issues": [],
            "implementation_quality_issues": [],
            "test_issues": [],
            "scope_issues": [],
            "developer_feedback": "",
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_design_semantic_specialist_review(prompt: str = "") -> str:
    """Sliced semantic task fixture; it assesses only task-owned claims."""

    packet = _json_object_after_marker(prompt, "Assigned review packet:\n")
    task = packet.get("task") if isinstance(packet.get("task"), dict) else {}
    claims = packet.get("claims") if isinstance(packet.get("claims"), list) else []
    nodes = packet.get("nodes") if isinstance(packet.get("nodes"), list) else []
    primary_ids = {
        str(item) for item in task.get("primary_claim_ids", []) if str(item)
    }
    assessments = []
    for claim in claims:
        if not isinstance(claim, dict) or str(claim.get("claim_id") or "") not in primary_ids:
            continue
        scenario_id = str(claim.get("scenario_id") or "")
        candidates = [
            node
            for node in nodes
            if isinstance(node, dict)
            and (not scenario_id or str(node.get("scenario_id") or "") == scenario_id)
        ]
        design_ref = str(candidates[0].get("id") or "") if candidates else ""
        assessments.append(
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "status": "satisfied",
                "design_refs": [design_ref] if design_ref else [],
                "reason": "Fake specialist found exact Design evidence for the assigned claim.",
            }
        )
    return json.dumps(
        {
            "ok": True,
            "summary": "Fake sliced specialist review passed.",
            "assessments": assessments,
            "findings": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_design_semantic_coherence_review() -> str:
    return json.dumps(
        {"ok": True, "summary": "Fake task results are coherent.", "findings": [], "conflicts": []},
        ensure_ascii=False,
        indent=2,
    )


def fake_design_semantic_adjudication(prompt: str) -> str:
    findings = _json_value_after_marker(prompt, "Existing findings:")
    evidence = _json_value_after_marker(prompt, "Exact Design evidence:")
    nodes = {
        str(item.get("id") or ""): item
        for item in (evidence.get("nodes", []) if isinstance(evidence, dict) else [])
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    edges = {
        str(item.get("id") or ""): item
        for item in (evidence.get("edges", []) if isinstance(evidence, dict) else [])
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    routes = []
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict) or str(finding.get("severity") or "") != "blocking":
            continue
        diagram_ids = [str(item) for item in finding.get("diagram_ids", []) if str(item)]
        node = next((nodes.get(ref) for ref in diagram_ids if nodes.get(ref)), None)
        if node is None:
            edge = next((edges.get(ref) for ref in diagram_ids if edges.get(ref)), None)
            node = nodes.get(str((edge or {}).get("from") or ""))
        scenario_id = str((node or {}).get("scenario_id") or "")
        component_id = str((node or {}).get("component_id") or "")
        target_id = f"{scenario_id}@{component_id}" if scenario_id and component_id else ""
        routes.append(
            {
                "finding_id": str(finding.get("id") or ""),
                "remediation_mode": "local_patch" if target_id else "inconclusive",
                "reason": "Fake adjudication selected the existing owning partition.",
                "repair_steps": (
                    [
                        {
                            "target_id": target_id,
                            "diagram_ids": [str(node.get("id") or "")],
                            "expected_change": str(finding.get("expected_change") or "Repair the existing node."),
                            "depends_on": [],
                        }
                    ]
                    if target_id
                    else []
                ),
            }
        )
    return json.dumps(
        {
            "ok": True,
            "summary": "Fake conflict resolved.",
            "drop_finding_ids": [],
            "remediation_modes": routes,
            "findings": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def _json_value_after_marker(prompt: str, marker: str) -> object:
    if marker not in prompt:
        return None
    text = prompt.split(marker, 1)[1].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    return value


def _json_object_after_marker(prompt: str, marker: str) -> dict[str, object]:
    value = _json_value_after_marker(prompt, marker)
    return value if isinstance(value, dict) else {}


def fake_developer_implementation_plan(prompt: str = "") -> str:
    """Developer.plan giả lập để test Smart Developer không gọi API thật."""

    typescript = '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    service_path = (
        "apps/api/src/DeleteCulturalHeritageService.ts"
        if typescript
        else "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java"
    )
    repository_path = (
        "packages/db/src/CulturalHeritageRepository.ts"
        if typescript
        else "src/main/java/com/sdlc/etour/heritage/CulturalHeritageRepository.java"
    )
    flow_test_path = (
        "apps/api/src/DeleteCulturalHeritageFlow.test.ts"
        if typescript
        else "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"
    )
    source_evidence = (
        "Planning profile routes services to apps/api/src and repositories to packages/db/src."
        if typescript
        else "CodeGraph context found existing CulturalHeritageRepository in src/main/java."
    )
    test_evidence = (
        "Planning profile co-locates API tests under apps/api/src."
        if typescript
        else "Target repo already has a test folder under src/test/java/com/sdlc/etour/heritage."
    )

    payload = {
            "summary": "Plan uses CodeGraph context to map the approved delete heritage design into service, boundary, repository, and tests.",
            "repo_evidence": [source_evidence, test_evidence],
            "implementation_steps": [
                {
                    "order": 1,
                    "title": "Add delete flow application service",
                    "description": "Implement the approved validation, confirmation, delete, cancel, and result behavior.",
                    "files": [service_path, repository_path],
                    "design_refs": [
                        "class:DeleteCulturalHeritageService",
                        "method:DeleteCulturalHeritageService.deleteHeritage",
                    ],
                },
                {
                    "order": 2,
                    "title": "Verify delete flow",
                    "description": "Cover success, cancellation, interruption, and duplicate-submit prevention.",
                    "files": [flow_test_path],
                    "design_refs": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "proposed_files": [
                {
                    "path": service_path,
                    "action": "create",
                    "reason": "Holds the approved delete use-case behavior.",
                    "related_design_elements": ["class:DeleteCulturalHeritageService"],
                },
                {
                    "path": flow_test_path,
                    "action": "create",
                    "reason": "Verifies the approved sequence and exception flows.",
                    "related_design_elements": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "design_mappings": [],
            "test_intents": (
                [
                    {
                        "scope": "unit_or_service",
                        "intent": "Validate delete success, cancel, connection interruption, and duplicate submission prevention.",
                        "command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                    },
                    {
                        "scope": "regression",
                        "intent": "Run the target repo regression test command after Phase 4 code changes.",
                        "command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                    },
                ]
            ),
            "assumptions": ["Repository can expose a delete operation or be extended in Phase 4."],
            "open_questions": [],
        }
    if typescript:
        payload["implementation_signature_contract"] = (
            _fake_typescript_implementation_signature_contract(prompt)
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def fake_component_aware_implementation_plan(prompt: str) -> str:
    """Enrich Harness-owned V4 units without changing their identity."""

    units = _fake_json_prompt_value(
        prompt=prompt,
        start="Harness-owned work units:",
        end="Prior scenario plan context",
    )
    policy = _fake_json_prompt_value(
        prompt=prompt,
        start="Planning policy:",
        end="Verification policy:",
    )
    components = {
        str(item.get("component_id") or ""): item
        for item in policy.get("components", [])
        if isinstance(item, dict)
    } if isinstance(policy, dict) else {}
    unit_items = (
        [item for item in units if isinstance(item, dict)]
        if isinstance(units, list)
        else []
    )
    needs_shared_contract = (
        "contracts" in components
        and any(
            str(item.get("component_id") or "") == "web"
            and "ui" in item.get("surfaces", [])
            for item in unit_items
        )
        and any(
            str(item.get("component_id") or "") == "api"
            and "api" in item.get("surfaces", [])
            for item in unit_items
        )
    )
    contract_owner = next(
        (
            str(item.get("unit_id") or "")
            for item in unit_items
            if str(item.get("component_id") or "") == "api"
            and "api" in item.get("surfaces", [])
        ),
        "",
    )
    updates: list[dict[str, object]] = []
    browser_qa_active = '"browser_e2e"' in prompt
    for unit in unit_items:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("unit_id") or "")
        component_id = str(unit.get("component_id") or "")
        component = components.get(component_id, {})
        targets = [item for item in unit.get("planned_code_targets", []) if isinstance(item, dict)]
        if not targets:
            root = str(component.get("source_root") or "apps/api/src").rstrip("/")
            suffix = str(component.get("source_suffix") or ".ts")
            symbol = f"Planned{component_id.title()}Change{unit_id[-4:]}"
            targets = [{"path": f"{root}/{symbol}{suffix}", "symbol": symbol}]
        if needs_shared_contract and unit_id == contract_owner:
            targets.append(
                {
                    "path": "packages/contracts/src/planned-contract.ts",
                    "symbol": "PlannedContract",
                }
            )
        acceptance_refs = [str(item) for item in unit.get("acceptance_refs", []) if str(item)]
        test_root = str(component.get("source_root") or "apps/api/src").rstrip("/")
        test_suffix = str(component.get("test_suffix") or ".test.ts")
        test_path = f"{test_root}/DeleteCulturalHeritageFlow{test_suffix}"
        obligation_ids = [
            str(item.get("id") or "") for item in unit.get("ui_obligations", []) if isinstance(item, dict)
        ]
        update: dict[str, object] = {
            "unit_id": unit_id,
            "title": str(unit.get("title") or f"Implement {component_id} change"),
            "objective": str(unit.get("objective") or "Implement the approved Vertical Design contract."),
            "planned_code_targets": targets,
            "test_intents": [
                {
                    "stage_id": "unit",
                    "intent": f"Verify the approved behavior for {unit_id}.",
                    "acceptance_refs": acceptance_refs,
                    "target_paths": [test_path],
                    **({"obligation_refs": obligation_ids} if obligation_ids else {}),
                }
            ],
            "repo_evidence_refs": list(unit.get("repo_evidence_refs", [])),
        }
        if browser_qa_active and obligation_ids:
            update["qa_test_intents"] = [
                {
                    "stage_id": "browser_e2e",
                    "intent": f"Verify the approved UI behavior for {unit_id} in Chromium.",
                    "acceptance_refs": acceptance_refs,
                    "target_paths": [f"tests/e2e/{unit_id.lower()}.spec.ts"],
                    "obligation_refs": obligation_ids,
                    "data_setup": {
                        "mode": "synthetic",
                        "reason": "Fake mode has no repository-backed browser data source.",
                        "requirements": [
                            "Create deterministic values for the UI runtime inputs and states covered by this intent."
                        ],
                    },
                }
            ]
        if "data" in unit.get("surfaces", []):
            update["migration_impact"] = {
                "status": "not_required",
                "reason": "The approved data contract does not require a schema migration in fake mode.",
                "targets": [],
            }
        updates.append(update)
    scenario_match = re.search(
        r"This call plans exactly scenario `([^`]+)`", prompt
    )
    return json.dumps(
        {
            **({"scenario_id": scenario_match.group(1)} if scenario_match else {}),
            "summary": "Fake component-aware plan enriched every Harness-owned work unit.",
            "repo_evidence": ["Fake mode uses the frozen planning policy and approved graph bindings."],
            "work_unit_updates": updates,
            **(
                {
                    "runtime_config_impact": {
                        "status": "not_required",
                        "reason": "Fake mode plans no environment-specific configuration.",
                        "changes": [],
                    }
                }
                if "Runtime configuration rules:" in prompt
                else {}
            ),
            "implementation_signature_contract": _fake_typescript_implementation_signature_contract(
                prompt,
                unit_updates=updates,
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _fake_typescript_implementation_signature_contract(
    prompt: str,
    *,
    unit_updates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Mô phỏng Phase 3 producer; builder không được tự suy luận type từ Phase 2."""

    class_graph = _fake_json_prompt_section(
        prompt,
        start="Class graph:",
        end="Sequence graph:",
    )
    if not class_graph:
        class_graph = _fake_json_prompt_section(
            prompt,
            start="Legacy Class graph (signature contract only):",
            end="Legacy Sequence graph (signature contract only):",
        )
    component_symbols = _fake_component_symbols(prompt, unit_updates=unit_updates)
    component_aware = (
        "Component-aware Implementation Plan V4" in prompt
        or "Component-aware Implementation Plan V5" in prompt
    )
    if component_aware:
        targets = _fake_json_prompt_value(
            prompt=prompt,
            start="Harness-owned signature targets:",
            end="Harness-owned work units:",
        )
        return {
            "entries": [
                {
                    "target_path": str(item.get("target_path") or ""),
                    "target_symbol": str(item.get("target_symbol") or ""),
                    "parameters": [],
                    "return_type": (
                        "JSX.Element"
                        if str(item.get("target_path") or "").endswith(".tsx")
                        else "Promise<unknown>"
                    ),
                    "type_parameters": [],
                    "async": not str(item.get("target_path") or "").endswith(".tsx"),
                }
                for item in targets
                if isinstance(item, dict)
                and str(item.get("target_path") or "")
                and str(item.get("target_symbol") or "")
            ]
            if isinstance(targets, list)
            else []
        }
    entries: list[dict[str, object]] = []
    nodes = class_graph.get("nodes") if isinstance(class_graph, dict) else []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        owner = str(node.get("className") or node.get("id") or "").strip()
        owner_kind = str(node.get("type") or "class").strip().lower()
        methods = node.get("methods")
        if not owner or not isinstance(methods, list):
            continue
        for method in methods:
            if not isinstance(method, dict):
                continue
            name = str(method.get("name") or "").strip()
            if not name:
                continue
            implementation_symbol = component_symbols.get(owner, name)
            parameters = []
            raw_parameters = method.get("parameters")
            for parameter in raw_parameters if isinstance(raw_parameters, list) else []:
                if not isinstance(parameter, dict):
                    continue
                raw_name = str(parameter.get("name") or "").strip()
                parameters.append(
                    {
                        "name": raw_name.removeprefix("...").removesuffix("?"),
                        "type": _fake_typescript_type(parameter.get("type")),
                        "optional": raw_name.endswith("?"),
                        "rest": raw_name.startswith("..."),
                    }
                )
            if component_aware and not parameters:
                parameters.append(
                    {
                        "name": "input",
                        "type": "unknown",
                        "optional": False,
                        "rest": False,
                    }
                )
            return_type = _fake_typescript_type(method.get("returnType") or "void")
            if component_aware and return_type in {"void", "Promise<void>"}:
                return_type = "unknown"
            entries.append(
                {
                    "design_element_id": f"method:{owner}.{name}",
                    "symbol_kind": "method",
                    "owner_kind": owner_kind,
                    "qualified_name": f"{owner}.{implementation_symbol}",
                    "parameters": parameters,
                    "return_type": return_type,
                    "type_parameters": [],
                    "async": False,
                }
            )
    return {
        "version": "implementation_signature_contract_v1",
        "entries": entries,
    }


def _fake_component_symbols(
    prompt: str,
    *,
    unit_updates: list[dict[str, object]] | None,
) -> dict[str, str]:
    units = _fake_json_prompt_value(
        prompt=prompt,
        start="Harness-owned work units:",
        end="Vertical Design slice:",
    )
    design = _fake_json_prompt_value(
        prompt=prompt,
        start="Vertical Design slice:",
        end="Vertical Trace slice:",
    )
    if not isinstance(units, list) or not isinstance(design, dict):
        return {}
    updates_by_id = {
        str(item.get("unit_id") or ""): item
        for item in unit_updates or []
        if str(item.get("unit_id") or "")
    }
    unit_by_node = {
        str(node_id): unit
        for unit in units
        if isinstance(unit, dict)
        for node_id in unit.get("design_node_ids", [])
    }
    result: dict[str, str] = {}
    for node in design.get("nodes", []):
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "").strip()
        unit = unit_by_node.get(str(node.get("id") or ""))
        targets = unit.get("planned_code_targets", []) if isinstance(unit, dict) else []
        if isinstance(unit, dict):
            patch = updates_by_id.get(str(unit.get("unit_id") or ""), {})
            targets = patch.get("planned_code_targets") or targets
        candidates = [item for item in targets if isinstance(item, dict) and str(item.get("symbol") or "")]
        if not name or not candidates:
            continue
        name_tokens = set(_fake_identifier_tokens(name))

        def rank(target: dict[str, object]) -> tuple[int, int, str]:
            path = str(target.get("path") or "")
            symbol = str(target.get("symbol") or "")
            stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
            tokens = set(_fake_identifier_tokens(stem)) | set(_fake_identifier_tokens(symbol))
            generic_penalty = 1 if stem.lower() in {"app", "index", "main", "server"} else 0
            return len(name_tokens & tokens), -generic_penalty, path

        selected = max(candidates, key=rank)
        result[name] = str(selected.get("symbol") or "")
    return result


def _fake_identifier_tokens(value: object) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return [item.lower() for item in re.findall(r"[A-Za-z0-9]+", text)]


def _fake_json_prompt_section(
    prompt: str,
    *,
    start: str,
    end: str,
) -> dict[str, object]:
    start_marker = f"{start}\n"
    end_marker = f"\n\n{end}"
    if start_marker not in prompt:
        return {}
    raw = prompt.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _fake_json_prompt_value(*, prompt: str, start: str, end: str) -> object:
    start_marker = f"{start}\n"
    end_marker = f"\n{end}"
    if start_marker not in prompt:
        return {}
    raw = prompt.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _fake_typescript_type(value: object) -> str:
    text = str(value or "unknown").strip()
    replacements = {
        "String": "string",
        "Boolean": "boolean",
        "Integer": "number",
        "Long": "number",
        "Double": "number",
        "Float": "number",
    }
    return replacements.get(text, text)


def fake_code_trace_review(prompt: str = "") -> str:
    """LLM review giả lập cho code trace hybrid rule + LLM."""

    review_input = _fake_json_after_marker(
        prompt,
        "Return only valid JSON. Do not wrap the JSON in markdown.",
    )
    mappings = review_input.get("static_trace_mappings") if isinstance(review_input, dict) else []
    graph_edges: list[dict[str, object]] = []
    for mapping in mappings if isinstance(mappings, list) else []:
        if not isinstance(mapping, dict):
            continue
        design_ids = mapping.get("design_element_ids")
        design_id = str(mapping.get("design_element_id") or "")
        if not design_id and isinstance(design_ids, list) and design_ids:
            design_id = str(design_ids[0])
        graph_edges.append(
            {
                "requirement_ref": str(mapping.get("requirement_ref") or ""),
                "design_element_id": design_id,
                "code_ref": str(mapping.get("code_ref") or ""),
                "test_ref": str(mapping.get("test_ref") or ""),
                "evidence": "Static trace mapping supplied by the harness.",
            }
        )

    return json.dumps(
        {
            "ok": True,
            "confidence": 0.91,
            "summary": "Fake LLM review accepts the requirement-design-code-test graph for deterministic tests.",
            "graph_edges": graph_edges,
            "missing_links": [],
            "risks": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def _fake_json_after_marker(prompt: str, marker: str) -> dict[str, object]:
    """Đọc JSON object đầu tiên sau marker mà không phụ thuộc phần prompt theo sau."""

    if marker not in prompt:
        return {}
    raw = prompt.split(marker, 1)[1].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}

