"""Contract and deterministic coverage tests for L4 vertical trace."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from sdlc_harness.agents.designer import DELTA_VERSION, apply_designer_delta, new_vertical_design_graph
from sdlc_harness.agents.requirements import RequirementModelV2
from sdlc_harness.agents.validator import (
    VERTICAL_TRACE_GRAPH_VERSION,
    bounded_trace_view,
    build_vertical_trace_graph,
    validate_vertical_trace_coverage,
    validate_vertical_trace_graph,
    validate_vertical_trace_report,
)
from sdlc_harness.agents.designer.graph import graph_digest
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.workflows.phase2 import _vertical_trace_repair_feedback


class VerticalTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(
            (Path(__file__).parent / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )
        cls.model = RequirementModelV2.from_dict(payload)

    def _design_graph(
        self,
        *,
        skip: tuple[str, str] | None = None,
        unknown_ref: bool = False,
        include_not_applicable: tuple[str, str] | None = None,
        include_branch_parents: bool = True,
    ) -> dict[str, object]:
        graph = new_vertical_design_graph(
            run_id=self.model.run_id,
            snapshot_binding={
                "requirement_digest": "sha256:req",
                "repo_landscape_digest": "sha256:repo",
                "workspace_ref": "run_workspace_v1",
                "base_sha": "abc123",
            },
        )
        scenario_contract = self.model.scenario_contract
        components = {"ui": "web", "api": "api", "business_rule": "api", "data": "database"}
        kinds = {"ui": "ui_interaction", "api": "api_contract", "business_rule": "business_rule", "data": "data_operation"}
        for scenario in scenario_contract["scenarios"]:
            scenario_id = str(scenario["id"])
            for surface, contract in scenario["surfaces"].items():
                selected = contract["status"] == "required" or include_not_applicable == (scenario_id, surface)
                if not selected or skip == (scenario_id, surface):
                    continue
                requirement_refs = list(contract.get("requirement_refs") or scenario.get("requirement_refs") or [scenario_id])
                if unknown_ref and not graph["nodes"]:
                    requirement_refs = ["UNKNOWN-1"]
                graph, _ = apply_designer_delta(
                    graph,
                    {
                        "schema_version": DELTA_VERSION,
                        "base_graph_digest": graph["artifact_digest"],
                        "partition": {
                            "use_case_id": str(self.model.use_case["id"]),
                            "scenario_id": scenario_id,
                            "component_id": components[surface],
                        },
                        "operations": [
                            {"op": "set_partition_coverage", "coverage": {surface: "covered"}},
                            {
                                "op": "add_node",
                                "node": {
                                    "temp_ref": f"tmp:{scenario_id}:{surface}",
                                    "semantic_key": f"{scenario_id}:{surface}",
                                    "name": f"{scenario_id} {surface}",
                                    "kind": kinds[surface],
                                    "surface": surface,
                                    "disposition": "add",
                                    "requirement_refs": requirement_refs,
                                    "evidence_refs": [],
                                    "evidence_reason": "Required behavior is new in the frozen repo.",
                                    "contract": f"Realize {surface} for {scenario_id}.",
                                },
                            },
                        ],
                        "completion_claims": [],
                        "unresolved_items": [],
                    },
                    valid_evidence_refs=set(),
                )
        if include_branch_parents:
            main = next(item for item in graph["nodes"] if item["scenario_id"] == "SC-1")
            scenario_kinds = {
                str(item.get("id") or ""): str(item.get("kind") or "")
                for item in self.model.scenario_contract["scenarios"]
            }
            for branch_id, scenario_kind in scenario_kinds.items():
                if scenario_kind not in {"alternative", "exception"}:
                    continue
                branch = next(item for item in graph["nodes"] if item["scenario_id"] == branch_id)
                graph["edges"].append(
                    {
                        "id": f"VDE-{branch_id.lower()}-parent",
                        "from": branch["id"],
                        "to": main["id"],
                        "kind": "depends_on",
                        "label": f"{branch_id} depends on SC-1",
                        "branch_group": "",
                        "guard": "",
                        "terminal": False,
                        "scenario_id": branch_id,
                        "order": 1,
                    }
                )
            graph["artifact_digest"] = graph_digest(graph)
        return graph

    def test_build_is_deterministic_and_design_gate_passes(self) -> None:
        design = self._design_graph()
        first = build_vertical_trace_graph(requirement_model=self.model, design_graph=design)
        second = build_vertical_trace_graph(requirement_model=self.model, design_graph=deepcopy(design))

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], VERTICAL_TRACE_GRAPH_VERSION)
        result = validate_vertical_trace_coverage(first, mode="shadow")
        self.assertTrue(result.ok)
        self.assertEqual(result.coverage["required_surface_count"], result.coverage["covered_surface_count"])

    def test_missing_required_surface_is_exact_blocker(self) -> None:
        graph = build_vertical_trace_graph(
            requirement_model=self.model,
            design_graph=self._design_graph(skip=("SC-1", "api")),
        )
        result = validate_vertical_trace_coverage(graph, mode="enforce")

        self.assertFalse(result.ok)
        finding = next(item for item in result.blocking_findings if item.category == "required_surface_missing")
        self.assertEqual(finding.subject_ref, "SURF:SC-1:api")
        self.assertFalse(result.to_dict()["would_block"])

    def test_unknown_requirement_and_not_applicable_design_are_blocked(self) -> None:
        design = self._design_graph(unknown_ref=True, include_not_applicable=("SC-2", "api"))
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=design)
        categories = {item.category for item in validate_vertical_trace_coverage(graph, mode="shadow").blocking_findings}

        self.assertIn("unknown_requirement_ref", categories)
        self.assertIn("not_applicable_surface_designed", categories)

    def test_stale_binding_and_tampered_digest_fail_closed(self) -> None:
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=self._design_graph())
        with self.assertRaises(HarnessError) as stale:
            validate_vertical_trace_graph(graph, expected_requirement_digest="sha256:stale")
        self.assertEqual(stale.exception.error_code, "vertical_trace_binding_stale")

        graph["nodes"][0]["label"] = "tampered"
        with self.assertRaises(HarnessError) as tampered:
            validate_vertical_trace_graph(graph)
        self.assertEqual(tampered.exception.error_code, "vertical_trace_graph_invalid")

    def test_bounded_view_excludes_unrelated_scenario(self) -> None:
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=self._design_graph())
        view = bounded_trace_view(graph, scenario_ids={"SC-1"})
        scenario_ids = {str(item.get("scenario_id") or "") for item in view["nodes"]}
        self.assertNotIn("SC-2", scenario_ids)

    def test_cross_scenario_design_edge_is_blocked(self) -> None:
        design = self._design_graph()
        sc1 = next(item for item in design["nodes"] if item["scenario_id"] == "SC-2")
        sc2 = next(item for item in design["nodes"] if item["scenario_id"] == "SC-3")
        design["edges"].append(
            {
                "id": "VDE-cross",
                "from": sc1["id"],
                "to": sc2["id"],
                "kind": "depends_on",
                "label": "invalid cross scenario dependency",
                "branch_group": "",
                "guard": "",
                "terminal": False,
                "scenario_id": "SC-2",
                "order": 99,
            }
        )
        design["artifact_digest"] = graph_digest(design)
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=design)
        categories = {item.category for item in validate_vertical_trace_coverage(graph, mode="enforce").blocking_findings}
        self.assertIn("cross_scenario_design_edge", categories)

    def test_branch_to_main_parent_edges_are_required_and_allowed(self) -> None:
        design = self._design_graph()
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=design)
        result = validate_vertical_trace_coverage(graph, mode="enforce")

        self.assertNotIn("cross_scenario_design_edge", {item.category for item in result.blocking_findings})
        self.assertFalse(
            {"branch_parent_missing", "branch_parent_invalid", "branch_parent_ambiguous"}
            & {item.category for item in result.blocking_findings}
        )
        expected_branch_count = sum(
            1
            for item in self.model.scenario_contract["scenarios"]
            if item["kind"] in {"alternative", "exception"}
        )
        self.assertEqual(result.coverage["valid_branch_parent_count"], expected_branch_count)

    def test_missing_invalid_and_ambiguous_branch_parents_are_blocked(self) -> None:
        missing_design = self._design_graph(include_branch_parents=False)
        missing_graph = build_vertical_trace_graph(
            requirement_model=self.model,
            design_graph=missing_design,
        )
        missing_categories = {
            item.category
            for item in validate_vertical_trace_coverage(missing_graph, mode="enforce").blocking_findings
        }
        self.assertIn("branch_parent_missing", missing_categories)

        invalid_design = self._design_graph(include_branch_parents=False)
        main = next(item for item in invalid_design["nodes"] if item["scenario_id"] == "SC-1")
        exception = next(item for item in invalid_design["nodes"] if item["scenario_id"] == "SC-3")
        invalid_design["edges"].append(
            {
                "id": "VDE-invalid-parent-direction",
                "from": main["id"],
                "to": exception["id"],
                "kind": "depends_on",
                "label": "wrong parent direction",
                "branch_group": "",
                "guard": "",
                "terminal": False,
                "scenario_id": "SC-1",
                "order": 1,
            }
        )
        invalid_design["artifact_digest"] = graph_digest(invalid_design)
        invalid_graph = build_vertical_trace_graph(
            requirement_model=self.model,
            design_graph=invalid_design,
        )
        invalid_categories = {
            item.category
            for item in validate_vertical_trace_coverage(invalid_graph, mode="enforce").blocking_findings
        }
        self.assertIn("branch_parent_invalid", invalid_categories)

        ambiguous_design = self._design_graph()
        main = next(item for item in ambiguous_design["nodes"] if item["scenario_id"] == "SC-1")
        second_exception = [
            item for item in ambiguous_design["nodes"] if item["scenario_id"] == "SC-3"
        ][1]
        ambiguous_design["edges"].append(
            {
                "id": "VDE-second-exception-parent",
                "from": second_exception["id"],
                "to": main["id"],
                "kind": "depends_on",
                "label": "duplicate parent",
                "branch_group": "",
                "guard": "",
                "terminal": False,
                "scenario_id": "SC-3",
                "order": 2,
            }
        )
        ambiguous_design["artifact_digest"] = graph_digest(ambiguous_design)
        ambiguous_graph = build_vertical_trace_graph(
            requirement_model=self.model,
            design_graph=ambiguous_design,
        )
        ambiguous_categories = {
            item.category
            for item in validate_vertical_trace_coverage(ambiguous_graph, mode="enforce").blocking_findings
        }
        self.assertIn("branch_parent_ambiguous", ambiguous_categories)

    def test_enforce_report_must_match_graph_and_pass(self) -> None:
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=self._design_graph())
        report = validate_vertical_trace_coverage(graph, mode="enforce").to_dict()
        self.assertIs(validate_vertical_trace_report(report, graph=graph, expected_mode="enforce"), report)

        stale = deepcopy(report)
        stale["graph_digest"] = "sha256:stale"
        with self.assertRaises(HarnessError) as error:
            validate_vertical_trace_report(stale, graph=graph, expected_mode="enforce")
        self.assertEqual(error.exception.error_code, "vertical_trace_binding_stale")

    def test_untraced_design_node_is_a_stable_snapshot_bound_finding(self) -> None:
        design = self._design_graph()
        node = design["nodes"][0]
        node["surface"] = "interaction"
        node["requirement_refs"] = ["UNKNOWN-ORPHAN"]
        design["artifact_digest"] = graph_digest(design)
        graph = build_vertical_trace_graph(requirement_model=self.model, design_graph=design)

        finding = next(
            item
            for item in validate_vertical_trace_coverage(graph, mode="shadow").blocking_findings
            if item.category == "design_node_untraced"
        )
        self.assertEqual(finding.subject_ref, node["id"])
        self.assertEqual(finding.snapshot_fingerprint, graph["artifact_digest"])

    def test_enforce_revalidation_preflights_existing_graph_for_designer_feedback(self) -> None:
        design = self._design_graph()
        design["nodes"][0]["requirement_refs"] = ["UNKNOWN-L4-REF"]
        design["artifact_digest"] = graph_digest(design)
        feedback = _vertical_trace_repair_feedback(
            state={
                "vertical_trace_contract_version": VERTICAL_TRACE_GRAPH_VERSION,
                "vertical_trace_mode": "enforce",
            },
            approved=SimpleNamespace(canonical_model=self.model),
            graph=design,
        )

        self.assertIn("unknown_requirement_ref", {str(item.get("category") or "") for item in feedback})
        self.assertEqual(
            _vertical_trace_repair_feedback(
                state={
                    "vertical_trace_contract_version": VERTICAL_TRACE_GRAPH_VERSION,
                    "vertical_trace_mode": "shadow",
                },
                approved=SimpleNamespace(canonical_model=self.model),
                graph=design,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
