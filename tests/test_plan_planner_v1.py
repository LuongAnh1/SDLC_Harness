"""The H-OC Phase 3 Planner answer: prompt inputs, parsing and mechanical checks (no LLM)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from sdlc_harness.agents.developer import plan_graph_v1 as pg
from sdlc_harness.agents.developer import plan_planner_v1 as pp
from sdlc_harness.repo_context.plan_facts import RepoFacts

FIXTURES = Path(__file__).parent / "fixtures" / "hoc_plan_r4"
ALL_OTHER_ACS = ", ".join(f"AC-{n}" for n in range(1, 21) if n not in (3, 4))
# The Reliance/Base/Stubs/Notes sections are the real pilot answer; this Tests section is written for the tests.
UI_ROW = (f"| {ALL_OTHER_ACS} | ui | U-32, U-33, U-35 | tests/e2e/conduit.spec.ts | "
          "the draft lifecycle through the editor, profile and article pages |\n")
TESTS = (
    "## Tests\n"
    "| Proves | Level | Drives | Test file | What it checks |\n"
    "| --- | --- | --- | --- | --- |\n"
    + UI_ROW
    + "| AC-3, IMP-1 | unit | - | apps/api/src/controllers/articlesController/articlesGet.test.ts (new) | "
    "GET on another author's draft answers 404 with no draft data |\n"
    "| AC-4 | api | - | `tests/e2e/drafts.spec.ts` (new) | a visitor never sees a draft |\n"
    "| AC-13 | unit | - | apps/api/src/controllers/articlesController/articlesList.test.ts | the list leaves drafts out |\n"
    "\n"
)


def with_tests(answer: str, tests: str = TESTS) -> str:
    return answer.replace("## Notes", tests + "## Notes", 1)


class PlannerAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = json.loads((FIXTURES / "design_units.json").read_text(encoding="utf-8"))
        self.facts = RepoFacts.from_dict(json.loads((FIXTURES / "repo_facts.json").read_text(encoding="utf-8")))
        self.requirement = pp.requirement_items(
            json.loads((FIXTURES / "requirement_trace.json").read_text(encoding="utf-8")))
        self.plan_all = pg.build_plan_graph(self.trace, self.facts, k=3,
                                            reliance=pp.assumed_reliance(self.facts.reliance_candidates))
        self.answer = with_tests((FIXTURES / "planner_output.md").read_text(encoding="utf-8"))

    def issues(self, answer: str) -> list[str]:
        return pp.planner_issues(pp.parse_planner(answer), self.plan_all, self.requirement)

    def test_the_r4_answer_passes_keeps_four_edges_and_places_the_tests(self) -> None:
        parsed = pp.parse_planner(self.answer)
        self.assertEqual(pp.planner_issues(parsed, self.plan_all, self.requirement), [])
        kept = pp.kept_reliance(parsed, self.plan_all)
        self.assertEqual([(e["from"], e["to"]) for e in kept],
                         [("U-6", "U-26"), ("U-7", "U-26"), ("U-19", "U-26"), ("U-20", "U-26")])
        intents = pp.test_intents(parsed, self.plan_all)
        anchors = {item["path"]: item["anchor"] for item in intents}
        self.assertEqual(anchors["apps/api/src/controllers/articlesController/articlesGet.test.ts"], "U-18")
        self.assertEqual(anchors["tests/e2e/drafts.spec.ts"], "integration")
        self.assertEqual(anchors["tests/e2e/conduit.spec.ts"], "integration")
        self.assertIsNone(anchors["apps/api/src/controllers/articlesController/articlesList.test.ts"])
        option = pp.chosen_option(parsed, self.plan_all)
        final = pg.build_plan_graph(self.trace, self.facts, k=3, reliance=kept, fixed_base=option["base"],
                                    fixed_stubs=option["stubs"], test_intents=intents)
        self.assertEqual(final["base"]["units"], ["U-1", "U-3", "U-4", "U-26"])
        self.assertEqual(final["checks"]["issues"], [])
        tasks = {item["path"]: item["task"] for item in final["test_intents"]}
        self.assertTrue(all(tasks.values()), tasks)  # every intent has a task (plan 11.7 (2))
        self.assertEqual(tasks["tests/e2e/conduit.spec.ts"], "integration")
        self.assertEqual(final["test_intents"][0]["drives"], ["U-32", "U-33", "U-35"])
        self.assertIn("| ui | U-32, U-33, U-35 | tests/e2e/conduit.spec.ts | integration |", pg.plan_markdown(final))
        self.assertEqual(tasks["apps/api/src/controllers/articlesController/articlesGet.test.ts"], "G-1")
        self.assertEqual(tasks["apps/api/src/controllers/articlesController/articlesList.test.ts"], "G-5")
        self.assertEqual(final["integration"]["new_tests"], ["tests/e2e/drafts.spec.ts"])
        core = final["groups"][0]
        self.assertEqual(core["new_tests"], ["apps/api/src/controllers/articlesController/articlesGet.test.ts"])
        self.assertEqual(core["weight"], 21 + 2 * 0.5 + 1)  # files + two existing tests + one new test

    def test_the_prompt_lists_candidates_options_requirement_and_tests(self) -> None:
        prompt = pp.planner_prompt(design_ref="d.md", report_ref="r.md", output_ref="o.md", plan_all=self.plan_all,
                                   requirement=self.requirement)
        self.assertEqual(prompt.count("-> U-26"), 21)
        self.assertIn("- Option 0: base U-1, U-3, U-4, U-26", prompt)
        self.assertIn("at least 15 %", prompt)
        self.assertIn("- AC-3 A draft is private to its Author. API:", prompt)
        self.assertIn("- IMP-1 Single-article reads must hide drafts", prompt)
        self.assertIn("apps/api/src/controllers/articlesController/articlesList.test.ts (imports U-4, U-11, U-16)", prompt)
        self.assertIn("packages/db/src/smoke.ts, tests/e2e/conduit.spec.ts", prompt)
        self.assertIn("under tests/e2e/", prompt)
        self.assertIn("- U-33 (apps/web/src/components/Pages/ProfilePage/ProfilePage.tsx, ", prompt)
        self.assertIn("| Proves | Level | Drives | Test file | What it checks |", prompt)
        self.assertNotIn("{{", prompt)

    def test_layout_and_reliance_problems_are_named(self) -> None:
        broken = (
            self.answer.replace("| U-25 -> U-26 | drop |", "| U-24 -> U-26 | drop |")  # duplicate, U-25 unanswered
            .replace("Option: 0", "Option: 7")
        )
        broken = broken.replace("## Notes", "## Notes\n| U-30 -> U-26 | keep | not a candidate |", 1)
        text = "\n".join(self.issues(broken))
        self.assertIn("candidate U-25 -> U-26 is not answered", text)
        self.assertIn("U-24 -> U-26 is answered 2 times", text)
        self.assertIn("Option must be a number from 0 to 2", text)
        self.assertNotIn("U-30 -> U-26", text)  # rows outside ## Reliance are not read

    def test_test_rows_follow_the_file_rules_and_cover_every_ac(self) -> None:
        bad = (
            "## Tests\n| Proves | Level | Drives | Test file | What it checks |\n| --- | --- | --- | --- | --- |\n"
            "| AC-1 | api | - | apps/api/src/view/articleViewer.test.ts | x |\n"
            "| AC-2 | unit | - | apps/api/src/controllers/articlesController/nothing.test.ts (new) | x |\n"
            "| AC-5 | unit | - | apps/api/src/app.test.ts | x |\n"
            "| AC-6 | unit | - | apps/api/src/view/articleViewer.test.ts (new) | x |\n"
            "| AC-7 | ui | U-32 | tests/other/flow.spec.ts (new) | x |\n"
            "| AC-99 | unit | - | apps/api/src/view/articleViewer.test.ts | x |\n"
            "| AC-8 | unit | - | apps/api/src/view/articleViewer.ts | x |\n"
            "| AC-9 | unit | - | apps/api/src/view/articleViewer.test.ts |  |\n"
            "| AC-10 | unit | - | packages/db/src/smoke.ts | x |\n"
            "| AC-11 | e2e | tests/e2e/conduit.spec.ts | the old four-column layout |\n\n"
        )
        text = "\n".join(self.issues(with_tests((FIXTURES / "planner_output.md").read_text(encoding="utf-8"), bad)))
        self.assertIn("articleViewer.test.ts is an existing unit test: use level unit", text)
        self.assertIn("new unit test apps/api/src/controllers/articlesController/nothing.test.ts must be X.test.ts", text)
        self.assertIn("apps/api/src/app.test.ts is not an existing test that imports a Design unit", text)
        self.assertIn("apps/api/src/view/articleViewer.test.ts already exists: remove (new)", text)
        self.assertIn("new ui test tests/other/flow.spec.ts must be a .spec.ts file under tests/e2e", text)
        self.assertIn("names codes not in the Requirement: AC-99", text)
        self.assertIn("apps/api/src/view/articleViewer.ts is a source file of U-4", text)
        self.assertIn("(apps/api/src/view/articleViewer.test.ts) does not say what it checks", text)
        self.assertIn("packages/db/src/smoke.ts is a whole-system check: use level api or ui", text)
        self.assertIn("test row does not follow the layout | Proves | Level | Drives | Test file | What it checks | "
                      "with level unit, api or ui: | AC-11 | e2e |", text)
        self.assertIn("these acceptance criteria have no valid test intent: AC-1, AC-2, AC-3", text)
        # the ui row on a bad file drives nothing, so every caller of a cut http edge is uncovered
        self.assertIn("no valid ui test intent drives them: U-32, U-33, U-35", text)

    def test_every_caller_of_a_cut_http_edge_is_driven_by_a_ui_intent(self) -> None:
        missing_u33 = self.answer.replace(UI_ROW, UI_ROW.replace("U-32, U-33, U-35", "U-32, U-35"))
        self.assertEqual(self.issues(missing_u33),
                         ["these units call a cut http edge but no valid ui test intent drives them: U-33"])
        on_api = self.answer.replace(UI_ROW, UI_ROW.replace("| ui |", "| api |"))
        text = "\n".join(self.issues(on_api))
        self.assertIn("is level api: only a ui row drives screens (write - under Drives)", text)
        self.assertIn("drives them: U-32, U-33, U-35", text)
        strange = self.answer.replace(UI_ROW, UI_ROW.replace("U-35 |", "U-35, U-99 |"))
        text = "\n".join(self.issues(strange))
        self.assertIn("drives units that are not in the Design: U-99", text)
        self.assertIn("drives them: U-32, U-33, U-35", text)

    def test_a_stub_option_needs_a_yes_row_per_stub(self) -> None:
        stub_index = next(i for i, o in enumerate(self.plan_all["search"]["top"]) if o["stubs"])
        stubs = self.plan_all["search"]["top"][stub_index]["stubs"]
        answer = self.answer.replace("Option: 0", f"Option: {stub_index}")
        self.assertTrue(any(f"stubs {stubs[0]}" in issue for issue in self.issues(answer)))
        rows = "".join(f"| {unit} | yes | the Design states it |\n" for unit in stubs)
        answer = answer.replace("## Stubs\nnone", "## Stubs\n| Unit | Decision | Reason |\n| --- | --- | --- |\n" + rows)
        self.assertEqual(self.issues(answer), [])

    def test_missing_sections_and_reasons(self) -> None:
        issues = self.issues("## Reliance\n| U-4 -> U-26 | keep |  |\n")
        self.assertIn("missing section ## Base", issues)
        self.assertIn("missing section ## Tests", issues)
        self.assertIn("U-4 -> U-26 has no reason", issues)


if __name__ == "__main__":
    unittest.main()
