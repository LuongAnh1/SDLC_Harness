"""Browser Readiness scope and gates (L5.4.1B).

Fixtures use a vocabulary unrelated to the controlled run, so the derivation is shown to depend
on typed contract fields, the path policy and AST module facts only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sdlc_harness.agents.developer.browser_stage_contract import (
    MIN_DEMO_LITERAL_CHARS,
    assemble_ui_polish_contract,
    build_browser_repair_work_packet,
    build_ui_polish_work_packet,
    build_readiness_work_packet,
    demo_render_problems,
    derive_readiness_context,
    polish_contract_rule_issues,
    readiness_findings,
    readiness_scope_drift,
    resolve_module,
    static_import_problems,
)
from sdlc_harness.profiles import (
    browser_stage_facts,
    build_planning_path_policy,
    get_language_profile,
    get_project_profile,
)

FIXTURE = "apps/web/src/ledger-fixture.ts"
FIXTURE_TEST = "apps/web/src/ledger-fixture.test.ts"
ENTRY = "apps/web/src/main.tsx"
ENTRY_TEST = "apps/web/src/main.test.tsx"
VIEW = "apps/web/src/LedgerView.tsx"
LAZY = "apps/web/src/lazy.ts"
FILES = sorted([FIXTURE, FIXTURE_TEST, ENTRY, ENTRY_TEST, VIEW, LAZY])
CONTRACT = {
    "runtime_inputs": [
        {
            "input_id": "RI-A",
            "resolution": {
                "kind": "approved_demo_fixture",
                "provider": {"kind": "symbol", "ref": f"{FIXTURE}#LEDGER_ROWS"},
                "decision_ref": "CD-1",
            },
        },
        {
            # Demo data for the database seed is not part of the browser bundle.
            "input_id": "RI-B",
            "resolution": {
                "kind": "approved_demo_fixture",
                "provider": {"kind": "symbol", "ref": "packages/db/src/ledger-seed.ts#seedLedger"},
                "decision_ref": "CD-1",
            },
        },
        {
            "input_id": "RI-C",
            "resolution": {"kind": "existing_runtime", "provider": {"kind": "env_var", "env_key": "LEDGER_URL"}},
        },
    ],
    "demo_decisions": [{"id": "CD-1", "input_ids": ["RI-A", "RI-B"], "statement": "Demo rows.", "scope": "demo_and_verification_only"}],
    "runtime_probes": [],
    "verification_stand_ins": [],
    "new_write_targets": [FIXTURE, FIXTURE_TEST, "packages/db/src/ledger-seed.ts"],
}
CLOSURE_CONTEXT = {"plan_allowed_paths": [ENTRY, ENTRY_TEST, VIEW]}
MODULE_FACTS = {
    FIXTURE: {
        "string_literals": ["ledger-demo-account", "short", "Shared Production Label"],
        "module_references": [{"specifier": "./LedgerView", "kind": "type_only"}],
    },
    FIXTURE_TEST: {"string_literals": [], "module_references": [{"specifier": "./ledger-fixture", "kind": "static"}]},
    ENTRY: {"string_literals": ["root"], "module_references": [{"specifier": "./ledger-fixture", "kind": "static"}]},
    ENTRY_TEST: {
        "string_literals": [],
        "module_references": [{"specifier": "./ledger-fixture", "kind": "static"}, {"specifier": "./main", "kind": "static"}],
    },
    VIEW: {"string_literals": ["Shared Production Label"], "module_references": [{"specifier": "./ledger-fixture", "kind": "type_only"}]},
    LAZY: {"string_literals": [], "module_references": [{"specifier": "./ledger-fixture.js", "kind": "dynamic"}]},
}
TEXTS = {VIEW: 'export const label = "Shared Production Label";\n', ENTRY: "", LAZY: ""}


class BrowserReadinessScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        project = get_project_profile("typescript_fullstack_v1")
        self.policy = build_planning_path_policy(project=project, language=get_language_profile(project.language_profile_id))
        self.facts = browser_stage_facts(project.profile_id)

    def derive(self, *, contract=None, context=None, files=None, facts=None) -> dict[str, object]:
        return derive_readiness_context(
            run_id="run_ledger",
            version=1,
            closure_contract=contract or CONTRACT,
            closure_contract_ref="artifacts/runtime_closure_contract_v1.json",
            closure_context=context or CLOSURE_CONTEXT,
            planning_policy=self.policy,
            facts=self.facts,
            files=files or FILES,
            module_facts=facts or MODULE_FACTS,
            read_text=lambda path: TEXTS.get(path),
        )

    def test_scope_comes_from_typed_contract_fields_and_ast_facts(self) -> None:
        context = self.derive()

        self.assertEqual(context["status"], "required")
        self.assertEqual(
            context["demo_modules"],
            [{"path": FIXTURE, "symbols": ["LEDGER_ROWS"], "input_ids": ["RI-A"], "decision_refs": ["CD-1"]}],
        )
        self.assertEqual(context["static_importers"], [ENTRY])  # tests and type-only imports never ship
        self.assertEqual(context["sources"], sorted([FIXTURE, ENTRY, LAZY]))  # a dynamic importer may need the flag
        self.assertEqual(context["tests"], sorted([FIXTURE_TEST, ENTRY_TEST]))
        self.assertEqual(context["tests_requiring_update"], [ENTRY_TEST])
        self.assertEqual(context["write_scope"], sorted([FIXTURE, ENTRY, LAZY, FIXTURE_TEST, ENTRY_TEST]))
        self.assertEqual(context["demo_literals"], ["ledger-demo-account"])
        self.assertEqual(context["shared_literals"], [{"literal": "Shared Production Label", "files": [VIEW]}])
        self.assertEqual(context["short_literals"], ["short"])
        self.assertEqual(MIN_DEMO_LITERAL_CHARS, 8)
        self.assertEqual(context, self.derive())  # deterministic

    def test_no_demo_fixture_in_the_browser_bundle_needs_no_readiness(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["runtime_inputs"] = [contract["runtime_inputs"][1], contract["runtime_inputs"][2]]
        context = self.derive(contract=contract)
        self.assertEqual((context["status"], context["demo_modules"]), ("not_required", []))

    def test_unisolable_missing_or_literal_free_providers_block(self) -> None:
        not_created = copy.deepcopy(CONTRACT)
        not_created["new_write_targets"] = []
        in_plan = {"plan_allowed_paths": [*CLOSURE_CONTEXT["plan_allowed_paths"], FIXTURE]}
        no_literal = copy.deepcopy(MODULE_FACTS)
        no_literal[FIXTURE]["string_literals"] = ["short", "Shared Production Label"]
        for kwargs, error_code in (
            ({"contract": not_created}, "browser_readiness_demo_module_not_isolable"),
            ({"context": in_plan}, "browser_readiness_demo_module_not_isolable"),
            ({"files": [path for path in FILES if path != FIXTURE]}, "browser_readiness_demo_module_missing"),
            ({"facts": no_literal}, "browser_readiness_no_demo_literal"),
        ):
            with self.subTest(error_code=error_code, kwargs=list(kwargs)):
                context = self.derive(**kwargs)
                self.assertEqual(context["status"], "blocked")
                self.assertEqual(context["blocked"]["error_code"], error_code)

    def test_relative_specifiers_resolve_like_the_bundler(self) -> None:
        known = {FIXTURE, "apps/web/src/rows/index.ts", "apps/web/src/view.tsx"}
        self.assertEqual(resolve_module(ENTRY, "./ledger-fixture", known), FIXTURE)
        self.assertEqual(resolve_module(ENTRY, "./ledger-fixture.js", known), FIXTURE)
        self.assertEqual(resolve_module(ENTRY, "./rows", known), "apps/web/src/rows/index.ts")
        self.assertEqual(resolve_module("apps/web/src/a/b.ts", "../view", known), "apps/web/src/view.tsx")
        self.assertEqual(resolve_module(ENTRY, "react", known), "")
        self.assertEqual(resolve_module(ENTRY, "../../../../outside", known), "")

    def test_r5_reports_only_production_static_imports(self) -> None:
        context = self.derive()
        problems = static_import_problems(context=context, files=FILES, module_facts=MODULE_FACTS, planning_policy=self.policy)
        self.assertEqual([item["paths"] for item in problems], [[ENTRY]])
        self.assertEqual(problems[0]["source"], "R5")
        self.assertIn("VITE_SDLC_DEMO_RUNTIME", problems[0]["expected_change"])

        fixed = copy.deepcopy(MODULE_FACTS)
        fixed[ENTRY]["module_references"] = [{"specifier": "./ledger-fixture", "kind": "dynamic"}]
        self.assertEqual(static_import_problems(context=context, files=FILES, module_facts=fixed, planning_policy=self.policy), [])

    def test_r1_scope_drift_ignores_allowed_paths_and_the_code_graph(self) -> None:
        hashes = {"apps/api/src/app.ts": "a", ENTRY: "b"}
        current = {"apps/api/src/app.ts": "changed", ENTRY: "changed"}
        drift = readiness_scope_drift(
            baselines=hashes,
            current_dirty=["apps/api/src/app.ts", ENTRY, "apps/api/src/new.ts", ".codegraph/index.db"],
            current_sha256=lambda path: current.get(path, ""),
            allowed=[ENTRY],
        )
        self.assertEqual(drift, {"drifted": ["apps/api/src/app.ts"], "outside": ["apps/api/src/new.ts"]})

    def test_r4_needs_a_demo_literal_in_the_last_capture_of_every_viewport(self) -> None:
        def capture(viewport: str, step: int, text: str, status: str = "passed") -> dict[str, object]:
            return {"view_id": "V-0", "viewport_id": viewport, "step": step, "status": status, "aria_snapshot": text}

        literals = ["ledger-demo-account"]
        shown = [capture("desktop", 0, '- cell "ledger-demo-account"'), capture("mobile", 0, "- ledger-demo-account")]
        self.assertEqual(demo_render_problems(capture_results=shown, demo_literals=literals, view_id="V-0"), [])
        missing = [shown[0], capture("mobile", 0, "- main")]
        failed = [shown[0], capture("mobile", 0, '- cell "ledger-demo-account"', status="step_failed")]
        for results in (missing, failed):
            problems = demo_render_problems(capture_results=results, demo_literals=literals, view_id="V-0")
            self.assertEqual(len(problems), 1)
            self.assertIn("mobile", problems[0]["issue"])
        self.assertEqual(len(demo_render_problems(capture_results=[], demo_literals=literals, view_id="V-0")), 1)

    def test_work_packet_is_one_unit_over_the_exact_scope(self) -> None:
        context = self.derive()
        frozen = {
            "run_id": "run_ledger",
            "schema_version": "developer_work_packet_profiled_v3",
            "runtime_config_impact": {"status": "required", "changes": [{"kind": "env_var", "name": "LEDGER_URL"}]},
            "output_contract": {"format": "files", "allowed_paths": [ENTRY, VIEW]},
            "implementation_signature_contract": {
                "entries": [
                    {"contract_id": "SIG-ENTRY", "source_path": ENTRY},
                    {"contract_id": "SIG-VIEW", "source_path": VIEW},
                ]
            },
        }
        packet = build_readiness_work_packet(
            context=context,
            context_ref="artifacts/browser_readiness_context_v1.json",
            closure_contract=CONTRACT,
            closure_contract_ref="artifacts/runtime_closure_contract_v1.json",
            closure_context=CLOSURE_CONTEXT,
            frozen_packet=frozen,
            version=1,
        )

        self.assertEqual((packet["mode"], packet["version"]), ("browser_readiness", "browser_readiness_work_packet_v1"))
        self.assertEqual(packet["runtime_config_impact"]["changes"], [])
        self.assertEqual(packet["output_contract"]["allowed_paths"], context["write_scope"])
        (unit,) = packet["execution_units"]
        self.assertRegex(unit["unit_id"], r"^BR-[0-9A-F]{12}$")
        self.assertEqual(unit["write_paths"], context["write_scope"])
        self.assertEqual(unit["test_target_paths"], [ENTRY_TEST])
        self.assertEqual(unit["required_stage_ids"], ["typecheck", "unit"])
        self.assertEqual(unit["signature_entry_ids"], ["SIG-ENTRY"])
        self.assertEqual(unit["planned_code_targets"], [{"path": FIXTURE, "symbol": "LEDGER_ROWS"}])
        closure_context = unit["closure_context"]
        self.assertEqual(closure_context["demo_flag"], {"env_key": "VITE_SDLC_DEMO_RUNTIME", "enabled_value": "1"})
        self.assertEqual(
            closure_context["readiness_acceptance"],
            {
                "disabled_build": {
                    "output_dir": "apps/web/dist",
                    "forbidden_literals": ["ledger-demo-account"],
                },
                "enabled_capture": {"required_any_literal": ["ledger-demo-account"]},
            },
        )
        self.assertIn("runtime is insufficient", " ".join(closure_context["rules"]))
        self.assertIn("authoritative proof", " ".join(closure_context["rules"]))
        self.assertFalse({"probes", "stand_ins", "env_providers"} & set(closure_context))
        self.assertEqual([item["input_id"] for item in closure_context["resolves"]], ["RI-A"])
        self.assertEqual(closure_context["new_file_roots"], [])
        self.assertNotIn(ENTRY, closure_context["frozen_baseline_paths"])  # in scope, so editable
        self.assertIn(VIEW, closure_context["frozen_baseline_paths"])
        self.assertTrue(unit["input_digest"].startswith("sha256:"))
        findings = readiness_findings([{"source": "R5", "paths": [ENTRY], "issue": "i", "expected_change": "e"}], unit_id=unit["unit_id"])
        self.assertEqual((findings[0]["unit_ids"], findings[0]["paths"]), ([unit["unit_id"]], [ENTRY]))

    def test_browser_repair_packet_keeps_runtime_authority_without_expanding_scope(self) -> None:
        frozen = {
            "schema_version": "developer_work_packet_profiled_v3",
            "output_contract": {"allowed_paths": [ENTRY]},
            "execution_units": [
                {
                    "unit_id": "WU-A",
                    "write_paths": [ENTRY],
                    "required_stage_ids": ["unit"],
                    "input_digest": "sha256:old",
                }
            ],
        }
        contract = {
            **CONTRACT,
            "runtime_probes": [
                {"probe_id": "RP-1", "method": "DELETE", "path": "/records/demo", "expect_status": 204}
            ],
            "verification_stand_ins": [{"stand_in_id": "SI-1", "env_key": "REMOTE_URL"}],
        }
        packet = build_browser_repair_work_packet(
            frozen_packet=frozen,
            closure_contract=contract,
            closure_contract_ref="artifacts/runtime_closure_contract_v1.json",
            closure_context={"execution_facts": ["Harness runs the approved probes before browser capture."]},
            probe_reports=[
                {
                    "source_ref": "artifacts/runtime_closure_verification_v1.json",
                    "probe_results": [
                        {
                            "probe_id": "RP-1",
                            "status": "passed",
                            "observation": {
                                "actual_status": 204,
                                "response_body_excerpt": "must not enter the prompt",
                            },
                        }
                    ],
                }
            ],
            browser_evidence_reports=[
                {
                    "source_ref": "artifacts/browser_baseline_evidence_manifest_v2.json",
                    "variant": "baseline",
                    "records": [
                        {
                            "view_id": "BV-demo",
                            "viewport_id": "desktop",
                            "step": 4,
                            "network_truncated": False,
                            "network": [
                                {
                                    "kind": "response",
                                    "method": "DELETE",
                                    "path": "/records/demo",
                                    "cross_origin": True,
                                    "request_header_names": [
                                        "authorization",
                                        "content-type",
                                        "x-operator-id",
                                    ],
                                    "status": 204,
                                },
                                {
                                    "kind": "request_failed",
                                    "method": "DELETE",
                                    "path": "/records/demo",
                                    "cross_origin": True,
                                    "error": "net::ERR_FAILED",
                                },
                            ],
                        }
                    ],
                }
            ],
        )

        self.assertNotIn("runtime_closure_authority", frozen["execution_units"][0])
        unit = packet["execution_units"][0]
        self.assertEqual(unit["write_paths"], [ENTRY])
        self.assertEqual(unit["required_stage_ids"], ["unit"])
        self.assertNotEqual(unit["input_digest"], "sha256:old")
        authority = unit["runtime_closure_authority"]
        self.assertEqual(authority["runtime_probes"][0]["expect_status"], 204)
        self.assertEqual(authority["probe_observations"][0]["actual_status"], 204)
        self.assertEqual(
            authority["browser_network_observations"],
            [
                {
                    "source_ref": "artifacts/browser_baseline_evidence_manifest_v2.json",
                    "evidence_ref": "baseline:BV-demo:desktop:s04",
                    "kind": "response",
                    "method": "DELETE",
                    "path": "/records/demo",
                    "cross_origin": True,
                    "request_header_names": ["authorization", "content-type", "x-operator-id"],
                    "status": 204,
                    "network_truncated": False,
                },
                {
                    "source_ref": "artifacts/browser_baseline_evidence_manifest_v2.json",
                    "evidence_ref": "baseline:BV-demo:desktop:s04",
                    "kind": "request_failed",
                    "method": "DELETE",
                    "path": "/records/demo",
                    "cross_origin": True,
                    "error": "net::ERR_FAILED",
                    "network_truncated": False,
                },
            ],
        )
        self.assertNotIn("response_body_excerpt", json.dumps(authority))

    def test_module_carries_no_use_case_vocabulary(self) -> None:
        root = Path(__file__).resolve().parents[1] / "sdlc_harness"
        for relative in ("agents/developer/browser_stage_contract.py", "workflows/phase4_browser.py"):
            source = (root / relative).read_text(encoding="utf-8")
            for word in ("CulturalHeritage", "cultural", "ETOUR", "agencyOperator", "heritage", "Eliminate"):
                self.assertNotIn(word, source, relative)


class UiPolishContractTest(unittest.TestCase):
    def setUp(self) -> None:
        project = get_project_profile("typescript_fullstack_v1")
        self.policy = build_planning_path_policy(project=project, language=get_language_profile(project.language_profile_id))
        self.temp = TemporaryDirectory()
        self.target = Path(self.temp.name)
        self.view = "apps/web/src/LedgerView.tsx"
        self.test = "apps/web/src/LedgerView.test.tsx"
        self.foreign_test = "apps/web/src/OtherView.test.tsx"
        self.api = "apps/api/src/ledger.ts"
        self.demo = "apps/web/src/ledger-fixture.ts"
        for path in (self.view, self.test, self.foreign_test, self.api, self.demo):
            target = self.target / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("export {};\n", encoding="utf-8")
        self.visual_profile = {"tokens": {"primary": "#123456", "background": "#FFFFFF"}}
        self.visual_contracts = {"VDN-UI": {"layout": ["main"]}, "VDN-OTHER": {"layout": ["aside"]}}
        self.profile_ref = {"app_scope_id": "web:apps/web", "sha256": "sha256:" + "a" * 64}
        self.frozen = {
            "schema_version": "developer_work_packet_profiled_v3",
            "output_contract": {"format": "files", "allowed_paths": [self.view, self.test, self.foreign_test]},
            "execution_units": [
                {
                    "unit_id": "DU-UI",
                    "component_id": "web",
                    "surfaces": ["ui"],
                    "design_node_ids": ["VDN-UI"],
                    "write_paths": [self.view],
                    "test_target_paths": [self.test],
                    "surface_contracts": [
                        {
                            "design_node_id": "VDN-UI",
                            "surface": "ui",
                            "surface_contract": {"experience_contract": {"visual_contract": self.visual_contracts["VDN-UI"]}},
                        }
                    ],
                },
                {
                    "unit_id": "DU-OTHER",
                    "component_id": "web",
                    "surfaces": ["ui"],
                    "design_node_ids": ["VDN-OTHER"],
                    "write_paths": [],
                    "test_target_paths": [self.foreign_test],
                    "surface_contracts": [
                        {
                            "design_node_id": "VDN-OTHER",
                            "surface": "ui",
                            "surface_contract": {"experience_contract": {"visual_contract": self.visual_contracts["VDN-OTHER"]}},
                        }
                    ],
                },
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def output(self) -> dict[str, object]:
        return {
            "authority": {"visual_profile_ref": dict(self.profile_ref), "design_node_ids": sorted(self.visual_contracts)},
            "polish_units": [
                {
                    "key": "ledger-shell",
                    "objective": "Improve spacing and hierarchy.",
                    "write_paths": [self.view],
                    "new_paths": [],
                    "depends_on": [],
                    "authority_refs": ["primary", "VDN-UI"],
                }
            ],
            "out_of_scope": ["Behavior"],
        }

    def contract(self, output=None) -> dict[str, object]:
        return assemble_ui_polish_contract(
            run_id="run_ledger",
            version=1,
            planner_output=output or self.output(),
            planning_policy=self.policy,
        )

    def issues(self, contract) -> list[dict[str, object]]:
        return polish_contract_rule_issues(
            contract=contract,
            expected_profile_ref=self.profile_ref,
            visual_profile=self.visual_profile,
            visual_contracts=self.visual_contracts,
            frozen_packet=self.frozen,
            planning_policy=self.policy,
            target_path=self.target,
            forbidden_paths=[self.demo, "tests/e2e/sdlc-browser-capture.spec.ts"],
        )

    def gates(self, contract) -> set[str]:
        return {str(item["gate"]) for item in self.issues(contract)}

    def test_valid_contract_gets_stable_ids_and_passes_u1_to_u6(self) -> None:
        contract = self.contract()
        self.assertEqual(self.issues(contract), [])
        self.assertRegex(contract["polish_units"][0]["unit_id"], r"^UP-[0-9A-F]{12}$")
        self.assertEqual(contract, self.contract())

    def test_u1_schema_keys_and_dependency_cycle(self) -> None:
        output = self.output()
        second = copy.deepcopy(output["polish_units"][0])
        second.update({"key": "second", "depends_on": ["ledger-shell"]})
        output["polish_units"][0]["depends_on"] = ["second"]
        output["polish_units"].append(second)
        contract = self.contract(output)
        contract["schema_version"] = "wrong"
        self.assertIn("U1", self.gates(contract))
        self.assertTrue(any("cycle" in item["message"] for item in self.issues(contract)))

    def test_u2_rejects_non_ui_and_another_units_test(self) -> None:
        for path in (self.api, self.foreign_test):
            with self.subTest(path=path):
                output = self.output()
                output["polish_units"][0]["write_paths"] = [path]
                self.assertIn("U2", self.gates(self.contract(output)))

    def test_u3_requires_existing_writes_and_new_source_or_stylesheet(self) -> None:
        output = self.output()
        output["polish_units"][0]["write_paths"] = ["apps/web/src/Missing.tsx"]
        self.assertIn("U3", self.gates(self.contract(output)))
        for path in (self.view, "apps/web/src/readme.md", self.test):
            with self.subTest(path=path):
                output = self.output()
                output["polish_units"][0]["new_paths"] = [path]
                self.assertIn("U3", self.gates(self.contract(output)))

    def test_u4_requires_exact_known_authority(self) -> None:
        output = self.output()
        output["authority"]["design_node_ids"] = ["VDN-UI"]
        output["polish_units"][0]["authority_refs"] = ["unknown-token"]
        self.assertIn("U4", self.gates(self.contract(output)))

    def test_u5_detects_profile_file_drift(self) -> None:
        contract = self.contract()
        contract["authority"]["visual_profile_ref"]["sha256"] = "sha256:" + "b" * 64
        self.assertIn("U5", self.gates(contract))

    def test_u6_rejects_demo_and_harness_rendered_paths(self) -> None:
        for path in (self.demo, "tests/e2e/sdlc-browser-capture.spec.ts"):
            with self.subTest(path=path):
                output = self.output()
                output["polish_units"][0]["write_paths"] = [path]
                self.assertIn("U6", self.gates(self.contract(output)))

    def test_polish_packet_reuses_closure_projection(self) -> None:
        contract = self.contract()
        packet = build_ui_polish_work_packet(
            contract=contract,
            contract_ref="artifacts/ui_polish_contract_v1.json",
            frozen_packet=self.frozen,
            version=1,
        )
        self.assertEqual((packet["mode"], packet["version"]), ("ui_polish", "ui_polish_work_packet_v1"))
        self.assertEqual(packet["runtime_config_impact"]["status"], "not_required")
        self.assertEqual(packet["output_contract"]["allowed_paths"], [self.view])
        self.assertEqual(packet["execution_units"][0]["surfaces"], ["ui"])
        self.assertIn("visual_authority", packet["execution_units"][0]["closure_context"])

    def test_stylesheet_only_polish_unit_can_read_the_markup_of_the_approved_scope(self) -> None:
        stylesheet = "apps/web/src/LedgerView.css"
        output = self.output()
        output["polish_units"].append(
            {
                "key": "ledger-styles",
                "objective": "Tighten the focus ring.",
                "write_paths": [],
                "new_paths": [stylesheet],
                "depends_on": [],
                "authority_refs": ["primary"],
            }
        )
        packet = build_ui_polish_work_packet(
            contract=self.contract(output),
            contract_ref="artifacts/ui_polish_contract_v1.json",
            frozen_packet=self.frozen,
            version=1,
        )
        units = {unit["title"]: unit for unit in packet["execution_units"]}
        styles = units["UI Polish: ledger-styles"]
        # The markup is read-only context: the unit still writes only its own stylesheet.
        self.assertEqual(styles["write_paths"], [stylesheet])
        self.assertEqual(styles["context_paths"], [stylesheet, self.view])
        self.assertEqual(units["UI Polish: ledger-shell"]["context_paths"], [self.view])


if __name__ == "__main__":
    unittest.main()
    polish_contract_rule_issues,
