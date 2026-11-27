"""Deterministic checks of the H-OC Phase 3 Plan graph (no LLM, no repository access)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from sdlc_harness.agents.developer import plan_graph_v1 as pg
from sdlc_harness.repo_context.plan_facts import RepoFacts

FIXTURES = Path(__file__).parent / "fixtures" / "hoc_plan_r4"


def facts(**overrides) -> RepoFacts:
    """A small web/api/db/contracts workspace like Conduit."""

    values = dict(
        package_dirs=("", "apps/api", "apps/web", "packages/contracts", "packages/db"),
        imports={
            "": frozenset({""}),
            "apps/api": frozenset({"apps/api", "packages/contracts", "packages/db"}),
            "apps/web": frozenset({"apps/web", "packages/contracts"}),
            "packages/contracts": frozenset({"packages/contracts"}),
            "packages/db": frozenset({"packages/db"}),
        },
        check_files=frozenset({"tests/e2e/app.spec.ts"}),
        generate_inputs=frozenset({"packages/db/prisma/schema.prisma"}),
        generated_consumers=frozenset({"packages/db", "apps/api"}),
        barrels=frozenset({"packages/db/src/index.ts"}),
        editable_roots=("apps/api/src", "apps/web/src", "packages/contracts/src", "packages/db/src",
                        "packages/db/prisma", "tests/e2e"),
        protected_paths=("package.json",),
    )
    values.update(overrides)
    return RepoFacts(**values)


def trace(*units: tuple[str, list[str], list[str]]) -> dict[str, object]:
    return {"units": [{"id": uid, "files": files, "uses": uses} for uid, files, uses in units]}


SCHEMA = ("U-1", ["packages/db/prisma/schema.prisma"], [])
CONTRACT = ("U-2", ["packages/contracts/src/api.ts"], [])


class EdgeTypeTests(unittest.TestCase):
    def test_types_come_from_packages_and_facts(self) -> None:
        t = trace(
            SCHEMA,
            CONTRACT,
            ("U-3", ["packages/db/src/read.ts (new)", "packages/db/src/index.ts"], ["U-1"]),
            ("U-4", ["apps/api/src/get.ts"], ["U-3", "U-2"]),
            ("U-5", ["apps/web/src/Page.tsx"], ["U-4", "U-2"]),
            ("U-6", ["tests/e2e/app.spec.ts"], ["U-4"]),
        )
        kinds = {(e["from"], e["to"]): e["type"] for e in pg.classify_edges(t, facts())}
        self.assertEqual(kinds[("U-3", "U-1")], "build")
        self.assertEqual(kinds[("U-4", "U-3")], "import")
        self.assertEqual(kinds[("U-4", "U-2")], "import")
        self.assertEqual(kinds[("U-5", "U-4")], "http")
        self.assertEqual(kinds[("U-5", "U-2")], "import")
        self.assertEqual(kinds[("U-6", "U-4")], "verify")

    def test_web_unit_does_not_get_a_build_edge(self) -> None:
        t = trace(SCHEMA, ("U-2", ["apps/web/src/a.ts"], ["U-1"]))
        self.assertEqual(pg.classify_edges(t, facts())[0]["type"], "http")


class GraphTests(unittest.TestCase):
    def test_tarjan_finds_cycles_and_keeps_them_in_one_group(self) -> None:
        sccs = pg.strongly_connected(["U-1", "U-2", "U-3", "U-4"], {"U-1": ["U-2"], "U-2": ["U-1", "U-3"], "U-3": []})
        self.assertIn(["U-1", "U-2"], sccs)
        self.assertLess(sccs.index(["U-3"]), sccs.index(["U-1", "U-2"]))  # dependencies first
        t = trace(
            ("U-1", ["apps/api/src/a.ts"], ["U-2"]),
            ("U-2", ["apps/api/src/b.ts"], ["U-1"]),
            ("U-3", ["apps/api/src/c.ts"], []),
        )
        plan = pg.build_plan_graph(t, facts(), k=2)
        self.assertEqual(plan["sccs"], [["U-1", "U-2"]])
        self.assertIn(["U-1", "U-2"], [g["units"] for g in plan["groups"]])
        self.assertEqual(plan["checks"]["issues"], [])

    def test_build_source_and_contract_start_in_the_base(self) -> None:
        t = trace(
            SCHEMA,
            CONTRACT,
            ("U-3", ["packages/db/src/read.ts"], ["U-1"]),
            ("U-4", ["apps/api/src/get.ts"], ["U-3", "U-2"]),
            ("U-5", ["apps/web/src/Page.tsx"], ["U-4"]),
        )
        plan = pg.build_plan_graph(t, facts(), k=2)
        self.assertEqual(plan["base"]["forced"]["build_sources"], ["U-1"])
        self.assertEqual(plan["base"]["forced"]["contract_units"], ["U-2"])
        self.assertTrue({"U-1", "U-2"} <= set(plan["base"]["units"]))
        groups = [g["units"] for g in plan["groups"]]
        self.assertIn(["U-5"], groups)  # the http edge to U-4 is cut
        self.assertEqual(plan["checks"]["issues"], [])

    def test_barrel_does_not_glue_groups_but_another_shared_file_does(self) -> None:
        t = trace(
            ("U-1", ["packages/db/src/a.ts", "packages/db/src/index.ts"], []),
            ("U-2", ["packages/db/src/b.ts", "packages/db/src/index.ts"], []),
            ("U-3", ["apps/api/src/x.ts", "apps/api/src/shared.ts"], []),
            ("U-4", ["apps/api/src/y.ts", "apps/api/src/shared.ts"], []),
        )
        plan = pg.build_plan_graph(t, facts(), k=3)
        groups = sorted(g["units"] for g in plan["groups"])
        self.assertEqual(groups, [["U-1"], ["U-2"], ["U-3", "U-4"]])
        barrel = next(c for c in plan["conflicts"] if c["file"] == "packages/db/src/index.ts")
        self.assertEqual(barrel["resolution"], "barrel_union")

    def test_check_units_run_last_and_a_unit_needing_one_is_a_g3_issue(self) -> None:
        t = trace(
            ("U-1", ["apps/api/src/a.ts"], []),
            ("U-2", ["tests/e2e/app.spec.ts"], ["U-1"]),
            ("U-3", ["apps/api/src/b.ts"], ["U-2"]),
        )
        plan = pg.build_plan_graph(t, facts(), k=2)
        self.assertEqual(plan["integration"]["units"], ["U-2"])
        self.assertIn("G3", {i["check"] for i in plan["checks"]["issues"]})

    def test_paths_outside_roots_and_protected_paths_are_g0(self) -> None:
        t = trace(("U-1", ["package.json"], []), ("U-2", ["scripts/x.ts"], []))
        checks = [i["check"] for i in pg.build_plan_graph(t, facts(), k=1)["checks"]["issues"]]
        self.assertEqual(checks.count("G0"), 3)  # package.json is protected and outside roots; scripts/ outside

    def test_reliance_edges_need_a_reason(self) -> None:
        t = trace(("U-1", ["apps/api/src/a.ts"], []), ("U-2", ["apps/api/src/err.ts"], []))
        plan = pg.build_plan_graph(t, facts(), reliance=[{"from": "U-1", "to": "U-2"}])
        self.assertIn("G1", {i["check"] for i in plan["checks"]["issues"]})

    def test_second_order_cycle_from_a_custom_grouping_is_caught(self) -> None:
        t = trace(
            ("U-1", ["apps/api/src/a.ts"], ["U-2"]),
            ("U-2", ["apps/api/src/b.ts"], ["U-3"]),
            ("U-3", ["apps/api/src/c.ts"], []),
        )
        plan = pg.build_plan_graph(t, facts(), k=2)
        plan["groups"] = [
            {"group_id": "G-1", "units": ["U-1", "U-3"]},
            {"group_id": "G-2", "units": ["U-2"]},
        ]
        graph = pg._Graph(t, facts(), pg.classify_edges(t, facts()))
        checks = {i["check"] for i in pg.check_plan(graph, plan)}
        self.assertIn("G4", checks)


class SearchTests(unittest.TestCase):
    def hub_trace(self) -> dict[str, object]:
        """U-2 is a hub used by three slices; without it in the base everything is one group."""

        return trace(
            SCHEMA,
            ("U-2", ["packages/db/src/hub.ts"], ["U-1"]),
            *[
                (f"U-{n}", [f"packages/db/src/q{n}.ts", f"apps/api/src/e{n}a.ts", f"apps/api/src/e{n}b.ts"], ["U-2"])
                for n in (3, 4, 5)
            ],
        )

    def test_hub_is_promoted_when_it_lowers_t_by_the_threshold(self) -> None:
        plan = pg.build_plan_graph(self.hub_trace(), facts(), k=3, c0=0)
        self.assertEqual(plan["base"]["units"], ["U-1", "U-2"])
        self.assertEqual(len(plan["groups"]), 3)

    def test_hub_stays_out_when_the_gain_is_below_the_threshold(self) -> None:
        plan = pg.build_plan_graph(self.hub_trace(), facts(), k=1, c0=0)
        self.assertEqual(plan["base"]["units"], ["U-1"])

    def test_a_stub_never_cuts_a_reliance_edge(self) -> None:
        t = trace(
            ("U-1", ["apps/api/src/err.ts"], []),
            ("U-2", ["apps/api/src/a.ts"], []),
            ("U-3", ["apps/api/src/b.ts"], []),
        )
        reliance = [{"from": "U-2", "to": "U-1", "reason": "r"}, {"from": "U-3", "to": "U-1", "reason": "r"}]
        graph = pg._Graph(t, facts(), pg.classify_edges(t, facts(), reliance))
        self.assertEqual(graph.groups(set(), stubs={"U-1"}), [["U-1", "U-2", "U-3"]])

    def test_stub_options_need_the_planner_and_lose_to_base_promotion_below_threshold(self) -> None:
        plan = pg.build_plan_graph(self.hub_trace(), facts(), k=3, c0=0)
        self.assertEqual(plan["stubs"], [])
        for option in plan["search"]["top"]:
            self.assertEqual(option["needs_planner"], bool(option["stubs"]))

    def test_oversized_group_is_cut_into_prefix_slices(self) -> None:
        t = trace(
            ("U-1", [f"apps/api/src/a{i}.ts" for i in range(4)], []),
            ("U-2", [f"apps/api/src/b{i}.ts" for i in range(4)], ["U-1"]),
            ("U-3", [f"apps/api/src/c{i}.ts" for i in range(4)], ["U-2"]),
        )
        group = pg.build_plan_graph(t, facts(), k=1, turn_budget=8)["groups"][0]
        self.assertEqual(group["slices"], [["U-1", "U-2"], ["U-3"]])

    def test_the_turn_budget_counts_design_files_only(self) -> None:
        t = trace(
            ("U-1", [f"apps/api/src/a{i}.ts" for i in range(4)], []),
            ("U-2", [f"apps/api/src/b{i}.ts" for i in range(4)], []),
        )
        tests = tuple({"path": f"apps/api/src/a{i}.test.ts", "units": ["U-1"], "check": False} for i in range(4))
        plan = pg.build_plan_graph(t, facts(tests=tests), k=1, turn_budget=8)
        self.assertEqual([g["weight"] for g in plan["groups"]], [4 + 4 * pg.TEST_WEIGHT, 4])  # tests weigh on the load
        # ... but the two groups' 8 Design files fit one turn of budget 8 (with test weights they would need two)
        self.assertEqual(plan["schedule"]["mode_b"]["agents"][0]["turns"], [["G-1", "G-2"]])
        self.assertEqual(plan["schedule"]["mode_a"]["agents"][0]["turns"], [["G-1", "G-2"]])
        self.assertIn("the turn budget counts Design files only", pg.plan_markdown(plan))


class TestAttachmentTests(unittest.TestCase):
    """Existing tests go to the task of the units they import; new test files weigh on their anchor's task."""

    def plan(self, **kwargs):
        t = trace(
            SCHEMA,
            ("U-2", ["apps/api/src/x.ts"], []),
            ("U-3", ["apps/api/src/y.ts"], []),
            ("U-4", ["apps/api/src/z.ts"], ["U-1"]),  # build edge: U-1 starts in the base
        )
        tests = (
            {"path": "apps/api/src/x.test.ts", "units": ["U-2"], "check": False},
            {"path": "apps/api/src/xy.test.ts", "units": ["U-2", "U-3"], "check": False},  # glues U-2 and U-3
            {"path": "tests/e2e/a.spec.ts", "units": ["U-2"], "check": True},
            {"path": "packages/db/src/schema.test.ts", "units": ["U-1"], "check": False},
            {"path": "apps/api/src/z.test.ts", "units": ["U-1", "U-4"], "check": False},  # base + one group
        )
        return pg.build_plan_graph(t, facts(tests=tests, check_roots=("tests/e2e",)), k=2, **kwargs)

    def test_existing_tests_are_attached_glued_and_weighed(self) -> None:
        plan = self.plan()
        groups = {tuple(g["units"]): g for g in plan["groups"]}
        self.assertEqual(sorted(groups), [("U-2", "U-3"), ("U-4",)])
        self.assertEqual(groups[("U-2", "U-3")]["tests"], ["apps/api/src/x.test.ts", "apps/api/src/xy.test.ts"])
        self.assertEqual(groups[("U-2", "U-3")]["weight"], 2 + 2 * pg.TEST_WEIGHT)
        self.assertEqual(groups[("U-4",)]["tests"], ["apps/api/src/z.test.ts"])
        self.assertEqual(plan["base"]["tests"], ["packages/db/src/schema.test.ts"])
        self.assertEqual(plan["base"]["weight"], 1 + pg.TEST_WEIGHT)
        self.assertEqual(plan["integration"]["tests"], ["tests/e2e/a.spec.ts"])
        self.assertEqual(plan["checks"]["issues"], [])

    def test_a_new_test_file_weighs_on_the_task_of_its_anchor(self) -> None:
        intent = {"proves": ["AC-1"], "level": "unit", "path": "apps/api/src/z.spec2.test.ts", "new": True,
                  "anchor": "U-4", "intent": "x"}
        plan = self.plan(test_intents=[intent, {**intent, "proves": ["AC-2"]}])  # two intents, one new file
        group = next(g for g in plan["groups"] if g["units"] == ["U-4"])
        self.assertEqual(group["new_tests"], ["apps/api/src/z.spec2.test.ts"])
        self.assertEqual(group["weight"], 1 + pg.TEST_WEIGHT + pg.NEW_TEST_WEIGHT)
        self.assertEqual(plan["test_intents"][0]["task"], group["group_id"])
        self.assertIn("## Test intents", pg.plan_markdown(plan))


class R4FixtureTests(unittest.TestCase):
    """Design r4 (UC4 SaveArticleAsDraft) with the Conduit facts read on 2026-11-27."""

    def setUp(self) -> None:
        self.trace = json.loads((FIXTURES / "design_units.json").read_text(encoding="utf-8"))
        self.facts = RepoFacts.from_dict(json.loads((FIXTURES / "repo_facts.json").read_text(encoding="utf-8")))

    def test_edge_counts_and_no_cycle(self) -> None:
        plan = pg.build_plan_graph(self.trace, self.facts, k=3)
        counts = {kind: sum(1 for e in plan["edges"] if e["type"] == kind) for kind in pg.EDGE_TYPES}
        self.assertEqual(counts, {"build": 14, "import": 33, "http": 12, "verify": 7, "reliance": 0})
        self.assertEqual(plan["sccs"], [])
        self.assertEqual(plan["integration"]["units"], ["U-28", "U-29"])

    def test_k3_keeps_the_article_core_group_whole(self) -> None:
        plan = pg.build_plan_graph(self.trace, self.facts, k=3)
        self.assertEqual(plan["base"]["units"], ["U-1", "U-3", "U-4"])
        self.assertEqual(len(plan["groups"]), 9)
        self.assertEqual(
            plan["groups"][0]["units"], ["U-5", "U-6", "U-7", "U-8", "U-9", "U-10", "U-18", "U-19", "U-20", "U-21", "U-27"]
        )
        self.assertEqual(plan["groups"][0]["slices"], [plan["groups"][0]["units"]])  # 21 files fit one turn
        self.assertEqual(plan["checks"]["issues"], [])

    def test_r4_existing_tests_are_attached_without_changing_the_groups(self) -> None:
        plan = pg.build_plan_graph(self.trace, self.facts, k=3)
        groups = {g["group_id"]: g for g in plan["groups"]}
        web = next(g for g in plan["groups"] if "U-30" in g["units"])
        self.assertEqual(len(web["tests"]), 12)
        self.assertEqual(plan["base"]["tests"], ["apps/api/src/view/articleViewer.test.ts"])
        self.assertEqual(len(plan["tests"]), 18)
        self.assertEqual(len(groups), 9)

    def test_kept_reliance_on_the_error_handler_moves_it_into_the_base(self) -> None:
        kept = [{**c, "reason": "fixture"} for c in self.facts.reliance_candidates]
        plan = pg.build_plan_graph(self.trace, self.facts, k=3, reliance=kept)
        self.assertEqual(plan["base"]["units"], ["U-1", "U-3", "U-4", "U-26"])
        self.assertEqual(len(plan["groups"]), 8)
        self.assertEqual(plan["checks"]["issues"], [])

    def test_report_and_diagram_render(self) -> None:
        plan = pg.build_plan_graph(self.trace, self.facts, k=3)
        report = pg.plan_markdown(plan)
        self.assertIn("## Base", report)
        self.assertIn("U-26 <- U-4, U-6", report)
        self.assertIn("G0-G7 pass", report)
        self.assertTrue(pg.plan_mermaid(plan).startswith("flowchart LR"))
        self.assertEqual(plan["test_rules"]["ui_callers"], ["U-32", "U-33", "U-35"])
        self.assertIn("Callers of a cut edge (each driven by >= 1 ui test intent): U-32, U-33, U-35", report)


if __name__ == "__main__":
    unittest.main()
