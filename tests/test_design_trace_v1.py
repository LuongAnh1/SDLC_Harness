"""Deterministic checks of the H-OC Phase 2 Design frame, review ledgers and re-review scope (no LLM)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.designer import design_trace_v1 as dt

TRACE = {
    "uc_clauses": [
        {"id": "UC-1", "kind": "main_step", "scenario": "SC-1"},
        {"id": "UC-2", "kind": "exception", "scenario": "SC-2"},
        {"id": "UC-3", "kind": "context", "scenario": None},
    ],
    "acceptance_criteria": [
        {"id": "AC-1", "covers": ["UC-1"]},
        {"id": "AC-2", "covers": ["UC-2"]},
    ],
    "implied_obligations": [{"id": "IMP-1"}],
    "questions": [{"id": "Q-1"}],
    "decisions": [{"id": "D-1", "question_id": "Q-1"}],
}
ROUTES = ["GET /api/items", "web /items"]

DESIGN = """# Design

## Units

### U-1 Item table
- Kind: data
- Status: modify
- Files: `db/schema.prisma`
- Serves: IMP-1
- Decided by: Q-1
- Uses: -
Adds the draft column.

### U-2 Item query
- Kind: query
- Status: new
- Files: `db/items.ts` (new)
- Serves: AC-1, AC-2
- Decided by: DD-1
- Uses: U-1
Reads published items.

### U-3 Item list endpoint
- Kind: endpoint
- Status: modify
- Files: `api/items.ts`
- Serves: AC-1, AC-2
- Decided by: Q-2
- Uses: U-2
`GET /api/items` answers 200; a draft answers 404 (decided by Q-2, agent decision).

### U-4 Item screen
- Kind: screen
- Status: modify
- Files: `web/Items.tsx`
- Serves: AC-1
- Decided by: -
- Uses: U-3
`/items` lists them.

## Endpoint branches
| Endpoint | Branch | Status / body | Scenarios |
| --- | --- | --- | --- |
| `GET /api/items` | published | 200 | SC-1 |
| `GET /api/items` | draft | 404 | SC-2 |

## Scenario paths
| Scenario | Units in order | ACs |
| --- | --- | --- |
| SC-1 | U-4, U-3, U-2 | AC-1 |
| SC-2 | U-3, U-2 | AC-2 |

## Unchanged surfaces
- none

## Design decisions
### DD-1 Filter in the query
- Decision: filter drafts in the query.
- Reason: one place.
- Alternatives: filter in the endpoint.

## Requirement decisions
### Q-2 What does a draft answer?
- Disposition: agent_decided
- Answer: 404.
- Reason: like a missing item.
- Status: answered

## Notes
"""

FILES = {"db/schema.prisma", "api/items.ts", "web/Items.tsx"}


def exists(path: str) -> bool:
    return path in FILES


class FrameTests(unittest.TestCase):
    def test_a_complete_design_passes(self) -> None:
        self.assertEqual(dt.frame_issues(DESIGN, TRACE, ROUTES, exists), [])

    def test_frame_problems_are_named(self) -> None:
        broken = (
            DESIGN.replace("- Uses: U-3\n`/items`", "- Uses: U-2\n`/items`")  # screen skips the endpoint
            .replace("- Serves: IMP-1\n", "- Serves: AC-1\n")  # IMP-1 no longer served
            .replace("- Files: `api/items.ts`", "- Files: `api/missing.ts`")
            .replace("| SC-2 | U-3, U-2 | AC-2 |", "| SC-2 | U-3, U-9 | AC-1 |")
            .replace("`/items` lists them.", "lists them.")  # web route no longer named
        )
        issues = dt.frame_issues(broken, TRACE, ROUTES, exists)
        for expected in (
            "IMP-1 is served by no unit.",
            "U-3: file api/missing.ts does not exist; mark a planned file `(new)`.",
            "SC-2: path names U-9, which is not a unit.",
            "SC-2: ACs ['AC-1'] differ from the trace ['AC-2'].",
            "U-4: screen reaches no endpoint through Uses.",
            "route `web /items` is neither a unit nor an unchanged surface.",
            "U-9 is referenced but not defined.",
        ):
            self.assertIn(expected, issues)

    def test_uses_links_are_checked(self) -> None:
        unit_blocks = {
            "U-1": "### U-1\n- Kind: data\n- Uses: -\n",
            "U-2": "### U-2\n- Kind: endpoint\n- Uses: U-9\n",
            "U-3": "### U-3\n- Kind: screen\n- Uses: U-3\n",
            "U-4": "### U-4\n- Kind: seed\n",
        }
        self.assertEqual(
            set(dt.uses_issues(unit_blocks, {"U-2", "U-3"})),
            {
                "U-2: Uses names U-9, which is not a unit.",
                "U-2: endpoint reaches no query or data unit through Uses.",
                "U-3: Uses names itself.",
                "U-3: screen reaches no endpoint through Uses.",
                "U-4: needs Uses (`-` when it uses no unit).",
                "U-1: on no Scenario path and reached from none through Uses.",
                "U-4: on no Scenario path and reached from none through Uses.",
            },
        )

    def test_design_questions_are_agent_decisions_applied_by_a_unit(self) -> None:
        self.assertEqual(dt.design_question_issues(DESIGN, TRACE), [])
        broken = DESIGN.replace("- Disposition: agent_decided\n", "").replace("- Decided by: Q-2\n", "- Decided by: -\n")
        broken = broken.replace("### DD-1", "### Q-1 Redefined\n- Answer: x\n\n### DD-1")
        self.assertEqual(
            dt.design_question_issues(broken, TRACE),
            [
                "Q-1 is a requirement question; its answer is final, do not redefine it.",
                "Q-2: a question settled in the Design needs '- Disposition: agent_decided'.",
                "Q-2: no unit applies it; cite it under Decided by of every unit it decides.",
            ],
        )

    def test_groups_flow_and_scenarios(self) -> None:
        scenario_map = dt.scenarios(TRACE)
        self.assertEqual(scenario_map["SC-1"]["acs"], ["AC-1"])
        self.assertEqual(dt.groups(DESIGN, scenario_map), [["SC-1", "SC-2"]])  # they share endpoint U-3
        self.assertEqual(dt.groups(DESIGN, scenario_map, size=1), [["SC-1"], ["SC-2"]])
        self.assertIn("U3 --> U2", dt.flow_mermaid(DESIGN))


class LedgerTests(unittest.TestCase):
    def test_group_ledger_accepts_labels_and_checks_findings(self) -> None:
        good = "## Group 1 (SC-1)\n| AC | Branches | Verdict | Evidence |\n| --- | --- | --- | --- |\n| AC-1 (list) | U-3 | ok | x |\n\nFindings: none\n"
        self.assertEqual(dt.group_ledger_issues(1, ["AC-1"], good), [])
        bad = good.replace("| ok |", "| problem: G1-1 |") + "| AC-1 | U-2 | fine | y |\n"
        self.assertEqual(
            dt.group_ledger_issues(1, ["AC-1", "AC-2"], bad),
            [
                "Group 1: no ledger row for AC-2.",
                "Group 1: AC-1 has 2 rows.",
                "Group 1: G1-1 is cited but not listed under Findings.",
                "Group 1: verdict 'fine' is neither ok nor problem (| AC-1 | U-2 | fine | y |...).",
            ],
        )
        self.assertEqual(dt.group_ledger_issues(2, [], good), ["Group 2: section missing."])

    def test_global_heading_variants(self) -> None:
        body = "# Review — Global (B2)\n| UC | Units | Verdict | Evidence |\n| UC-1 | U-1 | problem: GL-1 | x |\n\n## Findings\n1. **GL-1** x\n"
        self.assertEqual(dt.global_issues(body), [])
        self.assertEqual(dt.global_issues(body.replace("1. **GL-1** x", "none")), ["Global: GL-1 is cited but not listed under Findings."])
        self.assertEqual(dt.global_issues("no heading"), ["Global section missing."])
        self.assertEqual(dt.global_rows(body), [("UC-1", {"U-1"})])

    def test_responses_do_not_swallow_the_next_line(self) -> None:
        text = "- F-1: rejected\n- **F-2**: Fixed — U-3\n"
        self.assertEqual(dt.responses(text), {"F-1": ("rejected", ""), "F-2": ("fixed", "U-3")})
        self.assertEqual(dt.finding_ids("### F-2 x\n1. **F-1** y\n"), ["F-1", "F-2"])


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenarios = dt.scenarios(TRACE)
        self.groups = [["SC-1"], ["SC-2"]]
        self.review = "### F-1 x\n### F-2 y\n"

    def test_changed_unit_below_the_path_and_ledger_rows(self) -> None:
        after = DESIGN.replace("Reads published items.", "Reads published items with a lock.")
        ledgers = {
            1: "| AC-1 | U-4 then U-2 | ok | x |",  # cites the changed query: re-check
            2: "| AC-2 | U-3 only | ok | x |",  # cites only the endpoint: keeps its verdict
        }
        glob = "| UC | Units | Verdict | Evidence |\n| UC-1 | U-2 | ok | x |\n| UC-2 | U-1 | ok | x |\n"
        scope = dt.rereview_scope(DESIGN, after, self.review, "- F-1: fixed - U-2\n- F-2: rejected\n", self.scenarios, self.groups, ledgers, glob)
        self.assertEqual([c["id"] for c in scope["changed_blocks"]], ["U-2"])
        self.assertEqual(scope["affected_scenarios"]["SC-2"], ["path unit changed: U-2"])
        self.assertEqual(scope["groups"][0]["recheck_acs"], {"AC-1": "ledger cites U-2"})
        self.assertFalse(scope["groups"][1]["rerun"])
        self.assertEqual(scope["groups"][1]["keep_acs"], ["AC-2"])
        self.assertEqual(scope["global"]["recheck_rows"], [{"row": "UC-1", "cites": ["U-2"]}])
        self.assertEqual(scope["response_issues"], ["F-2: rejected without a reason."])

    def test_changed_decision_is_reported_not_spread(self) -> None:
        after = DESIGN.replace("- Reason: one place.", "- Reason: one place, cheaper.")
        scope = dt.rereview_scope(DESIGN, after, self.review, "", self.scenarios, self.groups)
        self.assertEqual(scope["decision_issues"], ["DD-1 changed but no unit applying it changed (applied by U-2)."])
        self.assertEqual(scope["affected_scenarios"], {})
        self.assertFalse(scope["global"]["rerun"])

    def test_missing_ledgers_and_global_fail_safe_and_table_rows(self) -> None:
        after = DESIGN.replace("| `GET /api/items` | draft | 404 | SC-2 |", "| `GET /api/items` | draft | 404 empty | SC-2 |")
        after = after.replace("Reads published items.", "Reads items.")
        scope = dt.rereview_scope(DESIGN, after, self.review, "", self.scenarios, self.groups)
        self.assertEqual(scope["groups"][1]["recheck_acs"], {"AC-2": "no ledger row"})
        self.assertTrue(scope["global"]["rerun"])  # no previous global review
        self.assertIn("Endpoint branches row changed: `GET /api/items` / draft", scope["affected_scenarios"]["SC-2"])
        cites_none = dt.rereview_scope(DESIGN, after, self.review, "", self.scenarios, self.groups, {2: "| AC-2 | endpoint | ok | x |"})
        self.assertEqual(cites_none["groups"][1]["recheck_acs"], {"AC-2": "ledger row cites no unit"})

    def test_round_issues(self) -> None:
        scope = {
            "groups": [{"group": 1, "rerun": True, "recheck_acs": {"AC-1": "x", "AC-2": "y"}}, {"group": 2, "rerun": False, "recheck_acs": {}}],
            "global": {"rerun": True, "rows_total": 3, "recheck_rows": [{"row": "UC-1"}, {"row": "U-2"}]},
            "fix_checks": [{"finding": "F-1"}, {"finding": "F-2"}],
        }
        files = {
            "group-1": "| AC | B | Verdict | E |\n| --- | --- | --- | --- |\n| AC-1 (l) | a | ok | e |\n| AC-3 | a | problem: R2-G1-2 | e |\n\nFindings:\n1. R2-G1-1 x\n",
            "global": "| Row | Units | Verdict | Evidence |\n| --- | --- | --- | --- |\n| UC-1 | U-2 | ok | e |\n",
            "final": "## Fix checks\n| Finding | Status | Evidence |\n| --- | --- | --- |\n| F-1 | fixed | e |\n| F-2 | maybe | e |\n",
        }
        self.assertEqual(
            set(dt.round_issues(scope, files)),
            {
                "Group 1: no row for AC-2.",
                "Group 1: row for AC-3, which was not voided.",
                "Group 1: R2-G1-2 is cited but not listed under Findings.",
                "Global: 1 rows for 2 voided rows.",
                "Final: F-2 status 'maybe' is not one of fixed, not fixed, rejection accepted, rejection refused.",
            },
        )


class TraceJsonTests(unittest.TestCase):
    def converted(self) -> dict[str, object]:
        return {
            "schema_version": dt.DESIGN_TRACE_SCHEMA_VERSION,
            "units": [
                {"id": "U-1", "kind": "data", "status": "modify", "files": ["db/schema.prisma"], "serves": ["IMP-1"], "decided_by": ["Q-1"], "uses": []},
                {"id": "U-2", "kind": "query", "status": "new", "files": ["db/items.ts"], "serves": ["AC-1", "AC-2"], "decided_by": ["DD-1"], "uses": ["U-1"]},
                {"id": "U-3", "kind": "endpoint", "status": "modify", "files": ["api/items.ts"], "serves": ["AC-1", "AC-2"], "decided_by": ["Q-2"], "uses": ["U-2"]},
                {"id": "U-4", "kind": "screen", "status": "modify", "files": ["web/Items.tsx"], "serves": ["AC-1"], "decided_by": [], "uses": ["U-3"]},
            ],
            "scenario_paths": [{"scenario": "SC-1", "units": ["U-4", "U-3", "U-2"]}, {"scenario": "SC-2", "units": ["U-3", "U-2"]}],
            "endpoint_branches": [],
            "design_decisions": [{"id": "DD-1", "decision": "filter in the query", "reason": "one place"}],
        }

    def test_the_converted_json_matches_the_document(self) -> None:
        self.assertEqual(dt.design_trace_issues(self.converted(), DESIGN, TRACE), [])
        wrong = self.converted()
        wrong["units"][2]["uses"] = ["U-1"]
        wrong["scenario_paths"][1]["units"] = ["U-3"]
        self.assertEqual(
            dt.design_trace_issues(wrong, DESIGN, TRACE),
            [
                "U-3: uses is ['U-1'] in the JSON but ['U-2'] in the document.",
                "scenario_paths differ from the document's Scenario paths table (same scenarios, units in order).",
            ],
        )
        self.assertIn('"schema_version"', dt.design_trace_issues({}, DESIGN, TRACE)[0])

    def test_design_questions_become_decisions(self) -> None:
        completed = dt.complete_design_trace(self.converted(), DESIGN, TRACE)
        self.assertEqual(completed["design_decisions_from_questions"][0]["id"], "D-2")
        self.assertEqual(completed["design_decisions_from_questions"][0]["question_id"], "Q-2")
        self.assertEqual([u["decision_refs"] for u in completed["units"]], [["D-1"], [], ["D-2"], []])


class RouteInventoryTests(unittest.TestCase):
    def test_api_mounts_and_web_route_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "api/routes").mkdir(parents=True)
            (root / "web").mkdir()
            (root / "api/app.ts").write_text('import items from "./routes/items";\napp.use("/api", items);\n', encoding="utf-8")
            (root / "api/routes/items.ts").write_text(
                'const router = Router();\nrouter.get("/items", list);\nrouter.post("/items/:id", save);\nexport default router;\n',
                encoding="utf-8",
            )
            (root / "web/App.tsx").write_text(
                "<Route path='/items'><Items/></Route>\n<UserOnlyRoute exact path='/editor' userIsLogged={x}>\n<Route path='*'/>\n",
                encoding="utf-8",
            )
            routes = dt.route_inventory(root, {"api": ["api"], "web": ["web"]})
        self.assertEqual(routes, ["GET /api/items", "POST /api/items/:id", "web /items", "web /editor"])


if __name__ == "__main__":
    unittest.main()
