"""The H-OC Phase 3 Planner answer: prompt inputs, parsing and mechanical checks (no LLM)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from sdlc_harness.agents.developer import plan_graph_v1 as pg
from sdlc_harness.agents.developer import plan_planner_v1 as pp
from sdlc_harness.repo_context.plan_facts import RepoFacts

FIXTURES = Path(__file__).parent / "fixtures" / "hoc_plan_r4"


class PlannerAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = json.loads((FIXTURES / "design_units.json").read_text(encoding="utf-8"))
        self.facts = RepoFacts.from_dict(json.loads((FIXTURES / "repo_facts.json").read_text(encoding="utf-8")))
        self.plan_all = pg.build_plan_graph(self.trace, self.facts, k=3,
                                            reliance=pp.assumed_reliance(self.facts.reliance_candidates))
        self.answer = (FIXTURES / "planner_output.md").read_text(encoding="utf-8")

    def test_the_r4_pilot_answer_passes_and_keeps_four_edges(self) -> None:
        parsed = pp.parse_planner(self.answer)
        self.assertEqual(pp.planner_issues(parsed, self.plan_all), [])
        kept = pp.kept_reliance(parsed, self.plan_all)
        self.assertEqual([(e["from"], e["to"]) for e in kept],
                         [("U-6", "U-26"), ("U-7", "U-26"), ("U-19", "U-26"), ("U-20", "U-26")])
        self.assertTrue(all(e["reason"] and e["decided_by"] == "planner" for e in kept))
        option = pp.chosen_option(parsed, self.plan_all)
        final = pg.build_plan_graph(self.trace, self.facts, k=3, reliance=kept,
                                    fixed_base=option["base"], fixed_stubs=option["stubs"])
        self.assertEqual(final["base"]["units"], ["U-1", "U-3", "U-4", "U-26"])
        self.assertEqual(final["checks"]["issues"], [])
        self.assertEqual(final["search"]["fixed"], {"base": ["U-1", "U-3", "U-4", "U-26"], "stubs": []})

    def test_the_prompt_lists_every_candidate_and_option(self) -> None:
        prompt = pp.planner_prompt(design_ref="d.md", report_ref="r.md", output_ref="o.md", plan_all=self.plan_all)
        self.assertEqual(prompt.count("-> U-26"), 21)
        self.assertIn("- Option 0: base U-1, U-3, U-4, U-26", prompt)
        self.assertIn("at least 15 %", prompt)
        self.assertNotIn("{{", prompt)

    def test_layout_and_consistency_problems_are_named(self) -> None:
        broken = (
            self.answer.replace("| U-25 -> U-26 | drop |", "| U-24 -> U-26 | drop |")  # duplicate, U-25 unanswered
            .replace("| U-9 -> U-26 | drop | U-9", "| U-9 -> U-26 | drop | ")
            .replace("Option: 0", "Option: 7")
            + "| U-2 -> U-40 | add | quote |\n"
        )
        broken = broken.replace("## Notes", "## Notes\n| U-30 -> U-26 | keep | not a candidate |", 1)
        issues = pp.planner_issues(pp.parse_planner(broken), self.plan_all)
        text = "\n".join(issues)
        self.assertIn("candidate U-25 -> U-26 is not answered", text)
        self.assertIn("U-24 -> U-26 is answered 2 times", text)
        self.assertIn("Option must be a number from 0 to 2", text)
        self.assertNotIn("U-30 -> U-26", text)  # rows outside ## Reliance are not read

    def test_a_stub_option_needs_a_yes_row_per_stub(self) -> None:
        stub_index = next(i for i, o in enumerate(self.plan_all["search"]["top"]) if o["stubs"])
        stubs = self.plan_all["search"]["top"][stub_index]["stubs"]
        answer = self.answer.replace("Option: 0", f"Option: {stub_index}")
        issues = pp.planner_issues(pp.parse_planner(answer), self.plan_all)
        self.assertTrue(any(f"stubs {stubs[0]}" in issue for issue in issues))
        rows = "".join(f"| {unit} | yes | the Design states it |\n" for unit in stubs)
        answer = answer.replace("## Stubs\nnone", "## Stubs\n| Unit | Decision | Reason |\n| --- | --- | --- |\n" + rows)
        self.assertEqual(pp.planner_issues(pp.parse_planner(answer), self.plan_all), [])

    def test_missing_sections_and_reasons(self) -> None:
        issues = pp.planner_issues(pp.parse_planner("## Reliance\n| U-4 -> U-26 | keep |  |\n"), self.plan_all)
        self.assertIn("missing section ## Base", issues)
        self.assertIn("U-4 -> U-26 has no reason", issues)


if __name__ == "__main__":
    unittest.main()
