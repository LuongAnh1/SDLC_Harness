"""L3.10 M1: UI runtime surface binding (entrypoint -> composition -> required inputs)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.designer import (
    GRAPH_VERSION_V2,
    RUNTIME_BINDING_VERSION,
    SURFACE_CONTRACT_VERSION,
    UI_EXPERIENCE_CONTRACT_V2,
    UIUX_CONTRACT_VERSION,
    DesignerToolLoopExecutor,
    completion_findings,
    design_bundle_findings,
    new_vertical_design_graph,
    resolve_ui_contract_pin,
    validate_vertical_design_graph,
)
from sdlc_harness.agents.designer.execution import (
    _feature_ux_decisions,
    _materialize_delta_wiring,
    _wiring_operation_templates,
    _active_partition,
    _active_requirement_slice,
    _construction_graph_view,
    _designer_prompt,
    _guidance_key,
    _json_structure_hint,
    _parse_designer_tool_action,
)
from sdlc_harness.agents.designer.graph import (
    UI_LAYERED_LAYOUT_V1,
    UI_LAYOUT_CONTRACT_KEY,
    canonicalize_composition_wiring,
    graph_digest,
)
from sdlc_harness.agents.designer.projections import render_design_projections, render_ui_feature_experience
from sdlc_harness.agents.validator.design_validation import (
    _holistic_ui_review_instruction,
    _vertical_design_review_view,
)
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.agents.designer import UI_CONTRACT_STATE_KEY
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from tests._support import GitRepositoryTemplate, populate_typescript_target
from sdlc_harness.agents.designer.ui_context import build_ui_repo_context, chain_path_to, required_props
from sdlc_harness.agents.designer.uiux import RUNTIME_SURFACE_BINDING_VERSION
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError

MAIN = """import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { AppShell } from "./AppShell";

function Root() {
  const [health] = useState<HealthContract | null>(null);
  return <AppShell title="Target" health={health} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
"""

APP_SHELL = """import { Banner as TopBanner } from "./Banner";

export interface AppShellProps {
  readonly title: string;
  readonly health: string | null;
  readonly items?: readonly string[];
}

export function AppShell({ title, health, items = [] }: AppShellProps) {
  return (
    <main>
      <TopBanner text={title} />
      <p role="status">{health}</p>
    </main>
  );
}
"""

BANNER = """export function Banner({ text }: { text: string }) {
  return <h1>{text}</h1>;
}
"""

VIEW = """export interface DeleteViewProps {
  readonly items: readonly string[];
  readonly onDeleted?: (id: string) => void;
}

export function DeleteView({ items }: DeleteViewProps) {
  return <ul>{items.length}</ul>;
}
"""

ENTRY = "apps/web/src/main.tsx#Root"
SHELL = "apps/web/src/AppShell.tsx#AppShell"
VIEW_REF = "apps/web/src/DeleteView.tsx#DeleteView"


def write_web_shell(target: Path, *, with_view: bool = True) -> None:
    web = target / "apps/web/src"
    web.mkdir(parents=True, exist_ok=True)
    (web / "main.tsx").write_text(MAIN, encoding="utf-8")
    (web / "AppShell.tsx").write_text(APP_SHELL, encoding="utf-8")
    (web / "Banner.tsx").write_text(BANNER, encoding="utf-8")
    if with_view:
        (web / "DeleteView.tsx").write_text(VIEW, encoding="utf-8")
    (target / "vitest.config.ts").write_text('export default { test: { environment: "node" } };\n', encoding="utf-8")


LANDSCAPE = {
    "components": [{"component_id": "web", "roots": ["apps/web/src"]}],
    "anchors": [
        {"anchor_id": "RLA-main", "kind": "ui_entrypoint", "component_id": "web", "path": "apps/web/src/main.tsx", "symbol": "main"},
        {"anchor_id": "RLA-root", "kind": "ui_component", "component_id": "web", "path": "apps/web/src/main.tsx", "symbol": "Root"},
        {"anchor_id": "RLA-shell", "kind": "ui_component", "component_id": "web", "path": "apps/web/src/AppShell.tsx", "symbol": "AppShell"},
        {"anchor_id": "RLA-view", "kind": "ui_component", "component_id": "web", "path": "apps/web/src/DeleteView.tsx", "symbol": "DeleteView"},
    ],
}


class UiRepoContextTest(unittest.TestCase):
    def test_extracts_entrypoint_chain_props_and_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            write_web_shell(target)
            context = build_ui_repo_context(
                target_path=target,
                landscape=LANDSCAPE,
                project_profile_id="typescript_fullstack_v1",
            )
            again = build_ui_repo_context(
                target_path=target,
                landscape=LANDSCAPE,
                project_profile_id="typescript_fullstack_v1",
            )

        assert context is not None and again is not None
        self.assertEqual(context["artifact_digest"], again["artifact_digest"])
        self.assertEqual(context["entrypoint"]["ref"], ENTRY)
        hops = {hop["ref"]: hop for hop in context["composition_chain"]}
        self.assertEqual(hops[ENTRY]["renders"], [SHELL])
        self.assertEqual(hops[ENTRY]["passes_props"][SHELL], ["health", "title"])
        # Aliased import resolves to the original symbol; generic `<HealthContract | null>` is not JSX.
        self.assertEqual(hops[SHELL]["renders"], ["apps/web/src/Banner.tsx#Banner"])
        self.assertEqual(required_props(context, SHELL), ["title", "health"])
        self.assertEqual(required_props(context, VIEW_REF), ["items"])
        self.assertEqual(chain_path_to(context, "apps/web/src/Banner.tsx#Banner"), [ENTRY, SHELL, "apps/web/src/Banner.tsx#Banner"])
        self.assertEqual(chain_path_to(context, VIEW_REF), [])
        self.assertEqual(context["styles"]["convention"], "none")
        self.assertFalse(context["auth"]["present"])
        self.assertFalse(context["verification"]["dom_capable"])
        self.assertFalse(context["verification"]["browser_stage_active"])
        self.assertTrue(all(ref.startswith("repo:apps/web/src/") for ref in context["evidence_refs"]))
        # Inline object prop types are parsed too.
        self.assertEqual(required_props(context, "apps/web/src/Banner.tsx#Banner"), ["text"])

    def test_entrypoint_skips_external_wrappers_and_hashes_styles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            write_web_shell(target)
            main = target / "apps/web/src/main.tsx"
            main.write_text(
                main.read_text(encoding="utf-8").replace("<Root />", "<BrowserRouter><Root /></BrowserRouter>"),
                encoding="utf-8",
            )
            stylesheet = target / "apps/web/src/app.css"
            stylesheet.write_text("main { color: black; }\n", encoding="utf-8")
            first = build_ui_repo_context(target_path=target, landscape=LANDSCAPE, project_profile_id="typescript_fullstack_v1")
            stylesheet.write_text("main { color: red; }\n", encoding="utf-8")
            second = build_ui_repo_context(target_path=target, landscape=LANDSCAPE, project_profile_id="typescript_fullstack_v1")
        assert first is not None and second is not None
        self.assertEqual(first["entrypoint"]["ref"], ENTRY)
        self.assertEqual(first["styles"]["convention"], "stylesheet")
        self.assertNotEqual(first["artifact_digest"], second["artifact_digest"])

    def test_backend_only_landscape_has_no_ui_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            context = build_ui_repo_context(
                target_path=Path(temp),
                landscape={"components": [{"component_id": "api", "roots": ["apps/api/src"]}], "anchors": []},
                project_profile_id="typescript_fullstack_v1",
            )
        self.assertIsNone(context)


class UiContractPinTest(unittest.TestCase):
    def test_v2_requires_graph_v2_and_uiux_enforce(self) -> None:
        self.assertEqual(
            resolve_ui_contract_pin(
                ui_contract_version=UI_EXPERIENCE_CONTRACT_V2, graph_v2=True, uiux_mode="enforce", run_id="r"
            ),
            UI_EXPERIENCE_CONTRACT_V2,
        )
        for graph_v2, mode in ((False, "enforce"), (True, "off")):
            with self.assertRaises(HarnessError) as raised:
                resolve_ui_contract_pin(
                    ui_contract_version=UI_EXPERIENCE_CONTRACT_V2, graph_v2=graph_v2, uiux_mode=mode, run_id="r"
                )
            self.assertEqual(raised.exception.error_code, "designer_ui_contract_invalid")

    def test_config_defaults_to_v1(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            default = load_settings(root=root, runtime_root=Path(temp), overrides={})
            pinned = load_settings(
                root=root,
                runtime_root=Path(temp),
                overrides={"SDLC_DESIGNER_UI_CONTRACT_VERSION": UI_EXPERIENCE_CONTRACT_V2},
            )
        self.assertEqual(default.designer_ui_contract_version, "ui_experience_contract_v1")
        self.assertEqual(pinned.designer_ui_contract_version, UI_EXPERIENCE_CONTRACT_V2)


class _UiGraphFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        write_web_shell(Path(temp.name))
        self.context = build_ui_repo_context(
            target_path=Path(temp.name),
            landscape=LANDSCAPE,
            project_profile_id="typescript_fullstack_v1",
        )

    def _node(
        self,
        node_id: str,
        target_ref: str,
        *,
        owner: str,
        owner_anchor: str,
        composition: list[str],
        inputs: list[dict[str, object]],
        status: str,
        disposition: str = "add",
        runtime_kind: str = "ui_component",
        relation: str = "rendered_by",
    ) -> dict[str, object]:
        path, _, symbol = target_ref.partition("#")
        owner_path, _, owner_symbol = owner.partition("#")
        return {
            "id": node_id,
            "scenario_id": "SC-1",
            "component_id": "web",
            "kind": "ui_interaction",
            "surface": "ui",
            "disposition": disposition,
            "requirement_refs": ["AC-1"],
            "evidence_refs": ["RLA-root"] if disposition != "add" else [],
            "evidence_reason": "New screen." if disposition == "add" else "",
            "contract": "Show records and delete one.",
            "surface_contract": {
                "schema_version": SURFACE_CONTRACT_VERSION,
                "surface": "ui",
                "entrypoint": target_ref,
                "states": ["ready"],
                "transitions": [{"from": "ready", "action": "delete", "to": "ready", "outcome": "row removed"}],
                "validations": [],
                "outcomes": ["row removed"],
                "runtime_surface": {
                    "schema_version": RUNTIME_SURFACE_BINDING_VERSION,
                    "screen_id": "delete-screen",
                    "entrypoint": ENTRY,
                    "composition_path": composition,
                    "required_inputs": inputs,
                    "runnable_status": status,
                    "evidence_refs": [],
                },
            },
            "runtime_binding": {
                "schema_version": RUNTIME_BINDING_VERSION,
                "runtime_kind": runtime_kind,
                "target": {"component_id": "web", "path": path, "symbol": symbol},
                "attachment": {
                    "relation": relation,
                    "owner_ref": owner_anchor,
                    "owner_path": owner_path,
                    "owner_symbol": owner_symbol,
                },
                "evidence_refs": [owner_anchor],
            },
        }

    def _graph(self, nodes: list[dict[str, object]], *, layered: bool = False) -> dict[str, object]:
        graph: dict[str, object] = {
            "schema_version": GRAPH_VERSION_V2,
            "revision": 1,
            "run_id": "run-ui-v2",
            "snapshot_binding": {
                "requirement_digest": "sha256:req",
                "designer_ui_contract_version": UI_EXPERIENCE_CONTRACT_V2,
                "ui_repo_context": self.context,
                "requirement_reference_ids": ["AC-1", "DEC-1", "MF-1", "PRE-1"],
                **({UI_LAYOUT_CONTRACT_KEY: UI_LAYERED_LAYOUT_V1} if layered else {}),
            },
            "partitions": [
                {
                    "use_case_id": "UC1",
                    "scenario_id": scenario_id,
                    "component_id": "web",
                    "coverage": {"ui": "covered"},
                    "source_observation_refs": ["repo:apps/web/src/AppShell.tsx#L1@sha256:x"],
                }
                for scenario_id in sorted({"SC-1", *(str(node.get("scenario_id") or "SC-1") for node in nodes)})
            ],
            "nodes": nodes,
            "edges": [],
            "unresolved_items": [],
        }
        graph["artifact_digest"] = graph_digest(graph)
        return validate_vertical_design_graph(graph, expected_run_id="run-ui-v2")

    def _categories(self, nodes: list[dict[str, object]]) -> set[str]:
        return {str(item["category"]) for item in design_bundle_findings(self._graph(nodes), landscape=LANDSCAPE)}

    def _items_input(self, **overrides: object) -> dict[str, object]:
        return {
            "name": "items",
            "source": "external_use_case",
            "binding": "PRE-1",
            "provided_by": ENTRY,
            "requirement_refs": ["PRE-1"],
            "states": ["loading", "ready", "empty", "error"],
            **overrides,
        }

    def _wiring_nodes(self, *, declare_shell_props: bool = True) -> list[dict[str, object]]:
        shell_inputs = [
            {"name": name, "source": "existing_runtime", "binding": ENTRY, "provided_by": ENTRY, "requirement_refs": [], "states": []}
            for name in ("title", "health")
        ]
        nodes = [
            self._node(
                "VDN-1",
                ENTRY,
                owner="apps/web/src/main.tsx#main",
                owner_anchor="RLA-main",
                composition=[ENTRY],
                inputs=[],
                status="bound",
                disposition="modify",
                runtime_kind="entrypoint",
                relation="owned_by",
            ),
            self._node(
                "VDN-2",
                SHELL,
                owner=ENTRY,
                owner_anchor="RLA-root",
                composition=[ENTRY, SHELL],
                inputs=shell_inputs if declare_shell_props else [],
                status="bound",
                disposition="modify",
            ),
        ]
        for node, child in zip(nodes, (SHELL, VIEW_REF)):
            surface = node["surface_contract"]["runtime_surface"]
            surface["role"] = "composition_wiring"
            surface["forwarded_inputs"] = [
                {"screen_id": "delete-screen", "to": child, "names": ["items"]}
            ]
        return nodes

    def _view(self, **overrides: object) -> dict[str, object]:
        options: dict[str, object] = {
            "owner": SHELL,
            "owner_anchor": "RLA-shell",
            "composition": [ENTRY, SHELL, VIEW_REF],
            "inputs": [self._items_input()],
            "status": "externally_blocked",
        }
        options.update(overrides)
        return self._node("VDN-3", VIEW_REF, **options)  # type: ignore[arg-type]


class UiRuntimeSurfaceFindingsTest(_UiGraphFixture):
    def test_external_input_with_modified_hops_passes(self) -> None:
        categories = self._categories([*self._wiring_nodes(), self._view()])
        self.assertFalse({c for c in categories if c.startswith("ui_") or c == "composition_hop_unmodeled"}, categories)

    def test_each_declared_input_is_tracked_on_every_wiring_hop(self) -> None:
        view = self._view(inputs=[self._items_input(), self._items_input(name="agencySession")])
        graph = self._graph([*self._wiring_nodes(), view])
        self.assertIn(
            "composition_hop_unmodeled",
            {str(item["category"]) for item in design_bundle_findings(graph, landscape=LANDSCAPE)},
        )

        canonical = canonicalize_composition_wiring(graph)
        self.assertNotIn(
            "composition_hop_unmodeled",
            {str(item["category"]) for item in design_bundle_findings(canonical, landscape=LANDSCAPE)},
        )
        wiring = [
            node
            for node in canonical["nodes"]
            if node["surface_contract"]["runtime_surface"].get("role") == "composition_wiring"
        ]
        self.assertEqual(len(wiring), 2)
        for node in wiring:
            groups = node["surface_contract"]["runtime_surface"]["forwarded_inputs"]
            self.assertEqual(groups[0]["names"], ["agencySession", "items"])

    def test_empty_default_input_without_hop_changes_is_blocked(self) -> None:
        # Mirrors run_20261030: the view is mounted but nothing upstream supplies its records.
        categories = self._categories([self._view()])
        self.assertIn("composition_hop_unmodeled", categories)

    def test_view_must_be_reachable_from_app_entrypoint(self) -> None:
        view = self._view(composition=[SHELL, VIEW_REF])
        view["surface_contract"]["runtime_surface"]["entrypoint"] = SHELL
        view["surface_contract"]["runtime_surface"]["required_inputs"][0]["provided_by"] = SHELL
        categories = self._categories([*self._wiring_nodes(), view])
        self.assertIn("ui_entrypoint_unreachable", categories)

    def test_owner_must_be_parent_hop(self) -> None:
        categories = self._categories(
            [*self._wiring_nodes(), self._view(owner=ENTRY, owner_anchor="RLA-root")]
        )
        self.assertIn("ui_attachment_off_path", categories)

    def test_consecutive_as_is_hops_must_be_a_real_render_edge(self) -> None:
        banner = "apps/web/src/Banner.tsx#Banner"
        # Root renders AppShell, not Banner: a path that skips AppShell is not reachable as-is.
        view = self._view(composition=[ENTRY, banner, VIEW_REF], owner=banner, owner_anchor="RLA-shell")
        self.assertIn("ui_composition_hop_invalid", self._categories([view]))

    def test_backend_only_v2_graph_is_not_blocked_by_missing_web_entrypoint(self) -> None:
        self.context = None
        graph = self._graph([])
        categories = {str(item["category"]) for item in design_bundle_findings(graph, landscape=LANDSCAPE)}
        self.assertNotIn("ui_entrypoint_unresolved", categories)

    def test_target_owned_state_is_not_a_runtime_input(self) -> None:
        view = self._view(inputs=[self._items_input(), self._items_input(name="pendingDeletion", provided_by=VIEW_REF)])
        findings = design_bundle_findings(self._graph([*self._wiring_nodes(), view]), landscape=LANDSCAPE)
        message = next(item["message"] for item in findings if item["category"] == "ui_required_input_unbound")
        self.assertIn("state owned by the target component itself", message)

    def test_unknown_hop_is_rejected(self) -> None:
        categories = self._categories(
            [*self._wiring_nodes(), self._view(composition=[ENTRY, "apps/web/src/Ghost.tsx#Ghost", VIEW_REF], owner="apps/web/src/Ghost.tsx#Ghost")]
        )
        self.assertIn("ui_composition_hop_invalid", categories)

    def test_external_input_must_cite_existing_precondition(self) -> None:
        categories = self._categories(
            [*self._wiring_nodes(), self._view(inputs=[self._items_input(binding="PRE-9", requirement_refs=["PRE-9"])])]
        )
        self.assertIn("ui_required_input_unbound", categories)

    def test_external_input_may_cite_the_flow_step_that_states_the_dependency(self) -> None:
        view = self._view(inputs=[self._items_input(binding="MF-1", requirement_refs=["MF-1"])])
        self.assertNotIn("ui_required_input_unbound", self._categories([*self._wiring_nodes(), view]))

    def test_runnable_status_must_follow_inputs(self) -> None:
        categories = self._categories([*self._wiring_nodes(), self._view(status="bound")])
        self.assertIn("ui_runnable_status_inconsistent", categories)

    def test_modified_as_is_component_must_declare_required_props(self) -> None:
        self.assertIn("ui_required_input_undeclared", self._categories(self._wiring_nodes(declare_shell_props=False)))
        self.assertNotIn("ui_required_input_undeclared", self._categories(self._wiring_nodes()))

    def test_missing_runtime_surface_is_structurally_invalid_under_v2(self) -> None:
        node = self._view()
        del node["surface_contract"]["runtime_surface"]
        with self.assertRaises(HarnessError) as raised:
            self._graph([node])
        self.assertIn("runtime_surface", raised.exception.message)

    def test_v1_graph_ignores_runtime_surface_rules(self) -> None:
        graph = self._graph([self._view()])
        binding = {key: value for key, value in graph["snapshot_binding"].items() if key != "designer_ui_contract_version"}
        historical = json.loads(json.dumps({**graph, "snapshot_binding": binding}))
        historical["artifact_digest"] = graph_digest(historical)
        categories = {str(item["category"]) for item in design_bundle_findings(historical, landscape=LANDSCAPE)}
        self.assertFalse({c for c in categories if c.startswith("ui_") or c == "composition_hop_unmodeled"})


class FakeDesignerRuntimeSurfaceTest(unittest.TestCase):
    REQUIREMENT = {
        "use_case_id": "UC-UI",
        "preconditions": [{"id": "PRE-1", "text": "Records come from search."}],
        "scenario_contract": {
            "scenarios": [
                {
                    "id": "SC-1",
                    "requirement_refs": ["MAIN-1"],
                    "surfaces": {"ui": {"status": "required", "requirement_refs": ["MAIN-1"]}},
                }
            ]
        },
    }

    def _setup(self, root: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
        target = root / "target"
        write_web_shell(target)
        landscape = {
            **LANDSCAPE,
            "artifact_digest": "sha256:repo",
            "scenario_matches": [{"scenario_id": "SC-1", "surface": "ui", "candidate_anchor_refs": ["RLA-shell"]}],
        }
        binding = {
            "requirement_digest": "sha256:req",
            "repo_landscape_digest": "sha256:repo",
            "workspace_ref": "run_workspace_v1",
            "base_sha": "abc123",
            "designer_uiux_mode": "enforce",
            "designer_uiux_contract_version": UIUX_CONTRACT_VERSION,
            "designer_ui_contract_version": UI_EXPERIENCE_CONTRACT_V2,
            "ui_repo_context": build_ui_repo_context(
                target_path=target, landscape=landscape, project_profile_id="typescript_fullstack_v1"
            ),
            "requirement_reference_ids": ["MAIN-1", "PRE-1"],
            UI_LAYOUT_CONTRACT_KEY: UI_LAYERED_LAYOUT_V1,
        }
        return target, landscape, binding

    def test_fake_designer_publishes_entrypoint_bound_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, landscape, binding = self._setup(root)
            settings = load_settings(
                root=root,
                runtime_root=root / ".runtime",
                overrides={"SDLC_LLM_MODE": "fake", "SDLC_UIUX_SKILL_MODE": "enforce"},
            )
            result = DesignerToolLoopExecutor(
                settings=settings,
                store=RuntimeStore(repo_root=root, runtime_root=root / ".runtime"),
                llm_client=None,
            ).execute(
                run_id="run-ui-v2",
                requirement_model=self.REQUIREMENT,
                landscape=landscape,
                target_path=target,
                snapshot_binding=binding,
                contract_version=GRAPH_VERSION_V2,
                uiux_mode="enforce",
                uiux_contract_version=UIUX_CONTRACT_VERSION,
            )

        ui_node = next(node for node in result.graph["nodes"] if node["surface"] == "ui")
        surface = ui_node["surface_contract"]["runtime_surface"]
        self.assertEqual(surface["composition_path"], [ENTRY, SHELL])
        self.assertEqual({item["name"] for item in surface["required_inputs"]}, {"title", "health"})
        self.assertEqual(surface["runnable_status"], "bound")
        self.assertEqual(ui_node["surface_contract"]["experience_contract"]["schema_version"], UI_EXPERIENCE_CONTRACT_V2)
        self.assertEqual(ui_node["runtime_binding"]["attachment"]["owner_ref"], "RLA-root")
        experience = ui_node["surface_contract"]["experience_contract"]
        self.assertEqual(experience["screen"]["screen_id"], surface["screen_id"])
        self.assertEqual(experience["visual_contract"]["style_target_policy"], "new_stylesheet")
        self.assertTrue(all(item["layer"] in {"page", "overlay"} for item in experience["visual_contract"]["layout"]))
        markdown = render_design_projections(result.graph)["design_markdown"]
        self.assertIn("## UI Runtime Surfaces", markdown)
        self.assertIn("Root → AppShell", markdown)
        feature = render_ui_feature_experience(result.graph)
        assert feature is not None
        self.assertIn("Wireframe (mobile)", str(feature["markdown"]))

    def test_model_prompt_scaffolds_runtime_surface_from_ui_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, landscape, binding = self._setup(Path(directory))
        graph = new_vertical_design_graph(run_id="run-ui-v2", snapshot_binding=binding, contract_version=GRAPH_VERSION_V2)
        partition = _active_partition(requirement_model=self.REQUIREMENT, landscape=landscape, graph=graph)
        assert partition is not None
        node = next(op["node"] for op in partition["delta_scaffold"]["operations"] if op["op"] == "add_node")
        scaffold = node["surface_contract"]["runtime_surface"]
        self.assertEqual(scaffold["entrypoint"], ENTRY)
        self.assertEqual(scaffold["composition_path"][0], ENTRY)
        self.assertEqual(node["surface_contract"]["experience_contract"]["schema_version"], UI_EXPERIENCE_CONTRACT_V2)

        prompt = _designer_prompt(
            requirement_model=self.REQUIREMENT,
            landscape=landscape,
            graph=graph,
            observations=[],
            repair_feedback=[],
            active_partition=partition,
            repair_task=None,
            observations_since_delta=0,
            last_invalid_action="",
            uiux_mode="enforce",
        )
        payload = json.loads(prompt[prompt.index('{"active_partition"'):])
        self.assertEqual(payload["ui_runtime_context"]["entrypoint"], ENTRY)
        self.assertEqual(
            payload["ui_runtime_context"]["dependency_refs"],
            [{"id": "PRE-1", "text": "Records come from search."}],
        )
        self.assertIn("styles", payload["ui_runtime_context"])
        self.assertIn("layout as {region,layer,content}", prompt)
        layout = node["surface_contract"]["experience_contract"]["visual_contract"]["layout"]
        self.assertEqual(layout[0]["layer"], "<page|overlay>")
        self.assertIn("owned by <scenario id>", prompt)
        self.assertNotIn("ui_repo_context", payload["graph"]["snapshot_binding"])
        self.assertIn("UI contract v2", prompt)


def holistic_experience(screen_id: str, evidence_refs: list[str], **visual: object) -> dict[str, object]:
    return {
        "schema_version": UI_EXPERIENCE_CONTRACT_V2,
        "component_structure": ["List with a delete action per row."],
        "feedback": [{"trigger": "delete succeeds", "response": "Announce removal."}],
        "accessibility": ["Rows are keyboard reachable."],
        "responsive": ["Single column on small screens."],
        "focus_flow": ["Focus follows focus_policies."],
        "guidance_refs": ["uiux:sha256:x"],
        "screen": {"screen_id": screen_id, "purpose": "Delete a record.", "entry_condition": "Open the app."},
        "runtime_states": {
            "loading": {"presentation": "Busy indicator.", "trigger": "Records requested."},
            "ready": {"presentation": "Record list.", "trigger": "Records loaded."},
            "empty": {"presentation": "Explain there is nothing to delete.", "trigger": "No records."},
            "error": {"presentation": "Alert with retry.", "trigger": "Request failed."},
            "success": {"not_applicable": True, "reason": "Success is shown in the ready list."},
        },
        "visual_contract": {
            "hierarchy": ["Title, list, status."],
            "layout": ["header: title", "main: record list", "footer: status region"],
            "typography": ["h1 title."],
            "color_and_tokens": ["Semantic danger color for delete."],
            "focus_appearance": ["2px outline."],
            "responsive_rules": ["Stack below 600px."],
            "style_target_policy": "new_stylesheet",
            "style_target_reason": "No stylesheet exists.",
            **visual,
        },
        "focus_policies": [
            {
                "event": "delete succeeds",
                "source_removed": True,
                "removed_element": "deleted row",
                "primary_target": "next row",
                "fallback_target": "status region",
                "selection_strategy": "next row below, else previous, else status region",
                "announcement": {"target": "status region", "moves_focus": False},
            }
        ],
        "repo_evidence_refs": evidence_refs,
    }


class HolisticExperienceTest(_UiGraphFixture):
    def _view_with_experience(self, **visual: object) -> dict[str, object]:
        view = self._view()
        view["surface_contract"]["experience_contract"] = holistic_experience(
            "delete-screen", list(self.context["evidence_refs"]), **visual
        )
        return view

    def test_complete_holistic_contract_passes(self) -> None:
        categories = self._categories([*self._wiring_nodes(), self._view_with_experience()])
        self.assertFalse({c for c in categories if c.startswith("ui_")}, categories)

    def test_structural_rules_reject_incomplete_decisions(self) -> None:
        cases = {
            "runtime_states.empty": lambda exp: exp["runtime_states"].update({"empty": {"not_applicable": True}}),
            "removed element": lambda exp: exp["focus_policies"][0].update({"primary_target": "deleted row"}),
            "screen_id": lambda exp: exp["screen"].update({"screen_id": "other-screen"}),
            "style_target_policy": lambda exp: exp["visual_contract"].update({"style_target_policy": "tailwind"}),
        }
        for expected, mutate in cases.items():
            view = self._view_with_experience()
            mutate(view["surface_contract"]["experience_contract"])
            with self.subTest(expected), self.assertRaises(HarnessError) as raised:
                self._graph([view])
            self.assertIn(expected, raised.exception.message)

    def test_cross_node_rules(self) -> None:
        reuse = self._categories([*self._wiring_nodes(), self._view_with_experience(style_target_policy="reuse_existing")])
        self.assertIn("ui_style_policy_unsupported", reuse)

        view = self._view_with_experience()
        view["surface_contract"]["experience_contract"]["repo_evidence_refs"] = ["repo:elsewhere#L1@sha256:x"]
        self.assertIn("ui_visual_evidence_missing", self._categories([*self._wiring_nodes(), view]))

        shell, root = self._wiring_nodes()[1], self._wiring_nodes()[0]
        for node in (shell, root):
            node["surface_contract"]["runtime_surface"].pop("role", None)
            node["surface_contract"]["runtime_surface"].pop("forwarded_inputs", None)
        shell["surface_contract"]["runtime_surface"]["screen_id"] = "delete-screen"
        root["surface_contract"]["runtime_surface"]["screen_id"] = "delete-screen"
        view = self._view_with_experience()
        view["surface_contract"]["runtime_surface"]["composition_path"] = [ENTRY, "apps/web/src/Banner.tsx#Banner", VIEW_REF]
        view["runtime_binding"]["attachment"].update(
            {"owner_path": "apps/web/src/Banner.tsx", "owner_symbol": "Banner"}
        )
        # A screen rooted at the app root contains every descendant; sibling branches without that root do not.
        self.assertNotIn("ui_screen_inconsistent", self._categories([root, shell, view]))
        self.assertIn("ui_screen_inconsistent", self._categories([shell, view]))

    def test_feature_projection_renders_screen_spec_and_wireframe(self) -> None:
        graph = self._graph([*self._wiring_nodes(), self._view_with_experience()])
        first = render_ui_feature_experience(graph)
        second = render_ui_feature_experience(graph)
        assert first is not None and second is not None
        self.assertEqual(first, second)
        screen = next(item for item in first["json"]["screens"] if item["screen_id"] == "delete-screen")
        self.assertEqual(screen["runnable_status"], "externally_blocked")
        items = next(entry for entry in screen["required_inputs"] if entry["name"] == "items")
        self.assertEqual(items["binding"], "PRE-1")
        markdown = str(first["markdown"])
        for expected in ("## Screen `delete-screen`", "Wireframe (desktop)", "| MAIN: record list", "| empty |", "next row below"):
            self.assertIn(expected, markdown)

    def test_screen_states_merge_across_scenario_owned_nodes(self) -> None:
        owner = self._view_with_experience()
        other = self._view_with_experience()
        other["id"] = "VDN-4"
        other["scenario_id"] = "SC-1"
        other["surface_contract"]["experience_contract"]["runtime_states"]["success"] = {
            "presentation": "Announce deletion.", "trigger": "Deletion confirmed."
        }
        owner["surface_contract"]["experience_contract"]["runtime_states"]["success"] = {
            "not_applicable": True, "reason": "owned by SC-3"
        }
        feature = render_ui_feature_experience(self._graph([*self._wiring_nodes(), owner, other]))
        assert feature is not None
        screen = next(item for item in feature["json"]["screens"] if item["screen_id"] == "delete-screen")
        self.assertEqual([state["presentation"] for state in screen["runtime_states"]["success"]], ["Announce deletion."])

    def test_screen_projection_merges_every_scenario_node(self) -> None:
        main, cancel = self._view_with_experience(), self._view_with_experience()
        main["scenario_id"] = "SC-1"
        cancel.update({"id": "VDN-4", "scenario_id": "SC-2"})
        main_exp = main["surface_contract"]["experience_contract"]
        cancel_exp = cancel["surface_contract"]["experience_contract"]
        main_exp["component_structure"] = ["Dialog: confirms the deletion"]
        cancel_exp["component_structure"] = ["Dialog (shared): offers cancel", "RetryAction: re-submits"]
        cancel_exp["runtime_states"]["loading"] = {"presentation": "Dialog busy.", "trigger": "Cancel pressed."}
        cancel_exp["visual_contract"]["layout"] = [
            "main: record list; selection unchanged",
            "dialog layer: confirm and cancel actions",
            "alert region: interruption notice with retry",
        ]
        feature = render_ui_feature_experience(self._graph([*self._wiring_nodes(), cancel, main]))
        assert feature is not None
        screen = next(item for item in feature["json"]["screens"] if item["screen_id"] == "delete-screen")

        loading = screen["runtime_states"]["loading"]
        self.assertEqual([(s["scenario_ids"], s["presentation"]) for s in loading], [(["SC-1"], "Busy indicator."), (["SC-2"], "Dialog busy.")])
        self.assertEqual(screen["runtime_states"]["ready"][0]["scenario_ids"], ["SC-1", "SC-2"])
        self.assertEqual(
            [(item["region"], item["layer"], item["content"]) for item in screen["visual_contract"]["layout"]],
            [
                ("header", "page", "title"),
                ("main", "page", "record list; selection unchanged"),
                ("footer", "page", "status region"),
                ("dialog layer", "overlay", "confirm and cancel actions"),
                ("alert region", "page", "interruption notice with retry"),
            ],
        )
        self.assertEqual(len(screen["composition_paths"]), 1)
        dialog = next(item for item in screen["components"] if item["name"] == "Dialog")
        self.assertEqual([r["scenario_id"] for r in dialog["responsibilities"]], ["SC-1", "SC-2"])

        markdown = str(feature["markdown"])
        self.assertEqual(markdown.count("- **Dialog**"), 1)
        self.assertIn("With overlay open", markdown)
        self.assertIn("ALERT REGION: interruption notice with retry", markdown)
        desktop = markdown.split("### Wireframe (desktop)")[1].split("### Wireframe (mobile)")[0]
        stacked, overlaid = desktop.split("With overlay open")
        self.assertNotIn("DIALOG LAYER", stacked)
        self.assertIn("| MAIN :::", overlaid)
        self.assertTrue(all(len(line) == 64 for line in desktop.splitlines() if line.startswith(("|", "+"))))

    @staticmethod
    def _layered(view: dict[str, object], *layout: tuple[str, str]) -> dict[str, object]:
        view["surface_contract"]["experience_contract"]["visual_contract"]["layout"] = [
            {"region": region, "layer": layer, "content": f"{region} content"} for region, layer in layout
        ]
        return view

    def test_layered_layout_is_required_only_for_pinned_graphs(self) -> None:
        legacy = self._view_with_experience()
        self._graph([*self._wiring_nodes(), legacy])
        with self.assertRaises(HarnessError) as raised:
            self._graph([*self._wiring_nodes(), self._view_with_experience()], layered=True)
        self.assertIn("{region,layer,content}", raised.exception.message)
        with self.assertRaises(HarnessError):
            self._graph([*self._wiring_nodes(), self._layered(self._view_with_experience(), ("main", "floating"))], layered=True)
        self._graph([*self._wiring_nodes(), self._layered(self._view_with_experience(), ("main", "page"))], layered=True)

    def test_screen_nodes_must_agree_on_region_layer_and_style(self) -> None:
        main = self._layered(self._view_with_experience(), ("main", "page"), ("notice", "overlay"))
        main["scenario_id"] = "SC-1"
        other = self._layered(
            self._view_with_experience(style_target_policy="reuse_existing"), ("main", "page"), ("Notice", "page")
        )
        other.update({"id": "VDN-4", "scenario_id": "SC-3"})
        nodes = [*self._wiring_nodes(), main, other]
        findings = design_bundle_findings(self._graph(nodes, layered=True), landscape=LANDSCAPE)
        layout = [item for item in findings if item["category"] == "ui_screen_layout_inconsistent"]
        self.assertEqual([item["subject_ref"] for item in layout], ["delete-screen:VDN-4:notice"])
        self.assertIn("layer overlay", layout[0]["expected_change"])
        self.assertIn("ui_screen_style_inconsistent", {item["category"] for item in findings})
        legacy = {str(item["category"]) for item in design_bundle_findings(self._graph(nodes), landscape=LANDSCAPE)}
        self.assertFalse(legacy & {"ui_screen_layout_inconsistent", "ui_screen_style_inconsistent"})

    def test_declared_overlay_layer_drives_the_wireframe(self) -> None:
        view = self._layered(self._view_with_experience(), ("main", "page"), ("retry notice", "overlay"))
        feature = render_ui_feature_experience(self._graph([*self._wiring_nodes(), view], layered=True))
        assert feature is not None
        stacked, overlaid = str(feature["markdown"]).split("### Wireframe (desktop)")[1].split("With overlay open")[:2]
        self.assertNotIn("RETRY NOTICE", stacked)
        self.assertIn("RETRY NOTICE: retry notice content", overlaid)
        self.assertIn("- `overlay` retry notice: retry notice content", str(feature["markdown"]))

    def test_semantic_view_and_guidance_key_are_v2_only(self) -> None:
        graph = self._graph([self._view()])
        self.assertIn("ui_repo_context", _vertical_design_review_view(graph))
        self.assertIn("ui_journey_coherence", _holistic_ui_review_instruction(graph))
        self.assertEqual(
            _guidance_key(graph=graph, requirement_model={"use_case_id": "UC1"}, partition_key="SC-1|web"),
            "ui-feature|UC1",
        )
        historical = json.loads(json.dumps(graph))
        historical["snapshot_binding"].pop("designer_ui_contract_version")
        self.assertNotIn("ui_repo_context", _vertical_design_review_view(historical))
        self.assertEqual(_holistic_ui_review_instruction(historical), "")
        self.assertIsNone(render_ui_feature_experience(historical))
        self.assertEqual(
            _guidance_key(graph=historical, requirement_model={"use_case_id": "UC1"}, partition_key="SC-1|web"),
            "SC-1|web",
        )


class OutcomeContextSliceTest(unittest.TestCase):
    REQUIREMENT = {
        "use_case": {"id": "UC1"},
        "preconditions": [
            {
                "id": "PRE-1",
                "text": "The agency has logged in.",
                "status": "approved",
                "evidence_refs": ["SRC-1"],
                "open_question_refs": [],
            }
        ],
        "postconditions": [{"id": "POST-1", "text": "The system notifies the successful deletion."}],
        "acceptance_criteria": [
            {"id": "AC-1", "text": "Confirming deletes the record and notifies success."},
            {"id": "AC-4", "text": "Cancellation blocks input."},
        ],
        "flows": {"main": [{"id": "MF-1", "action": "Select a record."}, {"id": "MF-4", "action": "Delete it."}]},
        "scenario_contract": {
            "scenarios": [
                {
                    "id": "SC-1",
                    "kind": "main",
                    "requirement_refs": ["MF-1", "MF-4"],
                    "acceptance_refs": ["AC-1", "AC-4"],
                    "surfaces": {
                        "ui": {"status": "required", "requirement_refs": ["MF-1", "AC-4"]},
                        "api": {"status": "required", "requirement_refs": ["MF-4", "AC-1"]},
                    },
                },
                {
                    "id": "SC-3",
                    "kind": "exception",
                    "requirement_refs": ["EF-1"],
                    "acceptance_refs": [],
                    "surfaces": {"ui": {"status": "required", "requirement_refs": ["EF-1"]}},
                },
            ]
        },
    }

    def _slice(self, surface: str, *, v2: bool, scenario_id: str = "SC-1") -> dict[str, object]:
        return _active_requirement_slice(
            requirement_model=self.REQUIREMENT,
            active_partition={
                "partition": {"scenario_id": scenario_id, "component_id": "web"},
                "surface_work": [{"surface": surface, "requirement_refs": ["MF-1", "AC-4"]}],
            },
            include_outcome_context=v2,
        )

    def test_ui_slice_gets_presentation_only_outcome_context_under_v2(self) -> None:
        context = self._slice("ui", v2=True)["outcome_context"]
        self.assertEqual([item["id"] for item in context], ["AC-1", "POST-1"])
        self.assertEqual(self._slice("ui", v2=True)["allowed_requirement_refs"], ["MF-1", "AC-4"])
        self.assertNotIn("outcome_context", self._slice("ui", v2=False))
        self.assertNotIn("outcome_context", self._slice("api", v2=True))

    def test_branch_scenario_does_not_inherit_main_success_postcondition(self) -> None:
        # An exception that must not notify success would otherwise be told POST-1 is its final result.
        self.assertNotIn("outcome_context", self._slice("ui", v2=True, scenario_id="SC-3"))

    def test_approved_preconditions_are_cross_cutting_facts_not_behavior_scope(self) -> None:
        active_slice = self._slice("api", v2=True)

        self.assertEqual(active_slice["allowed_requirement_refs"], ["MF-1", "AC-4"])
        self.assertNotIn("prohibited_effects", active_slice)
        self.assertEqual(
            active_slice["cross_cutting_conditions"],
            [
                {
                    "id": "PRE-1",
                    "text": "The agency has logged in.",
                    "status": "approved",
                    "evidence_refs": ["SRC-1"],
                    "open_question_refs": [],
                }
            ],
        )

class BackendConstructionContextTest(unittest.TestCase):
    def test_backend_view_keeps_topology_but_strips_unrelated_ui_experience(self) -> None:
        graph = {
            "nodes": [
                {
                    "id": "VDN-ui",
                    "surface": "ui",
                    "surface_contract": {
                        "surface": "ui",
                        "entrypoint": "DeleteView",
                        "experience_contract": {"wireframe": "large UI payload"},
                    },
                }
            ],
            "edges": [{"id": "VDE-ui", "from": "VDN-root", "to": "VDN-ui", "kind": "calls"}],
        }

        view = _construction_graph_view(
            graph,
            active_partition={"surface_work": [{"surface": "api"}]},
        )

        self.assertNotIn("experience_contract", view["nodes"][0]["surface_contract"])
        self.assertEqual(view["edges"], graph["edges"])
        self.assertIn("experience_contract", graph["nodes"][0]["surface_contract"])


class WiringTemplateTest(_UiGraphFixture):
    # Real landscapes only anchor exported symbols, so the app Root has no RLA anchor.
    REAL_LANDSCAPE = {
        "components": [{"component_id": "web", "roots": ["apps/web/src"]}],
        "anchors": [
            {"anchor_id": "RLA-main", "kind": "ui_entrypoint", "component_id": "web", "path": "apps/web/src/main.tsx", "symbol": "main"},
            {"anchor_id": "RLA-shell", "kind": "ui_component", "component_id": "web", "path": "apps/web/src/AppShell.tsx", "symbol": "AppShell"},
        ],
    }

    def test_templates_wire_every_mount_hop_and_satisfy_composition_gates(self) -> None:
        graph = self._graph([])
        templates = _wiring_operation_templates(
            graph,
            landscape=self.REAL_LANDSCAPE,
            scenario_id="SC-1",
            component_id="web",
            owner_anchors=[{"path": "apps/web/src/AppShell.tsx", "symbol": "AppShell"}],
            requirement_refs=["AC-1"],
        )
        targets = [op["node"]["runtime_binding"]["target"]["symbol"] for op in templates]
        self.assertEqual(targets, ["Root", "AppShell"])
        root, shell = (op["node"] for op in templates)
        self.assertEqual(root["runtime_binding"]["runtime_kind"], "entrypoint")
        self.assertEqual(shell["runtime_binding"]["attachment"]["owner_ref"], ENTRY)
        self.assertEqual(
            [item["name"] for item in shell["surface_contract"]["runtime_surface"]["required_inputs"]],
            ["title", "health"],
        )

        nodes = []
        for index, node in enumerate((root, shell), start=1):
            nodes.append({**{k: v for k, v in node.items() if k != "temp_ref"}, "id": f"VDN-{index}", "scenario_id": "SC-1", "component_id": "web"})
        view = self._view()
        view["surface_contract"]["experience_contract"] = holistic_experience(
            "delete-screen", list(self.context["evidence_refs"])
        )
        graph = canonicalize_composition_wiring(self._graph([*nodes, view]))
        categories = {str(item["category"]) for item in design_bundle_findings(graph, landscape=self.REAL_LANDSCAPE)}
        self.assertFalse(
            {c for c in categories if c.startswith("ui_") or c in {"composition_hop_unmodeled", "runtime_attachment_missing"}},
            categories,
        )
        enforced = json.loads(json.dumps(graph))
        enforced["snapshot_binding"]["designer_uiux_mode"] = "enforce"
        enforced["artifact_digest"] = graph_digest(enforced)
        codes = {str(item.get("code")) for item in completion_findings(enforced, required_surfaces={"SC-1": {"ui"}}, landscape=self.REAL_LANDSCAPE)}
        self.assertNotIn("ui_experience_contract_missing", codes)
        feature = render_ui_feature_experience(graph)
        assert feature is not None
        self.assertEqual([screen["screen_id"] for screen in feature["json"]["screens"]], ["delete-screen"])

    def test_existing_target_is_not_templated_again(self) -> None:
        templates = _wiring_operation_templates(
            self._graph(self._wiring_nodes()),
            landscape=self.REAL_LANDSCAPE,
            scenario_id="SC-2",
            component_id="web",
            owner_anchors=[{"path": "apps/web/src/AppShell.tsx", "symbol": "AppShell"}],
            requirement_refs=["AC-1"],
        )
        self.assertEqual(templates, [])

    def test_harness_materializes_wiring_for_screen_inputs(self) -> None:
        graph = self._graph([])
        templates = _wiring_operation_templates(
            graph,
            landscape=self.REAL_LANDSCAPE,
            scenario_id="SC-1",
            component_id="web",
            owner_anchors=[{"path": "apps/web/src/AppShell.tsx", "symbol": "AppShell"}],
            requirement_refs=["AC-1"],
        )
        partition = {
            "partition": {"scenario_id": "SC-1", "component_id": "web"},
            "harness_wiring_operations": templates,
        }

        def delta(*ops: dict[str, object]) -> dict[str, object]:
            return {"delta": {"operations": [{"op": "set_partition_coverage", "coverage": []}, *ops]}}

        def wiring(arguments: object) -> dict[str, dict[str, object]]:
            return {
                op["node"]["runtime_binding"]["target"]["symbol"]: op["node"]
                for op in arguments["delta"]["operations"]  # type: ignore[index]
                if op.get("op") == "add_node" and op["node"]["surface_contract"]["runtime_surface"].get("role") == "composition_wiring"
            }

        delta_view = self._view()
        delta_view.pop("id")
        delta_view["temp_ref"] = "tmp:SC-1:ui"
        view_op = {"op": "add_node", "node": delta_view}
        edge_op = {"op": "add_edge", "edge": {"kind": "contains", "from": "tmp:SC-1:wire:AppShell", "to": "tmp:SC-1:ui"}}
        result = _materialize_delta_wiring(arguments=delta(view_op, edge_op), active_partition=partition, graph=graph, run_id="r")
        ops = result["delta"]["operations"]
        self.assertEqual(
            [op["op"] for op in ops],
            ["set_partition_coverage", "add_node", "add_node", "add_node", "add_edge", "add_edge"],
        )
        self.assertEqual(
            ops[-2]["edge"],
            {
                "from": "tmp:SC-1:wire:Root",
                "to": "tmp:SC-1:wire:AppShell",
                "kind": "calls",
                "label": "Root forwards runtime inputs to AppShell",
                "scenario_id": "SC-1",
                "terminal": False,
            },
        )
        wired = wiring(result)
        self.assertEqual(sorted(wired), ["AppShell", "Root"])
        self.assertNotIn("<input names>", json.dumps(result))
        self.assertIn("next component receives items", json.dumps(wired["Root"]))
        shell_surface = wired["AppShell"]["surface_contract"]["runtime_surface"]
        self.assertEqual([item["name"] for item in shell_surface["required_inputs"]], ["title", "health"])
        self.assertEqual(shell_surface["runnable_status"], "bound")

        # A drifted Designer-written wiring node (run r6) is replaced by the template, keeping its temp_ref.
        drifted = json.loads(json.dumps(templates[1]["node"]))
        drifted["temp_ref"] = "tmp:custom-shell"
        drifted["surface_contract"]["runtime_surface"]["required_inputs"].append(self._items_input())
        drifted["surface_contract"]["runtime_surface"]["runnable_status"] = "externally_blocked"
        result = _materialize_delta_wiring(
            arguments=delta({"op": "add_node", "node": drifted}, view_op), active_partition=partition, graph=graph, run_id="r"
        )
        shell = wiring(result)["AppShell"]
        self.assertEqual(shell["temp_ref"], "tmp:custom-shell")
        self.assertEqual([item["name"] for item in shell["surface_contract"]["runtime_surface"]["required_inputs"]], ["title", "health"])
        self.assertEqual(shell["surface_contract"]["runtime_surface"]["runnable_status"], "bound")
        self.assertEqual(len(wiring(result)), 2)

        # An external input the Designer bound to the screen itself (run r7) enters at the entrypoint instead.
        self_bound = self._view(inputs=[self._items_input(provided_by=VIEW_REF)])
        result = _materialize_delta_wiring(
            arguments=delta({"op": "add_node", "node": self_bound}), active_partition=partition, graph=graph, run_id="r"
        )
        screen = result["delta"]["operations"][-1]["node"]["surface_contract"]["runtime_surface"]
        self.assertEqual(screen["required_inputs"][0]["provided_by"], ENTRY)
        self.assertEqual(sorted(wiring(result)), ["AppShell", "Root"])
        self.assertEqual(self_bound["surface_contract"]["runtime_surface"]["required_inputs"][0]["provided_by"], VIEW_REF)
        # An intermediate hop is not a valid external provider either: Root must also forward the input.
        mid_bound = self._view(inputs=[self._items_input(provided_by=SHELL)])
        result = _materialize_delta_wiring(
            arguments=delta({"op": "add_node", "node": mid_bound}), active_partition=partition, graph=graph, run_id="r"
        )
        screen = result["delta"]["operations"][-1]["node"]["surface_contract"]["runtime_surface"]
        self.assertEqual(screen["required_inputs"][0]["provided_by"], ENTRY)
        self.assertEqual(sorted(wiring(result)), ["AppShell", "Root"])
        # Other sources are not guessed; graph validation keeps reporting them.
        local_view = self._view(inputs=[self._items_input(source="existing_runtime", provided_by=VIEW_REF)])
        local = delta({"op": "add_node", "node": local_view})
        self.assertIs(_materialize_delta_wiring(arguments=local, active_partition=partition, graph=graph, run_id="r"), local)

        # Hops modeled by an earlier partition need no wiring; a hop without a template is rejected.
        unchanged = delta(view_op)
        rematerialized = _materialize_delta_wiring(
            arguments=unchanged,
            active_partition={},
            graph=self._graph(self._wiring_nodes()),
            run_id="r",
        )
        self.assertFalse(
            any(
                op.get("op") == "add_node"
                and op["node"]["surface_contract"]["runtime_surface"].get("role") == "composition_wiring"
                for op in rematerialized["delta"]["operations"]
            )
        )
        with self.assertRaises(HarnessError) as raised:
            _materialize_delta_wiring(arguments=delta(view_op), active_partition={}, graph=graph, run_id="r")
        self.assertEqual(raised.exception.error_code, "designer_delta_invalid")
        self.assertIn(f"{ENTRY} does not pass items", raised.exception.message)

    def test_harness_reconciles_missing_edge_between_existing_wiring_hops(self) -> None:
        wiring_nodes = self._wiring_nodes()
        for node in wiring_nodes:
            node["surface_contract"]["runtime_surface"]["role"] = "composition_wiring"
        graph = self._graph([*wiring_nodes, self._view()])
        graph["edges"] = [
            {
                "id": "VDE-shell-screen",
                "from": "VDN-2",
                "to": "VDN-3",
                "kind": "calls",
                "label": "AppShell mounts screen",
                "scenario_id": "SC-1",
                "terminal": True,
            }
        ]
        arguments = {"delta": {"operations": [{"op": "set_partition_coverage", "coverage": {"api": "covered"}}]}}

        result = _materialize_delta_wiring(
            arguments=arguments,
            active_partition={"partition": {"scenario_id": "SC-1"}},
            graph=graph,
            run_id="r",
        )

        self.assertEqual(
            result["delta"]["operations"][-1]["edge"],
            {
                "from": "VDN-1",
                "to": "VDN-2",
                "kind": "calls",
                "label": "Root forwards runtime inputs to AppShell",
                "scenario_id": "SC-1",
                "terminal": False,
            },
        )

    def test_parser_canonicalizes_external_inputs_before_step_review(self) -> None:
        view = self._view(inputs=[self._items_input(provided_by=VIEW_REF, requirement_refs=[])])
        raw = json.dumps(
            {
                "action": "design.propose_delta",
                "arguments": {"delta": {"operations": [{"op": "add_node", "node": view}]}},
            }
        )

        action, normalization = _parse_designer_tool_action(raw, require_runtime_binding=True)

        runtime_input = action["arguments"]["delta"]["operations"][0]["node"]["surface_contract"][
            "runtime_surface"
        ]["required_inputs"][0]
        self.assertEqual(runtime_input["provided_by"], ENTRY)
        self.assertEqual(runtime_input["requirement_refs"], ["PRE-1"])
        self.assertEqual(normalization["canonicalized_external_inputs"], ["items"])


class FeatureUxDecisionsTest(unittest.TestCase):
    def test_focus_policies_keep_their_scenario_and_wiring_is_not_a_screen(self) -> None:
        # run_20261102_061023 designer-0015: SC-3 copied SC-1's screen-level focus policy.
        def node(node_id: str, scenario_id: str, *, role: str = "screen", event: str = "") -> dict[str, object]:
            return {
                "id": node_id,
                "scenario_id": scenario_id,
                "surface": "ui",
                "name": node_id,
                "surface_contract": {
                    "runtime_surface": {"role": role, "screen_id": "manage" if role == "screen" else "sc-1-wiring"},
                    "experience_contract": {
                        "screen": {"screen_id": "manage"},
                        "focus_policies": [{"event": event, "primary_target": "Confirm"}] if event else [],
                    },
                },
            }

        graph = {
            "nodes": [
                node("VDN-wire", "SC-1", role="composition_wiring"),
                node("VDN-1", "SC-1", event="dialog opens"),
                node("VDN-2", "SC-2", event="dialog closes"),
            ]
        }
        decisions = _feature_ux_decisions(graph)
        self.assertEqual(list(decisions), ["manage"])
        screen = decisions["manage"]
        self.assertNotIn("focus_policies", screen)
        self.assertEqual(
            [(item["scenario_id"], item["event"]) for item in screen["scenario_focus_policies"]],
            [("SC-1", "dialog opens"), ("SC-2", "dialog closes")],
        )


class DesignerOutputNormalizationTest(unittest.TestCase):
    @staticmethod
    def _action(node: dict[str, object]) -> dict[str, object]:
        return {
            "action": "design.propose_delta",
            "arguments": {
                "delta": {
                    "schema_version": "designer_delta_v2",
                    "base_graph_digest": "sha256:x",
                    "partition": {"use_case_id": "UC1", "scenario_id": "SC-1", "component_id": "web"},
                    "completion_claims": [{"scenario_id": "SC-1", "surfaces": ["ui"]}],
                    "unresolved_items": [],
                    "operations": [{"op": "add_node", "node": node}],
                }
            },
        }

    NODE = {
        "temp_ref": "tmp:SC-1:ui",
        "surface_contract": {"schema_version": "design_surface_contract_v1", "experience_contract": {"screen": {}}},
        "runtime_binding": {
            "schema_version": "runtime_binding_v1",
            "runtime_kind": "ui_component",
            "target": {"component_id": "web", "path": "apps/web/src/View.tsx", "symbol": "View"},
            "attachment": {"relation": "rendered_by", "owner_ref": "RLA-1", "owner_path": "a.tsx", "owner_symbol": "A"},
            "evidence_refs": [],
        },
    }

    def test_operations_array_closed_with_brace_is_repaired(self) -> None:
        raw = json.dumps(self._action(json.loads(json.dumps(self.NODE))), separators=(",", ":"))
        # Model closes `operations` with '}' and stops: the last closers are missing or wrong.
        broken = raw[: raw.rindex("]")] + "}}}"
        with self.assertRaises(json.JSONDecodeError):
            # v1 runs and length-truncated outputs never get bracket repair.
            _parse_designer_tool_action(broken, require_runtime_binding=True)
        payload, normalization = _parse_designer_tool_action(broken, require_runtime_binding=True, repair_closers=True)
        self.assertEqual(normalization["kind"], "closed_unbalanced_brackets")
        self.assertEqual(payload["arguments"]["delta"]["operations"][0]["node"]["temp_ref"], "tmp:SC-1:ui")

    def test_object_closed_with_bracket_is_repaired(self) -> None:
        raw = json.dumps(self._action(json.loads(json.dumps(self.NODE))), separators=(",", ":"))
        # Model forgets the add_node operation's closing brace before closing `operations`.
        broken = raw.replace("}}]", "}]", 1)
        self.assertNotEqual(raw, broken)
        payload, normalization = _parse_designer_tool_action(broken, require_runtime_binding=True, repair_closers=True)
        self.assertEqual(normalization["kind"], "closed_unbalanced_brackets")
        self.assertEqual(payload, json.loads(raw))

    def test_stray_closers_are_removed_where_the_parser_stops(self) -> None:
        action = self._action(json.loads(json.dumps(self.NODE)))
        action["arguments"]["delta"]["operations"].insert(0, {"op": "set_partition_coverage", "coverage": {"ui": "covered"}})
        raw = json.dumps(action, separators=(",", ":"))
        first_op = '{"op":"set_partition_coverage","coverage":{"ui":"covered"}}'
        # Real DeepSeek slip: an extra brace between two operations, plus one at the very end.
        broken = raw.replace(first_op, first_op + "}", 1) + "}"
        with self.assertRaises(json.JSONDecodeError):
            _parse_designer_tool_action(broken, require_runtime_binding=True)
        payload, normalization = _parse_designer_tool_action(broken, require_runtime_binding=True, repair_closers=True)
        self.assertEqual(normalization["kind"], "removed_redundant_closers")
        self.assertEqual(payload, json.loads(raw))

    def test_runtime_surface_under_experience_is_relocated(self) -> None:
        node = json.loads(json.dumps(self.NODE))
        node["surface_contract"]["experience_contract"]["runtime_surface"] = {"schema_version": RUNTIME_SURFACE_BINDING_VERSION}
        payload, normalization = _parse_designer_tool_action(json.dumps(self._action(node)), require_runtime_binding=True)
        contract = payload["arguments"]["delta"]["operations"][0]["node"]["surface_contract"]
        self.assertIn("runtime_surface", contract)
        self.assertNotIn("runtime_surface", contract["experience_contract"])
        self.assertEqual(normalization["relocated_runtime_surface"], ["tmp:SC-1:ui"])

    def test_unrepairable_json_names_the_misclosed_array(self) -> None:
        raw = '{"action":"design.propose_delta","arguments":{"delta":{"operations":[{"op":"add_node"}},"completion_claims":[]}}}'
        with self.assertRaises(json.JSONDecodeError):
            _parse_designer_tool_action(raw, require_runtime_binding=True)
        self.assertIn("'operations' array was closed", _json_structure_hint(raw))


def _populate_with_web_shell(target: Path) -> None:
    populate_typescript_target(target)
    write_web_shell(target, with_view=False)


class UiContractWorkflowPinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = GitRepositoryTemplate(_populate_with_web_shell)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.template.close()

    def _settings(self, temp_path: Path, **overrides: str):
        target = self.template.materialize(temp_path / "target")
        return load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=temp_path / ".runtime",
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_VERTICAL_TRACE_MODE": "shadow",
                "SDLC_DESIGNER_CONTRACT_VERSION": GRAPH_VERSION_V2,
                "SDLC_UIUX_SKILL_MODE": "enforce",
                "SDLC_DESIGNER_UI_CONTRACT_VERSION": UI_EXPERIENCE_CONTRACT_V2,
                "SDLC_DESIGN_LLM_CALL_BUDGET": "24",
                "SDLC_TARGET_REPO_PATH": str(target.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(target.remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_TARGET_WORKTREE_ROOT": str(temp_path / "worktrees"),
                **overrides,
            },
        )

    def test_v2_pin_freezes_ui_context_and_revalidates_without_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
            run_id = str(created["run_id"])
            store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
            self.assertEqual(store.read_state(run_id)[UI_CONTRACT_STATE_KEY], UI_EXPERIENCE_CONTRACT_V2)

            designed = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)
            self.assertEqual(designed["status"], "waiting_design_approval", designed)
            graph = json.loads(
                (settings.repo_root / str(designed["latest_artifacts"]["vertical_design_graph"])).read_text(encoding="utf-8")
            )
            binding = graph["snapshot_binding"]
            self.assertEqual(binding["designer_ui_contract_version"], UI_EXPERIENCE_CONTRACT_V2)
            self.assertEqual(binding["ui_repo_context"]["entrypoint"]["ref"], ENTRY)
            self.assertTrue(binding["requirement_reference_ids"])
            self.assertEqual(binding[UI_LAYOUT_CONTRACT_KEY], UI_LAYERED_LAYOUT_V1)

            revalidated = PhaseTwoWorkflow(settings).revalidate_design(run_id=run_id)
            self.assertEqual(revalidated["status"], "waiting_design_approval", revalidated)

    def test_run_pinned_before_layered_layout_keeps_legacy_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            run_id = str(
                PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")["run_id"]
            )
            store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
            state = store.read_state(run_id)
            state.pop(UI_LAYOUT_CONTRACT_KEY)
            store.write_state(state)

            designed = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)
            self.assertEqual(designed["status"], "waiting_design_approval", designed)
            graph = json.loads(
                (settings.repo_root / str(designed["latest_artifacts"]["vertical_design_graph"])).read_text(encoding="utf-8")
            )
            self.assertNotIn(UI_LAYOUT_CONTRACT_KEY, graph["snapshot_binding"])
            revalidated = PhaseTwoWorkflow(settings).revalidate_design(run_id=run_id)
            self.assertEqual(revalidated["status"], "waiting_design_approval", revalidated)

    def test_v2_without_uiux_enforce_fails_closed_at_run_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = self._settings(Path(temp), SDLC_UIUX_SKILL_MODE="off")
            with self.assertRaises(HarnessError) as raised:
                PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        self.assertEqual(raised.exception.error_code, "designer_ui_contract_invalid")


if __name__ == "__main__":
    unittest.main()
