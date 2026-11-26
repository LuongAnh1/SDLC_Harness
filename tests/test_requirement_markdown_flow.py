"""Deterministic checks of the H-OC Phase 1 Markdown flow and the requirement trace."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.requirements.trace_v1 import (
    coverage_issues,
    defined_ids,
    frame_issues,
    parse_markdown_frame,
    use_case_segments,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.opencode import OpenCodeWorker, final_answer
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.requirement_markdown_flow import (
    agent_decisions,
    agent_defaults,
    answered_document_issues,
    complete_trace,
    question_file_issues,
    trace_issues,
)

SOURCE = """Use case name: SaveArticleAsDraft
Participating Actors:
- Author: a signed-in user who writes the draft.
Flow of events User System:
1. The Author writes a new article and saves it as a draft.
2. The system saves the draft; it shows the draft marked as not published.
"""

DOCUMENT = """# Requirements

## Use-case clauses
- **UC-1** "Use case name: SaveArticleAsDraft" (no observable behavior: the name only)
- **UC-2** "- Author: a signed-in user who writes the draft." (no observable behavior: actor definition)
- **UC-3** "1. The Author writes a new article and saves it as a draft."
- **UC-4** "2. The system saves the draft; it shows the draft marked as not published."

## Acceptance criteria
### AC-1 Save a draft
Given a signed-in author, when they save, then the draft is stored and marked.
- API: 201 with draft true.
- Web: a Draft badge.
- Covers: UC-3, UC-4

## Implied obligations
- **IMP-1** Every list hides drafts. Serves: UC-4. Repo: `src/list.ts`

## Open questions
### Q-1 Refusal status
Which status answers a refused request?
- Affects: AC-1
"""


def exists(path: str) -> bool:
    return path == "src/list.ts"


class FrameTests(unittest.TestCase):
    def test_segments_skip_headings_and_fragments(self) -> None:
        self.assertEqual(
            use_case_segments(SOURCE),
            [
                "a signed-in user who writes the draft.",
                "The Author writes a new article and saves it as a draft.",
                "The system saves the draft;",
                "it shows the draft marked as not published.",
            ],
        )

    def test_a_document_with_the_frame_passes(self) -> None:
        self.assertEqual(defined_ids(DOCUMENT), {"UC-1", "UC-2", "UC-3", "UC-4", "AC-1", "IMP-1", "Q-1"})
        self.assertEqual(frame_issues(parse_markdown_frame(DOCUMENT), SOURCE, exists), [])

    def test_frame_problems_are_named(self) -> None:
        broken = (
            DOCUMENT.replace('"1. The Author writes a new article and saves it as a draft."', "1. paraphrased")
            .replace("- Covers: UC-3, UC-4", "- Covers: UC-4")
            .replace("`src/list.ts`", "`src/missing.ts`")
        )
        self.assertEqual(
            frame_issues(parse_markdown_frame(broken), SOURCE, exists),
            [
                "UC-3: write its verbatim quote in double quotes right after the ID.",
                'No UC clause quotes this use-case sentence: "The Author writes a new article and saves it as a draft."',
                "UC-3: no acceptance criterion covers it; cover it or mark it '(no observable behavior: <reason>)'.",
                "IMP-1: these repository files do not exist: ['src/missing.ts'].",
            ],
        )

    def test_a_clause_may_quote_several_spans_and_skips_field_labels(self) -> None:
        document = DOCUMENT.replace(
            '- **UC-2** "- Author: a signed-in user who writes the draft." (no observable behavior: actor definition)',
            '- **UC-2** "Author: a signed-in user" "who writes the draft." (no observable behavior: actor definition)',
        ).replace('"Use case name: SaveArticleAsDraft"', '"SaveArticleAsDraft"')
        self.assertEqual(frame_issues(parse_markdown_frame(document), SOURCE, exists), [])
        paraphrase = DOCUMENT.replace('"- Author: a signed-in user who writes the draft."', '"Author: someone" "who writes the draft."')
        self.assertEqual(
            frame_issues(parse_markdown_frame(paraphrase), SOURCE, exists),
            [
                'UC-2: the quote "Author: someone" is not verbatim from the use case.',
                'No UC clause quotes this use-case sentence: "a signed-in user who writes the draft."',
            ],
        )

    def test_paraphrased_quote_and_partial_coverage(self) -> None:
        self.assertEqual(
            coverage_issues(SOURCE, ["2. The system saves the draft"]),
            [
                'No UC clause quotes this use-case sentence: "a signed-in user who writes the draft."',
                'No UC clause quotes this use-case sentence: "The Author writes a new article and saves it as a draft."',
                'No UC clause quotes this use-case sentence: "it shows the draft marked as not published."',
            ],
        )


class QuestionAndTraceTests(unittest.TestCase):
    QUESTION = {
        "id": "Q-1", "question": "Which status?", "source": "repository_survey", "proposed_disposition": "human_required",
        "proposed_blocking": True, "affects": ["AC-1"], "answer_mode": "multiple_choice", "recommended_option": "a",
        "options": [{"key": "a", "answer": "404"}, {"key": "b", "answer": "409"}],
    }
    TRACE = {
        "schema_version": "requirement_trace_v1",
        "uc_clauses": [
            {"id": "UC-1", "quote": "Use case name: SaveArticleAsDraft", "observable": False, "reason": "name",
             "kind": "context", "scenario": None},
            {"id": "UC-2", "quote": "- Author: a signed-in user who writes the draft.", "observable": False, "reason": "actor",
             "kind": "context", "scenario": None},
            {"id": "UC-3", "quote": "1. The Author writes a new article and saves it as a draft.", "observable": True, "reason": None,
             "kind": "main_step", "scenario": "SC-1"},
            {"id": "UC-4", "quote": "2. The system saves the draft; it shows the draft marked as not published.",
             "observable": True, "reason": None, "kind": "main_step", "scenario": "SC-1"},
        ],
        "acceptance_criteria": [
            {"id": "AC-1", "text": "saved and marked", "api": "201", "web": "badge", "covers": ["UC-3", "UC-4"],
             "decided_by": ["Q-1"]},
        ],
        "implied_obligations": [
            {"id": "IMP-1", "text": "lists hide drafts", "serves": ["UC-4"], "repo_evidence": ["src/list.ts"], "decided_by": []},
        ],
    }
    ANSWER = {"question_id": "Q-1", "answer": "404", "source": "recommended_option", "answered_by": "owner",
              "answered_at": "2026-11-26T00:00:00Z"}

    def test_question_file_matches_the_document(self) -> None:
        self.assertEqual(question_file_issues([self.QUESTION], DOCUMENT), [])
        wrong = {**self.QUESTION, "source": "guess", "affects": ["Q-1", "SC-1"]}
        self.assertEqual(
            question_file_issues([wrong], DOCUMENT),
            [
                "Q-1: source must be one of ['repository_survey', 'use_case'].",
                "Q-1: affects may hold only UC, AC and IMP IDs the document defines, not ['Q-1', 'SC-1'].",
            ],
        )

    def test_trace_checks_labels_and_answered_questions(self) -> None:
        self.assertEqual(trace_issues(self.TRACE, DOCUMENT, SOURCE, exists, [self.QUESTION], [self.ANSWER]), [])
        undecided = {
            **self.TRACE,
            "acceptance_criteria": [{**self.TRACE["acceptance_criteria"][0], "decided_by": []}],
            "implied_obligations": [],
        }
        self.assertEqual(
            trace_issues(undecided, DOCUMENT, SOURCE, exists, [self.QUESTION], [self.ANSWER]),
            [
                "The document defines IMP-1, but the JSON has no entry IMP-1.",
                "Q-1 has an answer, but no AC or IMP lists it in decided_by; mark the items whose text states the answer.",
            ],
        )

    def test_clause_kinds_and_scenarios_are_checked(self) -> None:
        clauses = [dict(item) for item in self.TRACE["uc_clauses"]]
        clauses[2].update(kind="step", scenario="SC-1")
        clauses[3].update(kind="alternative", scenario=None)
        clauses[0].update(scenario="SC-9")
        trace = {**self.TRACE, "uc_clauses": clauses}
        self.assertEqual(
            trace_issues(trace, DOCUMENT, SOURCE, exists, [self.QUESTION], [self.ANSWER]),
            [
                "UC-1: a context clause has scenario null.",
                "UC-3: kind must be one of ['alternative', 'context', 'exception', 'exit', 'main_step', 'quality'].",
                "UC-4: scenario must be an SC-n ID.",
            ],
        )
        clauses = [dict(item) for item in self.TRACE["uc_clauses"]]
        clauses[3].update(kind="alternative")
        self.assertEqual(
            trace_issues({**self.TRACE, "uc_clauses": clauses}, DOCUMENT, SOURCE, exists, [self.QUESTION], [self.ANSWER]),
            ["SC-1 mixes clause kinds ['alternative', 'main_step']; a scenario groups clauses of one kind."],
        )

    def test_agent_defaults_become_decisions(self) -> None:
        default_question = {
            **self.QUESTION, "id": "Q-2", "proposed_disposition": "agent_default", "recommended_option": "b",
            "why_it_is_safe": "demo only", "reversible": True,
        }
        defaults = agent_defaults([self.QUESTION, default_question])
        self.assertEqual(
            defaults,
            [{"question_id": "Q-2", "answer": "409", "submitted": None, "source": "agent_default",
              "answered_by": "refiner", "answered_at": None}],
        )
        trace = complete_trace(self.TRACE, [self.QUESTION, default_question], [self.ANSWER, *defaults])
        self.assertEqual([(d["id"], d["source"]) for d in trace["decisions"]], [("D-1", "recommended_option"), ("D-2", "agent_default")])
        self.assertEqual([q["status"] for q in trace["questions"]], ["resolved", "agent_default"])

    def test_complete_trace_records_decisions(self) -> None:
        trace = complete_trace(self.TRACE, [self.QUESTION], [self.ANSWER])
        self.assertEqual(trace["decisions"][0]["id"], "D-1")
        self.assertEqual(trace["acceptance_criteria"][0]["decision_refs"], ["D-1"])
        self.assertEqual((trace["questions"][0]["status"], trace["questions"][0]["decision_ref"]), ("resolved", "D-1"))


ANSWERED = DOCUMENT.replace("- API: 201 with draft true.", "- API: 201 with draft true; a refusal answers 404 (decided by Q-1).") + (
    "- Answer: 404\n- Status: answered\n"
)
AGENT_QUESTION = """
### Q-2 Publishing an incomplete draft
What does publishing a draft with an empty body answer?
- Disposition: agent_decided
- Answer: 422 with field errors
- Reason: Q-1 lets drafts be partial; published articles keep the existing validation.
- Raised by: answer review finding 1
- Status: answered
"""


class AnsweredStageTests(unittest.TestCase):
    QUESTION = QuestionAndTraceTests.QUESTION

    def test_the_answered_document_passes(self) -> None:
        self.assertEqual(answered_document_issues(ANSWERED, ANSWERED, [self.QUESTION]), [])

    def test_agent_decisions_are_complete_and_marked_next_to_each_item(self) -> None:
        marked = ANSWERED.replace(
            "- Web: a Draft badge.", "- Web: a Draft badge; publishing an empty body is refused (decided by Q-2, agent decision).",
        ) + AGENT_QUESTION
        self.assertEqual(answered_document_issues(marked, ANSWERED, [self.QUESTION]), [])
        unmarked = marked.replace("(decided by Q-2, agent decision)", "(decided by Q-2)")
        self.assertEqual(
            answered_document_issues(unmarked, ANSWERED, [self.QUESTION]),
            ["AC-1: mark Q-2 as yours: write (decided by Q-2, agent decision)."],
        )
        asked = marked.replace("- Disposition: agent_decided", "- Disposition: human_required").replace(
            "- Reason: Q-1 lets drafts be partial; published articles keep the existing validation.\n", "",
        )
        self.assertEqual(
            answered_document_issues(asked, ANSWERED, [self.QUESTION]),
            [
                "Q-2: the owner is not asked again; a question added after the answers is settled by you with "
                "'- Disposition: agent_decided'.",
                "Q-2: an agent decision needs a '- Reason:' line.",
            ],
        )
        self.assertEqual(
            answered_document_issues(ANSWERED + AGENT_QUESTION, ANSWERED, [self.QUESTION]),
            ["Q-2: no AC or IMP names it; write (decided by Q-2, agent decision) in each item it decides."],
        )

    def test_the_owner_answers_are_final(self) -> None:
        changed = ANSWERED.replace("- Answer: 404", "- Answer: 409")
        self.assertEqual(
            answered_document_issues(changed, ANSWERED, [self.QUESTION]),
            ["Q-1: its 'Answer:' line changed after the answers; restore it, the answers are final."],
        )
        removed = ANSWERED.split("## Open questions")[0]
        self.assertEqual(
            answered_document_issues(removed, ANSWERED, [self.QUESTION]),
            ["Q-1 is missing; keep every question of the questions file."],
        )

    def test_agent_decisions_become_decisions_of_the_trace(self) -> None:
        decided, answers = agent_decisions(ANSWERED + AGENT_QUESTION, [self.QUESTION])
        self.assertEqual(
            decided,
            [{
                "id": "Q-2", "title": "Publishing an incomplete draft",
                "question": "What does publishing a draft with an empty body answer?",
                "proposed_disposition": "agent_decided", "answer": "422 with field errors",
                "reason": "Q-1 lets drafts be partial; published articles keep the existing validation.",
                "raised_by": "answer review finding 1",
            }],
        )
        self.assertEqual(
            answers,
            [{"question_id": "Q-2", "answer": "422 with field errors", "submitted": None, "source": "agent_decided",
              "answered_by": "refiner", "answered_at": None}],
        )
        base = QuestionAndTraceTests.TRACE
        trace = {**base, "acceptance_criteria": [{**base["acceptance_criteria"][0], "decided_by": ["Q-1", "Q-2"]}]}
        completed = complete_trace(trace, [self.QUESTION, *decided], [QuestionAndTraceTests.ANSWER, *answers])
        self.assertEqual([(d["id"], d["source"]) for d in completed["decisions"]], [("D-1", "recommended_option"), ("D-2", "agent_decided")])
        self.assertEqual([q["status"] for q in completed["questions"]], ["resolved", "agent_decided"])
        self.assertEqual(completed["acceptance_criteria"][0]["decision_refs"], ["D-1", "D-2"])


class FinalAnswerTests(unittest.TestCase):
    def test_verdict_is_the_last_non_empty_line(self) -> None:
        self.assertEqual(final_answer("I re-verified the draft.\n\nNothing needs fixing.\n\nOK\n"), "OK")
        self.assertEqual(final_answer("Findings written.\n`docs\\sdlc-local\\review-1.md`"), "docs/sdlc-local/review-1.md")
        self.assertEqual(final_answer("OK, but see below.\nSomething else"), "Something else")
        self.assertEqual(final_answer("  \n"), "")


class RetiredOpenCodeFlowTests(unittest.TestCase):
    def test_run_pinned_to_an_older_opencode_flow_is_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = PhaseOneWorkflow(load_settings(root=Path(temp), runtime_root=Path(temp) / ".runtime"))
            self.assertIsNone(workflow._opencode_model({}, run_id="run_x"))
            with self.assertRaises(HarnessError) as raised:
                workflow._opencode_model(
                    {"requirement_opencode_model": "deepseek/deepseek-flash#high", "requirement_opencode_flow": "markdown_first_v2"},
                    run_id="run_x",
                )
            self.assertEqual(raised.exception.error_code, "requirement_session_not_resumable")


class RepoEvidenceCheckerTest(unittest.TestCase):
    def test_files_and_directories_count_but_the_root_and_outside_paths_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "worktree"
            (root / "packages" / "db" / "prisma" / "migrations").mkdir(parents=True)
            (root / "README.md").write_text("readme", encoding="utf-8")
            (Path(temp) / "outside.md").write_text("outside", encoding="utf-8")

            @contextmanager
            def fake_root():
                yield root

            worker = object.__new__(OpenCodeWorker)
            worker._root = fake_root
            exists = worker.repo_file_checker()
            self.assertTrue(exists("README.md"))
            self.assertTrue(exists("packages/db/prisma/migrations"))
            self.assertTrue(exists("packages/db/prisma/migrations/"))
            self.assertFalse(exists("packages/db/prisma/missing"))
            self.assertFalse(exists("."))
            self.assertFalse(exists(""))
            self.assertFalse(exists("../outside.md"))


if __name__ == "__main__":
    unittest.main()
