"""Design Validator packets: Harness-established facts and trimmed UI context (plan 2026-11-19 validator packet)."""

from __future__ import annotations

import copy
import unittest

from sdlc_harness.agents.designer.graph import ESTABLISHED_NOT_PROVEN, _runtime_composition_findings, established_facts
from sdlc_harness.agents.validator.semantic_review import materialize_task_context, review_ui_repo_context

PATH = "apps/web/src/App.tsx#App"
SCREEN = "apps/web/src/Editor.tsx#Editor"


def _graph() -> dict[str, object]:
    contract = {
        "schema_version": "boundary_contract_v1",
        "consumer_operation": "Editor submits",
        "provider_operation": "PUT /api/items/:slug",
        "input_mapping": [{"from": "item.slug", "to": "slug", "required": True}],
        "auth_mapping": [{"from": "session token", "to": "Authorization header", "required": True}],
        "response_mapping": [{"from": "200 item", "to": "saved", "required": True}],
        "error_mapping": [{"from": "403", "to": "refused", "required": False}],
    }
    return {
        "run_id": "run-packet",
        "nodes": [
            {
                "id": "VDN-ui",
                "scenario_id": "SC-1",
                "component_id": "web",
                "surface": "ui",
                "kind": "ui_interaction",
                "runtime_binding": {"target": {"path": "apps/web/src/Editor.tsx", "symbol": "Editor"}},
                "surface_contract": {"runtime_surface": {"composition_path": [PATH, SCREEN]}},
            },
            {
                "id": "VDN-api",
                "scenario_id": "SC-1",
                "component_id": "api",
                "surface": "api",
                "kind": "api_contract",
                "runtime_binding": {"target": {"path": "apps/api/src/items.ts", "symbol": "itemsUpdate"}},
                "surface_contract": {
                    "method": "PUT",
                    "path": "/api/items/:slug",
                    "request_fields": [{"name": "slug", "location": "path", "type": "string"}],
                    "responses": [{"status": "200", "outcome": "saved"}, {"status": "403", "outcome": "refused"}],
                    "authentication_required": True,
                    "authentication": "Token",
                },
            },
        ],
        "edges": [
            {"id": "VDE-call", "scenario_id": "SC-1", "order": 1, "from": "VDN-ui", "to": "VDN-api", "kind": "calls",
             "terminal": False, "boundary_contract": copy.deepcopy(contract)},
            {"id": "VDE-return", "scenario_id": "SC-1", "order": 2, "from": "VDN-api", "to": "VDN-ui", "kind": "returns",
             "terminal": True, "boundary_contract": copy.deepcopy(contract)},
        ],
    }


class EstablishedFactsTest(unittest.TestCase):
    def test_facts_are_stated_only_for_checks_that_pass(self) -> None:
        facts = established_facts(_graph(), scenario_ids=["SC-1"], edge_ids=["VDE-call", "VDE-return"])

        call = facts["boundaries"]["VDE-call"]
        self.assertIn("every request field is mapped as a required target by name: slug", call)
        self.assertIn("auth_mapping is declared (the provider requires authentication or authorization)", call)
        self.assertIn("error_mapping is declared for the provider's error statuses 403", call)
        self.assertIn("input_mapping repeats paired calls edge VDE-call", facts["boundaries"]["VDE-return"])
        self.assertIn(
            "SC-1: the ordered behavior edges form one continuous path with exactly one terminal edge, last",
            facts["scenario"],
        )
        self.assertEqual(facts["not_established"], list(ESTABLISHED_NOT_PROVEN))
        # The minimal test graph is not a full Design graph, so no schema fact is claimed for it.
        self.assertEqual(facts["design"], [])

    def test_a_broken_check_removes_its_facts(self) -> None:
        graph = _graph()
        graph["edges"][0]["boundary_contract"]["input_mapping"] = []
        graph["edges"][1]["boundary_contract"]["input_mapping"] = [{"from": "x", "to": "slug", "required": True}]
        graph["edges"][1]["terminal"] = False
        facts = established_facts(graph, scenario_ids=["SC-1"], edge_ids=["VDE-call", "VDE-return"])

        self.assertNotIn("VDE-call", facts["boundaries"])
        self.assertNotIn(
            "input_mapping repeats paired calls edge VDE-call", facts["boundaries"].get("VDE-return", [])
        )
        self.assertFalse(any("continuous path" in item for item in facts["scenario"]))

    def test_a_data_call_states_its_bound_callable(self) -> None:
        graph = _graph()
        graph["nodes"].append(
            {
                "id": "VDN-data",
                "scenario_id": "SC-1",
                "component_id": "database",
                "surface": "data",
                "kind": "data_operation",
                "runtime_binding": {
                    "runtime_kind": "persistence_adapter",
                    "target": {"path": "packages/db/src/itemUpdate.ts", "symbol": "itemUpdate"},
                },
            }
        )
        graph["edges"].append(
            {
                "id": "VDE-write",
                "scenario_id": "SC-1",
                "order": 3,
                "from": "VDN-api",
                "to": "VDN-data",
                "kind": "writes",
                "boundary_contract": {"provider_operation": "itemUpdate", "input_mapping": []},
            }
        )
        facts = established_facts(graph, scenario_ids=["SC-1"], edge_ids=["VDE-write"])
        self.assertEqual(
            facts["boundaries"]["VDE-write"], ["provider_operation itemUpdate is the callable the provider is bound to"]
        )
        graph["edges"][-1]["boundary_contract"]["provider_operation"] = "somethingElse"
        self.assertNotIn("VDE-write", established_facts(graph, scenario_ids=["SC-1"], edge_ids=["VDE-write"])["boundaries"])

    def test_only_the_packet_edges_get_boundary_facts(self) -> None:
        facts = established_facts(_graph(), scenario_ids=["SC-1"], edge_ids=[])
        self.assertEqual(facts["boundaries"], {})


class CalledByOwnerGateTest(unittest.TestCase):
    """A called_by owner must be one of the nodes calling it (r7 v1-v4 and every eTour rule were owned by createApp)."""

    @staticmethod
    def _with_rule(owner_ref: str, owner_symbol: str) -> dict[str, object]:
        graph = _graph()
        graph["nodes"].append(
            {
                "id": "VDN-rule",
                "scenario_id": "SC-1",
                "component_id": "api",
                "surface": "business_rule",
                "kind": "business_rule",
                "disposition": "add",
                "runtime_binding": {
                    "runtime_kind": "service",
                    "target": {"path": "apps/api/src/itemGuard.ts", "symbol": "ensureOwner"},
                    "attachment": {
                        "relation": "called_by",
                        "owner_ref": owner_ref,
                        "owner_path": "apps/api/src/items.ts" if owner_symbol == "itemsUpdate" else "apps/api/src/app.ts",
                        "owner_symbol": owner_symbol,
                    },
                },
            }
        )
        graph["edges"].append(
            {"id": "VDE-guard", "scenario_id": "SC-1", "order": 2, "from": "VDN-api", "to": "VDN-rule", "kind": "calls"}
        )
        graph["partitions"] = []
        return graph

    @staticmethod
    def _codes(graph: dict[str, object]) -> list[str]:
        return [
            str(item["code"])
            for item in _runtime_composition_findings(graph, required_surfaces=None, landscape=None)
            if str(item.get("subject_ref")) == "VDN-rule"
        ]

    def test_an_app_root_owner_that_does_not_call_the_rule_is_rejected(self) -> None:
        self.assertIn("runtime_attachment_off_caller", self._codes(self._with_rule("RLA-app", "createApp")))

    def test_the_calling_node_is_accepted_by_node_id_or_by_its_target(self) -> None:
        self.assertNotIn("runtime_attachment_off_caller", self._codes(self._with_rule("VDN-api", "itemsUpdate")))
        self.assertNotIn("runtime_attachment_off_caller", self._codes(self._with_rule("RLA-items", "itemsUpdate")))


class UiRepoContextTest(unittest.TestCase):
    CONTEXT = {
        "entrypoint": "apps/web/src/main.tsx",
        "auth": {"source": "store"},
        "data_clients": [{"ref": "apps/web/src/services/api.ts#updateItem"}],
        "composition_chain": [
            {"ref": PATH, "renders": [SCREEN]},
            {"ref": SCREEN, "renders": []},
            {"ref": "apps/web/src/Footer.tsx#Footer", "renders": []},
        ],
        "styles": {"files": ["styles.css"]},
        "verification": {"stage": "unit"},
    }

    def test_runtime_view_keeps_auth_clients_and_only_the_hops_on_its_composition_paths(self) -> None:
        view = review_ui_repo_context(self.CONTEXT, nodes=_graph()["nodes"])
        self.assertEqual(sorted(view), ["auth", "composition_chain", "data_clients", "entrypoint"])
        self.assertEqual([item["ref"] for item in view["composition_chain"]], [PATH, SCREEN])

    def test_a_packet_without_ui_nodes_gets_no_ui_context(self) -> None:
        graph = _graph()
        graph["nodes"] = [node for node in graph["nodes"] if node["surface"] != "ui"]
        graph["edges"] = []
        graph["snapshot_binding"] = {"ui_repo_context": self.CONTEXT}
        packet = materialize_task_context(
            {"task_type": "scenario_runtime", "scenario_ids": ["SC-1"], "node_ids": ["VDN-api"], "edge_ids": []},
            claims=[],
            vertical_design_graph=graph,
            vertical_trace=None,
        )
        self.assertNotIn("ui_repo_context", packet)
        self.assertIn("harness_established", packet)

    def test_a_runtime_packet_with_ui_gets_the_trimmed_context_and_its_boundary_facts(self) -> None:
        graph = _graph()
        graph["snapshot_binding"] = {"ui_repo_context": self.CONTEXT}
        packet = materialize_task_context(
            {
                "task_type": "scenario_runtime",
                "scenario_ids": ["SC-1"],
                "node_ids": ["VDN-ui", "VDN-api"],
                "edge_ids": ["VDE-call", "VDE-return"],
            },
            claims=[],
            vertical_design_graph=graph,
            vertical_trace=None,
        )
        self.assertNotIn("styles", packet["ui_repo_context"])
        self.assertEqual(len(packet["ui_repo_context"]["composition_chain"]), 2)
        self.assertIn("VDE-call", packet["harness_established"]["boundaries"])


if __name__ == "__main__":
    unittest.main()
