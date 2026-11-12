"""L5.3.2 runtime closure contract: derivation, assembly and gates G1-G13/G15.

Fixtures deliberately use two unrelated domains (library loans, clinic triage) and none of
the controlled-run vocabulary, so the gates are shown to depend on typed IDs only.
"""

from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.developer.closure_contract import (
    RESOLUTION_COMPATIBILITY,
    RESOLUTION_KINDS,
    RUNTIME_PROBE_SPEC_PATH,
    assemble_closure_contract,
    closure_contract_rule_issues,
    closure_runnable_status,
    derive_closure_context,
    approved_closure_changes,
    closure_repair_feedback,
    extend_closure_work_packet,
    render_runtime_probe_spec,
    review_closure_planner_output,
    route_closure_problems,
    runtime_probe_failures,
    runtime_probe_run,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_planning_path_policy, get_language_profile, get_project_profile

POLICY = build_planning_path_policy(
    project=get_project_profile("typescript_fullstack_v1"),
    language=get_language_profile("typescript_v1"),
)
REPO_RUNTIME_CONFIG = {"env_reads": [], "workspaces": [], "compose_services": []}
BOUND = {
    "vertical_design_graph_digest": "sha256:graph",
    "implementation_plan_sha256": "sha256:plan",
    "code_snapshot_fingerprint": "sha256:code",
    "verification_policy_version": "runtime_closure_stage_policy_v1",
}
ROOT = "apps/web/src/main.tsx#Root"
DESK = "apps/web/src/LoanDesk.tsx#LoanDesk"


def _desk_surface(node_id: str, required_inputs: list[dict[str, object]], status: str) -> dict[str, object]:
    return {
        "design_node_id": node_id,
        "surface": "ui",
        "surface_contract": {
            "runtime_surface": {
                "schema_version": "runtime_surface_binding_v1",
                "entrypoint": ROOT,
                "composition_path": [ROOT, DESK],
                "required_inputs": required_inputs,
                "runnable_status": status,
            }
        },
    }


def _required(name: str, source: str, binding: str, refs: list[str], provided_by: str = ROOT) -> dict[str, object]:
    return {"name": name, "source": source, "binding": binding, "provided_by": provided_by, "requirement_refs": refs}


def library_packet() -> dict[str, object]:
    """Web UI with every design_source kind, one shared consumer over two scenarios, one env change."""

    desk_binding = {"runtime_kind": "ui_component", "target": {"path": "apps/web/src/LoanDesk.tsx", "symbol": "LoanDesk"}}
    return {
        "execution_units": [
            {
                "unit_id": "WU-A",
                "scenario_id": "SC-1",
                "design_node_ids": ["VDN-root", "VDN-desk-1"],
                "runtime_bindings": [
                    {"design_node_id": "VDN-root", "runtime_kind": "entrypoint", "target": {"path": "apps/web/src/main.tsx", "symbol": "Root"}},
                    {"design_node_id": "VDN-desk-1", **desk_binding},
                ],
                "surface_contracts": [
                    {
                        "design_node_id": "VDN-root",
                        "surface_contract": {
                            "runtime_surface": {"composition_path": [ROOT], "required_inputs": [], "runnable_status": "bound"}
                        },
                    },
                    _desk_surface(
                        "VDN-desk-1",
                        [
                            _required("borrowerRoster", "external_use_case", "PRE-2", ["PRE-2"]),
                            _required("librarianBadge", "new_boundary", "VDN-badge", []),
                            _required("shelfCatalog", "demo_fixture", "D-3", ["D-3"]),
                            _required("pageTitle", "existing_runtime", ROOT, []),
                        ],
                        "externally_blocked",
                    ),
                ],
            },
            {
                "unit_id": "WU-B",
                "scenario_id": "SC-2",
                "design_node_ids": ["VDN-desk-2"],
                "runtime_bindings": [{"design_node_id": "VDN-desk-2", **desk_binding}],
                "surface_contracts": [
                    _desk_surface(
                        "VDN-desk-2",
                        [
                            _required("borrowerRoster", "external_use_case", "PRE-2", ["PRE-2", "MF-4"]),
                            _required("bannerText", "existing_runtime", ROOT, []),
                        ],
                        "externally_blocked",
                    ),
                    # A bound surface: its existing_runtime input is not a closure input.
                    _desk_surface("VDN-desk-3", [_required("pageTitle", "existing_runtime", ROOT, [], provided_by="apps/web/src/main.tsx#Shell")], "bound"),
                ],
            },
            {"unit_id": "WU-C", "scenario_id": "SC-2", "design_node_ids": ["VDN-ledger"], "runtime_bindings": [], "surface_contracts": []},
        ],
        "runtime_config_impact": {
            "status": "required",
            "reason": "outbound ledger",
            "changes": [
                {
                    "kind": "env_var",
                    "unit_id": "WU-C",
                    "name": "LOAN_LEDGER_URL",
                    "default": "http://127.0.0.1:9400",
                    "read_by": "apps/api/src/ledger-client.ts",
                    "purpose": "ledger base URL",
                }
            ],
        },
        "approved_runtime_decisions": [{"id": "D-3", "statement": "demo shelves"}],
        "output_contract": {"allowed_paths": ["apps/web/src/main.tsx", "apps/web/src/LoanDesk.tsx", "apps/api/src/ledger-client.ts"]},
    }


def clinic_packet() -> dict[str, object]:
    """Second vocabulary, no UI: only a Plan env change is a closure input."""

    return {
        "execution_units": [
            {
                "unit_id": "WU-T",
                "scenario_id": "SC-1",
                "design_node_ids": ["VDN-triage"],
                "runtime_bindings": [
                    {"design_node_id": "VDN-triage", "runtime_kind": "api_route", "target": {"path": "apps/api/src/triage-routes.ts", "symbol": "registerTriage"}}
                ],
                "surface_contracts": [{"design_node_id": "VDN-triage", "surface_contract": {"method": "POST"}}],
            }
        ],
        "runtime_config_impact": {
            "status": "required",
            "reason": "queue",
            "changes": [
                {
                    "kind": "env_var",
                    "unit_id": "WU-T",
                    "name": "TRIAGE_QUEUE_URL",
                    "default": "http://127.0.0.1:9500",
                    "read_by": "apps/api/src/triage-routes.ts",
                    "purpose": "queue base URL",
                }
            ],
        },
        "approved_runtime_decisions": [],
        "output_contract": {"allowed_paths": ["apps/api/src/triage-routes.ts", "apps/api/src/triage-routes.test.ts"]},
    }


def library_context() -> dict[str, object]:
    return derive_closure_context(run_id="run_library", work_packet=library_packet(), bound_inputs=BOUND)


def ids_by_name(context: dict[str, object]) -> dict[str, str]:
    return {item["name"]: item["input_id"] for item in context["runtime_inputs"]}


def library_output(context: dict[str, object]) -> dict[str, object]:
    ids = ids_by_name(context)
    symbol = lambda ref: {"kind": "symbol", "ref": ref}  # noqa: E731
    ledger_env = {
        "kind": "env_var",
        "env_key": "LOAN_LEDGER_URL",
        "read_by": "apps/api/src/ledger-client.ts",
        "change_ref": "plan",
    }
    return {
        "resolutions": [
            {"input_id": ids["borrowerRoster"], "kind": "approved_demo_fixture", "provider": symbol("apps/api/src/borrower-roster.ts#listBorrowers"), "decision_ref": "CD-1", "reason": "demo roster"},
            {"input_id": ids["librarianBadge"], "kind": "new_boundary", "provider": symbol("apps/web/src/badge-provider.ts#loadBadge"), "reason": "new provider"},
            {"input_id": ids["shelfCatalog"], "kind": "approved_demo_fixture", "provider": symbol("packages/db/src/shelf-seed.ts#seedShelves"), "decision_ref": "D-3", "reason": "seeded shelves"},
            {"input_id": ids["pageTitle"], "kind": "existing_runtime", "provider": symbol(ROOT), "reason": "already rendered"},
            {"input_id": ids["bannerText"], "kind": "existing_runtime", "provider": symbol(ROOT), "reason": "already rendered"},
            {"input_id": ids["LOAN_LEDGER_URL"], "kind": "existing_runtime", "provider": ledger_env, "reason": "production URL"},
        ],
        "closure_declared_inputs": [],
        "demo_decisions": [
            {"id": "CD-1", "input_ids": [ids["borrowerRoster"]], "statement": "Serve a seeded roster.", "scope": "demo_and_verification_only"},
            {"id": "CD-2", "input_ids": [ids["LOAN_LEDGER_URL"]], "statement": "Local ledger stand-in.", "scope": "demo_and_verification_only"},
        ],
        "closure_units": [
            {
                "key": "api-roster",
                "component_id": "api",
                "objective": "Serve the roster.",
                "depends_on": ["db-seed"],
                "resolves_input_ids": [ids["borrowerRoster"]],
                "write_paths": ["apps/api/src/borrower-roster.ts", "apps/api/src/borrower-roster.test.ts"],
                "test_target_paths": ["apps/api/src/borrower-roster.test.ts"],
                "required_stage_ids": ["typecheck", "unit"],
                "test_intents": [
                    {"intent": "roster lists seeded rows", "test_path": "apps/api/src/borrower-roster.test.ts", "proves_input_ids": [ids["borrowerRoster"]]}
                ],
                "context_paths": [],
            },
            {
                "key": "web-badge",
                "component_id": "web",
                "objective": "Provide the badge at the entrypoint.",
                "depends_on": ["api-roster"],
                "resolves_input_ids": [ids["librarianBadge"], ids["pageTitle"], ids["bannerText"]],
                "write_paths": ["apps/web/src/badge-provider.ts", "apps/web/src/main.tsx"],
                "test_target_paths": [],
                "required_stage_ids": ["typecheck"],
                "test_intents": [],
                "context_paths": [],
            },
            {
                "key": "db-seed",
                "component_id": "database",
                "objective": "Seed demo shelves.",
                "depends_on": [],
                "resolves_input_ids": [ids["shelfCatalog"]],
                "write_paths": ["packages/db/src/shelf-seed.ts"],
                "test_target_paths": [],
                "required_stage_ids": [],
                "test_intents": [],
                "context_paths": [],
            },
            {
                "key": "e2e-ledger",
                "component_id": "e2e",
                "objective": "Ledger stand-in for verification.",
                "depends_on": [],
                "resolves_input_ids": [ids["LOAN_LEDGER_URL"]],
                "write_paths": ["tests/e2e/support/ledger-stand-in.ts"],
                "test_target_paths": [],
                "required_stage_ids": [],
                "test_intents": [],
                "context_paths": [],
            },
        ],
        "runtime_config_changes": [],
        "runtime_probes": [
            {
                "probe_id": "RP-1",
                "scenario_ids": ["SC-1"],
                "service": "api",
                "method": "GET",
                "path": "/borrowers",
                "headers_from_input_ids": [ids["librarianBadge"]],
                "headers": {"Authorization": "Bearer demo-badge"},
                "expect_status": 200,
                "expect_json_array_min_items": 1,
                "proves_input_ids": [ids["borrowerRoster"], ids["shelfCatalog"]],
            },
            {
                "probe_id": "RP-2",
                "scenario_ids": ["SC-2"],
                "service": "web",
                "method": "GET",
                "path": "/",
                "headers_from_input_ids": [],
                "expect_status": 200,
                "expect_json_array_min_items": None,
                "proves_input_ids": [ids["librarianBadge"], ids["pageTitle"], ids["bannerText"], ids["LOAN_LEDGER_URL"]],
            },
        ],
        "verification_stand_ins": [
            {
                "stand_in_id": "VS-1",
                "for_input_id": ids["LOAN_LEDGER_URL"],
                "decision_ref": "CD-2",
                "module": "tests/e2e/support/ledger-stand-in.ts",
                "start_export": "startLedgerStandIn",
                "env_key": "LOAN_LEDGER_URL",
            }
        ],
        "unresolved": [],
    }


EXISTING_SYMBOLS = {("apps/web/src/main.tsx", "Root"), ("apps/web/src/LoanDesk.tsx", "LoanDesk")}


def review(context, output, *, symbols=EXISTING_SYMBOLS, references=lambda a, b: True, source_issues=()):
    return review_closure_planner_output(
        context=context,
        planner_output=output,
        planning_policy=POLICY,
        repo_runtime_config=REPO_RUNTIME_CONFIG,
        symbol_exists=lambda path, name: (path, name) in symbols,
        references=references,
        runtime_config_source_issues=source_issues,
    )


def recheck(context, contract, **kwargs):
    return closure_contract_rule_issues(
        contract,
        context=context,
        planning_policy=POLICY,
        repo_runtime_config=REPO_RUNTIME_CONFIG,
        symbol_exists=lambda path, name: (path, name) in EXISTING_SYMBOLS,
        references=lambda a, b: True,
        **kwargs,
    )


def gates(result: dict[str, object], ref: str | None = None) -> set[str]:
    return {item["gate"] for item in result["issues"] if ref is None or ref in item["refs"]}


class ClosureContextDerivationTest(unittest.TestCase):
    def test_derives_inputs_entrypoints_and_scenarios_from_typed_fields(self) -> None:
        context = library_context()

        by_name = {item["name"]: item for item in context["runtime_inputs"]}
        self.assertEqual(
            sorted(by_name),
            ["LOAN_LEDGER_URL", "bannerText", "borrowerRoster", "librarianBadge", "pageTitle", "shelfCatalog"],
        )
        roster = by_name["borrowerRoster"]
        self.assertEqual(roster["origin"], "design_required_input")
        self.assertEqual(roster["design_source"], "external_use_case")
        self.assertEqual(roster["consumer"], DESK)
        self.assertEqual(roster["provided_by"], ROOT)
        self.assertEqual(roster["scenario_ids"], ["SC-1", "SC-2"])
        self.assertEqual(roster["source_refs"], ["MF-4", "PRE-2", "VDN-desk-1", "VDN-desk-2"])
        self.assertEqual(by_name["librarianBadge"]["source_refs"], ["VDN-badge", "VDN-desk-1"])
        self.assertEqual(by_name["shelfCatalog"]["design_decision_refs"], ["D-3"])
        # existing_runtime on a blocked surface is in scope; on a bound surface it is not.
        self.assertEqual(by_name["pageTitle"]["provided_by"], ROOT)
        ledger = by_name["LOAN_LEDGER_URL"]
        self.assertEqual(ledger["origin"], "plan_runtime_config")
        self.assertIsNone(ledger["design_source"])
        self.assertEqual(ledger["scenario_ids"], ["SC-2"])
        self.assertEqual(ledger["env"]["read_by"], "apps/api/src/ledger-client.ts")
        self.assertEqual(context["entrypoints"], [ROOT])
        self.assertEqual(context["scenario_ids"], ["SC-1", "SC-2"])
        self.assertEqual(context["approved_runtime_decision_ids"], ["D-3"])
        self.assertTrue(all(item["input_id"].startswith("RI-") for item in context["runtime_inputs"]))

    def test_derivation_is_deterministic(self) -> None:
        self.assertEqual(library_context(), library_context())
        reordered = library_packet()
        reordered["execution_units"].reverse()
        self.assertEqual(
            ids_by_name(derive_closure_context(run_id="run_library", work_packet=reordered, bound_inputs=BOUND)),
            ids_by_name(library_context()),
        )

    def test_design_without_ui_has_only_plan_config_inputs(self) -> None:
        context = derive_closure_context(run_id="run_clinic", work_packet=clinic_packet(), bound_inputs=BOUND)

        self.assertEqual([item["name"] for item in context["runtime_inputs"]], ["TRIAGE_QUEUE_URL"])
        self.assertEqual(context["entrypoints"], [])

    def test_missing_bound_inputs_fail_closed(self) -> None:
        with self.assertRaises(HarnessError) as error:
            derive_closure_context(run_id="run_x", work_packet=library_packet(), bound_inputs={})
        self.assertEqual(error.exception.error_code, "runtime_closure_context_invalid")


class ClosureContractGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = library_context()
        self.ids = ids_by_name(self.context)
        self.output = library_output(self.context)

    def assert_gate(self, gate: str, output: dict[str, object] | None = None, **kwargs) -> dict[str, object]:
        result = review(self.context, output if output is not None else self.output, **kwargs)
        self.assertIn(gate, gates(result), result["issues"])
        return result

    def mutated(self, mutate) -> dict[str, object]:
        output = copy.deepcopy(self.output)
        mutate(output)
        return output

    def resolution(self, output: dict[str, object], name: str) -> dict[str, object]:
        return next(item for item in output["resolutions"] if item["input_id"] == self.ids[name])

    def unit(self, output: dict[str, object], key: str) -> dict[str, object]:
        return next(item for item in output["closure_units"] if item["key"] == key)

    def test_valid_contract_passes_every_gate(self) -> None:
        result = review(self.context, self.output)

        self.assertTrue(result["ok"], result["issues"])
        self.assertFalse(result["config_unsupported"])
        self.assertEqual(result["reachability_unverified"], [])
        contract = result["contract"]
        self.assertEqual(
            contract["new_write_targets"],
            [
                "apps/api/src/borrower-roster.test.ts",
                "apps/api/src/borrower-roster.ts",
                "apps/web/src/badge-provider.ts",
                "packages/db/src/shelf-seed.ts",
                "tests/e2e/support/ledger-stand-in.ts",
            ],
        )
        units = {item["key"]: item for item in contract["closure_units"]}
        self.assertTrue(all(item["unit_id"].startswith("CU-") for item in units.values()))
        self.assertEqual(units["api-roster"]["depends_on"], [units["db-seed"]["unit_id"]])
        self.assertEqual(closure_runnable_status(contract), {"runnable_status": "fixture_bound", "blocked_scenario_ids": []})
        self.assertTrue(recheck(self.context, contract)["ok"])

    def test_assembly_is_deterministic_and_ignores_resolution_order(self) -> None:
        first, _ = assemble_closure_contract(context=self.context, planner_output=self.output)
        reordered = self.mutated(lambda output: output["resolutions"].reverse())
        second, _ = assemble_closure_contract(context=self.context, planner_output=reordered)
        self.assertEqual(first, second)

    def test_g1_rejects_bad_shape_enum_and_planner_ids(self) -> None:
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(method="FETCH")))
        self.assert_gate("G1", self.mutated(lambda o: self.resolution(o, "pageTitle").update(kind="magic")))
        self.assert_gate("G1", self.mutated(lambda o: o["demo_decisions"][0].update(scope="production")))
        self.assert_gate("G1", self.mutated(lambda o: self.unit(o, "db-seed").update(key="CU-123")))
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(expect_status=True)))
        contract = review(self.context, self.output)["contract"]
        contract["run_id"] = "run_other"
        self.assertIn("G1", gates(recheck(self.context, contract)))

    def test_g2_requires_exactly_the_harness_input_set(self) -> None:
        self.assert_gate("G2", self.mutated(lambda o: o["resolutions"].pop()))
        self.assert_gate("G2", self.mutated(lambda o: o["resolutions"].append(copy.deepcopy(o["resolutions"][0]))))
        self.assert_gate("G2", self.mutated(lambda o: o["resolutions"].append({"input_id": "RI-000000000000", "kind": "existing_runtime"})))
        contract = review(self.context, self.output)["contract"]
        contract["runtime_inputs"][0]["consumer"] = "apps/web/src/Other.tsx#Other"
        self.assertIn("G2", gates(recheck(self.context, contract)))

    def test_g3_compatibility_table_covers_every_cell(self) -> None:
        cases = {
            "pageTitle": "existing_runtime",
            "librarianBadge": "new_boundary",
            "shelfCatalog": "demo_fixture",
            "borrowerRoster": "external_use_case",
            "LOAN_LEDGER_URL": None,
        }
        for name, source in cases.items():
            for kind in RESOLUTION_KINDS:
                with self.subTest(source=source, kind=kind):
                    output = self.mutated(lambda o: self.resolution(o, name).update(kind=kind))
                    result = review(self.context, output)
                    allowed = kind in RESOLUTION_COMPATIBILITY[source]
                    self.assertEqual("G3" not in gates(result, self.ids[name]), allowed, result["issues"])

    def test_g4_existing_and_env_providers_must_exist(self) -> None:
        self.assert_gate("G4", self.mutated(lambda o: self.resolution(o, "pageTitle")["provider"].update(ref="apps/web/src/main.tsx#Missing")))
        self.assert_gate("G4", self.mutated(lambda o: self.resolution(o, "LOAN_LEDGER_URL")["provider"].update(env_key="OTHER_URL")))
        self.assert_gate(
            "G4",
            source_issues=[{"name": "LOAN_LEDGER_URL", "read_by": "apps/api/src/ledger-client.ts", "error_code": "runtime_config_default_missing"}],
        )

    def test_g5_new_providers_and_stand_ins_belong_to_closure_units(self) -> None:
        self.assert_gate("G5", self.mutated(lambda o: self.resolution(o, "librarianBadge")["provider"].update(ref="apps/web/src/elsewhere.ts#loadBadge")))
        self.assert_gate("G5", self.mutated(lambda o: o["verification_stand_ins"][0].update(env_key="WRONG_URL")))

        def move_stand_in(output):
            output["verification_stand_ins"][0]["module"] = "apps/web/src/ledger-stand-in.ts"
            self.unit(output, "web-badge")["write_paths"].append("apps/web/src/ledger-stand-in.ts")

        self.assert_gate("G5", self.mutated(move_stand_in))

    def test_g6_demo_fixtures_cite_matching_decisions(self) -> None:
        self.assert_gate("G6", self.mutated(lambda o: self.resolution(o, "borrowerRoster").update(decision_ref="CD-7")))
        self.assert_gate("G6", self.mutated(lambda o: self.resolution(o, "borrowerRoster").update(decision_ref="D-3")))
        self.assert_gate("G6", self.mutated(lambda o: self.resolution(o, "shelfCatalog").update(decision_ref="CD-1")))
        self.assert_gate("G6", self.mutated(lambda o: self.resolution(o, "shelfCatalog").update(decision_ref="D-9")))
        self.assert_gate("G6", self.mutated(lambda o: o["demo_decisions"][0].update(input_ids=[self.ids["shelfCatalog"]])))
        self.assert_gate("G6", self.mutated(lambda o: o["verification_stand_ins"][0].update(decision_ref="CD-9")))

    def test_g7_unresolved_rows_and_harness_blocked_scenarios(self) -> None:
        def unresolve(output):
            self.resolution(output, "borrowerRoster").update(kind="unresolved_external", provider=None, decision_ref=None)
            output["demo_decisions"].pop(0)

        self.assert_gate("G7", self.mutated(unresolve))
        self.assert_gate("G7", self.mutated(lambda o: o["unresolved"].append({"input_id": self.ids["shelfCatalog"], "reason": "x"})))

        def unresolve_with_row(output):
            unresolve(output)
            output["unresolved"].append({"input_id": self.ids["borrowerRoster"], "reason": "owned by another use case"})
            output["runtime_probes"] = []  # both scenarios are blocked now, so no probe is required

        result = review(self.context, self.mutated(unresolve_with_row))
        self.assertNotIn("G7", gates(result), result["issues"])
        self.assertNotIn("G15", gates(result), result["issues"])
        self.assertEqual(result["contract"]["unresolved"][0]["blocks_scenarios"], ["SC-1", "SC-2"])
        self.assertEqual(
            closure_runnable_status(result["contract"]),
            {"runnable_status": "externally_blocked", "blocked_scenario_ids": ["SC-1", "SC-2"]},
        )
        contract = result["contract"]
        contract["unresolved"][0]["blocks_scenarios"] = ["SC-1"]
        self.assertIn("G7", gates(recheck(self.context, contract)))

    def test_g8_composition_path_must_be_connected(self) -> None:
        self.assert_gate("G8", references=lambda earlier, later: False)
        self.assert_gate("G8", symbols={("apps/web/src/main.tsx", "Root")})
        unknown = review(self.context, self.output, references=None)
        self.assertNotIn("G8", gates(unknown))
        self.assertIn({"input_id": self.ids["borrowerRoster"], "from": ROOT, "to": DESK}, unknown["reachability_unverified"])

    def test_g9_paths_are_editable_unprotected_and_new_targets_exact(self) -> None:
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "db-seed")["write_paths"].append("package.json")))
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "db-seed")["write_paths"].append("docs/notes.md")))
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "e2e-ledger")["write_paths"].append(RUNTIME_PROBE_SPEC_PATH)))
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "db-seed")["write_paths"].append("")))
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "db-seed")["test_target_paths"].append("packages/db/src/shelf-seed.test.ts")))
        self.assert_gate("G9", self.mutated(lambda o: self.unit(o, "e2e-ledger")["write_paths"].append("tests/e2e/support/helper.ts")))
        contract = review(self.context, self.output)["contract"]
        contract["new_write_targets"].pop()
        self.assertIn("G9", gates(recheck(self.context, contract)))

    def test_g10_stages_and_probe_services(self) -> None:
        self.assert_gate("G10", self.mutated(lambda o: self.unit(o, "db-seed")["required_stage_ids"].append("browser_e2e")))
        self.assert_gate("G10", self.mutated(lambda o: o["runtime_probes"][0].update(service="worker")))

    def test_g11_every_resolved_input_is_proven(self) -> None:
        self.assert_gate("G11", self.mutated(lambda o: o["runtime_probes"][0].update(proves_input_ids=[self.ids["borrowerRoster"]])))

        def drop_stand_in(output):
            output["verification_stand_ins"] = []
            output["demo_decisions"].pop()

        result = self.assert_gate("G11", self.mutated(drop_stand_in))
        self.assertIn(self.ids["LOAN_LEDGER_URL"], {ref for item in result["issues"] for ref in item["refs"]})

        def unprobeable_far_end(output):
            drop_stand_in(output)
            output["unresolved"].append({"input_id": self.ids["LOAN_LEDGER_URL"], "reason": "far end not probeable"})

        result = review(self.context, self.mutated(unprobeable_far_end))
        self.assertNotIn("G11", gates(result), result["issues"])
        self.assertEqual(result["contract"]["unresolved"][0]["blocks_scenarios"], ["SC-2"])

    def test_g12_dependencies_exist_and_are_acyclic(self) -> None:
        self.assert_gate("G12", self.mutated(lambda o: self.unit(o, "db-seed")["depends_on"].append("web-badge")))
        self.assert_gate("G12", self.mutated(lambda o: self.unit(o, "db-seed")["depends_on"].append("ghost")))

    def test_g13_runtime_config_changes(self) -> None:
        compose = {"kind": "compose_service", "unit_key": "db-seed", "service": "ledger", "image": "ledger:1.0.0", "healthcheck": "true", "purpose": "x"}
        result = self.assert_gate("G13", self.mutated(lambda o: o["runtime_config_changes"].append(compose)))
        self.assertTrue(result["config_unsupported"])
        bad_env = {"kind": "env_var", "unit_key": "api-roster", "name": "bad-name", "default": "x", "read_by": "apps/api/src/borrower-roster.ts", "purpose": "x"}
        result = self.assert_gate("G13", self.mutated(lambda o: o["runtime_config_changes"].append(bad_env)))
        self.assertFalse(result["config_unsupported"])

    def test_closure_declared_input_with_closure_env_change_passes(self) -> None:
        def declare(output):
            output["closure_declared_inputs"].append(
                {
                    "key": "audit-sink",
                    "name": "AUDIT_SINK_URL",
                    "consumer": "apps/api/src/borrower-roster.ts#listBorrowers",
                    "provided_by": "apps/api/src/audit-sink.ts#auditSinkUrl",
                    "source_refs": ["VDN-desk-1"],
                    "resolution": {
                        "kind": "new_boundary",
                        "provider": {"kind": "env_var", "env_key": "AUDIT_SINK_URL", "read_by": "apps/api/src/audit-sink.ts", "change_ref": "closure"},
                        "reason": "new outbound sink",
                    },
                }
            )
            roster = self.unit(output, "api-roster")
            roster["write_paths"].append("apps/api/src/audit-sink.ts")
            roster["resolves_input_ids"].append("audit-sink")
            roster["test_intents"].append({"intent": "sink URL default", "test_path": "apps/api/src/borrower-roster.test.ts", "proves_input_ids": ["audit-sink"]})
            output["runtime_config_changes"].append(
                {"kind": "env_var", "unit_key": "api-roster", "name": "AUDIT_SINK_URL", "default": "http://127.0.0.1:9600", "read_by": "apps/api/src/audit-sink.ts", "purpose": "sink"}
            )
            output["unresolved"].append({"input_id": "audit-sink", "reason": "far end not probeable locally"})

        result = review(self.context, self.mutated(declare))
        self.assertTrue(result["ok"], result["issues"])
        declared = next(item for item in result["contract"]["runtime_inputs"] if item["origin"] == "closure_declared")
        self.assertRegex(declared["input_id"], r"^RI-[0-9A-F]{12}$")
        self.assertIn(declared["input_id"], self.unit(result["contract"], "api-roster")["resolves_input_ids"])
        self.assertEqual(result["contract"]["runtime_config_changes"][0]["unit_id"], self.unit(result["contract"], "api-roster")["unit_id"])
        self.assertEqual(result["contract"]["unresolved"][0]["blocks_scenarios"], ["SC-1"])

    def test_g1_probe_headers_are_single_line_literals_matching_header_inputs(self) -> None:
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(headers={})))
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(headers={"Bad Header": "x"})))
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(headers={"X-Token": "a\r\nInjected: 1"})))
        self.assert_gate("G1", self.mutated(lambda o: o["runtime_probes"][0].update(headers="Authorization: x")))

    def test_g1_probe_json_body_is_a_bounded_object_on_a_body_method(self) -> None:
        def body_issues(**probe_fields) -> list[dict[str, object]]:
            result = review(self.context, self.mutated(lambda o: o["runtime_probes"][0].update(**probe_fields)))
            return [issue for issue in result["issues"] if "json_body" in str(issue)]

        self.assertEqual(body_issues(method="DELETE", json_body={"confirmation": True}), [])
        self.assertEqual(body_issues(json_body=None), [])
        self.assertTrue(body_issues(json_body={"confirmation": True}))  # RP-1 is a GET
        self.assertTrue(body_issues(method="DELETE", json_body="confirmation=true"))
        self.assertTrue(body_issues(method="DELETE", json_body=[{"confirmation": True}]))
        self.assertTrue(body_issues(method="POST", json_body={"blob": "x" * 5000}))

    def test_g15_probes_cover_unblocked_scenarios(self) -> None:
        self.assert_gate("G15", self.mutated(lambda o: o["runtime_probes"][0].update(scenario_ids=["SC-9"])))
        self.assert_gate("G15", self.mutated(lambda o: o["runtime_probes"][1].update(scenario_ids=["SC-1"])))


class RuntimeProbeSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        context = library_context()
        self.contract = review(context, library_output(context))["contract"]

    def test_spec_is_deterministic_request_only_with_one_test_per_probe(self) -> None:
        spec = render_runtime_probe_spec(self.contract)

        self.assertEqual(spec, render_runtime_probe_spec(copy.deepcopy(self.contract)))
        for forbidden in ("page", "browser", "context"):
            self.assertNotRegex(spec, rf"\{{[^}}]*\b{forbidden}\b[^}}]*\}}\s*\)\s*=>")
        self.assertEqual(spec.count('test("RP-'), 2)
        self.assertIn('test("RP-1", async ({ request }) => {', spec)
        self.assertIn('test("RP-2", async ({ request }) => {', spec)
        self.assertIn('import config from "../../playwright.config";', spec)
        self.assertIn('import { startLedgerStandIn as standIn1 } from "./support/ledger-stand-in";', spec)
        self.assertIn('standIns.push(await standIn1(standInPort("LOAN_LEDGER_URL")));', spec)
        self.assertIn('"headers": {"Authorization": "Bearer demo-badge"}', spec)
        self.assertIn('type: "runtime_probe"', spec)
        self.assertIn("body.slice(0, 2000)", spec)

    def test_spec_sends_the_approved_json_body_only_where_declared(self) -> None:
        contract = copy.deepcopy(self.contract)
        contract["runtime_probes"][0].update(method="DELETE", json_body={"confirmation": True})
        spec = render_runtime_probe_spec(contract)

        self.assertIn("data: probe.json_body ?? undefined,", spec)
        self.assertIn('"json_body": {"confirmation": true}', spec)
        self.assertIn('"json_body": null', spec)  # RP-2 declares no body
        self.assertEqual(spec, render_runtime_probe_spec(copy.deepcopy(contract)))

    def test_spec_without_stand_ins_has_no_lifecycle_hooks(self) -> None:
        contract = {**self.contract, "verification_stand_ins": []}
        spec = render_runtime_probe_spec(contract)
        self.assertNotIn("beforeAll", spec)
        self.assertNotIn("standIn1", spec)

    def test_closure_work_packet_passes_the_real_runner_packet_validation(self) -> None:
        from sdlc_harness.agents.developer.closure_contract import build_closure_work_packet
        from sdlc_harness.profiles import build_verification_stage_policy
        from sdlc_harness.verification.runner import _validate_profiled_work_packet

        packet = build_closure_work_packet(
            contract=self.contract,
            context=library_context(),
            frozen_packet={"schema_version": "developer_work_packet_profiled_v3", "output_contract": {}},
            version=1,
            contract_ref="runtime_closure_contract_v1.json",
        )
        _validate_profiled_work_packet(
            packet,
            verification_policy=build_verification_stage_policy(project=get_project_profile("typescript_fullstack_v1")),
            run_id="run_library",
        )
        self.assertEqual(packet["test_intents"][0]["stage_id"], "typecheck")
        probes = next(unit for unit in packet["execution_units"] if unit["component_id"] == "web")["closure_context"]["probes"]
        self.assertIn({"Authorization": "Bearer demo-badge"}, [probe["headers"] for probe in probes])

    def test_closure_work_packet_orders_units_after_their_dependencies(self) -> None:
        from sdlc_harness.agents.developer.closure_contract import build_closure_work_packet

        contract = copy.deepcopy(self.contract)
        units = contract["closure_units"]
        self.assertGreaterEqual(len(units), 2)
        first, later = units[0]["unit_id"], units[-1]["unit_id"]
        units[-1]["depends_on"] = []
        units[0]["depends_on"] = [later]  # contract order puts the dependent unit first

        packet = build_closure_work_packet(
            contract=contract,
            context=library_context(),
            frozen_packet={"schema_version": "developer_work_packet_profiled_v3", "output_contract": {}},
            version=1,
            contract_ref="runtime_closure_contract_v1.json",
        )
        order = [unit["unit_id"] for unit in packet["execution_units"]]

        self.assertLess(order.index(later), order.index(first))
        self.assertEqual([unit["order"] for unit in packet["execution_units"]], list(range(1, len(order) + 1)))
        for position, unit in enumerate(packet["execution_units"]):
            self.assertTrue(set(unit["depends_on"]) <= set(order[:position]), unit["unit_id"])

    def test_supporting_unit_sees_every_approved_decision_and_probe(self) -> None:
        from sdlc_harness.agents.developer.closure_contract import build_closure_work_packet

        contract = copy.deepcopy(self.contract)
        contract["closure_units"][-1]["resolves_input_ids"] = []  # a unit that only prepares demo data
        packet = build_closure_work_packet(
            contract=contract,
            context=library_context(),
            frozen_packet={"schema_version": "developer_work_packet_profiled_v3", "output_contract": {}},
            version=1,
            contract_ref="runtime_closure_contract_v1.json",
        )
        supporting = next(
            unit for unit in packet["execution_units"] if unit["unit_id"] == contract["closure_units"][-1]["unit_id"]
        )["closure_context"]

        self.assertEqual(supporting["resolves"], [])
        self.assertEqual(
            [item["id"] for item in supporting["approved_decisions"]],
            [item["id"] for item in contract["demo_decisions"]],
        )
        self.assertEqual(
            [item["probe_id"] for item in supporting["probes"]],
            [item["probe_id"] for item in contract["runtime_probes"]],
        )

    def test_probe_run_and_failures_come_from_the_contract(self) -> None:
        run = runtime_probe_run(self.contract)
        self.assertEqual(run.probe_ids, ("RP-1", "RP-2"))
        self.assertEqual(run.stand_in_env_keys, ("LOAN_LEDGER_URL",))

        failures = runtime_probe_failures(
            self.contract,
            [
                {"probe_id": "RP-1", "status": "failed", "observation": {"actual_status": 500, "actual_items": None, "response_body_excerpt": "x" * 5000}},
                {"probe_id": "RP-2", "status": "passed", "observation": {"actual_status": 200}},
            ],
        )
        self.assertEqual(len(failures), 1)
        failure = failures[0]
        self.assertEqual(
            {key: failure[key] for key in ("probe_id", "scenario_ids", "service", "method", "path", "expected_status", "actual_status", "expected_min_items", "actual_items")},
            {
                "probe_id": "RP-1",
                "scenario_ids": ["SC-1"],
                "service": "api",
                "method": "GET",
                "path": "/borrowers",
                "expected_status": 200,
                "actual_status": 500,
                "expected_min_items": 1,
                "actual_items": None,
            },
        )
        self.assertEqual(len(failure["response_body_excerpt"]), 2000)
        self.assertEqual(len(failure["proves_input_ids"]), 2)


class ClosureRepairRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        context = library_context()
        self.ids = ids_by_name(context)
        self.contract = review(context, library_output(context))["contract"]
        self.units = {unit["key"]: unit["unit_id"] for unit in self.contract["closure_units"]}
        self.frozen = {
            "execution_units": [
                {"unit_id": "WU-PLAN-WEB", "write_paths": ["apps/web/src/LoanDesk.tsx", "apps/web/src/LoanDesk.test.tsx"], "depends_on": ["WU-PLAN-API"]},
                {"unit_id": "WU-PLAN-API", "write_paths": ["apps/api/src/ledger-client.ts"]},
            ],
            "output_contract": {"allowed_paths": ["apps/web/src/LoanDesk.tsx", "apps/web/src/LoanDesk.test.tsx", "apps/api/src/ledger-client.ts"]},
        }
        self.packet = {
            "execution_units": [
                {"unit_id": unit["unit_id"], "write_paths": unit["write_paths"], "component_id": unit["component_id"]}
                for unit in self.contract["closure_units"]
            ],
            "output_contract": {"allowed_paths": sorted({p for u in self.contract["closure_units"] for p in u["write_paths"]})},
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)
        (self.target / "docs").mkdir()
        (self.target / "docs/outside.ts").write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def route(self, *problems):
        return route_closure_problems(
            problems=list(problems),
            contract=self.contract,
            packet=self.packet,
            frozen_packet=self.frozen,
            planning_policy=POLICY,
            target_path=self.target,
        )

    def test_each_rule_routes_mechanically(self) -> None:
        result = self.route(
            {"source": "runtime_probe", "input_ids": [self.ids["borrowerRoster"]], "issue": "1", "expected_change": "1"},
            {"source": "typecheck", "paths": ["packages/db/src/shelf-seed.ts"], "issue": "2", "expected_change": "2"},
            {"source": "review", "unit_ids": [self.units["web-badge"]], "issue": "3", "expected_change": "3"},
            {"source": "runtime_probe", "probe": {"probe_id": "RP-9", "service": "api"}, "issue": "4", "expected_change": "4"},
            {"source": "runtime_probe", "webserver_names": ["web"], "issue": "5", "expected_change": "5"},
            {"source": "runtime_probe", "issue": "5b", "expected_change": "5b"},
        )
        findings = result["findings"]
        self.assertEqual([item["routing_rule"] for item in findings], [1, 2, 3, 4, 5, 5])
        self.assertEqual(findings[0]["unit_ids"], [self.units["api-roster"]])
        self.assertEqual(findings[1]["unit_ids"], [self.units["db-seed"]])
        self.assertEqual(findings[2]["unit_ids"], [self.units["web-badge"]])
        self.assertEqual(findings[3]["unit_ids"], [self.units["api-roster"]])
        self.assertEqual(findings[4]["unit_ids"], [self.units["web-badge"]])
        self.assertEqual(findings[5]["unit_ids"], sorted(self.units.values()))
        self.assertEqual((result["plan_extensions"], result["conflicts"]), ([], []))

    def test_frozen_plan_path_is_assigned_to_its_owning_plan_unit(self) -> None:
        result = self.route(
            {"source": "frontend_build", "paths": ["apps/web/src/LoanDesk.tsx"], "issue": "x", "expected_change": "y"},
            {"source": "validator_code_trace", "unit_ids": ["WU-PLAN-API"], "paths": ["apps/api/src/ledger-client.ts"], "issue": "x", "expected_change": "y"},
        )
        self.assertEqual([item["unit_ids"] for item in result["findings"]], [["WU-PLAN-WEB"], ["WU-PLAN-API"]])
        self.assertEqual(
            result["plan_extensions"],
            [
                {"plan_unit_id": "WU-PLAN-API", "paths": ["apps/api/src/ledger-client.ts"]},
                {"plan_unit_id": "WU-PLAN-WEB", "paths": ["apps/web/src/LoanDesk.tsx"]},
            ],
        )
        extended = extend_closure_work_packet(
            packet=self.packet, frozen_packet=self.frozen, extensions=result["plan_extensions"], version=2
        )
        plan_unit = next(unit for unit in extended["execution_units"] if unit["unit_id"] == "WU-PLAN-WEB")
        self.assertEqual(plan_unit["write_paths"], ["apps/web/src/LoanDesk.tsx"])  # only the cited path
        self.assertEqual(plan_unit["depends_on"], ["WU-PLAN-API"])  # the dependency is in the packet too
        self.assertIn("apps/web/src/LoanDesk.tsx", extended["output_contract"]["allowed_paths"])
        self.assertNotIn("apps/web/src/LoanDesk.test.tsx", extended["output_contract"]["allowed_paths"])
        alone = extend_closure_work_packet(
            packet=self.packet,
            frozen_packet=self.frozen,
            extensions=[{"plan_unit_id": "WU-PLAN-WEB", "paths": ["apps/web/src/LoanDesk.tsx"]}],
            version=2,
        )
        self.assertEqual(
            next(unit for unit in alone["execution_units"] if unit["unit_id"] == "WU-PLAN-WEB")["depends_on"],
            [],  # a dependency outside the packet is dropped
        )

    def test_path_shared_by_plan_units_goes_to_the_cited_unit(self) -> None:
        self.frozen["execution_units"].insert(
            1, {"unit_id": "WU-PLAN-WEB-2", "write_paths": ["apps/web/src/LoanDesk.tsx"]}
        )
        result = self.route(
            {"source": "validator_code_trace", "unit_ids": ["WU-PLAN-WEB-2"], "paths": ["apps/web/src/LoanDesk.tsx"], "issue": "x", "expected_change": "y"},
            {"source": "frontend_build", "paths": ["apps/web/src/LoanDesk.tsx"], "issue": "x", "expected_change": "y"},
        )
        self.assertEqual([item["unit_ids"] for item in result["findings"]], [["WU-PLAN-WEB-2"], ["WU-PLAN-WEB"]])

    def test_scope_conflicts_stop_instead_of_repairing(self) -> None:
        result = self.route(
            {"source": "validator_code_trace", "paths": ["package.json"], "unit_ids": [self.units["db-seed"]], "issue": "x", "expected_change": "y"},
            {"source": "validator_code_trace", "paths": ["docs/outside.ts"], "issue": "x", "expected_change": "y"},
            {"source": "validator_code_trace", "unit_ids": ["WU-PLAN-WEB"], "issue": "x", "expected_change": "y"},
        )
        self.assertEqual(len(result["conflicts"]), 3)
        self.assertTrue(any("protected" in item for item in result["conflicts"]))
        self.assertTrue(any("docs/outside.ts" in item for item in result["conflicts"]))
        self.assertTrue(any("without an exact path" in item for item in result["conflicts"]))

    def test_repair_feedback_uses_the_executor_finding_shape(self) -> None:
        routed = self.route({"source": "runtime_probe", "input_ids": [self.ids["borrowerRoster"]], "issue": "i", "expected_change": "e"})
        feedback = closure_repair_feedback(
            run_id="run_library",
            version=1,
            repair_iteration=1,
            source="runtime_closure_verification",
            summary="probe failed",
            findings=routed["findings"],
            plan_extensions=[{"plan_unit_id": "WU-PLAN-WEB", "paths": ["apps/web/src/LoanDesk.tsx"]}],
            allowed_paths=["apps/api/src/borrower-roster.ts"],
            extra={"probe_failures": [{"probe_id": "RP-1"}]},
        )
        self.assertEqual(feedback["repair_objective"]["contract_findings"][0]["unit_ids"], [self.units["api-roster"]])
        self.assertIn("apps/web/src/LoanDesk.tsx", feedback["developer_constraints"]["allowed_paths"])
        self.assertEqual(feedback["probe_failures"], [{"probe_id": "RP-1"}])
        self.assertEqual(feedback["missing_links"], [])
        changes = approved_closure_changes(contract=self.contract, packet=self.packet, pin={"scope_extensions": [{"path": "apps/web/src/LoanDesk.tsx"}]})
        self.assertIn("apps/web/src/LoanDesk.tsx", changes["changed_paths"])
        self.assertEqual([item["id"] for item in changes["demo_decisions"]], ["CD-1", "CD-2"])


class ClosureNewFileScopeTest(unittest.TestCase):
    """D11 (b): a closure unit may create a not-yet-existing file under its component root only."""

    def setUp(self) -> None:
        from sdlc_harness.agents.developer.closure_contract import closure_new_file_policies

        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        (self.repo / "apps/web/src").mkdir(parents=True)
        (self.repo / "apps/web/src/Existing.tsx").write_text("export {};\n", encoding="utf-8")
        self.item = {
            "id": "CU-WEB",
            "paths": ["apps/web/src/provider.ts"],
            "new_file_policy": closure_new_file_policies(POLICY)["web"],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, path: str, journal: dict[str, object], item_paths: list[str]) -> dict[str, object]:
        from sdlc_harness.agents.developer.execution import _execute_action

        return _execute_action(
            action={"action": "write_file", "arguments": {"path": path, "content": "export const x = 1;", "expected_sha256": ""}},
            run_id="run_d11b",
            work_packet={},
            repo_root=self.repo,
            item_paths=item_paths,
            item=self.item,
            session_dir=self.repo,
            max_write_chars=10000,
            repo_context_provider=None,
            verification_runner=None,
            verification_policy=None,
            journal=journal,
        )

    def test_new_file_under_the_component_root_is_created_and_recorded(self) -> None:
        journal: dict[str, object] = {}
        item_paths = list(self.item["paths"])

        result = self.write("apps/web/src/session-helper.ts", journal, item_paths)

        self.assertTrue(result["ok"], result)
        self.assertEqual([row["path"] for row in journal["scope_extensions"]], ["apps/web/src/session-helper.ts"])
        self.assertEqual(journal["scope_extensions"][0]["rule"], "D11b")
        self.assertIn("apps/web/src/session-helper.ts", item_paths)  # later actions may edit it again

    def test_other_paths_stay_out_of_scope(self) -> None:
        for path in (
            "apps/api/src/other-component.ts",  # another component's root
            "apps/web/src/Existing.tsx",  # existing file not in the unit's paths
            "apps/web/package.json",  # protected
            "apps/web/src/notes.md",  # not an editable suffix
            "tests/e2e/runtime-closure.probe.spec.ts",  # Harness-owned
        ):
            journal: dict[str, object] = {}
            with self.subTest(path=path), self.assertRaises(HarnessError) as caught:
                self.write(path, journal, list(self.item["paths"]))
            self.assertEqual(caught.exception.error_code, "developer_act_out_of_scope")
            self.assertNotIn("scope_extensions", journal)
        without_policy = {**self.item}
        without_policy.pop("new_file_policy")
        self.item = without_policy
        with self.assertRaises(HarnessError):
            self.write("apps/web/src/session-helper.ts", {}, list(self.item["paths"]))


class ApprovedClosureChangesPromptTest(unittest.TestCase):
    def test_only_tasks_touching_closure_paths_get_the_block(self) -> None:
        from sdlc_harness.agents.validator.code_trace import _code_validation_task_prompt

        def prompt(changes=None):
            return _code_validation_task_prompt(
                task={"task_id": "scenario:SC-1", "task_kind": "scenario", "units": [], "claims": []},
                code_snippets=[{"path": "apps/web/src/LoanDesk.tsx", "content": "export {};"}],
                trace_mappings=[],
                verification_evidence={},
                symbol_evidence={},
                scenario_results=[],
                approved_closure_changes=changes,
            )

        baseline = prompt()
        self.assertEqual(prompt({"note": "n", "changed_paths": ["apps/api/src/other.ts"]}), baseline)  # cache-stable
        touched = prompt({"note": "approved", "changed_paths": ["apps/web/src/LoanDesk.tsx"]})
        self.assertNotIn("approved_closure_changes", baseline)
        self.assertIn('"approved_closure_changes"', touched)
        self.assertIn('"changed_paths_in_this_task": ["apps/web/src/LoanDesk.tsx"]', touched.replace("\n", ""))


class ClosureContractVocabularyTest(unittest.TestCase):
    def test_second_domain_without_ui_is_bound(self) -> None:
        context = derive_closure_context(run_id="run_clinic", work_packet=clinic_packet(), bound_inputs=BOUND)
        queue = context["runtime_inputs"][0]["input_id"]
        output = {
            "resolutions": [
                {
                    "input_id": queue,
                    "kind": "existing_runtime",
                    "provider": {"kind": "env_var", "env_key": "TRIAGE_QUEUE_URL", "read_by": "apps/api/src/triage-routes.ts", "change_ref": "plan"},
                    "reason": "production queue URL",
                }
            ],
            "demo_decisions": [{"id": "CD-1", "input_ids": [queue], "statement": "Local queue stand-in.", "scope": "demo_and_verification_only"}],
            "closure_units": [
                {
                    "key": "queue-stand-in",
                    "component_id": "e2e",
                    "objective": "Queue stand-in.",
                    "resolves_input_ids": [queue],
                    "write_paths": ["tests/e2e/support/queue-stand-in.ts"],
                }
            ],
            "runtime_probes": [
                {"probe_id": "RP-1", "scenario_ids": ["SC-1"], "service": "api", "method": "POST", "path": "/triage", "expect_status": 202, "proves_input_ids": [queue]}
            ],
            "verification_stand_ins": [
                {"stand_in_id": "VS-1", "for_input_id": queue, "decision_ref": "CD-1", "module": "tests/e2e/support/queue-stand-in.ts", "start_export": "startQueue", "env_key": "TRIAGE_QUEUE_URL"}
            ],
        }

        result = review(context, output, symbols=set())

        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(closure_runnable_status(result["contract"]), {"runnable_status": "bound", "blocked_scenario_ids": []})

    def test_gate_module_carries_no_use_case_vocabulary(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "sdlc_harness/agents/developer/closure_contract.py").read_text(
            encoding="utf-8"
        )
        for word in ("CulturalHeritage", "cultural", "ETOUR", "agencyOperator", "heritage", "SearchCultural"):
            self.assertNotIn(word, source)


class ClosurePlannerGroundingTest(unittest.TestCase):
    def test_grounding_adds_one_level_of_relative_imports_after_primary_files(self) -> None:
        from sdlc_harness.workflows.phase4_closure import _ClosureRepository

        files = {
            "apps/api/src/http/routes.ts": 'import { act } from "./service";\nimport type { T } from "../lib/types.js";\n'
            'import { tool } from "./tools";\nimport x from "./missing";\nimport fastify from "fastify";\n'
            'import "./routes.css";\nimport { g } from "../../generated/g";\n',
            "apps/api/src/http/service.ts": 'import { deep } from "./deep";\nexport const act = 1;\n',
            "apps/api/src/http/deep.ts": "export const deep = 1;\n",
            "apps/api/src/lib/types.ts": "export type T = string;\n",
            "apps/api/src/http/tools/index.ts": "export const tool = 1;\n",
            "apps/api/src/http/routes.css": ".x { color: red; }\n",
            "apps/api/generated/g.ts": "export const g = 1;\n",
        }
        with tempfile.TemporaryDirectory() as root:
            for path, text in files.items():
                (Path(root) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(root) / path).write_text(text, encoding="utf-8")
            grounding = _ClosureRepository(Path(root)).grounding_files(
                context={"runtime_inputs": [{"consumer": "apps/api/src/http/routes.ts#register"}]},
                work_packet={"execution_units": []},
                planning_policy=POLICY,
            )

        # Primary file first, then its existing editable source imports. Package, missing, stylesheet and
        # non-editable (generated) imports are skipped, and imports of an imported module are not followed.
        self.assertEqual(
            [item["path"] for item in grounding],
            [
                "apps/api/src/http/routes.ts",
                "apps/api/src/http/service.ts",
                "apps/api/src/lib/types.ts",
                "apps/api/src/http/tools/index.ts",
            ],
        )

    def test_planner_sees_the_validator_duties_and_body_capable_probe_schema(self) -> None:
        import json

        from sdlc_harness.agents.developer.closure_contract import CLOSURE_REVIEW_DUTIES
        from sdlc_harness.agents.developer.closure_planner import closure_planner_prompt
        from sdlc_harness.agents.validator.closure_review import _REVIEW_DUTIES

        payload = json.loads(closure_planner_prompt(planner_view={}))

        self.assertIs(_REVIEW_DUTIES, CLOSURE_REVIEW_DUTIES)
        self.assertEqual(payload["validator_review_duties"], list(CLOSURE_REVIEW_DUTIES))
        self.assertIn("json_body", payload["output_schema"]["runtime_probes"][0])
        self.assertTrue(any("validator_review_duties" in rule for rule in payload["rules"]))

    def test_planner_and_validator_share_the_execution_facts(self) -> None:
        import json

        from sdlc_harness.agents.developer.closure_contract import CLOSURE_EXECUTION_FACTS
        from sdlc_harness.agents.developer.closure_planner import closure_planner_prompt
        from sdlc_harness.agents.validator.closure_review import closure_review_prompt

        planner = json.loads(closure_planner_prompt(planner_view={}))
        validator = json.loads(closure_review_prompt(contract={}, validator_view={}))

        self.assertEqual(planner["execution_facts"], list(CLOSURE_EXECUTION_FACTS))
        self.assertEqual(validator["execution_facts"], list(CLOSURE_EXECUTION_FACTS))
        self.assertTrue(any("port it is given" in fact for fact in CLOSURE_EXECUTION_FACTS))


if __name__ == "__main__":
    unittest.main()
