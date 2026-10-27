"""Contract, security, and workflow coverage for the L3 repo-aware Designer."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sdlc_harness.agents.designer import (
    DELTA_VERSION,
    DESIGN_BUNDLE_STATE_KEY,
    DESIGN_BUNDLE_VERSION,
    GRAPH_VERSION,
    SURFACE_CONTRACT_VERSION,
    DesignerExecutionError,
    DesignerToolLoopExecutor,
    apply_designer_delta,
    design_bundle_findings,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from sdlc_harness.agents.designer.tools import DesignerToolRegistry
from sdlc_harness.agents.designer.execution import _active_partition, _active_repair_task, _repair_targets
from sdlc_harness.agents.designer.graph import graph_digest
from sdlc_harness.agents.designer.projections import render_design_projections
from sdlc_harness.agents.validator import (
    VERTICAL_TRACE_GRAPH_VERSION,
    VERTICAL_TRACE_MODE_STATE_KEY,
    VERTICAL_TRACE_STATE_KEY,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.sync.atlassian_rendering import _confluence_design_storage_body
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from tests._support import GitRepositoryTemplate, populate_typescript_target


class VerticalDesignGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = {
            "requirement_digest": "sha256:req",
            "repo_landscape_digest": "sha256:repo",
            "workspace_ref": "run_workspace_v1",
            "base_sha": "abc123",
        }

    def test_delta_is_deterministic_and_allocates_stable_ids(self) -> None:
        graph = new_vertical_design_graph(run_id="run-1", snapshot_binding=self.binding)
        delta = {
            "schema_version": DELTA_VERSION,
            "base_graph_digest": graph["artifact_digest"],
            "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
            "operations": [
                {"op": "set_partition_coverage", "coverage": {"api": "covered"}},
                {
                    "op": "add_node",
                    "node": {
                        "temp_ref": "tmp:api",
                        "semantic_key": "SC-1:api",
                        "name": "Delete route",
                        "kind": "api_contract",
                        "surface": "api",
                        "disposition": "reuse",
                        "requirement_refs": ["REQ-1"],
                        "evidence_refs": ["RLA-0000000000000001"],
                        "evidence_reason": "",
                        "contract": "Validate and delete the selected record.",
                    },
                },
            ],
            "completion_claims": [],
            "unresolved_items": [],
        }

        first, first_refs = apply_designer_delta(graph, delta, valid_evidence_refs={"RLA-0000000000000001"})
        second, second_refs = apply_designer_delta(graph, delta, valid_evidence_refs={"RLA-0000000000000001"})

        self.assertEqual(first, second)
        self.assertEqual(first_refs, second_refs)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["nodes"][0]["id"], first_refs["tmp:api"])
        validate_vertical_design_graph(first, expected_run_id="run-1", expected_binding=self.binding)

    def test_invalid_evidence_and_stale_precondition_are_rejected(self) -> None:
        graph = new_vertical_design_graph(run_id="run-1", snapshot_binding=self.binding)
        delta = {
            "schema_version": DELTA_VERSION,
            "base_graph_digest": "sha256:stale",
            "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
            "operations": [{"op": "set_partition_coverage", "coverage": {"api": "covered"}}],
            "completion_claims": [],
            "unresolved_items": [],
        }
        with self.assertRaises(HarnessError) as caught:
            apply_designer_delta(graph, delta, valid_evidence_refs=set())
        self.assertEqual(caught.exception.error_code, "designer_delta_precondition_failed")
        self.assertEqual(graph["revision"], 0)

    def test_bundle_contract_is_additive_but_required_for_bundle_readiness(self) -> None:
        graph = new_vertical_design_graph(run_id="run-bundle", snapshot_binding=self.binding)
        designed, refs = apply_designer_delta(
            graph,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": graph["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {"api": "covered"}},
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": "tmp:api",
                            "semantic_key": "SC-1:api",
                            "name": "Create heritage endpoint",
                            "kind": "api_contract",
                            "surface": "api",
                            "disposition": "add",
                            "requirement_refs": ["REQ-1"],
                            "evidence_refs": [],
                            "evidence_reason": "No matching route exists in the frozen landscape.",
                            "contract": "Create a cultural heritage record.",
                        },
                    },
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs=set(),
        )
        self.assertEqual(design_bundle_findings(designed)[0]["category"], "surface_contract_missing")
        enriched, _ = apply_designer_delta(
            designed,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": designed["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {
                        "op": "update_node",
                        "node_id": refs["tmp:api"],
                        "changes": {
                            "surface_contract": {
                                "schema_version": SURFACE_CONTRACT_VERSION,
                                "surface": "api",
                                "operation_id": "createHeritage",
                                "method": "POST",
                                "path": "/cultural-heritage",
                                "request_fields": [{"name": "name", "type": "string", "location": "body"}],
                                "responses": [{"status": "201", "outcome": "record created"}],
                                "authorization": "authenticated agency operator",
                            }
                        },
                    }
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs=set(),
        )
        self.assertEqual(design_bundle_findings(enriched), [])
        self.assertEqual(render_design_projections(enriched)["surface_view"]["schema_version"], "design_surface_view_v2")

    def test_delta_cannot_mutate_a_node_from_another_partition(self) -> None:
        graph = new_vertical_design_graph(run_id="run-1", snapshot_binding=self.binding)
        created, refs = apply_designer_delta(
            graph,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": graph["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {"api": "covered"}},
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": "tmp:api",
                            "semantic_key": "SC-1:api",
                            "name": "Route",
                            "kind": "api_contract",
                            "surface": "api",
                            "disposition": "reuse",
                            "requirement_refs": ["REQ-1"],
                            "evidence_refs": ["RLA-0000000000000001"],
                            "evidence_reason": "",
                            "contract": "Handle request.",
                        },
                    },
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs={"RLA-0000000000000001"},
        )
        with self.assertRaises(HarnessError) as caught:
            apply_designer_delta(
                created,
                {
                    "schema_version": DELTA_VERSION,
                    "base_graph_digest": created["artifact_digest"],
                    "partition": {"use_case_id": "UC1", "scenario_id": "SC-2", "component_id": "api"},
                    "operations": [{"op": "update_node", "node_id": refs["tmp:api"], "changes": {"contract": "Wrong partition."}}],
                    "completion_claims": [],
                    "unresolved_items": [],
                },
                valid_evidence_refs={"RLA-0000000000000001"},
            )
        self.assertEqual(caught.exception.error_code, "designer_delta_invalid")

    def test_distinct_labeled_interactions_share_endpoints_and_project_as_typed_operations(self) -> None:
        graph = new_vertical_design_graph(run_id="run-edges", snapshot_binding=self.binding)
        designed, _ = apply_designer_delta(
            graph,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": graph["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {"api": "covered", "business_rule": "covered"}},
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": "tmp:source",
                            "semantic_key": "SC-1:api",
                            "name": "Insertion Endpoint",
                            "kind": "api_contract",
                            "surface": "api",
                            "disposition": "add",
                            "requirement_refs": ["REQ-1"],
                            "evidence_refs": [],
                            "evidence_reason": "The feature endpoint is new planned work.",
                            "contract": "Accept and confirm insertion requests.",
                        },
                    },
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": "tmp:target",
                            "semantic_key": "SC-1:rule",
                            "name": "Insertion Rule",
                            "kind": "business_rule",
                            "surface": "business_rule",
                            "disposition": "add",
                            "requirement_refs": ["REQ-1"],
                            "evidence_refs": [],
                            "evidence_reason": "The feature rule is new planned work.",
                            "contract": "Validate and confirm insertion data.",
                        },
                    },
                    {"op": "add_edge", "edge": {"from": "tmp:source", "to": "tmp:target", "kind": "calls", "label": "validate submitted data", "scenario_id": "SC-1"}},
                    {"op": "add_edge", "edge": {"from": "tmp:source", "to": "tmp:target", "kind": "calls", "label": "confirm operation", "scenario_id": "SC-1", "branch_group": "confirmed", "guard": "operator confirms", "terminal": True}},
                    {"op": "add_edge", "edge": {"from": "tmp:source", "to": "tmp:target", "kind": "depends_on", "label": "guarded branch contract", "scenario_id": "SC-1"}},
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs=set(),
        )

        self.assertEqual(len(designed["edges"]), 3)
        self.assertEqual(len({item["id"] for item in designed["edges"]}), 3)
        projections = render_design_projections(designed)
        sequence = projections["sequence_graph"]["sequenceDiagrams"][0]
        messages = sequence["messages"]
        self.assertEqual([item["message"] for item in messages], ["validateSubmittedData()", "confirmOperation()"])
        self.assertEqual(sequence["controlFlows"][0]["type"], "opt")
        self.assertEqual(sequence["controlFlows"][0]["condition"], "operator confirms")
        self.assertTrue(messages[1]["controlPath"])
        self.assertTrue(messages[1]["terminal"])
        self.assertIn("opt operator confirms", projections["design_markdown"])
        receiver = next(item for item in projections["class_graph"]["nodes"] if item["className"] == "Insertion Rule")
        self.assertEqual({item["name"] for item in receiver["methods"]}, {"confirmOperation", "validateSubmittedData"})

        by_label = {item["label"]: item for item in designed["edges"]}
        reordered, _ = apply_designer_delta(
            designed,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": designed["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "remove_edge", "edge_id": by_label["validate submitted data"]["id"]},
                    {"op": "remove_edge", "edge_id": by_label["confirm operation"]["id"]},
                    {"op": "add_edge", "edge": {"from": designed["nodes"][0]["id"], "to": designed["nodes"][1]["id"], "kind": "calls", "label": "confirm operation", "scenario_id": "SC-1"}},
                    {"op": "add_edge", "edge": {"from": designed["nodes"][0]["id"], "to": designed["nodes"][1]["id"], "kind": "calls", "label": "validate submitted data", "scenario_id": "SC-1"}},
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs=set(),
        )
        reordered_messages = render_design_projections(reordered)["sequence_graph"]["sequenceDiagrams"][0]["messages"]
        self.assertEqual([item["message"] for item in reordered_messages], ["confirmOperation()", "validateSubmittedData()"])
        with self.assertRaises(HarnessError) as missing_edge:
            apply_designer_delta(
                reordered,
                {
                    "schema_version": DELTA_VERSION,
                    "base_graph_digest": reordered["artifact_digest"],
                    "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                    "operations": [{"op": "remove_edge", "edge_id": "VDE-missing"}],
                    "completion_claims": [],
                    "unresolved_items": [],
                },
                valid_evidence_refs=set(),
            )
        self.assertIn("existing Design edge", missing_edge.exception.message)


class DesignerToolSecurityTest(unittest.TestCase):
    def test_repo_read_rejects_traversal_and_allows_profile_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "apps" / "api" / "src" / "route.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const route = '/health';\n", encoding="utf-8")
            landscape = {
                "components": [{"component_id": "api", "roots": ["apps/api"], "file_refs": ["apps/api/src/route.ts"]}],
                "anchors": [
                    {
                        "anchor_id": "RLA-0000000000000001",
                        "kind": "source_file",
                        "component_id": "api",
                        "path": "apps/api/src/route.ts",
                        "symbol": "",
                        "evidence_source": "test",
                    }
                ],
                "scenario_matches": [
                    {
                        "scenario_id": "SC-1",
                        "surface": "api",
                        "candidate_anchor_refs": ["RLA-0000000000000001"],
                    }
                ],
                "snapshot": {},
            }
            graph = new_vertical_design_graph(run_id="run-1", snapshot_binding=self._binding())
            registry = DesignerToolRegistry(
                run_id="run-1",
                target_path=root,
                landscape=landscape,
                graph=graph,
                fake_mode=True,
            )

            output, refs = registry.execute("repo.read", {"path": "apps/api/src/route.ts", "start_line": 1})
            self.assertIn("export const route", output["content"])
            self.assertTrue(refs[0].startswith("repo:apps/api/src/route.ts#L1@sha256:"))
            surface_matches, surface_refs = registry.execute(
                "landscape.search",
                {"scenario_id": "SC-1", "surface": "api"},
            )
            self.assertEqual(surface_matches["count"], 1)
            self.assertEqual(surface_refs, ["RLA-0000000000000001"])
            with self.assertRaises(HarnessError) as caught:
                registry.execute("repo.read", {"path": "../secret.txt"})
            self.assertEqual(caught.exception.error_code, "designer_repo_path_unsafe")
            with self.assertRaises(HarnessError) as missing_anchor:
                registry.execute("landscape.get_anchor", {"anchor_id": "RLA-missing"})
            self.assertEqual(missing_anchor.exception.error_code, "designer_evidence_not_found")
            missing_index, _ = registry.execute("codegraph.query", {"query": "route"})
            self.assertEqual(missing_index["status"], "unavailable")
            self.assertEqual(missing_index["uncertainty"], "target_repo_codegraph_missing")
            self.assertFalse((root / ".codegraph").exists())

            source.write_text("x" * 20_000, encoding="utf-8")
            bounded, _ = registry.execute("repo.read", {"path": "apps/api/src/route.ts", "start_line": 1})
            self.assertLessEqual(len(bounded["content"]), 8_000)
            self.assertTrue(bounded["truncated"])

            link = root / "apps" / "api" / "src" / "route-link.ts"
            try:
                link.symlink_to(source)
            except OSError:
                pass
            else:
                landscape["components"][0]["file_refs"].append("apps/api/src/route-link.ts")
                with self.assertRaises(HarnessError) as symlink:
                    registry.execute("repo.read", {"path": "apps/api/src/route-link.ts"})
                self.assertEqual(symlink.exception.error_code, "designer_repo_path_unsafe")

    @staticmethod
    def _binding() -> dict[str, object]:
        return {"requirement_digest": "req", "repo_landscape_digest": "repo", "workspace_ref": "run_workspace_v1", "base_sha": "abc"}


class _FailingDesignerClient:
    def complete_messages(self, **_: object) -> LlmCompletion:
        raise HarnessError("llm_transport_failed", "Temporary Designer transport failure.")


class _ScriptedDesignerClient:
    def __init__(self, actions: list[dict[str, object]]):
        self.actions = list(actions)

    def complete_messages(self, **_: object) -> LlmCompletion:
        return LlmCompletion(content=json.dumps(self.actions.pop(0)))


class DesignerResumeTest(unittest.TestCase):
    def test_repair_targets_preserve_finding_order_and_deduplicate_scenarios(self) -> None:
        self.assertEqual(
            _repair_targets(
                [
                    {"diagram_ids": ["sequence_sc_3", "sequence_sc_1"]},
                    {"subject_ref": "sequence_sc_1:message:0001"},
                    {"subject_ref": "SC-2:terminal"},
                ]
            ),
            ["SC-3", "SC-1", "SC-2"],
        )

    def test_bundle_repair_task_is_scoped_to_one_component_partition(self) -> None:
        graph = {
            "nodes": [
                {"id": "VDN-api", "use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "api"},
                {"id": "VDN-web", "use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "web"},
            ],
            "edges": [
                {"id": "VDE-1", "scenario_id": "SC-3", "from": "VDN-api", "to": "VDN-web"},
            ],
            "partitions": [
                {"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "api", "coverage": {"api": "covered"}},
                {"use_case_id": "UC1", "scenario_id": "SC-3", "component_id": "web", "coverage": {"ui": "covered"}},
            ],
        }
        feedback = [
            {
                "category": "surface_contract_missing",
                "subject_ref": "SC-3:VDN-api:surface_contract",
                "diagram_ids": ["SC-3"],
            },
            {
                "category": "surface_contract_missing",
                "subject_ref": "SC-3:VDN-web:surface_contract",
                "diagram_ids": ["SC-3"],
            },
        ]

        api_task = _active_repair_task(repair_feedback=feedback, graph=graph, completed_targets=[])
        self.assertEqual(api_task["target_id"], "SC-3@api")
        self.assertEqual(api_task["partition"]["component_id"], "api")
        self.assertEqual([item["id"] for item in api_task["nodes"]], ["VDN-api"])
        self.assertEqual(len(api_task["findings"]), 1)

        web_task = _active_repair_task(
            repair_feedback=feedback,
            graph=graph,
            completed_targets=["SC-3@api"],
        )
        self.assertEqual(web_task["target_id"], "SC-3@web")
        self.assertEqual(web_task["partition"]["component_id"], "web")
        self.assertEqual([item["id"] for item in web_task["nodes"]], ["VDN-web"])
        self.assertEqual(len(web_task["findings"]), 1)

    def test_active_partition_groups_missing_surfaces_and_advances_by_component(self) -> None:
        requirement = {
            "use_case_id": "UC1",
            "scenario_contract": {
                "scenarios": [
                    {
                        "id": "SC-1",
                        "surfaces": {
                            "ui": {"status": "required", "requirement_refs": ["REQ-UI"]},
                            "api": {"status": "required", "requirement_refs": ["REQ-API"]},
                            "business_rule": {"status": "required", "requirement_refs": ["REQ-RULE"]},
                            "data": {"status": "required", "requirement_refs": ["REQ-DATA"]},
                        },
                    }
                ]
            },
        }
        landscape = {
            "components": [
                {"component_id": "web", "roots": ["apps/web"]},
                {"component_id": "api", "roots": ["apps/api"]},
                {"component_id": "database", "roots": ["packages/db"]},
            ],
            "anchors": [
                {"anchor_id": "RLA-ui", "component_id": "web", "kind": "ui_component", "path": "apps/web/App.tsx", "symbol": "App"},
                {"anchor_id": "RLA-api", "component_id": "api", "kind": "api_route", "path": "apps/api/app.ts", "symbol": "GET /items"},
                {"anchor_id": "RLA-rule", "component_id": "api", "kind": "service_symbol", "path": "apps/api/items.ts", "symbol": "listItems"},
            ],
            "scenario_matches": [
                {"scenario_id": "SC-1", "surface": "ui", "candidate_anchor_refs": ["RLA-ui"]},
                {"scenario_id": "SC-1", "surface": "api", "candidate_anchor_refs": ["RLA-api"]},
                {"scenario_id": "SC-1", "surface": "business_rule", "candidate_anchor_refs": ["RLA-rule"]},
            ],
        }
        graph = new_vertical_design_graph(
            run_id="run-active",
            snapshot_binding={"requirement_digest": "req", "repo_landscape_digest": "repo", "workspace_ref": "workspace", "base_sha": "abc"},
        )

        first = _active_partition(requirement_model=requirement, landscape=landscape, graph=graph)

        self.assertEqual(first["partition"]["component_id"], "web")
        self.assertEqual(first["missing_surfaces"], ["ui"])
        self.assertEqual(first["surface_work"][0]["candidate_anchors"][0]["anchor_id"], "RLA-ui")
        self.assertEqual(first["delta_scaffold"]["operations"][0], {"op": "set_partition_coverage", "coverage": {"ui": "covered"}})
        self.assertEqual(first["delta_scaffold"]["operations"][1]["node"]["temp_ref"], "tmp:SC-1:ui")

        graph["partitions"] = [
            {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "web", "coverage": {"ui": "covered"}}
        ]
        graph["nodes"] = [
            {"scenario_id": "SC-1", "component_id": "web", "surface": "ui"}
        ]
        second = _active_partition(requirement_model=requirement, landscape=landscape, graph=graph)

        self.assertEqual(second["partition"]["component_id"], "api")
        self.assertEqual(second["missing_surfaces"], ["api", "business_rule"])
        self.assertEqual(second["required_coverage"], {"api": "covered", "business_rule": "covered"})

    def test_transport_pause_resumes_same_session_without_repeating_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            (target / "route.ts").write_text("export const route = '/items';\n", encoding="utf-8")
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            settings = SimpleNamespace(
                llm_mode="deepseek",
                design_llm_call_budget=5,
                llm_structured_max_tokens=1000,
            )
            run_id = "run-resume"
            requirement = {
                "use_case_id": "UC1",
                "scenario_contract": {
                    "scenarios": [{"id": "SC-1", "surfaces": {"api": {"status": "required"}}}]
                },
            }
            landscape = {
                "artifact_digest": "sha256:repo",
                "components": [{"component_id": "api", "roots": ["."], "file_refs": ["route.ts"]}],
                "anchors": [
                    {
                        "anchor_id": "RLA-0000000000000001",
                        "kind": "api_route",
                        "component_id": "api",
                        "path": "route.ts",
                        "symbol": "route",
                        "evidence_source": "test",
                    }
                ],
                "scenario_matches": [
                    {
                        "scenario_id": "SC-1",
                        "surface": "api",
                        "candidate_anchor_refs": ["RLA-0000000000000001"],
                    }
                ],
                "snapshot": {},
            }
            binding = {
                "requirement_digest": "sha256:req",
                "repo_landscape_digest": "sha256:repo",
                "workspace_ref": "run_workspace_v1",
                "base_sha": "abc123",
            }
            initial = new_vertical_design_graph(run_id=run_id, snapshot_binding=binding)
            delta = {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": initial["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {"api": "covered"}},
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": "tmp:api",
                            "semantic_key": "SC-1:api",
                            "name": "Items route",
                            "kind": "api_contract",
                            "surface": "api",
                            "disposition": "reuse",
                            "requirement_refs": ["REQ-1"],
                            "evidence_refs": ["RLA-0000000000000001"],
                            "evidence_reason": "",
                            "contract": "Handle the items request.",
                        },
                    },
                ],
                "completion_claims": [],
                "unresolved_items": [],
            }

            with self.assertRaises(DesignerExecutionError) as paused:
                DesignerToolLoopExecutor(settings=settings, store=store, llm_client=_FailingDesignerClient()).execute(
                    run_id=run_id,
                    requirement_model=requirement,
                    landscape=landscape,
                    target_path=target,
                    snapshot_binding=binding,
                )
            self.assertTrue(paused.exception.retriable)

            result = DesignerToolLoopExecutor(
                settings=settings,
                store=store,
                llm_client=_ScriptedDesignerClient(
                    [
                        {"action": "landscape.search", "arguments": {"scenario_id": "SC-1", "surface": "api"}},
                        {"action": "design.inspect_partition", "arguments": {"scenario_id": "SC-1"}},
                        {"action": "landscape.search", "arguments": {"scenario_id": "SC-1", "surface": "api"}},
                        {"action": "design.propose_delta", "arguments": {"delta": delta}},
                        {"action": "complete", "arguments": {}},
                    ]
                ),
            ).execute(
                run_id=run_id,
                requirement_model=requirement,
                landscape=landscape,
                target_path=target,
                snapshot_binding=binding,
            )

            self.assertEqual(result.graph["revision"], 1)
            self.assertEqual(len(result.delta_paths), 1)
            self.assertEqual(result.session_path, paused.exception.session_path)
            self.assertEqual(result.progress["status"], "completed")
            self.assertEqual(result.progress["invalid_action_budget"]["used"], 2)

            node_id = str(result.graph["nodes"][0]["id"])
            repair_delta = {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": result.graph["artifact_digest"],
                "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "api"},
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {"api": "covered"}},
                    {"op": "add_edge", "edge": {"from": node_id, "to": node_id, "kind": "depends_on", "label": "retain validated route contract", "scenario_id": "SC-1"}},
                ],
                "completion_claims": [],
                "unresolved_items": [],
            }
            repaired = DesignerToolLoopExecutor(
                settings=settings,
                store=store,
                llm_client=_ScriptedDesignerClient(
                    [{"action": "design.propose_delta", "arguments": {"delta": repair_delta}}]
                ),
            ).execute(
                run_id=run_id,
                requirement_model=requirement,
                landscape=landscape,
                target_path=target,
                snapshot_binding=binding,
                existing_graph=result.graph,
                repair_feedback=[{"id": "SEM-1", "message": "Add the missing interaction."}],
                session_ordinal=2,
            )
            self.assertEqual(repaired.graph["revision"], 2)
            self.assertEqual(len(repaired.graph["edges"]), 1)

            # Recreate the narrow crash window where the delta artifact reached
            # disk but the session cursor/graph update did not. Resume must
            # reconcile that result instead of issuing the mutation again.
            session_file = root / result.session_path
            persisted = json.loads(session_file.read_text(encoding="utf-8"))
            persisted["status"] = "paused"
            persisted["graph"] = initial
            persisted["delta_paths"] = []
            persisted["budgets"]["action"]["used"] = 0
            persisted["cursor"] = {
                "next_action_index": 4,
                "pending_call": {
                    "call_id": "designer-0004",
                    "stage": "tool",
                    "tool_id": "design.propose_delta",
                    "arguments": {"delta": delta},
                },
            }
            session_file.write_text(json.dumps(persisted), encoding="utf-8")

            reconciled = DesignerToolLoopExecutor(
                settings=settings,
                store=store,
                llm_client=_ScriptedDesignerClient([{"action": "complete", "arguments": {}}]),
            ).execute(
                run_id=run_id,
                requirement_model=requirement,
                landscape=landscape,
                target_path=target,
                snapshot_binding=binding,
            )
            self.assertEqual(reconciled.graph["revision"], 1)
            self.assertEqual(reconciled.delta_paths, result.delta_paths)


class RepoAwareDesignerWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = GitRepositoryTemplate(populate_typescript_target)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.template.close()

    def test_l3_fake_run_publishes_bundle_and_phase3_accepts_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parents[1]
            temp_path = Path(temp)
            target = self.template.materialize(temp_path / "target")
            runtime = temp_path / ".runtime"
            settings = load_settings(
                root=root,
                runtime_root=runtime,
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                    "SDLC_VERTICAL_TRACE_MODE": "shadow",
                    "SDLC_DESIGN_LLM_CALL_BUDGET": "24",
                    "SDLC_TARGET_REPO_PATH": str(target.canonical),
                    "SDLC_TARGET_REPO_REMOTE": str(target.remote.resolve()),
                    "SDLC_TARGET_BASE_BRANCH": "main",
                    "SDLC_TARGET_WORKTREE_ROOT": str(temp_path / "worktrees"),
                },
            )
            created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
            self.assertEqual(created["designer_contract_version"], GRAPH_VERSION)
            self.assertEqual(created[VERTICAL_TRACE_STATE_KEY], VERTICAL_TRACE_GRAPH_VERSION)
            self.assertEqual(created[VERTICAL_TRACE_MODE_STATE_KEY], "shadow")

            designed = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

            self.assertEqual(designed["status"], "waiting_design_approval", designed)
            self.assertEqual(designed["designer_execution"]["status"], "completed")
            self.assertEqual(designed[DESIGN_BUNDLE_STATE_KEY], DESIGN_BUNDLE_VERSION)
            self.assertEqual(designed["quality_reports"]["design_validation"]["mode"], GRAPH_VERSION)
            for key in (
                "vertical_design_graph",
                "design_projection_manifest",
                "design_finding_ledger",
                "design_artifact",
                "class_diagram_graph",
                "sequence_diagram_graph",
                "requirement_design_trace",
                "design_component_view",
                "design_surface_view",
                "vertical_trace_graph",
                "vertical_trace_validation",
            ):
                self.assertIn(key, designed["latest_artifacts"])
            graph_path = root / str(designed["latest_artifacts"]["vertical_design_graph"])
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            self.assertEqual(graph["schema_version"], GRAPH_VERSION)
            self.assertGreater(graph["revision"], 0)
            self.assertTrue(graph["nodes"])
            self.assertEqual(designed["pending_checkpoint"]["vertical_design_graph"], designed["latest_artifacts"]["vertical_design_graph"])
            sync_report_path = root / str(designed["last_sync"]["report"])
            sync_report = json.loads(sync_report_path.read_text(encoding="utf-8"))
            self.assertIn("vertical_design_graph_ref", sync_report["checkpoint"])
            self.assertIn("projection_manifest_ref", sync_report["checkpoint"])
            self.assertIn("vertical_trace_graph_ref", sync_report["checkpoint"])
            self.assertIn("vertical_trace_validation_ref", sync_report["checkpoint"])
            trace_report = json.loads(
                (root / str(designed["latest_artifacts"]["vertical_trace_validation"])).read_text(encoding="utf-8")
            )
            self.assertEqual(trace_report["mode"], "shadow")
            self.assertIn("vertical_trace", designed["quality_reports"])
            component_view = json.loads((root / str(designed["latest_artifacts"]["design_component_view"])).read_text(encoding="utf-8"))
            surface_view = json.loads((root / str(designed["latest_artifacts"]["design_surface_view"])).read_text(encoding="utf-8"))
            self.assertEqual(component_view["schema_version"], "design_component_view_v2")
            self.assertIn("@startuml", component_view["plantuml"])
            self.assertEqual(surface_view["schema_version"], "design_surface_view_v2")
            self.assertTrue(surface_view["business_rules"])
            self.assertFalse(surface_view["ui"])
            self.assertFalse(surface_view["api"])
            self.assertFalse(surface_view["data"])
            self.assertEqual(
                {item["surface"] for item in surface_view["not_applicable"]},
                {"ui", "api", "data"},
            )
            mock_page = (root / str(next(target["path"] for target in sync_report["targets"] if target["tool"] == "confluence"))).read_text(encoding="utf-8")
            for heading in (
                "1. Tóm tắt dành cho người duyệt",
                "2. Tính năng sẽ hoạt động như thế nào?",
                "3. Hệ thống sẽ thay đổi ở đâu?",
                "4. Các hợp đồng chính cần biết",
                "5. Điểm cần review trước khi duyệt",
            ):
                self.assertIn(heading, mock_page)
            for heading in ("System Architecture", "UI States and Interactions", "API Contracts", "Business Rules", "Data Model and Operations", "AS-IS / GAP / TO-BE Impact", "Requirement to Design Trace", "Compatibility UML"):
                self.assertIn(heading, mock_page)
            for detail in ("Authorization", "Inputs", "Migration", "TO-BE contract"):
                self.assertIn(detail, mock_page)
            self.assertIn("<summary>Mở phụ lục kỹ thuật", mock_page)
            self.assertLess(mock_page.index("1. Tóm tắt dành cho người duyệt"), mock_page.index("System Architecture"))
            confluence_body = _confluence_design_storage_body(
                PhaseTwoWorkflow(settings).store,
                str(created["run_id"]),
                str(designed["source_ref"]),
                {str(key): str(value) for key, value in designed["latest_artifacts"].items()},
            )
            for heading in (
                "<h2>1. Tóm tắt dành cho người duyệt</h2>",
                "<h2>2. Tính năng sẽ hoạt động như thế nào?</h2>",
                "<h2>3. Hệ thống sẽ thay đổi ở đâu?</h2>",
                "<h2>4. Các hợp đồng chính cần biết</h2>",
                "<h2>5. Điểm cần review trước khi duyệt</h2>",
                "<h2>Phụ lục kỹ thuật</h2>",
                "<h2>Phụ lục UML</h2>",
            ):
                self.assertIn(heading, confluence_body)
            for heading in (
                "<h2>System Architecture</h2>",
                "<h2>UI States and Interactions</h2>",
                "<h2>API Contracts</h2>",
                "<h2>Business Rules</h2>",
                "<h2>Data Model and Operations</h2>",
                "<h2>AS-IS / GAP / TO-BE Impact</h2>",
                "<h2>Requirement to Design Trace</h2>",
            ):
                self.assertIn(heading, confluence_body)
            self.assertIn("Mở phụ lục kỹ thuật: contracts, trace, artifacts và quality report", confluence_body)
            self.assertLess(confluence_body.index("1. Tóm tắt dành cho người duyệt"), confluence_body.index("System Architecture"))
            self.assertIn("plantuml-confluence", confluence_body)

            legacy_graph = json.loads(graph_path.read_text(encoding="utf-8"))
            for node in legacy_graph["nodes"]:
                node.pop("surface_contract", None)
            legacy_graph["artifact_digest"] = graph_digest(legacy_graph)
            graph_path.write_text(json.dumps(legacy_graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            state = PhaseTwoWorkflow(settings).store.read_state(str(created["run_id"]))
            state.pop(DESIGN_BUNDLE_STATE_KEY, None)
            state.update(
                {
                    "current_phase": "design_investigating",
                    "status": "design_blocked",
                    "allowed_next_actions": ["revalidate_design", "get_status", "cancel_run"],
                    "last_error": {
                        "ok": False,
                        "error_code": "designer_invalid_action_exhausted",
                        "message": "Designer exceeded its invalid-action budget.",
                        "run_id": str(created["run_id"]),
                    },
                    "designer_execution": {
                        **dict(state.get("designer_execution") or {}),
                        "status": "blocked",
                        "session_ordinal": 2,
                        "design_version": 2,
                    },
                }
            )
            PhaseTwoWorkflow(settings).store.write_state(state)

            revalidated = PhaseTwoWorkflow(settings).revalidate_design(run_id=str(created["run_id"]))
            self.assertEqual(revalidated["status"], "waiting_design_approval", revalidated)
            self.assertEqual(revalidated[DESIGN_BUNDLE_STATE_KEY], DESIGN_BUNDLE_VERSION)
            self.assertEqual(revalidated["designer_execution"]["session_ordinal"], 3)
            self.assertNotEqual(revalidated["latest_artifacts"]["vertical_design_graph"], str(designed["latest_artifacts"]["vertical_design_graph"]))
            revalidated_graph = json.loads((root / str(revalidated["latest_artifacts"]["vertical_design_graph"])).read_text(encoding="utf-8"))
            self.assertTrue(all(node.get("surface_contract") for node in revalidated_graph["nodes"]))

            workflow = PhaseThreeWorkflow(settings)
            state = workflow.store.read_state(str(created["run_id"]))
            state[VERTICAL_TRACE_MODE_STATE_KEY] = "enforce"
            workflow.store.write_state(state)
            with self.assertRaises(HarnessError) as stale_trace:
                workflow.approve_design_checkpoint(
                    run_id=str(created["run_id"]),
                    checkpoint_id=str(revalidated["pending_checkpoint"]["checkpoint_id"]),
                )
            self.assertEqual(stale_trace.exception.error_code, "requirement_v2_phase3_input_invalid")
            state = workflow.store.read_state(str(created["run_id"]))
            state[VERTICAL_TRACE_MODE_STATE_KEY] = "shadow"
            workflow.store.write_state(state)
            planned = workflow.approve_design_checkpoint(
                run_id=str(created["run_id"]),
                checkpoint_id=str(revalidated["pending_checkpoint"]["checkpoint_id"]),
            )
            self.assertEqual(planned["status"], "waiting_implementation_plan_review", planned)

    def test_vertical_trace_enforce_passes_and_admits_phase3(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parents[1]
            temp_path = Path(temp)
            target = self.template.materialize(temp_path / "target")
            settings = load_settings(
                root=root,
                runtime_root=temp_path / ".runtime",
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                    "SDLC_VERTICAL_TRACE_MODE": "enforce",
                    "SDLC_DESIGN_LLM_CALL_BUDGET": "24",
                    "SDLC_TARGET_REPO_PATH": str(target.canonical),
                    "SDLC_TARGET_REPO_REMOTE": str(target.remote.resolve()),
                    "SDLC_TARGET_BASE_BRANCH": "main",
                    "SDLC_TARGET_WORKTREE_ROOT": str(temp_path / "worktrees"),
                },
            )
            created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
            designed = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

            self.assertEqual(designed[VERTICAL_TRACE_MODE_STATE_KEY], "enforce")
            self.assertEqual(designed["status"], "waiting_design_approval", designed)
            self.assertTrue(designed["quality_reports"]["vertical_trace"]["ok"])
            self.assertEqual(designed["quality_reports"]["vertical_trace"]["blocking_count"], 0)

            planned = PhaseThreeWorkflow(settings).approve_design_checkpoint(
                run_id=str(created["run_id"]),
                checkpoint_id=str(designed["pending_checkpoint"]["checkpoint_id"]),
            )
            self.assertEqual(planned["status"], "waiting_implementation_plan_review", planned)

    def test_new_runs_always_pin_the_vertical_designer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parents[1]
            temp_path = Path(temp)
            target = self.template.materialize(temp_path / "target")
            settings = load_settings(
                root=root,
                runtime_root=temp_path / ".runtime",
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                    "SDLC_TARGET_REPO_PATH": str(target.canonical),
                    "SDLC_TARGET_REPO_REMOTE": str(target.remote.resolve()),
                    "SDLC_TARGET_BASE_BRANCH": "main",
                    "SDLC_TARGET_WORKTREE_ROOT": str(temp_path / "worktrees"),
                },
            )

            created = PhaseOneWorkflow(settings).start_or_get_run(
                dataset="eTour",
                use_case="UC1",
                requested_by="test",
            )

            self.assertEqual(created["designer_contract_version"], GRAPH_VERSION)


if __name__ == "__main__":
    unittest.main()
