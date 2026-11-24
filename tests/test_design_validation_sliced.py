from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from sdlc_harness.agents.validator import DesignValidator, TraceabilityResult
from sdlc_harness.agents.validator.semantic_review import (
    _branch_entry,
    build_semantic_review_plan,
    materialize_task_context,
    with_global_projections,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.fake import fake_completion
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from sdlc_harness.storage.runtime import RuntimeStore


class StaticLlmClient:
    def __init__(self, responses: list[dict[str, object] | str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        return response if isinstance(response, str) else json.dumps(response)


class RoutedFakeLlmClient:
    def complete(self, *, system: str, prompt: str) -> str:
        return fake_completion(prompt)


class MetadataStaticLlmClient(StaticLlmClient):
    def complete_with_metadata(self, *, system: str, prompt: str, **_: object) -> LlmCompletion:
        return LlmCompletion(content=self.complete(system=system, prompt=prompt))


def _traceability() -> TraceabilityResult:
    return TraceabilityResult(
        requirement_graph={"requirements": [], "count": 0},
        class_trace=[],
        sequence_trace=[],
        missing_links={"class_diagram": [], "sequence_diagram": []},
    )


def _inputs(*, shared_screen: bool = False, boundary: bool = False):
    scenarios = [
        {
            "id": "SC-1",
            "requirement_refs": ["MF-1"],
            "acceptance_refs": [],
            "surfaces": {
                "ui": {"status": "required", "reason": "Actor confirms.", "requirement_refs": ["MF-1"]}
            },
        }
    ]
    nodes = [
        {
            "id": "VDN-ui-1",
            "scenario_id": "SC-1",
            "component_id": "web",
            "surface": "ui",
            "kind": "ui_interaction",
            "requirement_refs": ["MF-1"],
            "surface_contract": {
                "experience_contract": {"screen": {"screen_id": "shared-screen"}},
                "transitions": [{"action": "confirm"}],
            },
        }
    ]
    if shared_screen:
        scenarios.append(
            {
                "id": "SC-2",
                "requirement_refs": ["AF-1"],
                "acceptance_refs": [],
                "surfaces": {
                    "ui": {"status": "required", "reason": "Actor cancels.", "requirement_refs": ["AF-1"]}
                },
            }
        )
        nodes.append(
            {
                "id": "VDN-ui-2",
                "scenario_id": "SC-2",
                "component_id": "web",
                "surface": "ui",
                "kind": "ui_interaction",
                "requirement_refs": ["AF-1"],
                "surface_contract": {
                    "experience_contract": {"screen": {"screen_id": "shared-screen"}},
                    "transitions": [{"action": "cancel"}],
                },
            }
        )
    if boundary:
        nodes.append(
            {
                "id": "VDN-api-1",
                "scenario_id": "SC-1",
                "component_id": "api",
                "surface": "api",
                "kind": "api_endpoint",
                "requirement_refs": ["MF-1"],
            }
        )
    edges = (
        [
            {
                "id": "VDE-boundary-1",
                "scenario_id": "SC-1",
                "from": "VDN-ui-1",
                "to": "VDN-api-1",
                "kind": "calls",
                "requirement_refs": ["MF-1"],
                "boundary_contract": {"schema_version": "boundary_contract_v1"},
            }
        ]
        if boundary
        else []
    )
    canonical = {
        "use_case": {"id": "UC-X"},
        "scenario_contract": {"scenarios": scenarios},
        "flows": {
            "main": [{"id": "MF-1", "action": "Confirm."}],
            "alternatives": [{"id": "AF-1", "action": "Cancel."}],
        },
        "preconditions": [],
        "postconditions": [],
        "quality_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "decisions": [],
    }
    graph = {
        "artifact_digest": "sha256:design",
        "nodes": nodes,
        "edges": edges,
        "unresolved_items": [],
    }
    return canonical, graph


def _task_success(*claim_ids: str) -> dict[str, object]:
    return {
        "ok": True,
        "summary": "task passed",
        "assessments": [
            {
                "claim_id": claim_id,
                "status": "satisfied",
                "design_refs": ["VDN-ui-1"],
                "reason": "Exact design evidence covers the claim.",
            }
            for claim_id in claim_ids
        ],
        "findings": [],
    }


def _warning_finding(finding_id: str, *, claim_id: str = "SC-1:MF-1") -> dict[str, object]:
    return {
        "id": finding_id,
        "category": "ui_journey_coherence",
        "severity": "warning",
        "message": "The shared journey may be inconsistent.",
        "expected_change": "Make the existing transition contract consistent.",
        "claim_ids": [claim_id],
        "requirement_ids": ["MF-1"],
        "diagram_ids": ["VDN-ui-1"],
        "subject_ref": "VDN-ui-1",
        "evidence": [{"source": "design", "ref": "VDN-ui-1", "excerpt": "confirm transition"}],
    }


def _coherence(*, conflicts: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "summary": "coherent",
        "findings": [],
        "conflicts": conflicts or [],
    }


def _runtime_passed() -> dict[str, object]:
    return {"ok": True, "summary": "runtime path is executable", "assessments": [], "findings": []}


def _runtime_blocked(
    *claim_ids: str,
    severity: str = "blocking",
) -> dict[str, object]:
    return {
        "ok": severity != "blocking",
        "summary": "runtime path review",
        "assessments": [],
        "findings": [
            {
                "id": "SEM-RUNTIME",
                "category": "runtime_boundary_feasibility",
                "severity": severity,
                "message": "The scenario has no executable boundary path.",
                "expected_change": "Add a credible runtime boundary mechanism.",
                "claim_ids": list(claim_ids),
                "requirement_ids": ["MF-1"],
                "diagram_ids": ["VDN-ui-1"],
                "subject_ref": "VDN-ui-1",
                "evidence": [{"source": "design", "ref": "VDN-ui-1", "excerpt": "blocked path"}],
            }
        ],
    }


def _adjudication(
    *finding_ids: str,
    remediation_mode: str = "local_patch",
    drop_finding_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ok": True,
        "summary": "final routing",
        "drop_finding_ids": drop_finding_ids or [],
        "remediation_modes": [
            {
                "finding_id": finding_id,
                "remediation_mode": remediation_mode,
                "reason": "The final evidence supports this repair route.",
                "repair_steps": (
                    [
                        {
                            "target_id": "SC-1@web",
                            "diagram_ids": ["VDN-ui-1"],
                            "expected_change": "Repair the existing web node contract.",
                            "depends_on": [],
                        }
                    ]
                    if remediation_mode in {"local_patch", "partition_rebuild"}
                    else []
                ),
            }
            for finding_id in finding_ids
        ],
        "findings": [],
    }


def _review(
    client: StaticLlmClient,
    *,
    budget: int = 8,
    inputs: tuple[dict[str, object], dict[str, object]] | None = None,
    plan: dict[str, object] | None = None,
):
    canonical, graph = inputs or _inputs()
    return DesignValidator(client).review(
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=_traceability(),
        canonical_requirement=canonical,
        vertical_design_graph=graph,
        semantic_review_plan=plan,
        semantic_call_budget=budget,
    )


def test_planner_collapses_reviews_and_exposes_shared_surfaces() -> None:
    canonical, graph = _inputs(shared_screen=True, boundary=True)
    validator = DesignValidator(StaticLlmClient([]))

    first = validator.build_review_plan(canonical_requirement=canonical, vertical_design_graph=graph)
    second = validator.build_review_plan(canonical_requirement=canonical, vertical_design_graph=graph)

    assert first == second
    assert first["plan_digest"].startswith("sha256:")
    tasks = first["tasks"]
    primary = [claim_id for task in tasks for claim_id in task["primary_claim_ids"]]
    assert len(primary) == len(set(primary)) == 4
    assert first["schema_version"] == "semantic_review_plan_v2"
    # SC-2 has no executable edges, so it gets no runtime task.
    assert [(task["task_id"], task["task_type"]) for task in tasks] == [
        ("runtime-sc-1", "scenario_runtime"),
        ("scenario-sc-1", "scenario_behavior"),
        ("scenario-sc-2", "scenario_behavior"),
    ]
    assert tasks[0]["primary_claim_ids"] == []
    assert tasks[0]["context_claim_ids"] == tasks[1]["primary_claim_ids"]
    assert tasks[0]["coverage_keys"] == ["boundary:VDE-boundary-1"]
    assert "every outcome declared by its boundary coverage keys" in tasks[0]["focus"]
    assert "complete executable path" not in tasks[0]["focus"]
    assert tasks[0]["node_ids"] == tasks[1]["node_ids"]
    assert all(key.startswith("claim:") for key in tasks[1]["coverage_keys"])
    shared = first["shared_surfaces"][0]
    assert set(shared) == {"identity", "scenario_ids", "node_ids"}
    assert shared["scenario_ids"] == ["SC-1", "SC-2"]
    assert shared["node_ids"] == ["VDN-ui-1", "VDN-ui-2"]
    assert first["coverage"]["shared_surface_count"] == 1


def test_planner_routes_global_boundaries_to_cross_cutting_and_covers_every_boundary() -> None:
    claims = [
        {
            "claim_id": "SC-1:MF-1",
            "claim_type": "requirement",
            "scenario_id": "SC-1",
            "requirement_ids": ["MF-1"],
        }
    ]
    graph = {
        "nodes": [
            {"id": "VDN-global-a"},
            {"id": "VDN-global-b"},
            {"id": "VDN-scenario", "scenario_id": "SC-1"},
        ],
        "edges": [
            {
                "id": "VDE-global",
                "from": "VDN-global-a",
                "to": "VDN-global-b",
                "boundary_contract": {"schema_version": "boundary_contract_v1"},
            },
            {
                "id": "VDE-scenario",
                "scenario_id": "SC-1",
                "from": "VDN-scenario",
                "to": "VDN-global-a",
                "boundary_contract": {"schema_version": "boundary_contract_v1"},
            },
        ],
    }

    claims = with_global_projections([
        *claims,
        {"claim_id": "GLOBAL:PRE-1", "claim_type": "precondition", "requirement_ids": ["PRE-1"]},
    ])
    plan = build_semantic_review_plan(claims=claims, vertical_design_graph=graph)

    assert [task["task_type"] for task in plan["tasks"]] == ["cross_cutting", "scenario_runtime", "scenario_behavior"]
    global_task, scenario_task, _ = plan["tasks"]
    assert global_task["task_id"] == "cross-cutting-global"
    assert global_task["primary_claim_ids"] == []
    assert global_task["node_ids"] == ["VDN-global-a", "VDN-global-b"]
    assert global_task["edge_ids"] == ["VDE-global"]
    assert global_task["coverage_keys"] == ["boundary:VDE-global"]
    packet = materialize_task_context(
        global_task,
        claims=claims,
        vertical_design_graph=graph,
        vertical_trace={"mappings": [{"from": "MF-1", "to": "VDN-scenario", "kind": "realizes"}]},
    )
    assert [node["id"] for node in packet["nodes"]] == ["VDN-global-a", "VDN-global-b"]
    assert [edge["id"] for edge in packet["edges"]] == ["VDE-global"]
    assert all(claim["claim_type"] != "global_projection" for claim in packet["claims"])
    prompt = DesignValidator._semantic_task_prompt(task_context=packet, rule_review={})
    assert "Context claims are evidence and may be cited by boundary findings" in prompt
    assert "Judge only primary global preconditions" not in prompt
    assert "boundary:VDE-scenario" in scenario_task["coverage_keys"]
    assert plan["coverage"]["boundary_count"] == 2
    assert "global boundary contracts" in global_task["focus"]


def test_global_claim_fallback_keeps_whole_graph_without_behavior_tasks() -> None:
    claims = [{"claim_id": "GLOBAL:PRE-1", "claim_type": "precondition", "requirement_ids": ["PRE-1"]}]
    graph = {
        "nodes": [{"id": "VDN-a", "scenario_id": "SC-1"}, {"id": "VDN-b", "scenario_id": "SC-1"}],
        "edges": [
            {
                "id": "VDE-scenario",
                "scenario_id": "SC-1",
                "from": "VDN-a",
                "to": "VDN-b",
                "boundary_contract": {"schema_version": "boundary_contract_v1"},
            }
        ],
    }

    plan = build_semantic_review_plan(claims=claims, vertical_design_graph=graph)

    global_task = next(task for task in plan["tasks"] if task["task_type"] == "cross_cutting")
    assert global_task["primary_claim_ids"] == ["GLOBAL:PRE-1"]
    assert global_task["coverage_keys"] == ["claim:GLOBAL:PRE-1"]
    assert "boundary contracts" not in global_task["focus"]
    assert "Scenario-local claims are evidence only" in global_task["focus"]
    # Global claims may lack trace mappings, so the packet is the whole graph, not a requirement_refs guess.
    assert global_task["node_ids"] == ["VDN-a", "VDN-b"]
    assert global_task["edge_ids"] == ["VDE-scenario"]


def _global_inputs(*, postcondition: bool = True, boundary: bool = False):
    canonical, graph = _inputs(shared_screen=True, boundary=boundary)
    section, requirement_id = ("postconditions", "POST-3") if postcondition else ("preconditions", "PRE-1")
    canonical[section] = [{"id": requirement_id, "text": "Approved cross-scenario behavior."}]
    return canonical, graph


def _projection_task(
    scenario: int,
    requirement_id: str,
    *,
    status: str = "not_applicable",
    refs: list[str] | None = None,
) -> dict[str, object]:
    claim_id = f"SC-{scenario}:GLOBAL:{requirement_id}"
    local_id = "MF-1" if scenario == 1 else "AF-1"
    payload = _task_success(f"SC-{scenario}:{local_id}", f"SC-{scenario}:surface:ui", claim_id)
    payload["assessments"][-1].update(
        status=status, design_refs=refs or [], reason="This is the scenario's part of the global item."
    )
    if status in {"contradicted", "missing", "uncertain"}:
        finding = _warning_finding("SEM-PROJECTION", claim_id=claim_id)
        finding["requirement_ids"] = [requirement_id]
        if status != "uncertain":
            finding["severity"] = "blocking"
            payload["ok"] = False
        payload["findings"] = [finding]
    return payload


def test_plan_and_review_use_the_same_global_projections_and_approved_context() -> None:
    canonical, graph = _global_inputs(boundary=True)
    canonical["preconditions"] = [{"id": "PRE-1", "text": "The actor is signed in."}]
    validator = DesignValidator(RoutedFakeLlmClient())
    plan = validator.build_review_plan(canonical_requirement=canonical, vertical_design_graph=graph)
    client = StaticLlmClient([
        _runtime_passed(),
        _task_success("SC-1:MF-1", "SC-1:surface:ui", "SC-1:GLOBAL:PRE-1", "SC-1:GLOBAL:POST-3"),
        _task_success("SC-2:AF-1", "SC-2:surface:ui", "SC-2:GLOBAL:PRE-1", "SC-2:GLOBAL:POST-3"),
        _coherence(),
    ])

    result = _review(client, inputs=(canonical, graph), plan=plan)

    assert result.ok
    assert [task["task_id"] for task in plan["tasks"]] == ["runtime-sc-1", "scenario-sc-1", "scenario-sc-2"]
    assert plan["coverage"]["claim_count"] == 10
    decoder = json.JSONDecoder()
    for task, prompt in zip(plan["tasks"], client.prompts):
        packet, _ = decoder.raw_decode(prompt.split("Assigned review packet:\n", 1)[1])
        assert packet["approved_context"]["preconditions"] == [
            {"requirement_id": "PRE-1", "text": "The actor is signed in."}
        ]
        if task["task_type"] == "scenario_runtime":
            assert all(claim["claim_type"] != "global_projection" for claim in packet["claims"])
            continue
        assert {claim["claim_id"] for claim in packet["claims"]} == set(task["primary_claim_ids"])
        scenario_id = task["scenario_ids"][0]
        projections = {claim["claim_id"]: claim for claim in packet["claims"] if claim["claim_type"] == "global_projection"}
        assert set(projections) == {f"{scenario_id}:GLOBAL:PRE-1", f"{scenario_id}:GLOBAL:POST-3"}
        assert projections[f"{scenario_id}:GLOBAL:POST-3"] == {
            "claim_id": f"{scenario_id}:GLOBAL:POST-3",
            "claim_type": "global_projection",
            "projection_of": "GLOBAL:POST-3",
            "global_claim_type": "postcondition",
            "requirement_ids": ["POST-3"],
            "scenario_id": scenario_id,
            "statement": "Approved cross-scenario behavior.",
        }
        assert {item["requirement_id"] for item in packet["realized_in_other_scenarios"]}.isdisjoint({"PRE-1", "POST-3"})
        assert "A claim with claim_type=global_projection projects" in prompt
    coherence = client.prompts[-1]
    assert "Global projection assessments of the same projection_of claim must be compatible across scenarios" in coherence
    summary, _ = decoder.raw_decode(coherence.split("global_claims:\n", 1)[1])
    assert summary == result.llm_review["global_claims"]


def test_semantic_plan_rejects_missing_global_projections() -> None:
    claims = [
        {"claim_id": "GLOBAL:PRE-1", "claim_type": "precondition"},
        {"claim_id": "SC-1:MF-1", "claim_type": "requirement", "scenario_id": "SC-1"},
        {"claim_id": "SC-2:AF-1", "claim_type": "requirement", "scenario_id": "SC-2"},
    ]
    projected = with_global_projections(claims)
    assert projected[-1]["claim_id"] == "SC-2:GLOBAL:PRE-1"
    with pytest.raises(HarnessError, match="must own global projection SC-2:GLOBAL:PRE-1"):
        build_semantic_review_plan(claims=projected[:-1], vertical_design_graph={})


@pytest.mark.parametrize(
    ("claim_index", "reason", "error"),
    [
        (0, "Not applicable here.", "has an invalid status"),
        (-1, None, "requires a non-empty reason"),
        (-1, "  ", "requires a non-empty reason"),
    ],
)
def test_not_applicable_requires_a_projection_and_a_non_empty_reason(claim_index, reason, error) -> None:
    invalid = _projection_task(1, "POST-3", status="satisfied", refs=["VDN-ui-1"])
    invalid["assessments"][claim_index].update(status="not_applicable", reason=reason)
    client = StaticLlmClient([invalid, invalid])

    result = _review(client, inputs=_global_inputs())

    assert not result.ok and result.inconclusive
    assert len(client.prompts) == 2
    assert error in client.prompts[1]
    assert error in result.llm_review["error"]["message"]


@pytest.mark.parametrize(
    ("postcondition", "status", "expected", "blocking_id"),
    [
        (True, "not_applicable", "missing", "global-aggregation:GLOBAL:POST-3"),
        (True, "satisfied", "satisfied", ""),
        (True, "contradicted", "contradicted", "scenario-sc-1:SEM-PROJECTION"),
        (True, "missing", "missing", "scenario-sc-1:SEM-PROJECTION"),
        (True, "uncertain", "uncertain", ""),
        (False, "not_applicable", "satisfied", ""),
    ],
)
def test_global_claim_aggregation(postcondition, status, expected, blocking_id) -> None:
    requirement_id = "POST-3" if postcondition else "PRE-1"
    refs = ["VDN-ui-1"] if status != "not_applicable" else []
    client = StaticLlmClient([
        _projection_task(1, requirement_id, status=status, refs=refs),
        _projection_task(2, requirement_id),
        _coherence(),
        *(
            [_adjudication(blocking_id, remediation_mode="inconclusive" if not refs else "local_patch")]
            if blocking_id
            else []
        ),
    ])

    result = _review(client, inputs=_global_inputs(postcondition=postcondition))

    assert result.ok == (not blocking_id)
    [summary] = result.llm_review["global_claims"]
    assert summary["claim_id"] == f"GLOBAL:{requirement_id}"
    assert summary["status"] == expected
    assert [(item["scenario_id"], item["status"]) for item in summary["projections"]] == [
        ("SC-1", status), ("SC-2", "not_applicable")
    ]
    assert summary["projections"][0]["design_refs"] == refs
    assert summary["projections"][0]["reason"]
    assert [finding.finding_id for finding in result.blocking_findings] == ([blocking_id] if blocking_id else [])
    if blocking_id.startswith("global-aggregation"):
        [finding] = result.blocking_findings
        assert result.inconclusive
        assert finding.origin == "deterministic"
        assert finding.message == "GLOBAL:POST-3 is realized by no scenario"
        assert finding.claim_ids == ["GLOBAL:POST-3"]
        assert finding.requirement_ids == ["POST-3"]
        assert finding.diagram_ids == []
        assert finding.finding_id in client.prompts[-1]


@pytest.mark.parametrize(("drop_runtime_blocker", "status"), [(False, ""), (True, "satisfied"), (True, "not_applicable")])
def test_global_aggregation_recomputes_when_deferred_behavior_runs(drop_runtime_blocker, status) -> None:
    blocker = "runtime-sc-1:SEM-RUNTIME"
    conflicts = [{
        "finding_ids": [blocker], "claim_ids": ["SC-1:MF-1"], "design_refs": ["VDN-ui-1"],
        "reason": "Sibling evidence contradicts the runtime blocker.",
    }] if drop_runtime_blocker else []
    client = StaticLlmClient([
        _runtime_blocked("SC-1:MF-1"),
        _projection_task(2, "POST-3"),
        _coherence(conflicts=conflicts),
        _adjudication(drop_finding_ids=[blocker]) if drop_runtime_blocker else _adjudication(blocker),
        *(
            [_projection_task(1, "POST-3", status=status, refs=["VDN-ui-1"] if status == "satisfied" else [])]
            if drop_runtime_blocker
            else []
        ),
    ])

    result = _review(client, inputs=_global_inputs(boundary=True))

    assert '"status":"deferred"' in client.prompts[2].split("global_claims:\n", 1)[1].split("Relationship index:", 1)[0]
    expected = {"": "deferred", "satisfied": "satisfied", "not_applicable": "missing"}[status]
    assert result.llm_review["global_claims"][0]["status"] == expected
    assert result.ok == (status == "satisfied")
    assert not client.responses
    if status == "not_applicable":
        assert [finding.finding_id for finding in result.blocking_findings] == ["global-aggregation:GLOBAL:POST-3"]


def test_uc4_like_plan_fits_24_call_budget() -> None:
    canonical, graph = _global_inputs()
    canonical["scenario_contract"]["scenarios"] = [
        {"id": f"SC-{number}", "requirement_refs": ["MF-1"]} for number in range(1, 12)
    ]
    graph["nodes"] = [{"id": f"VDN-{number}", "scenario_id": f"SC-{number}"} for number in range(1, 12)]
    graph["edges"] = [
        {"id": f"VDE-{number}", "scenario_id": f"SC-{number}", "from": f"VDN-{number}", "to": f"VDN-{number}"}
        for number in range(1, 12)
    ]
    client = RoutedFakeLlmClient()

    result = _review(client, inputs=(canonical, graph), budget=24)

    assert result.ok
    tasks = result.semantic_review_plan["tasks"]
    assert len(tasks) == 22
    assert [task["task_type"] for task in tasks].count("scenario_runtime") == 11
    assert [task["task_type"] for task in tasks].count("scenario_behavior") == 11
    assert not any(task["task_id"] == "cross-cutting-global" for task in tasks)
    assert result.llm_review["planned_calls"] == len(tasks) + 2 == 24


def test_main_scenario_does_not_absorb_inbound_branch_but_branch_keeps_parent() -> None:
    claims = [
        {
            "claim_id": "SC-1:MF-1",
            "claim_type": "requirement",
            "scenario_id": "SC-1",
            "requirement_ids": ["MF-1"],
        },
        {
            "claim_id": "SC-2:AF-1",
            "claim_type": "requirement",
            "scenario_id": "SC-2",
            "requirement_ids": ["AF-1"],
        },
    ]
    graph = {
        "nodes": [
            {"id": "VDN-main-ui", "scenario_id": "SC-1"},
            {"id": "VDN-main-api", "scenario_id": "SC-1"},
            {"id": "VDN-branch-ui", "scenario_id": "SC-2"},
        ],
        "edges": [
            {
                "id": "VDE-main-call",
                "scenario_id": "SC-1",
                "from": "VDN-main-ui",
                "to": "VDN-main-api",
            },
            {
                "id": "VDE-branch-parent",
                "scenario_id": "SC-2",
                "from": "VDN-branch-ui",
                "to": "VDN-main-ui",
                "kind": "depends_on",
            },
        ],
    }

    plan = build_semantic_review_plan(claims=claims, vertical_design_graph=graph)
    main = next(task for task in plan["tasks"] if task["task_id"] == "scenario-sc-1")
    branch = next(task for task in plan["tasks"] if task["task_id"] == "scenario-sc-2")

    assert main["node_ids"] == ["VDN-main-api", "VDN-main-ui"]
    assert main["edge_ids"] == ["VDE-main-call"]
    assert branch["node_ids"] == ["VDN-branch-ui", "VDN-main-ui"]
    assert branch["edge_ids"] == ["VDE-branch-parent"]


def test_materialized_scenario_context_does_not_reintroduce_inbound_children() -> None:
    claims = [
        {
            "claim_id": "SC-1:AC-4",
            "claim_type": "requirement",
            "scenario_id": "SC-1",
            "requirement_ids": ["AC-4"],
        }
    ]
    main_task = {
        "task_id": "scenario-sc-1",
        "primary_claim_ids": [],
        "context_claim_ids": ["SC-1:AC-4"],
        "scenario_ids": ["SC-1"],
        "node_ids": ["VDN-main-ui", "VDN-main-api"],
        "edge_ids": ["VDE-main-call"],
    }
    graph = {
        "nodes": [
            {"id": "VDN-main-ui", "scenario_id": "SC-1"},
            {"id": "VDN-main-api", "scenario_id": "SC-1"},
            {"id": "VDN-branch-2-ui", "scenario_id": "SC-2"},
            {"id": "VDN-branch-2-rule", "scenario_id": "SC-2"},
            {"id": "VDN-branch-3-ui", "scenario_id": "SC-3"},
            {"id": "VDN-branch-3-rule", "scenario_id": "SC-3"},
        ],
        "edges": [
            {
                "id": "VDE-main-call",
                "scenario_id": "SC-1",
                "from": "VDN-main-ui",
                "to": "VDN-main-api",
            },
            {
                "id": "VDE-branch-2-parent",
                "scenario_id": "SC-2",
                "from": "VDN-branch-2-ui",
                "to": "VDN-main-ui",
                "kind": "depends_on",
            },
            {
                "id": "VDE-branch-2-call",
                "scenario_id": "SC-2",
                "from": "VDN-branch-2-ui",
                "to": "VDN-branch-2-rule",
            },
            {
                "id": "VDE-branch-3-parent",
                "scenario_id": "SC-3",
                "from": "VDN-branch-3-ui",
                "to": "VDN-main-ui",
                "kind": "depends_on",
            },
            {
                "id": "VDE-branch-3-call",
                "scenario_id": "SC-3",
                "from": "VDN-branch-3-ui",
                "to": "VDN-branch-3-rule",
            },
        ],
    }
    trace = {
        "mappings": [
            {"kind": "realizes", "from": "AC-4", "to": "VDN-main-ui"},
            {"kind": "realizes", "from": "AC-4", "to": "VDN-branch-2-ui"},
            {"kind": "depends_on", "from": "VDN-branch-2-ui", "to": "VDN-main-ui"},
            {"kind": "depends_on", "from": "VDN-branch-3-ui", "to": "VDN-main-ui"},
        ]
    }

    context = materialize_task_context(
        main_task,
        claims=claims,
        vertical_design_graph=graph,
        vertical_trace=trace,
    )

    assert [node["id"] for node in context["nodes"]] == ["VDN-main-ui", "VDN-main-api"]
    assert [edge["id"] for edge in context["edges"]] == ["VDE-main-call"]
    assert context["trace_mappings"] == [
        {"kind": "realizes", "from": "AC-4", "to": "VDN-main-ui"}
    ]


def test_materialized_context_closes_claim_trace_evidence_without_unrelated_graph() -> None:
    claims = [
        {
            "claim_id": "GLOBAL:POST-1",
            "claim_type": "postcondition",
            "scenario_id": "",
            "requirement_ids": ["POST-1"],
        },
        {
            "claim_id": "SC-3:EF-1",
            "claim_type": "requirement",
            "scenario_id": "SC-3",
            "requirement_ids": ["EF-1"],
        },
        {
            "claim_id": "SC-3:surface:data",
            "claim_type": "required_surface",
            "scenario_id": "SC-3",
            "requirement_ids": ["EF-1"],
            "surface": "data",
        },
    ]
    task = {
        "task_id": "cross-cutting-global",
        "primary_claim_ids": ["GLOBAL:POST-1"],
        "context_claim_ids": ["SC-3:EF-1", "SC-3:surface:data"],
        "scenario_ids": ["SC-3"],
        "node_ids": ["VDN-api"],
        "edge_ids": [],
    }
    graph = {
        "artifact_digest": "sha256:design",
        "nodes": [
            {"id": "VDN-api"},
            {"id": "VDN-rule"},
            {"id": "VDN-adapter"},
            {"id": "VDN-data"},
            {"id": "VDN-unrelated"},
            {"id": "VDN-other-scenario"},
        ],
        "edges": [
            {
                "id": "VDE-call-adapter",
                "scenario_id": "SC-3",
                "from": "VDN-rule",
                "to": "VDN-adapter",
            },
            {
                "id": "VDE-write",
                "scenario_id": "SC-3",
                "from": "VDN-adapter",
                "to": "VDN-data",
            },
            {
                "id": "VDE-return",
                "scenario_id": "SC-3",
                "from": "VDN-data",
                "to": "VDN-rule",
            },
            {
                "id": "VDE-dead-end",
                "scenario_id": "SC-3",
                "from": "VDN-rule",
                "to": "VDN-unrelated",
            },
            {
                "id": "VDE-other-scenario-1",
                "scenario_id": "SC-X",
                "from": "VDN-rule",
                "to": "VDN-other-scenario",
            },
            {
                "id": "VDE-other-scenario-2",
                "scenario_id": "SC-X",
                "from": "VDN-other-scenario",
                "to": "VDN-data",
            },
        ],
    }
    trace = {
        "mappings": [
            {"kind": "realizes", "from": "EF-1", "to": "VDN-rule"},
            {"kind": "realizes", "from": "SURF:SC-3:data", "to": "VDN-data"},
            {"kind": "realizes", "from": "OTHER-1", "to": "VDN-unrelated"},
        ]
    }

    context = materialize_task_context(
        task,
        claims=claims,
        vertical_design_graph=graph,
        vertical_trace=trace,
    )

    assert {node["id"] for node in context["nodes"]} == {
        "VDN-api",
        "VDN-rule",
        "VDN-adapter",
        "VDN-data",
    }
    assert [edge["id"] for edge in context["edges"]] == [
        "VDE-call-adapter",
        "VDE-write",
        "VDE-return",
    ]
    assert {mapping["to"] for mapping in context["trace_mappings"]} == {
        "VDN-rule",
        "VDN-data",
    }


def test_sliced_enforce_runs_specialist_then_coherence() -> None:
    client = StaticLlmClient(
        [
            _task_success("SC-1:MF-1", "SC-1:surface:ui"),
            _coherence(),
        ]
    )

    result = _review(client)

    assert result.ok
    assert result.mode == "design_validation_sliced_v1"
    assert result.semantic_review_plan["coverage"]["task_count"] == 1
    assert len(client.prompts) == 2
    assert result.llm_review["usage"]["call_count"] == 2
    assert result.llm_review["usage"]["duration_ms"] >= 0
    assert "Primary claims require exactly one assessment each" in client.prompts[0]
    assert "Transitions that leave the same `from` state are alternative branches" in client.prompts[0]
    assert "Paths assigned to other scenario ids are intentionally reviewed" in client.prompts[0]
    assert "Do not discard a shared acceptance criterion or rule" in client.prompts[0]
    assert "Design Semantic Scenario Review v2" in client.prompts[0]
    assert "already passed a separate runtime review" in client.prompts[0]
    assert "Treat runtime feasibility as" in client.prompts[0]
    assert "do not re-review or re-report transport" in client.prompts[0]
    assert "runtime_status" not in client.prompts[0]
    assert "compact relationship index" in client.prompts[1]
    assert "fixed scenario-local presentation categories" in client.prompts[1]
    assert "key reuse alone is not a contradiction" in client.prompts[1]
    assert "main scenario may declare only its 200 response" in client.prompts[1]
    assert "Never require one projection to copy a sibling's responses" in client.prompts[1]
    assert "Do not use a deferred claim's behavior or Requirement ids" in client.prompts[1]
    assert "emit a conflict with that exact finding id" in client.prompts[1]
    assert "every additional exact claim id needed to understand the counter-evidence" in client.prompts[1]


def test_each_task_type_gets_only_its_scope_instruction() -> None:
    client = StaticLlmClient([{"ok": True, "findings": []}] * 3)
    validator = DesignValidator(client)

    for task_type in ("scenario_behavior", "cross_cutting", "scenario_runtime"):
        validator._semantic_task_review(
            task_context={"task": {"task_type": task_type, "focus": "duplicated task focus"}},
            rule_review={},
        )

    behavior, global_review, runtime = client.prompts
    assert behavior.startswith("Design Semantic Scenario Review v2")
    assert "new Requirements.\n\nScenario-task scope:" in behavior
    assert "Treat runtime feasibility as" in behavior
    assert "do not re-review or re-report transport" in behavior
    assert "Runtime wiring is intentionally omitted" in behavior
    assert "A node from another scenario is compact branch evidence only" in behavior
    assert "edges[].boundary_contract.input_mapping" not in behavior
    assert "ui_repo_context describes the current AS-IS repository" not in behavior
    assert "ui_runtime_input_binding" not in behavior
    assert "Do not assume every task contains a finding" in behavior
    assert "An invented blocker and a missed blocker" not in behavior
    assert "For runtime, a step on the modeled path" not in behavior
    assert "Global-task scope:" not in behavior and "Runtime-task scope:" not in behavior
    assert global_review.startswith("Design Semantic Global Review v2")
    assert "new Requirements.\n\nGlobal-task scope:" in global_review
    assert "Do not report\n  their runtime feasibility" in global_review
    assert "Scenario-task scope:" not in global_review
    assert runtime.startswith("Design Semantic Runtime Review v2")
    assert "new Requirements.\n\nRuntime-task scope:" in runtime
    assert "return an empty assessments array" in runtime
    assert "Review the assigned scenario through every boundary:<edge id> in coverage_keys" in runtime
    assert "A provider-node outcome not named by a covered edge is in scope only" in runtime
    assert "A UI transition outcome is its observable postcondition" in runtime
    assert "repeated wording in the outcome and an edge mapping is not by itself" in runtime
    assert "Do not turn hypothetical outcomes or repair alternatives into findings" in runtime
    assert "modeled but non-required alternative or error outcome is a warning" in runtime
    assert "Do not infer that a provider re-tests an upstream guard" in runtime
    assert "edges[].boundary_contract.input_mapping" in runtime
    assert "ui_repo_context describes the current AS-IS repository" in runtime
    assert "add does not prove a new store is empty" in runtime
    assert "ui_state_coverage" not in runtime
    assert "Task focus:" not in runtime
    assert "duplicated task focus" not in runtime
    assert "remediation_mode" not in runtime
    assert "typed contracts cannot express the required behavior" not in runtime
    assert "Scenario-task scope:" not in runtime and "Global-task scope:" not in runtime


def test_sliced_enforce_invalid_task_is_inconclusive_not_a_false_pass() -> None:
    result = _review(StaticLlmClient(["not-json", "not-json"]))

    assert result.inconclusive
    assert not result.ok
    assert result.llm_review["error"]["error_code"] == "design_semantic_review_failed"


def test_runtime_blocker_skips_behavior_call_but_other_scenarios_and_coherence_run() -> None:
    client = StaticLlmClient(
        [
            _runtime_blocked("SC-1:MF-1", "SC-1:surface:ui"),
            _task_success("SC-2:AF-1", "SC-2:surface:ui"),
            _coherence(),
            _adjudication("runtime-sc-1:SEM-RUNTIME"),
        ]
    )

    result = _review(client, inputs=_inputs(shared_screen=True, boundary=True))

    assert not result.inconclusive
    assert not result.ok
    assert [item.finding_id for item in result.blocking_findings] == ["runtime-sc-1:SEM-RUNTIME"]
    # runtime-sc-1, (scenario-sc-1 skipped), scenario-sc-2, coherence, final adjudication
    assert len(client.prompts) == 4
    assert client.prompts[0].startswith("Design Semantic Runtime Review v2")
    assert client.prompts[1].startswith("Design Semantic Scenario Review v2")
    assert '"primary_claim_ids":["SC-2:AF-1","SC-2:surface:ui"]' in client.prompts[1]
    assert result.llm_review["actual_calls"] == 4
    tasks = {task["task_id"]: task for task in result.llm_review["tasks"]}
    skipped = tasks["scenario-sc-1"]
    assert skipped["status"] == "deferred_runtime_blocked"
    assert skipped["deferred_claim_ids"] == ["SC-1:MF-1", "SC-1:surface:ui"]
    assert {item["status"] for item in skipped["assessments"]} == {"deferred"}
    assert all(item["blocked_by"] == ["runtime-sc-1:SEM-RUNTIME"] for item in skipped["assessments"])
    assert tasks["scenario-sc-2"]["status"] == "reviewed"
    assert result.llm_review["runtime_blocked_scenarios"] == ["SC-1"]
    coherence_prompt = client.prompts[-2]
    assert "shared_surfaces" in coherence_prompt
    assert "shared-screen" in coherence_prompt
    assert "deferred_runtime_blocked" in coherence_prompt


def test_behavior_deferred_by_a_runtime_blocker_that_adjudication_drops_is_reviewed() -> None:
    # r7: SC-1's Token/Bearer runtime blocker was dropped by adjudication, but its deferred behavior review never ran
    # and the Design reached the checkpoint with SC-1's behavior claims unreviewed.
    client = StaticLlmClient(
        [
            _runtime_blocked("SC-1:MF-1", "SC-1:surface:ui"),
            _task_success("SC-2:AF-1", "SC-2:surface:ui"),
            _coherence(
                conflicts=[
                    {
                        "finding_ids": ["runtime-sc-1:SEM-RUNTIME"],
                        "claim_ids": ["SC-1:MF-1"],
                        "design_refs": ["VDN-ui-1"],
                        "reason": "sibling evidence contradicts the runtime blocker",
                    }
                ]
            ),
            _adjudication(drop_finding_ids=["runtime-sc-1:SEM-RUNTIME"]),
            _task_success("SC-1:MF-1", "SC-1:surface:ui"),
        ]
    )

    result = _review(client, inputs=_inputs(shared_screen=True, boundary=True))

    assert not result.inconclusive
    assert result.ok
    assert result.blocking_findings == []
    # runtime-sc-1, scenario-sc-2, coherence, adjudication, then the deferred scenario-sc-1
    assert len(client.prompts) == 5
    assert client.prompts[-1].startswith("Design Semantic Scenario Review v2")
    assert '"primary_claim_ids":["SC-1:MF-1","SC-1:surface:ui"]' in client.prompts[-1]
    assert result.llm_review["actual_calls"] == 5
    tasks = {task["task_id"]: task for task in result.llm_review["tasks"]}
    assert tasks["scenario-sc-1"]["status"] == "reviewed"
    assert {item["status"] for item in tasks["scenario-sc-1"]["assessments"]} == {"satisfied"}


def test_final_adjudication_remediation_mode_is_required_and_routes_fail_closed() -> None:
    blocker = _runtime_blocked("SC-1:MF-1")
    missing_mode = _adjudication("runtime-sc-1:SEM-RUNTIME")
    missing_mode["remediation_modes"][0].pop("remediation_mode")
    invalid = _review(
        StaticLlmClient([blocker, _coherence(), missing_mode, missing_mode]),
        inputs=_inputs(boundary=True),
    )
    assert invalid.inconclusive
    assert "remediation_mode" in invalid.llm_review["error"]["message"]

    rebuild = _review(
        StaticLlmClient(
            [
                _runtime_blocked("SC-1:MF-1"),
                _coherence(),
                _adjudication(
                    "runtime-sc-1:SEM-RUNTIME",
                    remediation_mode="partition_rebuild",
                ),
            ]
        ),
        inputs=_inputs(boundary=True),
    )
    assert rebuild.blocking_findings[0].remediation_mode == "partition_rebuild"
    assert rebuild.blocking_findings[0].to_dict()["remediation_mode"] == "partition_rebuild"
    assert rebuild.blocking_findings[0].remediation_reason == "The final evidence supports this repair route."
    assert (
        rebuild.blocking_findings[0].to_dict()["remediation_reason"]
        == "The final evidence supports this repair route."
    )

    inconclusive = _review(
        StaticLlmClient(
            [
                _runtime_blocked("SC-1:MF-1"),
                _coherence(),
                _adjudication(
                    "runtime-sc-1:SEM-RUNTIME",
                    remediation_mode="inconclusive",
                ),
            ]
        ),
        inputs=_inputs(boundary=True),
    )
    assert inconclusive.inconclusive
    assert not inconclusive.ok


def test_final_adjudication_prompt_requires_a_concrete_repair_witness() -> None:
    client = StaticLlmClient(
        [
            _runtime_blocked("SC-1:MF-1"),
            _coherence(),
            _adjudication("runtime-sc-1:SEM-RUNTIME"),
        ]
    )

    _review(client, inputs=_inputs(boundary=True))

    prompt = client.prompts[-1]
    assert prompt.startswith("Design Semantic Adjudication v2")
    assert "reason is forwarded unchanged to the Designer" in prompt
    assert "expected_change as a semantic postcondition, not a prescribed topology" in prompt
    assert "never invent an observation with no provider" in prompt
    assert "remove, add or reorder" in prompt
    assert "one unique\n  target_id per affected component" in prompt
    assert "depends_on may name\n  only target_ids in the same route, must be acyclic" in prompt
    assert "that same finding's diagram_ids, plus subject_ref" in prompt
    assert "Other Exact Design evidence is read-only context" in prompt
    assert "have at least one endpoint in the target component" in prompt
    assert "Scope ids are anchors, not an instruction to mutate every" in prompt
    assert "owner-to-provider call, provider-to-consumer return" in prompt
    assert "earlier edge whose source did not observe it" in prompt


def test_final_adjudication_persists_component_scoped_dependency_steps() -> None:
    canonical, graph = _inputs(boundary=True)
    graph["nodes"].append(
        {
            "id": "VDN-data-1",
            "scenario_id": "SC-1",
            "component_id": "database",
            "surface": "data",
            "kind": "repository",
            "requirement_refs": ["MF-1"],
        }
    )
    graph["edges"].extend(
        [
            {
                "id": "VDE-api-data",
                "scenario_id": "SC-1",
                "from": "VDN-api-1",
                "to": "VDN-data-1",
                "kind": "calls",
                "requirement_refs": ["MF-1"],
            },
            {
                "id": "VDE-data-api",
                "scenario_id": "SC-1",
                "from": "VDN-data-1",
                "to": "VDN-api-1",
                "kind": "returns",
                "requirement_refs": ["MF-1"],
            },
        ]
    )
    blocker = _runtime_blocked("SC-1:MF-1")
    finding = blocker["findings"][0]
    finding["subject_ref"] = "VDN-api-1"
    finding["diagram_ids"] = ["VDN-api-1", "VDN-data-1", "VDE-api-data", "VDE-data-api"]
    adjudication = _adjudication("runtime-sc-1:SEM-RUNTIME")
    adjudication["remediation_modes"][0]["repair_steps"] = [
        {
            "target_id": "SC-1@database",
            "diagram_ids": ["VDN-data-1", "VDE-data-api"],
            "expected_change": "Declare the interrupted data outcome.",
            "depends_on": [],
        },
        {
            "target_id": "SC-1@api",
            "diagram_ids": ["VDN-api-1", "VDE-api-data", "VDE-data-api"],
            "expected_change": "Consume interrupted and retry the same call once.",
            "depends_on": ["SC-1@database"],
        },
    ]

    result = _review(
        StaticLlmClient([blocker, _coherence(), adjudication]),
        inputs=(canonical, graph),
    )

    assert not result.inconclusive
    assert result.blocking_findings[0].repair_steps == adjudication["remediation_modes"][0]["repair_steps"]
    assert result.blocking_findings[0].to_dict()["repair_steps"][1]["depends_on"] == ["SC-1@database"]


def test_final_adjudication_rejects_cyclic_repair_steps() -> None:
    blocker = _runtime_blocked("SC-1:MF-1")
    adjudication = _adjudication("runtime-sc-1:SEM-RUNTIME")
    adjudication["remediation_modes"][0]["repair_steps"] = [
        {
            "target_id": "SC-1@web",
            "diagram_ids": ["VDN-ui-1"],
            "expected_change": "Repair the web node.",
            "depends_on": ["SC-1@web"],
        }
    ]

    result = _review(
        StaticLlmClient([blocker, _coherence(), adjudication, adjudication]),
        inputs=_inputs(boundary=True),
    )

    assert result.inconclusive
    assert "invalid or duplicate repair step" in result.llm_review["error"]["message"]


def test_runtime_pass_or_warning_lets_behavior_review_run() -> None:
    for runtime_payload in (_runtime_passed(), _runtime_blocked("SC-1:MF-1", severity="warning")):
        client = StaticLlmClient(
            [
                runtime_payload,
                _task_success("SC-1:MF-1", "SC-1:surface:ui"),
                _task_success("SC-2:AF-1", "SC-2:surface:ui"),
                _coherence(),
            ]
        )

        result = _review(client, inputs=_inputs(shared_screen=True, boundary=True))

        assert result.ok
        assert len(client.prompts) == 4
        assert result.llm_review["runtime_blocked_scenarios"] == []
        assert all(task["status"] == "reviewed" for task in result.llm_review["tasks"])


def test_model_cannot_defer_claims_itself() -> None:
    payload = _task_success("SC-1:MF-1", "SC-1:surface:ui")
    payload["assessments"][0]["status"] = "deferred"

    result = _review(StaticLlmClient([payload, payload]))

    assert result.inconclusive
    assert result.llm_review["error"]["error_code"] == "design_semantic_review_failed"


def test_runtime_task_that_assesses_claims_is_inconclusive() -> None:
    payload = _runtime_passed()
    payload["assessments"] = _task_success("SC-1:MF-1")["assessments"]

    result = _review(StaticLlmClient([payload, payload]), inputs=_inputs(boundary=True))

    assert result.inconclusive
    assert result.llm_review["error"]["error_code"] == "design_semantic_review_failed"


def test_sliced_budget_is_checked_before_any_provider_call() -> None:
    client = StaticLlmClient([])

    result = _review(client, budget=2)

    assert result.inconclusive
    assert client.prompts == []
    assert result.llm_review["error"]["error_code"] == "design_semantic_review_budget_exceeded"


def test_coherence_conflict_triggers_one_targeted_adjudication() -> None:
    specialist = _task_success("SC-1:MF-1", "SC-1:surface:ui")
    specialist["findings"] = [_warning_finding("SEM-TARGET"), _warning_finding("SEM-OTHER")]
    client = StaticLlmClient(
        [
            specialist,
            _coherence(
                conflicts=[
                    {
                        "finding_ids": ["scenario-sc-1:SEM-TARGET"],
                        "claim_ids": ["SC-1:MF-1"],
                        "design_refs": ["VDN-ui-1"],
                        "reason": "Sibling evidence may disprove this finding.",
                    }
                ]
            ),
            {
                "ok": True,
                "summary": "resolved",
                "drop_finding_ids": ["scenario-sc-1:SEM-TARGET"],
                    "remediation_modes": [],
                "findings": [],
            },
        ]
    )

    result = _review(client)

    assert result.ok
    assert result.llm_review["actual_calls"] == 3
    adjudication_prompt = client.prompts[-1]
    assert "Resolve the supplied conflicts, then classify every retained existing blocking finding" in adjudication_prompt
    assert '"id":"scenario-sc-1:SEM-TARGET"' in adjudication_prompt
    assert "scenario-sc-1:SEM-OTHER" not in adjudication_prompt
    assert '"claim_id":"SC-1:MF-1"' in adjudication_prompt
    assert "SC-1:surface:ui" not in adjudication_prompt
    assert '"id":"VDN-ui-1"' in adjudication_prompt
    assert '"action":"confirm"' in adjudication_prompt


def test_coherence_conflicts_require_exact_existing_ids() -> None:
    invalid_conflicts = [
        {
            "finding_ids": [],
            "claim_ids": ["SC-1:MF-1"],
            "design_refs": ["VDN-ui-1"],
            "reason": "missing finding",
        },
        {
            "finding_ids": ["scenario-sc-1:SEM-TARGET"],
            "claim_ids": ["SC-UNKNOWN:MF-1"],
            "design_refs": ["VDN-ui-1"],
            "reason": "unknown claim",
        },
        {
            "finding_ids": ["scenario-sc-1:SEM-TARGET"],
            "claim_ids": ["SC-1:MF-1"],
            "design_refs": ["VDN-unknown"],
            "reason": "unknown design ref",
        },
    ]
    for conflict in invalid_conflicts:
        specialist = _task_success("SC-1:MF-1", "SC-1:surface:ui")
        specialist["findings"] = [_warning_finding("SEM-TARGET")]
        invalid_coherence = _coherence(conflicts=[conflict])
        client = StaticLlmClient([specialist, invalid_coherence, invalid_coherence])

        result = _review(client)

        assert result.inconclusive
        assert not result.ok
        assert result.llm_review["error"]["error_code"] == "design_semantic_review_failed"
        assert len(client.prompts) == 3
        assert "Harness contract correction" in client.prompts[-1]


def test_api_only_shape_does_not_invent_ui_shared_task() -> None:
    claims = [
        {
            "claim_id": "SC-A:surface:api",
            "claim_type": "required_surface",
            "scenario_id": "SC-A",
            "requirement_ids": ["MF-A"],
            "surface": "api",
            "statement": "API required",
        }
    ]
    graph = {
        "artifact_digest": "sha256:api",
        "nodes": [
            {
                "id": "VDN-api",
                "scenario_id": "SC-A",
                "component_id": "service",
                "surface": "api",
                "kind": "api_endpoint",
                "surface_contract": {"method": "POST", "route": "/jobs"},
            }
        ],
        "edges": [],
    }

    plan = build_semantic_review_plan(claims=claims, vertical_design_graph=graph)

    assert [task["task_type"] for task in plan["tasks"]] == ["scenario_behavior"]


def test_fake_mode_supports_sliced_specialist_and_coherence_prompts() -> None:
    canonical, graph = _inputs()

    result = DesignValidator(RoutedFakeLlmClient()).review(
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=_traceability(),
        canonical_requirement=canonical,
        vertical_design_graph=graph,
        semantic_call_budget=8,
    )

    assert result.ok
    assert result.llm_review["actual_calls"] == 2


def test_sliced_step_review_replays_approved_tasks_without_duplicate_provider_calls() -> None:
    base = MetadataStaticLlmClient(
        [
            _task_success("SC-1:MF-1", "SC-1:surface:ui"),
            _coherence(),
        ]
    )
    canonical, graph = _inputs()

    def run(store: RuntimeStore, review: dict[str, object]):
        client = ReviewableLlmClient(
            base,
            store=store,
            run_id="run_20261102_000000_abcdef12",
            review={"review_output": True, **review},
            call_prefix="design-validator",
            execution_dir="design_validator_execution/design_v1",
            error_prefix="design_validator",
            label="Design Validator",
        )
        return DesignValidator(client).review(
            class_graph={"nodes": []},
            sequence_graph={"sequenceDiagrams": []},
            traceability=_traceability(),
            canonical_requirement=canonical,
            vertical_design_graph=graph,
            semantic_call_budget=8,
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
        try:
            run(store, {})
        except LlmStepReviewRequired as first:
            first_id = str(first.progress["call_id"])
        else:
            raise AssertionError("First sliced task did not pause for output review.")
        try:
            run(store, {"call_id": first_id, "decision": "approve"})
        except LlmStepReviewRequired as second:
            second_id = str(second.progress["call_id"])
        else:
            raise AssertionError("Coherence call did not pause for output review.")
        result = run(store, {"call_id": second_id, "decision": "approve"})

    assert result.ok
    assert len(base.prompts) == 2
    assert first_id != second_id


def _branch_graph(child_transitions: list[dict[str, str]]) -> dict[str, object]:
    parent_transitions = [
        {"from": "idle", "to": "dialog open", "action": "open dialog"},
        {"from": "dialog open", "to": "request pending", "action": "confirm"},
        {"from": "request pending", "to": "done", "action": "complete"},
    ]
    return {
        "nodes": [
            {"id": "VDN-parent", "scenario_id": "SC-1", "surface_contract": {"transitions": parent_transitions}},
            {"id": "VDN-child", "scenario_id": "SC-2", "surface_contract": {"transitions": child_transitions}},
        ],
        "edges": [{"id": "VDE-dep", "scenario_id": "SC-2", "from": "VDN-child", "to": "VDN-parent", "kind": "depends_on"}],
    }


def _branch_entries(graph: dict[str, object]) -> list[dict[str, object]]:
    claims = [{"claim_id": "SC-2:AF-1", "claim_type": "requirement", "scenario_id": "SC-2", "requirement_ids": ["AF-1"]}]
    task = next(
        task
        for task in build_semantic_review_plan(claims=claims, vertical_design_graph=graph)["tasks"]
        if task["task_type"] == "scenario_runtime"
    )
    context = materialize_task_context(task, claims=claims, vertical_design_graph=graph, vertical_trace={})
    return context["branch_entries"]


def test_branch_entry_reports_parent_transitions_leading_to_and_after_entry_by_exact_state() -> None:
    [entry] = _branch_entries(
        _branch_graph(
            [
                {"from": "dialog open", "to": "cancelling", "action": "cancel"},
                {"from": "cancelling", "to": "cancelled", "action": "finish"},
            ]
        )
    )

    assert entry["status"] == "resolved"
    assert entry["parent_entry_state"] == "dialog open"
    assert [item["action"] for item in entry["parent_transitions_leading_to_entry"]] == ["open dialog"]
    assert [item["action"] for item in entry["parent_transitions_after_entry"]] == ["confirm", "complete"]


def test_behavior_packet_keeps_behavior_evidence_but_drops_runtime_and_foreign_scenario_detail() -> None:
    graph = _branch_graph(
        [
            {"from": "dialog open", "to": "cancelling", "action": "cancel"},
            {"from": "cancelling", "to": "cancelled", "action": "finish"},
        ]
    )
    parent, child = graph["nodes"]
    parent["runtime_binding"] = {"target": {"path": "parent.tsx", "symbol": "Parent"}}
    parent["surface_contract"]["experience_contract"] = {"focus_flow": ["foreign detail"]}
    child["runtime_binding"] = {"target": {"path": "child.tsx", "symbol": "Child"}}
    child["surface_contract"].update(
        {
            "runtime_surface": {"required_inputs": [{"name": "runtimeOnly"}]},
            "experience_contract": {"focus_flow": ["active behavior"]},
        }
    )
    graph["nodes"].append(
        {
            "id": "VDN-rule",
            "scenario_id": "SC-2",
            "surface": "business_rule",
            "surface_contract": {"decision": "cancelled means done"},
        }
    )
    graph["edges"].append(
        {
            "id": "VDE-call",
            "scenario_id": "SC-2",
            "from": "VDN-child",
            "to": "VDN-rule",
            "kind": "calls",
            "order": 2,
            "boundary_contract": {
                "consumer_operation": "show cancellation completed",
                "provider_operation": "runtimeOnly",
                "auth_mapping": [{"from": "session", "to": "session"}],
                "input_mapping": [{"from": "confirmed", "to": "confirmed"}],
                "response_mapping": [{"from": "cancelled", "to": "completion visible"}],
                "error_mapping": [],
            },
        }
    )
    graph["snapshot_binding"] = {"ui_repo_context": {"entrypoint": {"path": "main.tsx"}}}
    claims = [
        {
            "claim_id": "SC-2:AF-1",
            "claim_type": "requirement",
            "scenario_id": "SC-2",
            "requirement_ids": ["AF-1"],
            "statement": "Cancellation completes.",
        }
    ]
    task = next(
        task
        for task in build_semantic_review_plan(claims=claims, vertical_design_graph=graph)["tasks"]
        if task["task_type"] == "scenario_behavior"
    )

    context = materialize_task_context(task, claims=claims, vertical_design_graph=graph, vertical_trace={})

    child_view = next(node for node in context["nodes"] if node["id"] == "VDN-child")
    parent_view = next(node for node in context["nodes"] if node["id"] == "VDN-parent")
    call_view = next(edge for edge in context["edges"] if edge["id"] == "VDE-call")
    assert child_view["surface_contract"]["experience_contract"] == {"focus_flow": ["active behavior"]}
    assert "runtime_binding" not in child_view
    # Only which code the node owns stays, so prose naming other modules can be checked against it.
    assert child_view["owned_code"] == ["child.tsx#Child"]
    assert "runtime_surface" not in child_view["surface_contract"]
    assert parent_view == {
        "id": "VDN-parent",
        "scenario_id": "SC-1",
        "surface_contract": {"transitions": parent["surface_contract"]["transitions"]},
    }
    assert call_view["boundary_contract"] == {
        "consumer_operation": "show cancellation completed",
        "response_mapping": [{"from": "cancelled", "to": "completion visible"}],
    }
    assert "ui_repo_context" not in context


def test_branch_entry_lists_paths_to_and_after_entry_in_a_cyclic_parent() -> None:
    def ui(transitions: list[tuple[str, str, str]]) -> dict[str, object]:
        return {
            "surface_contract": {
                "transitions": [{"from": a, "action": act, "to": b} for a, act, b in transitions],
            }
        }

    parent = ui(
        [
            ("idle", "open", "confirming"),
            ("confirming", "confirm", "deleting"),
            ("deleting", "fail", "error"),
            ("error", "retry", "idle"),
        ]
    )
    child = ui([("confirming", "cancel", "idle")])

    entry = _branch_entry({"id": "E", "from": "C", "to": "P"}, nodes_by_id={"C": child, "P": parent})

    assert entry["parent_entry_state"] == "confirming"
    # The retry cycle makes the delete path one way to reach the entry state again ...
    assert [item["action"] for item in entry["parent_transitions_leading_to_entry"]] == ["open", "fail", "retry"]
    # ... while every transition reachable from the entry state is still listed as able to follow it.
    assert [item["action"] for item in entry["parent_transitions_after_entry"]] == ["open", "confirm", "fail", "retry"]


def test_branch_entry_never_guesses_a_similar_or_ambiguous_parent_state() -> None:
    [no_match] = _branch_entries(_branch_graph([{"from": "confirmed request pending", "to": "error", "action": "drop"}]))
    [ambiguous] = _branch_entries(
        _branch_graph(
            [
                {"from": "dialog open", "to": "x", "action": "a"},
                {"from": "request pending", "to": "y", "action": "b"},
            ]
        )
    )

    assert no_match["status"] == "unresolved"
    assert no_match["child_start_states"] == ["confirmed request pending"]
    assert "parent_entry_state" not in no_match
    assert ambiguous["status"] == "unresolved"
    assert "ambiguous" in ambiguous["reason"]


def test_every_prompt_explains_packet_fields_severity_and_deterministic_scope() -> None:
    client = StaticLlmClient([{"ok": True, "findings": []}] * 3)
    validator = DesignValidator(client)

    for task_type in ("scenario_behavior", "cross_cutting", "scenario_runtime"):
        validator._semantic_task_review(task_context={"task": {"task_type": task_type}}, rule_review={})

    for prompt in client.prompts:
        # A: Harness-owned vocabulary is defined instead of left for the model to guess.
        assert "Packet field guide" in prompt
        assert "branch_entries are Harness-derived from exact state names" in prompt
        assert "approved_context" in prompt
        assert "Set ok=true exactly when findings contains no blocking item" in prompt
        # C: one explicit blocking/warning criterion, no use-case-specific examples.
        assert "Severity:" in prompt
        assert "cancel API" not in prompt
        assert "not a test" not in prompt.replace("It is not a test: no defect was planted", "")
        # F: the deterministic scope is listed, not implied by ok=true.
        assert "branch parent wiring" in prompt
        # Findings describe the required outcome on existing ids; they never design or invent a system.
        assert "expected_change states one invariant that must become true" in prompt
        assert "do not select, add, remove or reorder an edge" in prompt
        assert "These ids authorize possible repair" in prompt
        assert "they do not\n  prescribe which id must change" in prompt
        assert "not introduce a system, service or dependency" in prompt

    behavior, global_review, runtime = client.prompts
    assert "Runtime wiring is intentionally omitted" in behavior
    assert "A UI transition outcome is its observable postcondition" in behavior
    assert "ui_repo_context describes the current AS-IS repository" not in behavior
    assert "composition forwarded-input propagation" not in behavior
    assert "primary claim or promised behavior guarantee" in behavior
    assert "Do not assume every task contains a finding" in behavior
    assert "An invented blocker and a missed blocker" not in behavior
    for prompt in (global_review, runtime):
        assert "A declared external input is an\n  accepted dependency, not a missing provider." in prompt
        assert "externally_blocked = runnable once its\n  declared external inputs exist" in prompt
        assert "ui_repo_context describes the current AS-IS repository" in prompt
        assert "composition forwarded-input propagation, branch parent wiring" in prompt
        assert "not evidence that this Design is semantically sound" in " ".join(prompt.split())
        # The facts proved for this packet are listed so the review does not re-derive them (r7 cost).
        assert "packet.harness_established lists the facts it proved for this packet" in " ".join(prompt.split())
        assert "spend the review on packet.harness_established.not_established" in " ".join(prompt.split())
        assert "explicitly modeled but non-required alternative or error outcome" in prompt
        assert "do not assume every\n  task contains a finding" in prompt
        assert "An invented blocker and a missed blocker" not in prompt


def test_each_task_type_gets_only_its_own_rules() -> None:
    client = StaticLlmClient([{"ok": True, "findings": []}] * 3)
    validator = DesignValidator(client)

    for task_type in ("scenario_behavior", "cross_cutting", "scenario_runtime"):
        validator._semantic_task_review(task_context={"task": {"task_type": task_type}}, rule_review={})

    behavior, global_review, runtime = client.prompts
    runtime_rule = "Review the assigned scenario through every boundary:<edge id> in coverage_keys"
    behavior_rule = "The UI experience contract (states, feedback, focus, accessibility, responsive layout)"
    global_rule = "Judge only primary global preconditions, postconditions, quality requirements and negative constraints"
    assert runtime_rule in runtime and runtime_rule not in behavior and runtime_rule not in global_review
    assert "is a\n  decision_input_sufficiency defect even when an input_mapping names it" in runtime
    assert "Do not audit UI completeness, feedback" in runtime
    assert behavior_rule in behavior and behavior_rule not in runtime and behavior_rule not in global_review
    assert global_rule in global_review and global_rule not in runtime and global_rule not in behavior
    # A case of a multi-case claim that no scenario realizes is a blocker, never a hedged warning (r7 SC-5 delete).
    case_rule = "A\n  case that neither realizes has no Design: report a blocking finding"
    assert case_rule in behavior and case_rule not in runtime and case_rule not in global_review
    assert "realized_in_other_scenarios lists, for each Requirement item of a non-projection primary claim" in behavior
    # A contract needing a change in code no node owns has no implementation owner (r7 SC-4 editor slice).
    assert "that no\n  packet node's owned_code includes, no implementation unit can change it" in behavior
    for prompt in (behavior, global_review, runtime):
        assert "It is not a test: no defect was planted" in prompt


def test_behavior_packet_lists_other_scenarios_realizing_each_primary_requirement_item() -> None:
    claims = [
        {
            "claim_id": "SC-1:AC-5",
            "claim_type": "requirement",
            "scenario_id": "SC-1",
            "requirement_ids": ["AC-5", "BR-1"],
            "statement": "An edit or delete request by another user is refused with 403 and a message.",
        }
    ]
    graph = {
        "nodes": [
            {"id": "VDN-edit", "scenario_id": "SC-1", "surface": "api", "name": "Edit refusal"},
            {"id": "VDN-rule", "scenario_id": "SC-1", "surface": "business_rule", "name": "Edit guard"},
            {"id": "VDN-delete", "scenario_id": "SC-2", "surface": "api", "name": "Delete guard"},
            {"id": "VDN-dep", "scenario_id": "SC-2", "surface": "data", "name": "Delete write"},
        ],
        "edges": [{"id": "VDE-1", "scenario_id": "SC-1", "from": "VDN-edit", "to": "VDN-rule", "kind": "calls"}],
    }
    trace = {
        "mappings": [
            {"kind": "realizes", "from": "AC-5", "to": "VDN-edit"},
            {"kind": "realizes", "from": "BR-1", "to": "VDN-rule"},
            {"kind": "realizes", "from": "BR-1", "to": "VDN-delete"},
            {"kind": "depends_on", "from": "BR-1", "to": "VDN-dep"},
        ]
    }
    task = next(
        task
        for task in build_semantic_review_plan(claims=claims, vertical_design_graph=graph)["tasks"]
        if task["task_type"] == "scenario_behavior" and task.get("scenario_ids") == ["SC-1"]
    )

    context = materialize_task_context(task, claims=claims, vertical_design_graph=graph, vertical_trace=trace)

    # The empty list is the fact the r7 SC-5 review lacked: no other scenario realizes AC-5.
    assert context["realized_in_other_scenarios"] == [
        {"requirement_id": "AC-5", "subjects": []},
        {"requirement_id": "BR-1", "subjects": [{"id": "VDN-delete", "scenario_id": "SC-2", "name": "Delete guard"}]},
    ]
    runtime_task = next(
        task
        for task in build_semantic_review_plan(claims=claims, vertical_design_graph=graph)["tasks"]
        if task["task_type"] == "scenario_runtime"
    )
    runtime = materialize_task_context(runtime_task, claims=claims, vertical_design_graph=graph, vertical_trace=trace)
    assert "realized_in_other_scenarios" not in runtime


def test_runtime_packet_drops_ui_experience_traces_and_audit_fields_but_keeps_behavior_inputs() -> None:
    claims = [
        {"claim_id": "GLOBAL:PRE-1", "claim_type": "precondition", "requirement_ids": ["PRE-1"],
         "statement": "Operator is logged in."},
        {"claim_id": "SC-1:MF-1", "claim_type": "requirement", "scenario_id": "SC-1", "requirement_ids": ["MF-1"],
         "statement": "View the list produced by SearchThings."},
    ]
    screen = {
        "id": "VDN-screen",
        "scenario_id": "SC-1",
        "surface": "ui",
        "evidence_refs": ["repo:a#L1@sha256:x"],
        "evidence_reason": "GAP: long audit text",
        "semantic_key": "k",
        "source_observation_refs": ["repo:a"],
        "runtime_binding": {"target": {"symbol": "Screen"}, "evidence_refs": ["x"], "attachment": {"owner_ref": "RLA-1", "relation": "rendered_by"}},
        "surface_contract": {
            "states": ["ready"],
            "transitions": [{"from": "ready", "to": "done", "action": "go"}],
            "experience_contract": {"focus_flow": ["x"], "guidance_refs": ["g"], "repo_evidence_refs": ["r"]},
            "runtime_surface": {
                "evidence_refs": ["e"],
                "required_inputs": [
                    {"name": "records", "source": "external_use_case", "provided_by": "Root", "requirement_refs": ["MF-1"]},
                    {"name": "localDraft", "source": "local_state", "requirement_refs": ["MF-1"]},
                ],
            },
        },
    }
    graph = {"nodes": [screen, {"id": "VDN-api", "scenario_id": "SC-1", "surface": "api"}],
             "edges": [{"id": "VDE-1", "scenario_id": "SC-1", "from": "VDN-screen", "to": "VDN-api", "kind": "calls"}]}
    trace = {"mappings": [{"from": "MF-1", "to": "VDN-screen"}]}
    claims = with_global_projections(claims)
    plan = build_semantic_review_plan(claims=claims, vertical_design_graph=graph)
    runtime_task = next(task for task in plan["tasks"] if task["task_type"] == "scenario_runtime")
    behavior_task = next(task for task in plan["tasks"] if task["task_type"] == "scenario_behavior")

    runtime = materialize_task_context(runtime_task, claims=claims, vertical_design_graph=graph, vertical_trace=trace)
    behavior = materialize_task_context(behavior_task, claims=claims, vertical_design_graph=graph, vertical_trace=trace)

    runtime_screen = next(node for node in runtime["nodes"] if node["id"] == "VDN-screen")
    behavior_screen = next(node for node in behavior["nodes"] if node["id"] == "VDN-screen")
    assert "trace_mappings" not in runtime and behavior["trace_mappings"]
    assert "experience_contract" not in runtime_screen["surface_contract"]
    assert runtime_screen["surface_contract"]["transitions"] == screen["surface_contract"]["transitions"]
    assert behavior_screen["surface_contract"]["experience_contract"] == {"focus_flow": ["x"]}
    for view in (runtime_screen, behavior_screen):
        assert not {"evidence_refs", "evidence_reason", "semantic_key", "source_observation_refs"} & set(view)
    assert runtime_screen["runtime_binding"] == {
        "target": {"symbol": "Screen"},
        "attachment": {"relation": "rendered_by"},
    }
    assert "evidence_refs" not in runtime_screen["surface_contract"]["runtime_surface"]
    assert "runtime_binding" not in behavior_screen
    assert "runtime_surface" not in behavior_screen["surface_contract"]
    # The graph itself is untouched.
    assert screen["evidence_refs"] and "experience_contract" in screen["surface_contract"]
    # E: approved Requirement facts behind declared external inputs, plus preconditions.
    assert runtime["approved_context"] == {
        "preconditions": [{"requirement_id": "PRE-1", "text": "Operator is logged in."}],
        "external_inputs": [
            {"name": "records", "source": "external_use_case", "provided_by": "Root", "requirement_refs": ["MF-1"],
             "requirement_text": ["View the list produced by SearchThings."]}
        ],
    }


def _warning_with_bad_evidence_ref() -> dict[str, object]:
    payload = _runtime_passed()
    payload["findings"] = [
        {
            "id": "SEM-001",
            "category": "runtime_boundary_feasibility",
            "severity": "warning",
            "remediation_mode": "local_patch",
            "message": "Retry path has no modeled error event.",
            "expected_change": "Map the failure event.",
            "claim_ids": ["SC-1:MF-1"],
            "requirement_ids": ["MF-1"],
            "diagram_ids": ["VDN-ui-1"],
            "subject_ref": "VDN-ui-1",
            # Wrong prefix for an existing edge id: a contract error, not a semantic judgement.
            "evidence": [{"source": "design", "ref": "VDN-boundary-1", "excerpt": "edge"}],
        }
    ]
    return payload


def test_contract_invalid_output_is_retried_once_with_the_exact_error() -> None:
    client = StaticLlmClient(
        [
            _warning_with_bad_evidence_ref(),
            _runtime_passed(),
            _task_success("SC-1:MF-1", "SC-1:surface:ui"),
            _coherence(),
        ]
    )

    result = _review(client, inputs=_inputs(boundary=True))

    assert not result.inconclusive
    assert result.ok
    assert len(client.prompts) == 4
    assert "Harness contract correction" not in client.prompts[0]
    assert client.prompts[1].startswith(client.prompts[0])
    assert "Harness contract correction" in client.prompts[1]
    assert "'VDN-boundary-1'" in client.prompts[1]
    assert "Harness contract correction" not in client.prompts[2]
    runtime = next(task for task in result.llm_review["tasks"] if task["task_id"] == "runtime-sc-1")
    assert runtime["contract_retries"] == 1
    assert result.llm_review["actual_calls"] == 4


def test_contract_retry_preserves_previous_semantic_response_context() -> None:
    invalid = _task_success("SC-1:MF-1", "SC-1:surface:ui")
    invalid["assessments"] = [
        *invalid["assessments"],
        {
            "claim_id": "SC-1:INVENTED",
            "status": "satisfied",
            "design_refs": ["VDN-ui-1"],
            "reason": "Not a supplied claim.",
        },
    ]
    invalid["findings"] = [_warning_finding("SEM-KEEP")]
    corrected = _task_success("SC-1:MF-1", "SC-1:surface:ui")
    corrected["findings"] = [_warning_finding("SEM-KEEP")]
    client = StaticLlmClient([invalid, corrected, _coherence()])

    result = _review(client)

    assert result.ok
    assert [item.finding_id for item in result.warnings] == ["scenario-sc-1:SEM-KEEP"]
    retry_prompt = client.prompts[1]
    assert "Previous JSON response:" in retry_prompt
    assert '"claim_id":"SC-1:INVENTED"' in retry_prompt
    assert '"id":"SEM-KEEP"' in retry_prompt
    assert "Correct only the fields implicated by the Harness error" in retry_prompt
    assert "Preserve every other semantic conclusion" in retry_prompt


def test_empty_semantic_mutation_scope_is_retried_once() -> None:
    invalid = _warning_finding("SEM-001")
    invalid["diagram_ids"] = []
    invalid_response = _task_success("SC-1:MF-1", "SC-1:surface:ui")
    invalid_response["findings"] = [invalid]
    client = StaticLlmClient(
        [
            invalid_response,
            _task_success("SC-1:MF-1", "SC-1:surface:ui"),
            _coherence(),
        ]
    )

    result = _review(client)

    assert result.ok
    assert len(client.prompts) == 3
    assert "diagram_ids must be a non-empty list" in client.prompts[1]


def test_contract_invalid_output_twice_stays_inconclusive() -> None:
    client = StaticLlmClient([_warning_with_bad_evidence_ref(), _warning_with_bad_evidence_ref()])

    result = _review(client, inputs=_inputs(boundary=True))

    assert result.inconclusive
    assert not result.ok
    assert len(client.prompts) == 2
    assert "VDN-boundary-1" in result.llm_review["error"]["message"]


def test_coherence_contract_error_is_retried_once() -> None:
    bad_coherence = _coherence()
    bad_coherence["findings"] = [
        {
            "id": "SEM-X",
            "category": "behavior_order",
            "severity": "warning",
            "remediation_mode": "local_patch",
            "message": "x",
            "expected_change": "y",
            "claim_ids": ["SC-UNKNOWN"],
            "requirement_ids": ["MF-1"],
            "subject_ref": "VDN-ui-1",
            "evidence": [],
        }
    ]
    client = StaticLlmClient([_task_success("SC-1:MF-1", "SC-1:surface:ui"), bad_coherence, _coherence()])

    result = _review(client)

    assert result.ok
    assert len(client.prompts) == 3
    assert "Harness contract correction" in client.prompts[2]
    assert "SC-UNKNOWN" in client.prompts[2]


def _cached_review(
    client: StaticLlmClient, graph: dict[str, object], canonical: dict[str, object], cache: Path, version: int = 1
):
    return DesignValidator(client).review(
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=_traceability(),
        canonical_requirement=canonical,
        vertical_design_graph=graph,
        semantic_call_budget=20,
        semantic_task_cache=cache,
        semantic_task_cache_version=version,
    )


def test_identical_task_inputs_reuse_validated_results_and_only_changed_scenarios_rerun() -> None:
    canonical, graph = _inputs(shared_screen=True, boundary=True)
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "semantic_task_cache_v1.json"
        first = StaticLlmClient(
            [
                _runtime_passed(),
                _task_success("SC-1:MF-1", "SC-1:surface:ui"),
                _task_success("SC-2:AF-1", "SC-2:surface:ui"),
                _coherence(),
            ]
        )
        assert _cached_review(first, graph, canonical, cache).ok

        # Only the whole-graph digest changes: every task input is still identical, so only coherence runs.
        second = StaticLlmClient([_coherence()])
        rerun = _cached_review(second, {**graph, "artifact_digest": "sha256:other"}, canonical, cache, version=2)
        assert rerun.ok
        assert len(second.prompts) == 1 and second.prompts[0].startswith("Design Semantic Coherence Review v2")
        assert {task["status"] for task in rerun.llm_review["tasks"]} == {"reused_identical_input"}
        assert rerun.llm_review["actual_calls"] == 1

        # Editing an SC-2 node changes only the SC-2 packet; SC-1 results are reused.
        changed = json.loads(json.dumps(graph))
        changed["nodes"][1]["surface_contract"]["transitions"] = [{"action": "cancel quickly"}]
        third = StaticLlmClient([_task_success("SC-2:AF-1", "SC-2:surface:ui"), _coherence()])
        partial = _cached_review(third, changed, canonical, cache, version=3)
        assert partial.ok
        assert third.prompts[0].startswith("Design Semantic Scenario Review v2") and "SC-2:AF-1" in third.prompts[0]
        statuses = {task["task_id"]: task["status"] for task in partial.llm_review["tasks"]}
        assert statuses == {
            "runtime-sc-1": "reused_identical_input",
            "scenario-sc-1": "reused_identical_input",
            "scenario-sc-2": "reviewed",
        }


def test_contract_invalid_output_is_never_cached() -> None:
    canonical, graph = _inputs(boundary=True)
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "semantic_task_cache_v1.json"
        failed = _cached_review(
            StaticLlmClient([_warning_with_bad_evidence_ref(), _warning_with_bad_evidence_ref()]), graph, canonical, cache
        )
        assert failed.inconclusive
        assert not cache.exists()

        # A corrected retry is cached under the original task input, so the next identical run reuses it.
        recovered = StaticLlmClient(
            [_warning_with_bad_evidence_ref(), _runtime_passed(), _task_success("SC-1:MF-1", "SC-1:surface:ui"), _coherence()]
        )
        assert _cached_review(recovered, graph, canonical, cache, version=2).ok
        again = StaticLlmClient([_coherence()])
        assert _cached_review(again, graph, canonical, cache, version=3).ok
        assert len(again.prompts) == 1


def test_results_of_the_current_validation_version_are_not_reused_so_step_review_replay_stays_ordered() -> None:
    canonical, graph = _inputs(boundary=True)
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory) / "semantic_task_cache_v1.json"
        responses = [_runtime_passed(), _task_success("SC-1:MF-1", "SC-1:surface:ui"), _coherence()]
        assert _cached_review(StaticLlmClient(list(responses)), graph, canonical, cache, version=7).ok

        # A resume of the same version replays every call through the provider client, in the original order.
        replay = StaticLlmClient(list(responses))
        assert _cached_review(replay, graph, canonical, cache, version=7).ok
        assert len(replay.prompts) == 3


def test_sequence_operation_is_checked_against_the_receiver_node_even_when_class_names_repeat() -> None:
    from sdlc_harness.agents.validator.design_validation import _structural_findings

    name = "Refuse favoriting a draft (POST /api/articles/:slug/favorite)"
    class_graph = {
        "nodes": [
            {"id": "VDN-sc4", "className": name, "methods": [{"name": "listRequestsFavorite"}]},
            {"id": "VDN-sc3", "className": name, "methods": [{"name": "execute"}]},
        ]
    }

    def sequence(operation: str) -> dict[str, object]:
        return {
            "sequenceDiagrams": [
                {
                    "id": "sequence_sc_4",
                    "nodes": [{"id": "VDN-web", "type": "participant"}, {"id": "VDN-sc4", "label": name}],
                    "messages": [
                        {"id": "m1", "from": "VDN-web", "to": "VDN-sc4", "type": "synchronous", "message": f"{operation}()"}
                    ],
                }
            ]
        }

    assert _structural_findings(class_graph=class_graph, sequence_graph=sequence("listRequestsFavorite")) == []
    missing = _structural_findings(class_graph=class_graph, sequence_graph=sequence("unknownOperation"))
    assert [item.category for item in missing] == ["cross_diagram_contract"]
    assert name in missing[0].message


def test_partition_rebuild_may_add_a_provider_in_another_component_of_the_finding_scenario() -> None:
    from sdlc_harness.agents.validator.design_validation import _normalize_adjudication_repair_steps

    graph = {
        "nodes": [
            {"id": "VDN-api", "scenario_id": "SC-9", "component_id": "api"},
            {"id": "VDN-rule", "scenario_id": "SC-9", "component_id": "api"},
            {"id": "VDN-insert", "scenario_id": "SC-9", "component_id": "database"},
            {"id": "VDN-other", "scenario_id": "SC-1", "component_id": "database"},
        ],
        "edges": [{"id": "VDE-call", "scenario_id": "SC-9", "from": "VDN-api", "to": "VDN-rule"}],
    }
    finding = {"diagram_ids": ["VDN-api", "VDN-rule", "VDE-call"], "subject_ref": "VDN-rule"}

    def route(*steps: dict[str, object]) -> dict[str, object]:
        return {"finding_id": "runtime-sc-9:SEM-001", "repair_steps": list(steps)}

    add_step = {"target_id": "SC-9@database", "diagram_ids": [], "expected_change": "add a title read", "depends_on": []}
    api_step = {
        "target_id": "SC-9@api",
        "diagram_ids": ["VDN-api", "VDE-call"],
        "expected_change": "call the read first",
        "depends_on": ["SC-9@database"],
    }
    steps = _normalize_adjudication_repair_steps(
        route=route(add_step, api_step),
        finding=finding,
        remediation_mode="partition_rebuild",
        vertical_design_graph=graph,
    )
    assert [step["target_id"] for step in steps] == ["SC-9@database", "SC-9@api"]
    assert steps[0]["diagram_ids"] == []

    # Only a rebuild may add, only in the finding's own scenario, only in a known component.
    for mode, target in (
        ("local_patch", "SC-9@database"),
        ("partition_rebuild", "SC-1@database"),
        ("partition_rebuild", "SC-9@payments"),
    ):
        with pytest.raises(HarnessError):
            _normalize_adjudication_repair_steps(
                route=route({**add_step, "target_id": target}),
                finding=finding,
                remediation_mode=mode,
                vertical_design_graph=graph,
            )
