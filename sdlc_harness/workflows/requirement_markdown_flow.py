"""H-OC Phase 1: the Refiner writes Markdown requirements, a Reviewer checks them, the owner answers once.

The Markdown document is what Design reads. The Harness checks its light frame mechanically after every Refiner edit
(clause quotes, sentence coverage, Covers, Serves, cited files) and asks for a format fix only when that fails; the
independent Reviewer then judges the meaning. Once accepted, the Refiner writes the open questions to a JSON file,
the owner answers them in one batch and the Refiner writes the answers into the document. The owner is asked only
once: the Reviewer then checks the answered document, and every gap or contradiction the answers open is settled by
the Refiner as an `agent_decided` question, marked next to each item it decides, until the Reviewer accepts.
Finally the Refiner converts the document to the requirement trace JSON, which the Harness checks again and
completes with the questions and the decisions (owner, agent default, agent decision). The trace is the
traceability graph; the Markdown is archived round by round.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

from ..agents.requirements import RequirementSourceCatalog
from ..agents.requirements.trace_v1 import (
    TRACE_SCHEMA_VERSION,
    block_field,
    defined_ids,
    definition_blocks,
    frame_from_trace,
    frame_issues,
    parse_markdown_frame,
)
from ..core.config import Settings, phase1_llm_call_options
from ..core.errors import HarnessError
from ..llm.client import LlmCompletion
from ..llm.opencode import OpenCodeWorker
from ..llm.step_review import ReviewableLlmClient
from ..sources.fixtures import UseCaseSource
from ..storage.runtime import RuntimeStore

MARKDOWN_FLOW_VERSION = "markdown_first_v3"
FLOW_RECORD = "requirement_markdown_flow_v1.json"
ANSWERS_RECORD = "requirement_markdown_answers_v1.json"
REVIEW_REPAIR_LIMIT = 2
FRAME_REPAIR_LIMIT = 2
JSON_REPAIR_LIMIT = 2
DRAFT_NAME = "requirements-draft.md"
# How ReviewableLlmClient starts the prompt of a call it reruns after the owner rejected its output.
_REJECTED_PREFIX = "Operator review rejected the previous response."

_PROMPTS = Path(__file__).resolve().parents[1] / "agents" / "requirements" / "prompts"
_DISPOSITIONS = {"human_required", "agent_default", "deferred_to_design"}
_SOURCES = {"use_case", "repository_survey"}
_KINDS = {"main_step", "alternative", "exception", "exit", "quality", "context"}
_SCENARIO = re.compile(r"SC-\d+")
# A question the Refiner settles itself after the owner's one batch of answers; the owner is never asked again.
AGENT_DECIDED = "agent_decided"
_DECIDED_BY = re.compile(r"\(decided by ([^)]*)\)", re.IGNORECASE)
_QUESTION_ID = re.compile(r"\bQ-\d+\b")


@dataclass(frozen=True)
class _Stage:
    """One review loop over the document: before the questions (draft) or after the answers (answered)."""

    call: str
    round_stem: str
    review_stem: str
    review_template: str
    revise_template: str
    unresolved: str


_DRAFT_STAGE = _Stage(
    "requirement-markdown", "requirement_markdown_draft", "requirement_markdown_review",
    "requirement_markdown_review.md", "requirement_markdown_revise.md", "requirement_coverage_unresolved",
)
_ANSWERED_STAGE = _Stage(
    "requirement-markdown-answered", "requirement_markdown_answered", "requirement_markdown_answered_review",
    "requirement_markdown_answer_review.md", "requirement_markdown_answer_revise.md", "requirement_answers_unresolved",
)


@dataclass(frozen=True)
class MarkdownRequirementResult:
    trace: dict[str, object]
    markdown_artifact: str
    artifacts: dict[str, str]


class MarkdownQuestionsPending(Exception):
    """The reviewed document has owner questions without answers; Phase 1 waits for one batch of answers."""

    def __init__(self, questions: list[dict[str, object]], artifacts: dict[str, str]):
        super().__init__(f"{len(questions)} question(s) wait for the owner.")
        self.questions = questions
        self.artifacts = artifacts


def label_mismatches(markdown: str, trace: dict[str, object]) -> list[str]:
    """The trace has exactly the UC, AC and IMP items the document defines."""

    drafted = {ident for ident in defined_ids(markdown) if not ident.startswith("Q-")}
    converted = {
        str(item.get("id"))
        for key in ("uc_clauses", "acceptance_criteria", "implied_obligations")
        for item in (trace.get(key) or [])
        if isinstance(item, dict)
    }
    return [
        *(f"The document defines {ident}, but the JSON has no entry {ident}." for ident in sorted(drafted - converted)),
        *(f"The JSON entry {ident} is not defined in the document." for ident in sorted(converted - drafted)),
    ]


def question_file_issues(questions: object, markdown: str) -> list[str]:
    """Mechanical checks of the questions file against the reviewed document (IDs, labels, answer shape)."""

    if not isinstance(questions, list) or not all(isinstance(item, dict) for item in questions):
        return ['The file must be one object {"questions": [ ... ]} whose entries are objects.']
    drafted = defined_ids(markdown)
    drafted_questions = {ident for ident in drafted if ident.startswith("Q-")}
    targets = drafted - drafted_questions
    ids = [str(item.get("id") or "") for item in questions]
    issues = [f"{ident} appears more than once." for ident in sorted({i for i in ids if ids.count(i) > 1})]
    issues += [f"The document defines {ident}, but the file has no entry for it." for ident in sorted(drafted_questions - set(ids))]
    issues += [f"{ident} is not a question the document defines." for ident in sorted(set(ids) - drafted_questions)]
    for item in questions:
        ident = str(item.get("id") or "?")
        if not str(item.get("question") or "").strip().endswith("?"):
            issues.append(f"{ident}: question must be a non-empty sentence ending with '?'.")
        if item.get("source") not in _SOURCES:
            issues.append(f"{ident}: source must be one of {sorted(_SOURCES)}.")
        disposition = item.get("proposed_disposition")
        if disposition not in _DISPOSITIONS:
            issues.append(f"{ident}: proposed_disposition must be one of {sorted(_DISPOSITIONS)}.")
        if not isinstance(item.get("proposed_blocking"), bool):
            issues.append(f"{ident}: proposed_blocking must be true or false.")
        affects = item.get("affects")
        if not isinstance(affects, list) or not affects:
            issues.append(f"{ident}: affects must list the UC, AC or IMP IDs the answer can change.")
        else:
            wrong = [str(ref) for ref in affects if str(ref) not in targets]
            if wrong:
                issues.append(f"{ident}: affects may hold only UC, AC and IMP IDs the document defines, not {wrong}.")
        options = item.get("options")
        recommended = item.get("recommended_option")
        if item.get("answer_mode") == "multiple_choice":
            keys = [str(option.get("key") or "") for option in options] if isinstance(options, list) and all(
                isinstance(option, dict) for option in options) else []
            if not 2 <= len(keys) <= 5 or len(set(keys)) != len(keys) or "" in keys or any(
                not str(option.get("answer") or "").strip() for option in options
            ):
                issues.append(f"{ident}: multiple_choice needs 2-5 options with unique keys and non-empty answers.")
            if recommended is not None and str(recommended) not in keys:
                issues.append(f"{ident}: recommended_option must be one of the option keys or null.")
        elif item.get("answer_mode") == "short":
            if options != [] or recommended is not None:
                issues.append(f"{ident}: a short question has options [] and recommended_option null.")
        else:
            issues.append(f"{ident}: answer_mode must be multiple_choice or short.")
        if disposition == "agent_default" and (
            not (item.get("suggested_answer") or recommended) or not item.get("why_it_is_safe")
            or item.get("reversible") is not True
        ):
            issues.append(f"{ident}: agent_default needs a suggested answer, why_it_is_safe and reversible true.")
    return issues


def trace_issues(
    trace: object,
    markdown: str,
    source_text: str,
    repo_file_exists: Callable[[str], bool],
    questions: list[dict[str, object]],
    answers: list[dict[str, str]],
) -> list[str]:
    """Mechanical checks of the converted trace: shape, the same frame checks as the document, labels, decided_by."""

    if not isinstance(trace, dict) or trace.get("schema_version") != TRACE_SCHEMA_VERSION or not all(
        isinstance(trace.get(key), list) for key in ("uc_clauses", "acceptance_criteria", "implied_obligations")
    ):
        return [
            f'The file must be one object with "schema_version": "{TRACE_SCHEMA_VERSION}" and the lists '
            "uc_clauses, acceptance_criteria and implied_obligations."
        ]
    issues = frame_issues(frame_from_trace(trace), source_text, repo_file_exists)
    issues += label_mismatches(markdown, trace)
    scenario_kinds: dict[str, set[str]] = {}
    for clause in trace["uc_clauses"]:
        ident, kind, scenario = clause.get("id"), clause.get("kind"), clause.get("scenario")
        if kind not in _KINDS:
            issues.append(f"{ident}: kind must be one of {sorted(_KINDS)}.")
        elif kind == "context":
            if scenario is not None:
                issues.append(f"{ident}: a context clause has scenario null.")
        elif not isinstance(scenario, str) or not _SCENARIO.fullmatch(scenario):
            issues.append(f"{ident}: scenario must be an SC-n ID.")
        else:
            scenario_kinds.setdefault(scenario, set()).add(str(kind))
    issues += [
        f"{scenario} mixes clause kinds {sorted(kinds)}; a scenario groups clauses of one kind."
        for scenario, kinds in sorted(scenario_kinds.items())
        if len(kinds) > 1
    ]
    question_ids = {str(item["id"]) for item in questions}
    decided: set[str] = set()
    for key in ("acceptance_criteria", "implied_obligations"):
        for item in trace[key]:
            refs = item.get("decided_by") if isinstance(item, dict) else None
            if not isinstance(refs, list):
                issues.append(f"{item.get('id') if isinstance(item, dict) else key}: decided_by must be a list.")
                continue
            unknown = [ref for ref in refs if ref not in question_ids]
            if unknown:
                issues.append(f"{item.get('id')}: decided_by names unknown questions {unknown}.")
            decided.update(refs)
    for answer in answers:
        if answer["question_id"] not in decided:
            issues.append(
                f"{answer['question_id']} has an answer, but no AC or IMP lists it in decided_by; mark the items "
                "whose text states the answer."
            )
    return issues


def complete_trace(
    trace: dict[str, object],
    questions: list[dict[str, object]],
    answers: list[dict[str, str]],
) -> dict[str, object]:
    """Python-owned part of the trace: the questions and one decision per answer (owner or agent default)."""

    completed = json.loads(json.dumps(trace))
    by_question = {answer["question_id"]: answer for answer in answers}
    decisions = []
    for index, answer in enumerate(answers, start=1):
        decision_id = f"D-{index}"
        decisions.append(
            {
                "id": decision_id,
                "question_id": answer["question_id"],
                "answer": answer["answer"],
                "source": answer["source"],
                "answered_by": answer["answered_by"],
                "answered_at": answer.get("answered_at"),
            }
        )
        for key in ("acceptance_criteria", "implied_obligations"):
            for item in completed[key]:
                if answer["question_id"] in item["decided_by"]:
                    item.setdefault("decision_refs", []).append(decision_id)
    completed["questions"] = [
        {
            **question,
            "status": (
                "open" if question["id"] not in by_question
                else by_question[question["id"]]["source"]
                if by_question[question["id"]]["source"] in ("agent_default", AGENT_DECIDED)
                else "resolved"
            ),
            "decision_ref": next(
                (decision["id"] for decision in decisions if decision["question_id"] == question["id"]), None,
            ),
        }
        for question in questions
    ]
    completed["decisions"] = decisions
    return completed


def answered_document_issues(markdown: str, answered: str, questions: list[dict[str, object]]) -> list[str]:
    """Mechanical checks after the answers: every answer stays as written, and every question added afterwards is an
    agent decision with its answer and reason that the items it decides name as one."""

    blocks = definition_blocks(markdown)
    before = definition_blocks(answered)
    asked = [str(question["id"]) for question in questions]
    issues = [f"{ident} is missing; keep every question of the questions file." for ident in asked if ident not in blocks]
    for ident in asked:
        if ident in blocks and ident in before:
            for name in ("Disposition", "Answer"):
                if block_field(blocks[ident][1], name) != block_field(before[ident][1], name):
                    issues.append(f"{ident}: its '{name}:' line changed after the answers; restore it, the answers are final.")
    added = [ident for ident in blocks if ident.startswith("Q-") and ident not in asked]
    named: set[str] = set()
    for ident, (_rest, block) in blocks.items():
        if not ident.startswith(("AC-", "IMP-")):
            continue
        for group in _DECIDED_BY.findall(block):
            refs = set(_QUESTION_ID.findall(group))
            named |= refs
            for ref in sorted(refs & set(added)):
                if "agent decision" not in group.lower():
                    issues.append(f"{ident}: mark {ref} as yours: write (decided by {ref}, agent decision).")
    for ident in added:
        block = blocks[ident][1]
        if (block_field(block, "Disposition") or "").strip("`*_ ") != AGENT_DECIDED:
            issues.append(
                f"{ident}: the owner is not asked again; a question added after the answers is settled by you with "
                f"'- Disposition: {AGENT_DECIDED}'."
            )
        for name in ("Answer", "Reason"):
            if not (block_field(block, name) or "").strip():
                issues.append(f"{ident}: an agent decision needs a '- {name}:' line.")
        if ident not in named:
            issues.append(f"{ident}: no AC or IMP names it; write (decided by {ident}, agent decision) in each item it decides.")
    return issues


def agent_decisions(
    markdown: str, questions: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The questions the Refiner settled after the answers, and their answers in the shape of a stored answer."""

    asked = {str(question["id"]) for question in questions}
    decided, answers = [], []
    for ident, (rest, block) in definition_blocks(markdown).items():
        if not ident.startswith("Q-") or ident in asked:
            continue
        lines = [line.strip() for line in block.splitlines()[1:] if line.strip()]
        answer = (block_field(block, "Answer") or "").strip()
        decided.append(
            {
                "id": ident,
                "title": rest.strip().strip("*_` ").strip(),
                "question": next((line for line in lines if not line.startswith(("-", "#"))), None),
                "proposed_disposition": AGENT_DECIDED,
                "answer": answer,
                "reason": block_field(block, "Reason"),
                "raised_by": block_field(block, "Raised by"),
            }
        )
        answers.append(
            {
                "question_id": ident, "answer": answer, "submitted": None, "source": AGENT_DECIDED,
                "answered_by": "refiner", "answered_at": None,
            }
        )
    return decided, answers


def agent_defaults(questions: list[dict[str, object]]) -> list[dict[str, object]]:
    """The default each agent_default question carries, in the shape of a stored answer."""

    defaults = []
    for question in questions:
        if question.get("proposed_disposition") != "agent_default":
            continue
        options = {str(option.get("key")): option for option in question.get("options") or [] if isinstance(option, dict)}
        chosen = options.get(str(question.get("recommended_option")))
        text = str((chosen or {}).get("answer") or question.get("suggested_answer") or "").strip()
        defaults.append(
            {
                "question_id": str(question["id"]),
                "answer": text,
                "submitted": None,
                "source": "agent_default",
                "answered_by": "refiner",
                "answered_at": None,
            }
        )
    return defaults


def review_prompt(
    number: int,
    source_ref: str,
    draft_ref: str,
    previous_feedback_ref: str | None,
    template: str = _DRAFT_STAGE.review_template,
) -> str:
    """The Reviewer prompt of one round; it is also the step-review input the owner approves."""

    prompt = (
        f"{_template(template)}\n\n"
        f"The use case is at {source_ref}. The requirements (round {number}) are at {draft_ref}."
    )
    if previous_feedback_ref:
        prompt += (
            f" Their author revised them after your feedback at {previous_feedback_ref}; the section "
            '"Review responses" explains findings the author did not apply. Review the whole revised document '
            "again, not only those findings."
        )
    return prompt


def review_task(prompt: str, output_ref: str) -> str:
    """The text sent to OpenCode for a review round: the prompt plus its file-delivery contract."""

    return (
        f"{prompt}\n\nIf nothing needs fixing, reply with exactly OK. Otherwise write the findings to "
        f"{output_ref} and reply with only that relative path."
    )


def run_markdown_requirement(
    *,
    settings: Settings,
    store: RuntimeStore,
    run_id: str,
    model: str,
    reviewer_model: str,
    source: UseCaseSource,
    catalog: RequirementSourceCatalog,
    step_review: dict[str, object],
) -> MarkdownRequirementResult:
    return _MarkdownFlow(
        settings=settings, store=store, run_id=run_id, model=model, reviewer_model=reviewer_model, source=source,
        catalog=catalog, step_review=step_review,
    ).run()


class _MarkdownFlow:
    def __init__(
        self,
        *,
        settings: Settings,
        store: RuntimeStore,
        run_id: str,
        model: str,
        reviewer_model: str,
        source: UseCaseSource,
        catalog: RequirementSourceCatalog,
        step_review: dict[str, object],
    ):
        self.store, self.run_id = store, run_id
        self.worker = OpenCodeWorker(settings, run_id=run_id, model=model)
        # The Reviewer judges the Refiner's work, so it runs on the stronger evaluation model.
        self.review_worker = OpenCodeWorker(settings, run_id=run_id, model=reviewer_model)
        self.review_mode = step_review if step_review.get("enabled") else {"review_input": False, "review_output": False}
        self.refiner_timeout = int(
            phase1_llm_call_options(settings, "refiner_synthesis").get("timeout_seconds") or settings.llm_timeout_seconds
        )
        self.reviewer_timeout = int(
            phase1_llm_call_options(settings, "coverage").get("timeout_seconds") or settings.llm_timeout_seconds
        )
        self.source_text = catalog.source_text_by_id[source.source_id]
        self.record_path = store.artifact_path(run_id, FLOW_RECORD)
        self.record = self._read_record()

    def run(self) -> MarkdownRequirementResult:
        source_ref = self.worker.stage("source", ".txt", self.source_text)
        draft_ref = self.worker.local_ref(DRAFT_NAME)
        rounds = self.record["rounds"]
        if not rounds:
            self._draft(draft_ref)
        self._review_loop(_DRAFT_STAGE, rounds, source_ref, draft_ref)

        reviewed = self._artifact_text(str(rounds[-1]["markdown_artifact"]))
        questions_artifact = self._questions(draft_ref, reviewed)
        questions = json.loads(self._artifact_text(questions_artifact))["questions"]
        artifacts = {
            "requirement_markdown_flow": self.store.relative(self.record_path),
            "requirement_markdown_questions": questions_artifact,
        }
        asked = [item for item in questions if item["proposed_disposition"] == "human_required"]
        answers = self._read_answers(asked)
        if asked and answers is None:
            raise MarkdownQuestionsPending(asked, artifacts)
        if answers:
            artifacts["requirement_markdown_answers"] = self.store.relative(self.store.artifact_path(self.run_id, ANSWERS_RECORD))
        # Agent defaults are written into the document and recorded as decisions too, so the trace shows every choice.
        applied = [*(answers or []), *agent_defaults(questions)]
        final_artifact = str(rounds[-1]["markdown_artifact"])
        if applied:
            if "answered" not in self.record:
                self._apply_answers(draft_ref, reviewed, applied)
            answered = self.record["answered"]
            answered_rounds = self.record.setdefault(
                "answered_rounds",
                [{"round": 1, "kind": "answers", "markdown_artifact": answered["markdown_artifact"], "sha256": answered["sha256"]}],
            )
            baseline = self._artifact_text(str(answered["markdown_artifact"]))
            # The owner was asked once; what the answers leave open the Refiner settles and marks as its own decision.
            self._review_loop(
                _ANSWERED_STAGE, answered_rounds, source_ref, draft_ref,
                lambda text: answered_document_issues(text, baseline, questions),
            )
            final_artifact = str(answered_rounds[-1]["markdown_artifact"])
        final_text = self._artifact_text(final_artifact)
        decided, decided_answers = agent_decisions(final_text, questions)
        trace = self._convert(
            source_ref, draft_ref, final_text, [*questions, *decided], [*applied, *decided_answers],
        )
        artifacts["requirement_markdown_draft"] = final_artifact
        return MarkdownRequirementResult(trace=trace, markdown_artifact=final_artifact, artifacts=artifacts)

    # Markdown rounds -------------------------------------------------------------------------------------------

    def _draft(self, draft_ref: str) -> None:
        prompt = f"{_template('requirement_markdown_draft.md')}\n\n## Use case\n\n{self.source_text}"

        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            self.worker.remove(draft_ref)  # A rejected draft is rewritten from scratch.
            task = (
                f"Write the requirements to {draft_ref}. Use the write tool to create the file and read it once "
                f"to check it. Then reply with only this relative path: {draft_ref}\n\n{prompt}"
            )
            completion, _ = self.worker.turn(
                task=task, timeout=self.refiner_timeout, role="refiner", output_ref=draft_ref, editable_refs=(draft_ref,),
            )
            return completion, {"draft": draft_ref}, (draft_ref,)

        self._reviewed("requirement-markdown-draft", "Requirement Refiner Markdown draft", prompt, call)
        self._add_round(_DRAFT_STAGE, self.record["rounds"], 1, "draft", draft_ref)

    def _review_loop(
        self,
        stage: _Stage,
        rounds: list[dict[str, object]],
        source_ref: str,
        draft_ref: str,
        extra_issues: Callable[[str], list[str]] | None = None,
    ) -> None:
        """Frame check, then the independent review, until the Reviewer accepts (both bounded)."""

        while True:
            current = rounds[-1]
            if "frame_issues" not in current:
                text = self._artifact_text(str(current["markdown_artifact"]))
                current["frame_issues"] = self._frame_issues(text) + (extra_issues(text) if extra_issues else [])
                self._write_record()
            if current["frame_issues"]:
                fixes = 0
                for item in reversed(rounds):
                    if item.get("kind") != "frame_fix":
                        break
                    fixes += 1
                if fixes >= FRAME_REPAIR_LIMIT:
                    raise HarnessError(
                        "requirement_frame_invalid",
                        "The requirements still break the frame after format fixes: " + "; ".join(current["frame_issues"]),
                        run_id=self.run_id,
                    )
                self._fix_frame(stage, rounds, current, draft_ref)
                continue
            review = current.get("review") or self._review(stage, rounds, current, source_ref, draft_ref)
            if review["status"] == "ok":
                return
            if sum(item.get("kind") == "revise" for item in rounds) >= REVIEW_REPAIR_LIMIT:
                raise HarnessError(
                    stage.unresolved,
                    f"The Reviewer still requires changes after {REVIEW_REPAIR_LIMIT} revisions. "
                    f"Feedback: {review['feedback_artifact']}.",
                    run_id=self.run_id,
                )
            self._revise(stage, rounds, current, draft_ref)

    def _fix_frame(self, stage: _Stage, rounds: list[dict[str, object]], current: dict[str, object], draft_ref: str) -> None:
        number = int(current["round"]) + 1
        prompt = (
            f"The Harness checked the frame of your requirements at {draft_ref} and found these problems:\n"
            + "\n".join(f"- {issue}" for issue in current["frame_issues"])
            + "\n\nEdit the document in place to fix them. Keep the layout of the requirements task and change "
            "nothing else."
        )
        self._edit_draft(
            f"{stage.call}-frame-r{number}", "Requirement Refiner format fix", prompt, draft_ref,
            self._artifact_text(str(current["markdown_artifact"])),
        )
        self._add_round(stage, rounds, number, "frame_fix", draft_ref)

    def _review(
        self,
        stage: _Stage,
        rounds: list[dict[str, object]],
        current: dict[str, object],
        source_ref: str,
        draft_ref: str,
    ) -> dict[str, object]:
        number = int(current["round"])
        reviewed = [item for item in rounds if item.get("review")]
        previous_feedback = str(reviewed[-1]["review"]["feedback_ref"]) if reviewed else None
        prompt = review_prompt(number, source_ref, draft_ref, previous_feedback, stage.review_template)

        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            output_ref = self.review_worker.reserve("review", ".md")
            task = review_task(prompt, output_ref)
            completion, _ = self.review_worker.turn(
                task=task, timeout=self.reviewer_timeout, role="reviewer", output_ref=output_ref,
                editable_refs=(output_ref,), allow_ok=True,
            )
            if completion.content == "OK":
                return completion, {"verdict": "ok"}, (draft_ref,)
            return completion, {"verdict": "needs_fix", "feedback": output_ref}, (draft_ref, output_ref)

        receipt = self._reviewed(f"{stage.call}-review-r{number}", "Requirement Markdown Reviewer", prompt, call)
        review: dict[str, object] = {"status": receipt["verdict"], "feedback_ref": None, "feedback_artifact": None}
        if receipt["verdict"] == "needs_fix":
            feedback_ref = str(receipt["feedback"])
            review["feedback_ref"] = feedback_ref
            review["feedback_artifact"] = self._archive(
                f"{stage.review_stem}_r{number}.md", self.worker.read(feedback_ref),
            )
        current["review"] = review
        self._write_record()
        return review

    def _revise(self, stage: _Stage, rounds: list[dict[str, object]], current: dict[str, object], draft_ref: str) -> None:
        number = int(current["round"]) + 1
        prompt = (
            f"{_template(stage.revise_template)}\n\n"
            f"The feedback is at {current['review']['feedback_ref']}. The requirements are at {draft_ref}."
        )
        self._edit_draft(
            f"{stage.call}-revise-r{number}", "Requirement Refiner Markdown revision", prompt, draft_ref,
            self._artifact_text(str(current["markdown_artifact"])),
        )
        self._add_round(stage, rounds, number, "revise", draft_ref)

    def _apply_answers(self, draft_ref: str, reviewed_text: str, answers: list[dict[str, str]]) -> None:
        lines = []
        for answer in answers:
            how = (
                "answered by the owner" if answer["source"] == "owner"
                else "your agent default; the owner was not asked" if answer["source"] == "agent_default"
                else f"the owner accepted your {answer['source'].replace('_', ' ')}"
            )
            lines.append(f"- {answer['question_id']} ({how}): {answer['answer']}")
        prompt = (
            _template("requirement_markdown_answers.md")
            .replace("{{DRAFT_REF}}", draft_ref)
            .replace("{{ANSWERS}}", "\n".join(lines))
        )
        self._edit_draft("requirement-markdown-answers", "Requirement Refiner answer update", prompt, draft_ref, reviewed_text)
        text = self.worker.read(draft_ref)
        self.record["answered"] = {
            "markdown_artifact": self._archive("requirement_markdown_draft_answered.md", text),
            "sha256": _sha(text),
        }
        self._write_record()

    def _edit_draft(self, prefix: str, label: str, prompt: str, draft_ref: str, base_text: str) -> None:
        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            self.worker.write(draft_ref, base_text)  # A rejected edit restarts from the accepted text.
            task = f"{prompt}\n\nAfter editing and checking the document, reply with only this relative path: {draft_ref}"
            if prompt.startswith(_REJECTED_PREFIX):
                # The session remembers the rejected edit, so say plainly that the file no longer contains it.
                task = (
                    f"Your previous edit was rejected and discarded: {draft_ref} is back to the version before that "
                    "edit. Make every change this task asks for again, and apply the operator's feedback.\n\n" + task
                )
            completion, _ = self.worker.turn(
                task=task, timeout=self.refiner_timeout, role="refiner", output_ref=draft_ref, editable_refs=(draft_ref,),
            )
            return completion, {"draft": draft_ref}, (draft_ref,)

        self._reviewed(prefix, label, prompt, call)

    def _add_round(self, stage: _Stage, rounds: list[dict[str, object]], number: int, kind: str, draft_ref: str) -> None:
        text = self.worker.read(draft_ref)
        rounds.append(
            {
                "round": number,
                "kind": kind,
                "markdown_artifact": self._archive(f"{stage.round_stem}_r{number}.md", text),
                "sha256": _sha(text),
            }
        )
        self._write_record()

    def _frame_issues(self, markdown: str) -> list[str]:
        return frame_issues(parse_markdown_frame(markdown), self.source_text, self.worker.repo_file_checker())

    # JSON files ------------------------------------------------------------------------------------------------

    def _questions(self, draft_ref: str, reviewed: str) -> str:
        prompt = _template("requirement_markdown_questions.md").replace("{{DRAFT_REF}}", draft_ref)

        def check(raw_text: str) -> list[str]:
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                return [f"The file is not valid JSON ({exc.msg} at line {exc.lineno})."]
            return question_file_issues(data.get("questions") if isinstance(data, dict) else None, reviewed)

        attempts = self._json_file(
            key="questions", prefix="requirement-markdown-questions", label="Requirement Refiner questions file",
            file_prefix="questions", artifact_stem="requirement_markdown_questions", first_prompt=prompt,
            draft_ref=draft_ref, check=check, error_code="requirement_questions_invalid",
        )
        return str(attempts[-1]["artifact"])

    def _convert(
        self,
        source_ref: str,
        draft_ref: str,
        markdown: str,
        questions: list[dict[str, object]],
        answers: list[dict[str, str]],
    ) -> dict[str, object]:
        prompt = (
            _template("requirement_json_conversion.md")
            .replace("{{DRAFT_REF}}", draft_ref)
            .replace("{{SOURCE_REF}}", source_ref)
        )
        repo_file_exists = self.worker.repo_file_checker()

        def check(raw_text: str) -> list[str]:
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                return [f"The file is not valid JSON ({exc.msg} at line {exc.lineno})."]
            return trace_issues(data, markdown, self.source_text, repo_file_exists, questions, answers)

        attempts = self._json_file(
            key="conversion", prefix="requirement-json-conversion", label="Requirement Refiner trace conversion",
            file_prefix="trace", artifact_stem="requirement_markdown_conversion", first_prompt=prompt,
            draft_ref=draft_ref, check=check, error_code="requirement_trace_invalid",
        )
        accepted = json.loads(self._artifact_text(str(attempts[-1]["artifact"])))
        if check(json.dumps(accepted)):
            raise HarnessError("requirement_trace_invalid", "The accepted trace no longer passes its checks.", run_id=self.run_id)
        return complete_trace(accepted, questions, answers)

    def _json_file(
        self,
        *,
        key: str,
        prefix: str,
        label: str,
        file_prefix: str,
        artifact_stem: str,
        first_prompt: str,
        draft_ref: str,
        check: Callable[[str], list[str]],
        error_code: str,
    ) -> list[dict[str, object]]:
        """One JSON file written by the Refiner, checked by the Harness and sent back with its issues (bounded)."""

        section = self.record.setdefault(key, {"attempts": []})
        attempts: list[dict[str, object]] = section["attempts"]
        while not (attempts and not attempts[-1]["issues"]):
            number = len(attempts) + 1
            if number > JSON_REPAIR_LIMIT + 1:
                raise HarnessError(
                    error_code, f"The {label} still fails the Harness checks: " + "; ".join(attempts[-1]["issues"]),
                    run_id=self.run_id,
                )
            if number == 1:
                prompt = first_prompt
            else:
                previous = attempts[-1]
                prompt = (
                    f"The JSON you wrote to {previous['json_ref']} failed these Harness checks:\n"
                    + "\n".join(f"- {issue}" for issue in previous["issues"])
                    + "\n\nWrite a corrected, complete JSON object. Fix what the checks name and keep everything else "
                    f"as the reviewed document at {draft_ref} states it. The rules of your first turn for this file "
                    "still apply; do not edit the document."
                )

            def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
                output_ref = self.worker.reserve(file_prefix, ".json")
                task = (
                    f"Write the JSON object to {output_ref}. Use the write tool, read the file back and fix any syntax "
                    "error. Put a single JSON object in the file, with no Markdown fences. "
                    f"Then reply with only this relative path: {output_ref}\n\n{prompt}"
                )
                completion, _ = self.worker.turn(
                    task=task, timeout=self.refiner_timeout, role="refiner", output_ref=output_ref,
                    editable_refs=(output_ref,),
                )
                return completion, {"json": output_ref}, (output_ref,)

            receipt = self._reviewed(f"{prefix}-a{number}", label, prompt, call)
            json_ref = str(receipt["json"])
            raw_text = self.worker.read(json_ref)
            artifact = self._archive(f"{artifact_stem}_a{number}.json", raw_text)
            attempts.append({"attempt": number, "json_ref": json_ref, "artifact": artifact, "issues": check(raw_text)})
            self._write_record()
        return attempts

    # Plumbing --------------------------------------------------------------------------------------------------

    def _reviewed(
        self,
        prefix: str,
        label: str,
        prompt: str,
        call: Callable[[str], tuple[LlmCompletion, dict[str, object], tuple[str, ...]]],
    ) -> dict[str, object]:
        """Run one OpenCode turn under step review. The receipt binds the files the owner reviewed."""

        def provide(*, system: str, prompt: str, **_options: object) -> LlmCompletion:
            completion, receipt, bound = call(prompt)
            if completion.finish_reason != "stop":
                raise HarnessError("llm_transport_failed", f"OpenCode did not finish the {label} turn.", run_id=self.run_id)
            receipt["files"] = {ref: _sha(self.worker.read(ref)) for ref in bound}
            return replace(completion, content=json.dumps(receipt, sort_keys=True))

        completion = ReviewableLlmClient(
            SimpleNamespace(complete_with_metadata=provide),
            store=self.store,
            run_id=self.run_id,
            review=self.review_mode,
            call_prefix=prefix,
            execution_dir=f"requirement_execution/{prefix}",
            error_prefix="requirement_refiner",
            label=label,
        ).complete_with_metadata(system=label, prompt=prompt)
        receipt = json.loads(completion.content)
        for ref, digest in receipt["files"].items():
            if _sha(self.worker.read(ref)) != digest:
                raise HarnessError(
                    "requirement_markdown_changed", f"{ref} changed after the {label} turn was reviewed.", run_id=self.run_id,
                )
        return receipt

    def _read_answers(self, asked: list[dict[str, object]]) -> list[dict[str, str]] | None:
        path = self.store.artifact_path(self.run_id, ANSWERS_RECORD)
        if not path.exists():
            return None
        answers = json.loads(path.read_text(encoding="utf-8"))["answers"]
        if sorted(str(item["question_id"]) for item in answers) != sorted(str(item["id"]) for item in asked):
            raise HarnessError(
                "requirement_answers_invalid", "The stored answers do not cover exactly the owner questions.",
                run_id=self.run_id,
            )
        return answers

    def _read_record(self) -> dict[str, object]:
        if not self.record_path.exists():
            return {"version": "requirement_markdown_flow_v1", "flow": MARKDOWN_FLOW_VERSION, "rounds": []}
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessError("requirement_session_not_resumable", "The Markdown flow record is unreadable.", run_id=self.run_id) from exc
        if not isinstance(record, dict) or record.get("flow") != MARKDOWN_FLOW_VERSION:
            raise HarnessError("requirement_session_not_resumable", "The Markdown flow record is invalid.", run_id=self.run_id)
        return record

    def _write_record(self) -> None:
        self.store.write_json_artifact(self.run_id, FLOW_RECORD, self.record)

    def _archive(self, filename: str, text: str) -> str:
        # Bytes, not text mode: the archive must equal the reviewed file exactly, line endings included.
        path = self.store.artifact_path(self.run_id, filename)
        data = text.encode("utf-8")
        if path.exists():
            if path.read_bytes() != data:
                raise HarnessError(
                    "requirement_revision_conflict", f"{filename} already exists with different content.", run_id=self.run_id,
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.store.relative(path)

    def _artifact_text(self, relative: str) -> str:
        path = self.store.artifact_path(self.run_id, Path(relative).name)
        return path.read_bytes().decode("utf-8")


def _template(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8").rstrip()


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
