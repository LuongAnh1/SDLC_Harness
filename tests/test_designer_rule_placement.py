"""place_surface: a scenario's business rule designed in the component whose code makes the decision (r7 SC-3)."""

from __future__ import annotations

import json
import unittest

from sdlc_harness.agents.designer import (
    DELTA_VERSION,
    RUNTIME_BINDING_VERSION,
    SURFACE_CONTRACT_VERSION,
    apply_designer_delta,
    completion_findings,
    new_vertical_design_graph,
)
from sdlc_harness.agents.designer.execution import (
    _active_partition,
    _reject_unbound_new_nodes,
    _validate_surface_placement_ops,
)
from sdlc_harness.agents.designer.projections import _surface_view
from sdlc_harness.agents.validator.semantic_review import materialize_task_context
from sdlc_harness.core.errors import HarnessError

BINDING = {"requirement_digest": "req", "repo_landscape_digest": "repo", "workspace_ref": "workspace", "base_sha": "abc"}
WEB_REF = "repo:apps/web/src/ArticlePage.tsx#L226@sha256:web"


def _requirement(*, business_rule: str = "required") -> dict[str, object]:
    return {
        "use_case_id": "UC1",
        "scenario_contract": {
            "scenarios": [
                {
                    "id": "SC-3",
                    "surfaces": {
                        "ui": {"status": "required", "requirement_refs": ["AF-1", "BR-3", "AC-3"]},
                        "api": {"status": "required", "requirement_refs": ["AF-1", "BR-2"]},
                        "business_rule": {
                            "status": business_rule,
                            "requirement_refs": ["BR-1", "BR-3", "AC-3"] if business_rule == "required" else [],
                        },
                        "data": {"status": "required", "requirement_refs": ["AF-1", "BR-2"]},
                    },
                }
            ]
        },
    }


LANDSCAPE = {
    "components": [
        {"component_id": "web", "roots": ["apps/web/src"]},
        {"component_id": "api", "roots": ["apps/api/src"]},
        {"component_id": "database", "roots": ["packages/db/src"]},
    ],
    "anchors": [
        {"anchor_id": "RLA-web", "component_id": "web", "kind": "ui_component", "path": "apps/web/src/App.tsx", "symbol": "App"},
        {"anchor_id": "RLA-api", "component_id": "api", "kind": "service_symbol", "path": "apps/api/src/app.ts", "symbol": "createApp"},
    ],
    "scenario_matches": [],
}


def _graph(**fields: object) -> dict[str, object]:
    graph = new_vertical_design_graph(run_id="run-placement", snapshot_binding=BINDING)
    graph.update(fields)
    return graph


def _rule_node(*, decision: str = "Show edit and delete only to the author.") -> dict[str, object]:
    return {
        "temp_ref": "tmp:SC-3:business_rule",
        "semantic_key": "SC-3:web:business_rule",
        "name": "Author-only article actions",
        "kind": "business_rule",
        "surface": "business_rule",
        "disposition": "reuse",
        "requirement_refs": ["BR-3", "AC-3"],
        "evidence_refs": [WEB_REF],
        "evidence_reason": "",
        "contract": "ArticleMeta renders the owner actions only for the author.",
        "surface_contract": {
            "schema_version": SURFACE_CONTRACT_VERSION,
            "surface": "business_rule",
            "inputs": ["viewerUsername", "authorUsername"],
            "decision": decision,
            "outcomes": ["owner actions", "reader actions"],
        },
        "runtime_binding": {
            "schema_version": RUNTIME_BINDING_VERSION,
            "runtime_kind": "service",
            "target": {"component_id": "web", "path": "apps/web/src/ArticlePage.tsx", "symbol": "ArticleMeta"},
            "attachment": {
                "relation": "called_by",
                "owner_ref": "RLA-web",
                "owner_path": "apps/web/src/App.tsx",
                "owner_symbol": "App",
            },
            "evidence_refs": ["RLA-web"],
        },
    }


def _delta(graph: dict[str, object], component_id: str, *operations: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": DELTA_VERSION,
        "base_graph_digest": graph["artifact_digest"],
        "partition": {"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": component_id},
        "operations": list(operations),
        "completion_claims": [],
        "unresolved_items": [],
    }


def _place(component_id: str = "web", **overrides: object) -> dict[str, object]:
    return {
        "op": "place_surface",
        "surface": "business_rule",
        "component_id": component_id,
        "reason": "ArticleMeta decides which article actions to render; reading the article runs no server rule.",
        "evidence_refs": [WEB_REF],
        **overrides,
    }


def _apply(graph: dict[str, object], delta: dict[str, object]) -> dict[str, object]:
    updated, _ = apply_designer_delta(graph, delta, valid_evidence_refs={WEB_REF, "RLA-web", "RLA-api"})
    return updated


class PlacementRoutingTest(unittest.TestCase):
    def test_ui_partition_offers_the_rule_and_the_api_partition_shows_its_default(self) -> None:
        first = _active_partition(requirement_model=_requirement(), landscape=LANDSCAPE, graph=_graph())
        self.assertEqual(first["missing_surfaces"], ["ui"])
        self.assertEqual(
            first["placeable_surfaces"],
            [
                {
                    "surface": "business_rule",
                    "requirement_refs": ["BR-1", "BR-3", "AC-3"],
                    # r7 designer-0040 read a "default" as the expected answer: it is labelled a guess.
                    "harness_routing_guess": "api",
                    "routing_guess_note": "guessed from the scenario's required surfaces, not a decision",
                    "placeable_component_ids": ["api", "database", "web"],
                }
            ],
        )

        graph = _graph(
            partitions=[{"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "web", "coverage": {"ui": "covered"}}],
            nodes=[{"id": "VDN-ui", "scenario_id": "SC-3", "component_id": "web", "surface": "ui"}],
        )
        second = _active_partition(requirement_model=_requirement(), landscape=LANDSCAPE, graph=graph)
        self.assertEqual(second["partition"]["component_id"], "api")
        self.assertEqual(second["missing_surfaces"], ["api", "business_rule"])
        rule_work = next(item for item in second["surface_work"] if item["surface"] == "business_rule")
        self.assertEqual(rule_work["placement"]["harness_routing_guess"], "api")
        # r7 designer-0040 kept business_rule because the scaffold still covered it here.
        self.assertIn("drop business_rule from the delta_scaffold", rule_work["placement"]["if_placed_elsewhere"])
        self.assertNotIn("placeable_surfaces", second)

    def test_a_placement_reopens_the_web_partition_for_the_rule(self) -> None:
        graph = _graph(
            partitions=[
                {"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "web", "coverage": {"ui": "covered"}},
                {"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "api", "coverage": {"api": "covered"}},
            ],
            nodes=[
                {"id": "VDN-ui", "scenario_id": "SC-3", "component_id": "web", "surface": "ui"},
                {"id": "VDN-api", "scenario_id": "SC-3", "component_id": "api", "surface": "api"},
            ],
            surface_placements=[
                {
                    "scenario_id": "SC-3",
                    "surface": "business_rule",
                    "component_id": "web",
                    "reason": "the UI decides",
                    "evidence_refs": [WEB_REF],
                    "placed_in_partition": "SC-3|api",
                }
            ],
        )
        active = _active_partition(requirement_model=_requirement(), landscape=LANDSCAPE, graph=graph)

        self.assertEqual(active["partition"]["component_id"], "web")
        self.assertEqual(active["missing_surfaces"], ["business_rule"])
        self.assertEqual(active["required_coverage"], {"ui": "covered", "business_rule": "covered"})
        self.assertEqual(active["surface_work"][0]["placement"]["component_id"], "web")
        self.assertEqual(active["later_partitions"], [{"component_id": "database", "surfaces": ["data"]}])


class PlacementGateTest(unittest.TestCase):
    def test_placing_the_rule_elsewhere_is_recorded_once(self) -> None:
        graph = _graph()
        updated = _apply(graph, _delta(graph, "api", {"op": "set_partition_coverage", "coverage": {"api": "covered"}}, _place()))

        self.assertEqual(
            updated["surface_placements"],
            [
                {
                    "scenario_id": "SC-3",
                    "surface": "business_rule",
                    "component_id": "web",
                    "reason": _place()["reason"],
                    "evidence_refs": [WEB_REF],
                    "placed_in_partition": "SC-3|api",
                }
            ],
        )
        with self.assertRaisesRegex(HarnessError, "already placed in web by partition SC-3\\|api; a placement is made once"):
            _apply(updated, _delta(updated, "database", {"op": "set_partition_coverage", "coverage": {"data": "covered"}}, _place("api")))

    def test_malformed_placements_are_rejected_by_name(self) -> None:
        graph = _graph()
        coverage = {"op": "set_partition_coverage", "coverage": {"api": "covered"}}
        cases = [
            (_place(surface="api"), "can only place business_rule, not api"),
            (_place(reason=""), "needs component_id, a reason and the evidence_refs"),
            (_place(evidence_refs=["repo:apps/web/src/Invented.tsx#L1@sha256:x"]), "refs no Designer tool returned"),
            (_place(scenario_id="SC-1"), "fields outside its contract: scenario_id"),
        ]
        for operation, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message):
                _apply(graph, _delta(graph, "api", coverage, operation))

    def test_placed_elsewhere_the_delta_leaves_the_rule_out(self) -> None:
        graph = _graph()
        delta = _delta(graph, "api", {"op": "set_partition_coverage", "coverage": {"api": "covered", "business_rule": "covered"}}, _place())
        with self.assertRaisesRegex(HarnessError, "must not add a business_rule node or cover business_rule"):
            _apply(graph, delta)

    def test_placed_in_its_own_component_the_delta_designs_the_rule(self) -> None:
        graph = _graph()
        coverage = {"op": "set_partition_coverage", "coverage": {"business_rule": "covered"}}
        with self.assertRaisesRegex(HarnessError, "this delta must also add the business_rule node"):
            _apply(graph, _delta(graph, "web", coverage, _place()))

        updated = _apply(graph, _delta(graph, "web", coverage, {"op": "add_node", "node": _rule_node()}, _place()))
        self.assertEqual(updated["surface_placements"][0]["placed_in_partition"], "SC-3|web")

    def test_requirement_and_landscape_bound_the_placement(self) -> None:
        delta = _delta(_graph(), "api", _place())
        _validate_surface_placement_ops({"delta": delta}, requirement_model=_requirement(), landscape=LANDSCAPE, run_id="r")
        with self.assertRaisesRegex(HarnessError, "does not require it for SC-3"):
            _validate_surface_placement_ops(
                {"delta": delta}, requirement_model=_requirement(business_rule="not_applicable"), landscape=LANDSCAPE, run_id="r"
            )
        with self.assertRaisesRegex(HarnessError, "mobile is not a landscape component; use one of api, database, web"):
            _validate_surface_placement_ops(
                {"delta": _delta(_graph(), "api", _place("mobile"))}, requirement_model=_requirement(), landscape=LANDSCAPE, run_id="r"
            )

    def test_a_new_node_without_runtime_binding_is_rejected_while_its_partition_is_active(self) -> None:
        # r7 designer-0072: the SC-7 API node had no runtime_binding; only completion reported it, in a later repair.
        unbound = {key: value for key, value in _rule_node().items() if key != "runtime_binding"}
        delta = _delta(_graph(), "web", {"op": "add_node", "node": unbound})
        with self.assertRaisesRegex(HarnessError, "Node tmp:SC-3:business_rule has no runtime_binding"):
            _reject_unbound_new_nodes({"delta": delta}, run_id="r")
        _reject_unbound_new_nodes({"delta": _delta(_graph(), "web", {"op": "add_node", "node": _rule_node()})}, run_id="r")
        step = {**unbound, "kind": "interaction_step", "surface": "interaction"}
        _reject_unbound_new_nodes({"delta": _delta(_graph(), "web", {"op": "add_node", "node": step})}, run_id="r")

    def test_errors_name_new_nodes_by_the_temp_ref_the_designer_wrote(self) -> None:
        # r7 designer-0038: "Node VDN-b2af... does not require authentication" named an id the Designer never saw.
        graph = _graph()
        delta = _delta(
            graph,
            "web",
            {"op": "set_partition_coverage", "coverage": {"business_rule": "covered"}},
            {"op": "add_node", "node": _rule_node(decision="")},
        )
        with self.assertRaises(HarnessError) as caught:
            _apply(graph, delta)
        self.assertIn("Node tmp:SC-3:business_rule surface contract is missing decision", caught.exception.message)
        self.assertNotIn("VDN-", caught.exception.message)
        self.assertEqual(str(caught.exception), caught.exception.message)


def _not_exercised(surface: str = "data", **overrides: object) -> dict[str, object]:
    return {
        "op": "not_exercised",
        "surface": surface,
        "reason": "the sign-in check refuses the request with 401 before any data access",
        "evidence_refs": ["RLA-api"],
        "requirement_refs": ["AF-1", "BR-2"],
        **overrides,
    }


class NotExercisedTest(unittest.TestCase):
    """r7 designer-0066: SC-6 data behind a 401 was invented as a node for code that never runs."""

    def _with_rule(self) -> dict[str, object]:
        graph = _graph()
        return _apply(
            graph,
            _delta(
                graph,
                "web",
                {"op": "set_partition_coverage", "coverage": {"business_rule": "covered"}},
                {"op": "add_node", "node": _rule_node()},
            ),
        )

    def test_declaration_is_recorded_with_its_requirement_ids(self) -> None:
        graph = self._with_rule()
        updated = _apply(
            graph,
            _delta(graph, "database", {"op": "set_partition_coverage", "coverage": {"data": "not_exercised"}}, _not_exercised()),
        )
        self.assertEqual(
            updated["surface_not_exercised"],
            [
                {
                    "scenario_id": "SC-3",
                    "surface": "data",
                    "reason": _not_exercised()["reason"],
                    "evidence_refs": ["RLA-api"],
                    "requirement_refs": ["AF-1", "BR-2"],
                    "declared_in_partition": "SC-3|database",
                }
            ],
        )

    def test_malformed_or_contradicting_declarations_are_rejected(self) -> None:
        graph = self._with_rule()
        coverage = {"op": "set_partition_coverage", "coverage": {"data": "not_exercised"}}
        cases = [
            (coverage, _not_exercised(surface="ui"), "can only declare api, business_rule, data, not ui"),
            (coverage, _not_exercised(reason=""), "needs a reason and the evidence_refs"),
            (coverage, _not_exercised(evidence_refs=["repo:invented#L1@sha256:x"]), "refs no Designer tool returned"),
            (coverage, _not_exercised(component_id="web"), "fields outside its contract: component_id"),
            (
                {"op": "set_partition_coverage", "coverage": {"data": "covered"}},
                _not_exercised(),
                "declared not exercised, so its coverage must be not_exercised",
            ),
            (
                {"op": "set_partition_coverage", "coverage": {"business_rule": "not_exercised"}},
                _not_exercised("business_rule"),
                "conflicts with a business_rule node of SC-3",
            ),
        ]
        for coverage_op, operation, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message):
                _apply(graph, _delta(graph, "database", coverage_op, operation))
        with self.assertRaisesRegex(HarnessError, "needs a \\{\"op\":\"not_exercised\"\\} operation"):
            _apply(graph, _delta(graph, "database", coverage))
        with self.assertRaisesRegex(HarnessError, "SC-3 has no Design node left"):
            fresh = _graph()
            _apply(fresh, _delta(fresh, "database", coverage, _not_exercised()))

    def test_executor_fills_the_requirement_ids_and_bounds_the_surface(self) -> None:
        delta = _delta(_graph(), "database", _not_exercised(requirement_refs=["invented"]))
        filled = _validate_surface_placement_ops({"delta": delta}, requirement_model=_requirement(), landscape=LANDSCAPE, run_id="r")
        self.assertEqual(filled["delta"]["operations"][0]["requirement_refs"], ["AF-1", "BR-2"])
        with self.assertRaisesRegex(HarnessError, "not_exercised business_rule: the approved Requirement does not require it"):
            _validate_surface_placement_ops(
                {"delta": _delta(_graph(), "api", _not_exercised("business_rule"))},
                requirement_model=_requirement(business_rule="not_applicable"),
                landscape=LANDSCAPE,
                run_id="r",
            )

    def test_routing_and_completion_treat_the_surface_as_done_but_keep_its_ids_owned(self) -> None:
        declaration = {key: value for key, value in _not_exercised().items() if key != "op"}
        graph = _graph(
            partitions=[{"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "web", "coverage": {"ui": "covered"}}],
            nodes=[{"id": "VDN-ui", "scenario_id": "SC-3", "component_id": "web", "surface": "ui"}],
            surface_not_exercised=[{"scenario_id": "SC-3", **declaration, "declared_in_partition": "SC-3|database"}],
        )
        active = _active_partition(requirement_model=_requirement(), landscape=LANDSCAPE, graph=graph)
        self.assertEqual(active["missing_surfaces"], ["api", "business_rule"])
        self.assertNotIn("later_partitions", active)

        updated = _apply(self._with_rule(), _delta(self._with_rule(), "database", {"op": "set_partition_coverage", "coverage": {"data": "not_exercised"}}, _not_exercised()))
        findings = completion_findings(updated, required_surfaces={"SC-3": {"business_rule", "data"}})
        codes = {item["code"]: item for item in findings}
        self.assertNotIn("required_surface_missing", {item["code"] for item in findings if "data" in item["subject_ref"]})
        # r7 designer-0068: the runtime path check also asked to reach the declared surface.
        self.assertNotIn("runtime_path_disconnected", codes)
        # The rule node carries BR-3/AC-3 only, so the data ids AF-1/BR-2 are owned by nobody yet.
        self.assertEqual(codes["not_exercised_ref_unowned"]["subject_ref"], "SC-3:data")
        self.assertIn("AF-1, BR-2", codes["not_exercised_ref_unowned"]["message"])


class PlacementReviewTest(unittest.TestCase):
    PLACEMENT = {
        "scenario_id": "SC-3",
        "surface": "business_rule",
        "component_id": "web",
        "reason": "the UI decides",
        "evidence_refs": [WEB_REF],
        "placed_in_partition": "SC-3|api",
    }

    def test_review_packet_and_design_view_show_the_placement(self) -> None:
        graph = _graph(surface_placements=[self.PLACEMENT])
        packet = materialize_task_context(
            {"task_type": "scenario_behavior", "scenario_ids": ["SC-3"]},
            claims=[],
            vertical_design_graph=graph,
            vertical_trace=None,
        )
        self.assertEqual(packet["surface_placements"], [self.PLACEMENT])
        other = materialize_task_context(
            {"task_type": "scenario_behavior", "scenario_ids": ["SC-1"]},
            claims=[],
            vertical_design_graph=graph,
            vertical_trace=None,
        )
        self.assertNotIn("surface_placements", other)

        view = _surface_view([], graph=graph, requirement_model=_requirement())
        self.assertEqual(
            view["surface_placements"],
            [{"scenario_id": "SC-3", "surface": "business_rule", "component_id": "web", "reason": "the UI decides"}],
        )
        self.assertEqual(_surface_view([], graph=_graph(), requirement_model=_requirement())["surface_placements"], [])
        json.dumps(view)

        declaration = {
            "scenario_id": "SC-3",
            "surface": "data",
            "reason": "refused before any data access",
            "evidence_refs": ["RLA-api"],
            "requirement_refs": ["AF-1"],
            "declared_in_partition": "SC-3|database",
        }
        declared = _graph(surface_not_exercised=[declaration])
        packet = materialize_task_context(
            {"task_type": "scenario_behavior", "scenario_ids": ["SC-3"]},
            claims=[],
            vertical_design_graph=declared,
            vertical_trace=None,
        )
        self.assertEqual(packet["surface_not_exercised"], [declaration])
        self.assertEqual(
            _surface_view([], graph=declared, requirement_model=_requirement())["surface_not_exercised"],
            [{"scenario_id": "SC-3", "surface": "data", "reason": "refused before any data access"}],
        )


if __name__ == "__main__":
    unittest.main()
