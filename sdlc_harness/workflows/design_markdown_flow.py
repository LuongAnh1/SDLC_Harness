"""H-OC Phase 2: the Designer writes a Markdown design by system unit; reviewers judge it by scenario group.

Flow (plan `docs/maintenance/2026-11-26-h-oc-phase2-design-plan.md`, section 2, owner decisions of 2026-11-26):

1. The Designer (OpenCode, its own session) reads the approved requirements Markdown and the repository and writes
   `docs/sdlc-local/design.md`; the Harness checks the frame mechanically and asks for format fixes (bounded).
2. Review round 1, in parallel threads, each turn in its own reviewer session writing only its own file: one turn per
   scenario group (an AC ledger) and one global turn (invariants, data consistency, changes to existing behavior). The
   Harness checks every file mechanically (format fixes in the same session, bounded), merges them, and a
   consolidation turn writes the final findings F-n (or OK).
3. Revision 1: the Designer resolves every F-n once (fix or reject with evidence; the reviewer's severity is not used)
   and writes a response line per finding. The Harness checks frame and response, computes the re-review scope from the
   diff (`design_trace_v1.rereview_scope`) and runs the voided rows again in the same reviewer sessions; the
   consolidation session checks every old F-n (Fix checks) and numbers new findings.
4. Revision 2 (only when findings remain) is final: mechanical checks only, no review; the checkpoint says so and shows
   its diff and response.
5. The Designer converts the design to `design_trace_v1` JSON; the Harness checks it against the document and adds the
   Designer's own requirement decisions as decisions D-n.

Every OpenCode turn runs under the same step review as Phase 1 (one exchange per call prefix). Parallel turns pause one
at a time; a pending output is re-raised without a new LLM call until the owner approves it. After every turn the
Harness reads the session export and fails closed when a file tool touched a path outside the run worktree.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

from ..agents.designer import design_trace_v1 as dt
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmCompletion
from ..llm.opencode import OpenCodeWorker
from ..llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ..storage.runtime import RuntimeStore

DESIGN_FLOW_VERSION = "design_markdown_v1"
FLOW_RECORD = "design_markdown_flow_v1.json"
TRACE_ARTIFACT = "design_trace_v1_r1.json"
FLOW_DIAGRAM = "design_flow_v1.mmd"
DESIGN_NAME = "design.md"
FRAME_REPAIR_LIMIT = 2
FORMAT_REPAIR_LIMIT = 2
JSON_REPAIR_LIMIT = 2
# Owner, 2026-11-26: at most two Designer revisions; revision 1 is re-reviewed, revision 2 is final and not reviewed.
REVISION_LIMIT = 2
MAX_PARALLEL = 6
# shortcut: fixed turn timeouts (pilot: Designer up to 600 s, consolidation up to 1158 s); move to settings when tuned.
DESIGNER_TIMEOUT = 1800
REVIEWER_TIMEOUT = 1800
_PROMPTS = Path(__file__).resolve().parents[1] / "agents" / "designer" / "prompts"
# How ReviewableLlmClient starts the prompt of a call it reruns after the owner rejected its output.
_REJECTED_PREFIX = "Operator review rejected the previous response."


@dataclass(frozen=True)
class DesignMarkdownResult:
    trace: dict[str, object]
    markdown_artifact: str
    diagram_artifact: str
    artifacts: dict[str, str]
    open_findings: list[str]
    unreviewed_revision: dict[str, object] | None


@dataclass(frozen=True)
class _Turn:
    """One reviewer turn of a parallel batch: its record key, session role, step-review prefix and checks."""

    key: str
    role: str
    prefix: str
    label: str
    prompt: str
    output_prefix: str
    check: Callable[[str], list[str]]
    allow_ok: bool = False


def run_markdown_design(
    *,
    settings: Settings,
    store: RuntimeStore,
    run_id: str,
    model: str,
    reviewer_model: str,
    requirement_markdown: str,
    requirement_trace: dict[str, object],
    component_roots: dict[str, list[str]],
    step_review: dict[str, object],
) -> DesignMarkdownResult:
    return _DesignFlow(
        settings=settings, store=store, run_id=run_id, model=model, reviewer_model=reviewer_model,
        requirement_markdown=requirement_markdown, requirement_trace=requirement_trace,
        component_roots=component_roots, step_review=step_review,
    ).run()


class _DesignFlow:
    def __init__(
        self,
        *,
        settings: Settings,
        store: RuntimeStore,
        run_id: str,
        model: str,
        reviewer_model: str,
        requirement_markdown: str,
        requirement_trace: dict[str, object],
        component_roots: dict[str, list[str]],
        step_review: dict[str, object],
    ):
        self.store, self.run_id = store, run_id
        self.designer = OpenCodeWorker(settings, run_id=run_id, model=model)
        # Reviewers judge the Designer's work, so they run on the stronger evaluation model.
        self.reviewer = OpenCodeWorker(settings, run_id=run_id, model=reviewer_model)
        self.review_mode = step_review if step_review.get("enabled") else {"review_input": False, "review_output": False}
        self.requirement_markdown = requirement_markdown
        self.trace = requirement_trace
        self.scenarios = dt.scenarios(requirement_trace)
        self.component_roots = component_roots
        self.record_path = store.artifact_path(run_id, FLOW_RECORD)
        self.record = self._read_record()

    # Flow ----------------------------------------------------------------------------------------------------------

    def run(self) -> DesignMarkdownResult:
        req_ref = self.designer.stage("design-requirements", ".md", self.requirement_markdown)
        design_ref = self.designer.local_ref(DESIGN_NAME)
        if "routes" not in self.record:
            self.record["routes"] = dt.route_inventory(self.designer.worktree(), self.component_roots)
            self._write_record()
        self.repo_exists = self.designer.repo_file_checker()

        drafts = self.record.setdefault("drafts", [])
        if not drafts:
            self._draft(req_ref, design_ref)
        self._frame_loop(drafts, design_ref, "design-markdown", "design_markdown")
        round_one = self._review_round(req_ref, design_ref)
        findings = list(round_one["final"]["findings"])
        self.record.setdefault("revisions", [])
        unreviewed: dict[str, object] | None = None
        open_findings: list[str] = []
        if findings:
            first = self._revision(1, findings, str(round_one["final"]["ref"]), design_ref)
            round_two = self._rereview(req_ref, design_ref, round_one, first)
            remaining = round_two["remaining"]
            if remaining:
                second = self._revision(2, remaining, str(round_two["final"]["ref"]), design_ref)
                unreviewed = {
                    "revision": 2, "findings": remaining, "diff_artifact": second["diff_artifact"],
                    "response_artifact": second["response_artifact"], "reviewed": False,
                }
                open_findings = remaining
        final_artifact = self._current_design()
        final_text = self._artifact_text(final_artifact)
        trace = self._convert(design_ref, final_text)
        diagram = self._archive(FLOW_DIAGRAM, dt.flow_mermaid(final_text) + "\n")
        artifacts = {
            "design_markdown_flow": self.store.relative(self.record_path),
            "design_markdown": final_artifact,
            "design_flow_diagram": diagram,
        }
        return DesignMarkdownResult(
            trace=trace, markdown_artifact=final_artifact, diagram_artifact=diagram, artifacts=artifacts,
            open_findings=open_findings, unreviewed_revision=unreviewed,
        )

    # Designer turns --------------------------------------------------------------------------------------------------

    def _draft(self, req_ref: str, design_ref: str) -> None:
        lines = [
            f"- {sc} ({info['kind'][0]}): clauses {', '.join(info['clauses'])}; ACs {', '.join(info['acs']) or 'none'}"
            for sc, info in self.scenarios.items()
        ]
        routes = "\n".join(f"- `{r[4:]}` (web)" if r.startswith("web ") else f"- `{r}`" for r in self.record["routes"])
        prompt = (
            _template("design_markdown_draft.md")
            .replace("{{REQ_REF}}", req_ref).replace("{{DESIGN_REF}}", design_ref)
            .replace("{{SCENARIOS}}", "\n".join(lines)).replace("{{ROUTES}}", routes)
        )

        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            self.designer.remove(design_ref)  # A rejected draft is rewritten from scratch.
            task = f"{prompt}\n\nAfter writing and checking the design, reply with only this relative path: {design_ref}"
            completion = self._designer_turn(task, design_ref, (design_ref,))
            return completion, {"design": design_ref}, (design_ref,)

        self._reviewed(self.designer, "design-markdown-draft", "Designer Markdown draft", prompt, call)
        self._add_design(self.record["drafts"], "draft", design_ref, "design_markdown")

    def _frame_loop(
        self,
        attempts: list[dict[str, object]],
        design_ref: str,
        prefix: str,
        stem: str,
        extra: Callable[[str], list[str]] | None = None,
        extra_refs: tuple[str, ...] = (),
        after_fix: Callable[[], None] | None = None,
    ) -> None:
        """Frame check of the latest design version; format fixes by the Designer until it passes (bounded). In a
        revision the response file is editable too (`extra_refs`) and re-archived after each fix (`after_fix`)."""

        while True:
            current = attempts[-1]
            if "frame_issues" not in current:
                text = self._artifact_text(str(current["artifact"]))
                current["frame_issues"] = self._frame_issues(text) + (extra(text) if extra else [])
                self._write_record()
            if not current["frame_issues"]:
                return
            fixes = 0
            for item in reversed(attempts):
                if item.get("kind") != "frame_fix":
                    break
                fixes += 1
            if fixes >= FRAME_REPAIR_LIMIT:
                raise HarnessError(
                    "design_frame_invalid",
                    "The design still breaks the frame after format fixes: " + "; ".join(current["frame_issues"]),
                    run_id=self.run_id,
                )
            number = len(attempts) + 1
            prompt = (
                f"The Harness checked the frame of your design at {design_ref} mechanically and found these issues:\n"
                + "\n".join(f"- {issue}" for issue in current["frame_issues"])
                + "\n\nFix only these: complete or correct the frame fields and links (Uses, Serves, Decided by, "
                "Scenario paths, route mentions, requirement decisions) so that they state what the units already do. "
                "Do not change any behavior, branch, decision or ID for this."
                + "".join(f" You may also edit {ref} to fix an issue about it." for ref in extra_refs)
            )
            self._edit_design(f"{prefix}-frame-r{number}", "Designer format fix", prompt, design_ref,
                              (design_ref, *extra_refs), self._artifact_text(str(current["artifact"])))
            self._add_design(attempts, "frame_fix", design_ref, stem)
            if after_fix:
                after_fix()

    def _revision(self, number: int, findings: list[str], findings_ref: str, design_ref: str) -> dict[str, object]:
        """One Designer revision: every listed finding fixed or rejected with evidence, a response line per finding."""

        revisions: list[dict[str, object]] = self.record["revisions"]
        if len(revisions) < number:
            if number > REVISION_LIMIT:
                raise HarnessError("design_revision_limit", "The design reached its revision limit.", run_id=self.run_id)
            revisions.append({"revision": number, "findings": findings, "before_artifact": self._current_design(), "attempts": []})
            self._write_record()
        revision = revisions[number - 1]
        attempts: list[dict[str, object]] = revision["attempts"]
        if not attempts:
            final = "" if number < REVISION_LIMIT else (
                " This is the last revision; it is not reviewed again, so change only what the findings need."
            )
            prompt = (
                f"The review of your design is at {findings_ref}. Revise {design_ref} once so that it resolves these "
                f"findings: {', '.join(findings)}. For each finding either change the design, or, when the finding is "
                "wrong, keep the design and say why with evidence (the requirement item, the design section, the "
                "repository file). The severity a finding states does not matter: every finding is either fixed or "
                f"rejected with evidence.{final}\n\n"
                "Keep the layout and the IDs: change an existing U-n, DD-n or Q-n in place, give a new one the next free "
                "number, never renumber or reuse an ID, and keep Uses, Scenario paths and Endpoint branches consistent "
                "with the units. The `Answer:` lines of the requirements' questions stay fixed; a new observable decision "
                "is a requirement decision Q-n marked on every unit it decides, as before.\n\n"
                "Then write the response file with one line per finding:\n"
                "- F-1: fixed - <what changed, naming the U-n/DD-n/Q-n>\n"
                "- F-2: rejected - <why the finding is wrong, with evidence>"
            )
            base = self._artifact_text(str(revision["before_artifact"]))
            receipt = self._edit_design(
                f"design-revise-{number}", f"Designer revision {number}", prompt, design_ref, (design_ref,), base,
                response=True,
            )
            response_text = self.designer.read(str(receipt["response"]))
            revision["response_artifact"] = self._archive(f"design_response_rev{number}.md", response_text)
            revision["response_ref"] = str(receipt["response"])
            self._add_design(attempts, "revision", design_ref, f"design_markdown_rev{number}")
        before = self._artifact_text(str(revision["before_artifact"]))

        def response_issues(_text: str) -> list[str]:
            answered = dt.responses(self._artifact_text(str(revision["response_artifact"])))
            issues = [f"{f}: no response line in the response file." for f in findings if f not in answered]
            issues += [f"{f}: rejected without a reason." for f in findings if answered.get(f, ("", "x"))[0] == "rejected" and not answered[f][1]]
            return issues

        def decision_issues(text: str) -> list[str]:
            # A changed DD/Q that no unit applying it follows: the Designer updates the units (no spreading in scope).
            return dt.rereview_scope(before, text, "", "", self.scenarios, [])["decision_issues"]

        def rearchive_response() -> None:
            text = self.designer.read(str(revision["response_ref"]))
            revision["response_artifact"] = self._archive(f"design_response_rev{number}_a{len(attempts)}.md", text)
            self._write_record()

        self._frame_loop(attempts, design_ref, f"design-revise-{number}", f"design_markdown_rev{number}",
                         lambda text: response_issues(text) + decision_issues(text),
                         (str(revision["response_ref"]),), rearchive_response)
        if "diff_artifact" not in revision:
            after = self._artifact_text(self._current_design())
            revision["diff_artifact"] = self._archive(
                f"design_diff_rev{number}.diff",
                dt.design_diff(before, after, f"design before revision {number}", DESIGN_NAME) + "\n",
            )
            self._write_record()
        return revision

    def _edit_design(
        self,
        prefix: str,
        label: str,
        prompt: str,
        design_ref: str,
        editable: tuple[str, ...],
        base_text: str,
        *,
        response: bool = False,
    ) -> dict[str, object]:
        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            self.designer.write(design_ref, base_text)  # A rejected edit restarts from the accepted text.
            receipt: dict[str, object] = {"design": design_ref}
            refs = editable
            output = design_ref
            if response:
                output = self.designer.reserve("design-response", ".md")
                receipt["response"] = output
                refs = (*editable, output)
                task = f"{prompt}\n\nWrite the response file to {output}. Reply with only this relative path: {output}"
            else:
                task = f"{prompt}\n\nAfter editing and checking the design, reply with only this relative path: {design_ref}"
            if prompt.startswith(_REJECTED_PREFIX):
                # The session remembers the rejected edit, so say plainly that the file no longer contains it.
                task = (
                    f"Your previous edit was rejected and discarded: {design_ref} is back to the version before that "
                    "edit. Make every change this task asks for again, and apply the operator's feedback.\n\n" + task
                )
            completion = self._designer_turn(task, output, refs)
            return completion, receipt, refs

        return self._reviewed(self.designer, prefix, label, prompt, call)

    def _designer_turn(self, task: str, output_ref: str, editable: tuple[str, ...]) -> LlmCompletion:
        completion, session_id = self.designer.turn(
            task=task, timeout=DESIGNER_TIMEOUT, role="designer", output_ref=output_ref, editable_refs=editable,
        )
        self._audit(self.designer, session_id, "designer")
        return completion

    # Review rounds -------------------------------------------------------------------------------------------------

    def _common(self, req_ref: str, design_ref: str) -> str:
        return _template("design_review_common.md").replace("{{REQ_REF}}", req_ref).replace("{{DESIGN_REF}}", design_ref)

    def _review_round(self, req_ref: str, design_ref: str) -> dict[str, object]:
        review = self.record.setdefault("review", {})
        if "design_artifact" not in review:
            design = self._current_design()
            review.update({"design_artifact": design, "groups": dt.groups(self._artifact_text(design), self.scenarios), "turns": {}})
            self._write_record()
        if review.get("final"):
            return review
        text = self._artifact_text(str(review["design_artifact"]))
        common = self._common(req_ref, design_ref)
        by_id = {str(ac["id"]): ac for ac in self.trace.get("acceptance_criteria") or []}
        rows = dt.scenario_rows(text)
        unit_blocks = dt.units(text)
        turns = []
        for index, members in enumerate(review["groups"], start=1):
            acs = sorted({ac for sc in members for ac in self.scenarios[sc]["acs"]}, key=dt._num)
            criteria = "\n".join(
                f"- {ac}: {by_id[ac].get('text')}\n  API: {by_id[ac].get('api')}\n  Web: {by_id[ac].get('web')}" for ac in acs
            )
            paths = "\n".join(f"- {sc}: {rows[sc][0][1] if sc in rows else '(no path)'}" for sc in members)
            on_paths = {u for sc in members if sc in rows for u in dt._UNIT.findall(rows[sc][0][1])} & set(unit_blocks)
            below = sorted(dt.reach({u: dt.uses(b) for u, b in unit_blocks.items()}, on_paths) - on_paths, key=dt._num)
            paths += f"\n- Units these paths reach through Uses: {', '.join(below) or 'none'}"
            prompt = (
                f"{common}\n\nThis turn is group {index} of {len(review['groups'])}: scenarios {', '.join(members)}. For "
                "each acceptance criterion below, follow every path of these scenarios through the design (Scenario "
                "paths, the units and their branches, Endpoint branches) and check that the criterion holds in its API "
                "and Web parts, including refusal and error branches and the decided values. A unit these scenarios "
                f"share with other scenarios must also stay consistent with how those use it.\n\nPaths of this group "
                f"(from the design):\n{paths}\n\nAcceptance criteria of this group (from the requirements):\n{criteria}\n\n"
                f"Write this section:\n\n## Group {index} ({', '.join(members)})\n"
                "| AC | Branches checked | Verdict | Evidence |\n| --- | --- | --- | --- |\n"
                "(one row per acceptance criterion above; Verdict is `ok` or `problem: G<group>-<k>, ...`)\n\n"
                f"Findings:\n1. G{index}-1 ... (or `Findings: none`)"
            )
            turns.append(_Turn(
                f"group-{index}", f"reviewer-g{index}", f"design-review-r1-g{index}", f"Design review group {index}",
                prompt, f"design-review-r1-g{index}", lambda body, i=index, a=acs: dt.group_ledger_issues(i, a, body),
            ))
        turns.append(_Turn(
            "global", "reviewer-global", "design-review-r1-global", "Design review global",
            self._global_prompt(common, text), "design-review-r1-global", dt.global_issues,
        ))
        self._batch(review["turns"], turns)
        merged = "\n\n".join(self._artifact_text(str(review["turns"][t.key]["artifact"])).strip() for t in turns) + "\n"
        review["merged_artifact"] = self._archive("design_review_r1_merged.md", merged)
        merged_ref = self.reviewer.stage("design-review-r1-merged", ".md", merged)
        prompt = (
            f"All groups and the global checks are in {merged_ref}. Read it again with the design at {design_ref}. Write "
            "the final findings: merge duplicates into one finding that names all affected IDs and the source finding "
            "numbers; drop a finding that is wrong on a second reading and list it under `## Dropped` with the reason; "
            "add contradictions between groups (a unit described or used inconsistently across scenarios). Number the "
            "final findings F-1, F-2, ... and keep each severity."
        )

        def check(body: str) -> list[str]:
            return [] if dt.finding_ids(body) else ["No finding is numbered F-n; number every final finding F-1, F-2, ..."]

        final_turn = _Turn("final", "reviewer-final", "design-review-r1-final", "Design review consolidation", prompt,
                           "design-review-r1-final", check, allow_ok=True)
        self._batch(review["turns"], [final_turn])
        done = review["turns"]["final"]
        review["final"] = {
            "ref": done.get("ref"), "artifact": done.get("artifact"),
            "findings": dt.finding_ids(self._artifact_text(str(done["artifact"]))) if done.get("artifact") else [],
        }
        self._write_record()
        return review

    def _global_prompt(self, common: str, text: str) -> str:
        unit_blocks = dt.units(text)
        invariants = "\n".join(
            f'- {c["id"]} ({c["kind"]}): "{c.get("quote") or " ".join(c.get("quotes") or [])}"'
            for c in self.trace.get("uc_clauses") or [] if c.get("kind") in {"alternative", "exception", "exit", "quality"}
        )
        kind = {k: dt.unit_kind(v) for k, v in unit_blocks.items()}
        data_units = ", ".join(f"{u} ({kind[u]})" for u in sorted(unit_blocks, key=dt._num) if kind[u] in dt.DATA_KINDS)
        modified = "\n".join(
            f"- {u} ({kind[u]}): {dt._DEF.search(unit_blocks[u]).group(2).strip()}"
            for u in sorted(unit_blocks, key=dt._num) if dt.field(unit_blocks[u], "Status").startswith("modify")
        )
        return (
            f"{common}\n\nThis turn checks what no single scenario owns. Write a section `## Global` with these three "
            "tables, then the findings.\n\n"
            "1. Invariants. Each use-case clause below states a rule that must hold everywhere, not only on its own "
            "scenario path. For each clause, check every unit that reads or writes the data it is about, including the "
            "seed, the migration and the web screens.\n| UC | Units checked | Verdict | Evidence |\n\n"
            f"Clauses:\n{invariants}\n\nUnits that read or write stored data (Kind data, migration, seed, query): {data_units}\n\n"
            "2. Changes to existing behavior. List every place where the design changes what the repository does today "
            "(status, body, validation, order, permission, stored data, existing tests) and check that a requirement item "
            f"asks for it. The units that modify existing code are:\n{modified}\n"
            "| Unit | Existing behavior (file) | Change | Required by | Verdict |\n\n"
            "3. Data consistency. Check that the schema, the migration, the seed and the queries describe the same stored "
            "data (columns, defaults, indexes, constraints, the state of existing rows) and that the stored data satisfies "
            "the requirements after the migration and after every seed run.\n| Check | Verdict | Evidence |\n\n"
            "Verdict is `ok` or `problem: GL-<k>`. Findings: `1. GL-1 ...` (or `Findings: none`)."
        )

    def _rereview(
        self, req_ref: str, design_ref: str, round_one: dict[str, object], revision: dict[str, object],
    ) -> dict[str, object]:
        """Round 2 after revision 1: the Harness decides the scope; the same sessions re-check only the voided rows."""

        second = self.record.setdefault("rereview", {"turns": {}})
        before = self._artifact_text(str(revision["before_artifact"]))
        after = self._artifact_text(self._current_design())
        response = self._artifact_text(str(revision["response_artifact"]))
        if "scope" not in second:
            ledgers = {
                int(key.split("-")[1]): self._artifact_text(str(turn["artifact"]))
                for key, turn in round_one["turns"].items() if key.startswith("group-")
            }
            global_text = self._artifact_text(str(round_one["turns"]["global"]["artifact"]))
            scope = dt.rereview_scope(
                before, after, self._artifact_text(str(round_one["final"]["artifact"])), response, self.scenarios,
                round_one["groups"], ledgers, global_text,
            )
            second["scope"] = scope
            second["scope_artifact"] = self._archive("design_scope_rev1.json", json.dumps(scope, ensure_ascii=False, indent=1) + "\n")
            self._write_record()
        scope = second["scope"]
        if second.get("final"):
            return second
        before_ref = self.reviewer.stage("design-before-rev1", ".md", before)
        context = (
            f"The Designer revised the design after the previous review. {design_ref} is the revised design and "
            f"{before_ref} is the version you reviewed. The Designer's response to the findings:\n{response.strip()}\n\n"
            f"The changes (unified diff):\n```diff\n{dt.design_diff(before, after, before_ref, design_ref)}\n```\n"
        )
        turns = []
        for group in (g for g in scope["groups"] if g["rerun"]):
            index = group["group"]
            rows = "\n".join(f"- {ac}: {why}" for ac, why in group["recheck_acs"].items())
            prompt = (
                f"{context}\nYour earlier verdicts for the acceptance criteria below no longer count, because the units "
                "their evidence relied on changed. Check each of them again against the revised design, following every "
                f"path of scenarios {', '.join(group['scenarios'])} through it as if for the first time, including "
                "refusal and error branches and the decided values, and report anything the revision broke. The other "
                f"criteria of your group keep their verdicts; do not repeat them.\n\nCriteria to check again (and why):\n"
                f"{rows}\n\nWrite this section:\n\n## Group {index} round 2 ({', '.join(group['scenarios'])})\n"
                "| AC | Branches checked | Verdict | Evidence |\n| --- | --- | --- | --- |\n"
                f"(one row per criterion above; Verdict is `ok` or `problem: R2-G{index}-<k>, ...`)\n\n"
                f"Findings:\n1. R2-G{index}-1 ... (or `Findings: none`)"
            )
            only = {"groups": [group], "global": {"rerun": False}, "fix_checks": []}
            turns.append(_Turn(
                f"group-{index}", f"reviewer-g{index}", f"design-review-r2-g{index}", f"Design re-review group {index}",
                prompt, f"design-review-r2-g{index}",
                lambda body, i=index, s=only: dt.round_issues(s, {f"group-{i}": body}),
            ))
        if scope["global"]["rerun"]:
            rows = "\n".join(f"- {row['row']} (cites {', '.join(row['cites'])})" for row in scope["global"]["recheck_rows"])
            rows = rows or "- every row of your three tables (no earlier global review is on record)"
            prompt = (
                f"{context}\nYour earlier verdicts for the rows of your three tables listed below no longer count, "
                "because units they cite changed. Check each again against the revised design (and the repository where "
                "the design relies on it) as if for the first time, including the seed, the migration and the web "
                "screens, and report anything the revision broke. The other rows keep their verdicts; do not repeat "
                f"them.\n\nRows to check again:\n{rows}\n\nWrite this section:\n\n## Global round 2\n"
                "| Row | Units checked | Verdict | Evidence |\n| --- | --- | --- | --- |\n"
                "(one row per row above, named as listed; Verdict is `ok` or `problem: R2-GL-<k>, ...`)\n\n"
                "Findings:\n1. R2-GL-1 ... (or `Findings: none`)"
            )
            only = {"groups": [], "global": scope["global"], "fix_checks": []}
            turns.append(_Turn(
                "global", "reviewer-global", "design-review-r2-global", "Design re-review global", prompt,
                "design-review-r2-global", lambda body, s=only: dt.round_issues(s, {"global": body}),
            ))
        self._batch(second["turns"], turns)
        merged = "\n\n".join(self._artifact_text(str(second["turns"][t.key]["artifact"])).strip() for t in turns) + "\n"
        second["merged_artifact"] = self._archive("design_review_r2_merged.md", merged)
        merged_ref = self.reviewer.stage("design-review-r2-merged", ".md", merged or "No group or global row was re-checked.\n")
        previous = ", ".join(check["finding"] for check in scope["fix_checks"])
        last = max((dt._num(check["finding"]) for check in scope["fix_checks"]), default=0)
        prompt = (
            f"{context}\nThe re-checks of this round are in {merged_ref}. Write:\n\n"
            "## Fix checks\n| Finding | Status | Evidence |\n| --- | --- | --- |\n"
            f"(one row for each of your previous findings {previous}; Status is `fixed`, `not fixed`, `rejection accepted` "
            "or `rejection refused`; judge the revised design itself, not only the Designer's response)\n\n"
            "## Findings\nConsolidate the new findings of this round as before: merge duplicates naming all affected IDs "
            "and the source finding numbers, drop a finding that is wrong on a second reading and list it under "
            "`## Dropped` with the reason, add contradictions between groups, and number them "
            f"F-{last + 1}, F-{last + 2}, ... with their severity. A previous finding that is not fixed stays in the "
            "fix-check table, not here. Write `Findings: none` if none remain."
        )
        only = {"groups": [], "global": {"rerun": False}, "fix_checks": scope["fix_checks"]}
        final_turn = _Turn("final", "reviewer-final", "design-review-r2-final", "Design re-review consolidation", prompt,
                           "design-review-r2-final", lambda body, s=only: dt.round_issues(s, {"final": body}))
        self._batch(second["turns"], [final_turn])
        final_text = self._artifact_text(str(second["turns"]["final"]["artifact"]))
        old = {check["finding"] for check in scope["fix_checks"]}
        statuses = _fix_statuses(final_text)
        remaining = sorted(
            [f for f in old if statuses.get(f) in {"not fixed", "rejection refused"}]
            + [f for f in dt.finding_ids(final_text) if f not in old],
            key=dt._num,
        )
        second["final"] = {"ref": second["turns"]["final"]["ref"], "artifact": second["turns"]["final"]["artifact"],
                           "fix_checks": statuses}
        second["remaining"] = remaining
        self._write_record()
        return second

    def _batch(self, section: dict[str, object], turns: list[_Turn]) -> None:
        """Run the turns not yet done in parallel threads; record what finished; raise a pause or an error afterwards."""

        todo = [turn for turn in turns if not section.get(turn.key, {}).get("done")]
        if not todo:
            return
        failures: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(todo))) as pool:
            futures = [pool.submit(self._reviewer_turn, section, turn) for turn in todo]
            for future in futures:
                error = future.exception()
                if error is not None:
                    failures.append(error)
        self._write_record()
        errors = [error for error in failures if not isinstance(error, LlmStepReviewRequired)]
        if errors:
            raise errors[0]
        if failures:
            raise sorted(failures, key=lambda error: str(error.progress["call_id"]))[0]

    def _reviewer_turn(self, section: dict[str, object], turn: _Turn) -> None:
        entry = section.setdefault(turn.key, {"attempts": []})
        attempts: list[dict[str, object]] = entry["attempts"]
        while True:
            if attempts and not attempts[-1]["issues"]:
                entry.update({"done": True, "ref": attempts[-1]["ref"], "artifact": attempts[-1]["artifact"]})
                return
            number = len(attempts) + 1
            if number > FORMAT_REPAIR_LIMIT + 1:
                raise HarnessError(
                    "design_review_format_invalid",
                    f"{turn.label} still fails the Harness checks: " + "; ".join(attempts[-1]["issues"]),
                    run_id=self.run_id,
                )
            if number == 1:
                prompt, output = turn.prompt, None
            else:
                previous = attempts[-1]
                output = str(previous["ref"])
                prompt = (
                    f"The Harness checked {output} mechanically and found these issues:\n"
                    + "\n".join(f"- {issue}" for issue in previous["issues"])
                    + "\n\nRewrite that file so that it passes: fix only the format and the listed rows, keep every "
                    "verdict and finding you wrote."
                )

            def call(prompt: str, output: str | None = output) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
                ref = output or self.reviewer.reserve(turn.output_prefix, ".md")
                ok = " If nothing needs fixing, reply with exactly OK." if turn.allow_ok and output is None else ""
                task = f"{prompt}\n\nWrite the result to {ref}.{ok} Otherwise reply with only this relative path: {ref}"
                completion, session_id = self.reviewer.turn(
                    task=task, timeout=REVIEWER_TIMEOUT, role=turn.role, output_ref=ref, editable_refs=(ref,),
                    allow_ok=turn.allow_ok and output is None, exclusive=False,
                )
                self._audit(self.reviewer, session_id, turn.role)
                if completion.content == "OK":
                    return completion, {"verdict": "ok"}, ()
                return completion, {"verdict": "file", "ref": ref}, (ref,)

            suffix = "" if number == 1 else f"-fix{number - 1}"
            receipt = self._reviewed(self.reviewer, f"{turn.prefix}{suffix}", turn.label, prompt, call)
            if receipt["verdict"] == "ok":
                attempts.append({"attempt": number, "ref": None, "artifact": None, "issues": []})
                continue
            text = self.reviewer.read(str(receipt["ref"]))
            attempts.append({
                "attempt": number, "ref": receipt["ref"],
                "artifact": self._archive(f"{turn.output_prefix.replace('-', '_')}_a{number}.md", text),
                "issues": turn.check(text),
            })

    # JSON conversion -------------------------------------------------------------------------------------------------

    def _convert(self, design_ref: str, markdown: str) -> dict[str, object]:
        section = self.record.setdefault("conversion", {"attempts": []})
        attempts: list[dict[str, object]] = section["attempts"]
        while not (attempts and not attempts[-1]["issues"]):
            number = len(attempts) + 1
            if number > JSON_REPAIR_LIMIT + 1:
                raise HarnessError(
                    "design_trace_invalid", "The design trace still fails the Harness checks: " + "; ".join(attempts[-1]["issues"]),
                    run_id=self.run_id,
                )
            if number == 1:
                prompt = _template("design_json_conversion.md").replace("{{DESIGN_REF}}", design_ref)
            else:
                previous = attempts[-1]
                prompt = (
                    f"The JSON you wrote to {previous['json_ref']} failed these Harness checks:\n"
                    + "\n".join(f"- {issue}" for issue in previous["issues"])
                    + f"\n\nWrite a corrected, complete JSON object. Keep everything as the design at {design_ref} states "
                    "it; the rules of your first turn for this file still apply; do not edit the design."
                )

            def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
                output = self.designer.reserve("design-trace", ".json")
                task = (
                    f"Write the JSON object to {output}. Use the write tool, read the file back and fix any syntax error. "
                    f"Put a single JSON object in the file, with no Markdown fences. Then reply with only this relative "
                    f"path: {output}\n\n{prompt}"
                )
                return self._designer_turn(task, output, (output,)), {"json": output}, (output,)

            receipt = self._reviewed(self.designer, f"design-json-conversion-a{number}", "Designer trace conversion", prompt, call)
            raw = self.designer.read(str(receipt["json"]))
            try:
                data = json.loads(raw)
                issues = dt.design_trace_issues(data, markdown, self.trace)
            except json.JSONDecodeError as exc:
                issues = [f"The file is not valid JSON ({exc.msg} at line {exc.lineno})."]
            attempts.append({"attempt": number, "json_ref": receipt["json"],
                             "artifact": self._archive(f"design_trace_conversion_a{number}.json", raw), "issues": issues})
            self._write_record()
        accepted = json.loads(self._artifact_text(str(attempts[-1]["artifact"])))
        return dt.complete_design_trace(accepted, markdown, self.trace)

    # Plumbing ------------------------------------------------------------------------------------------------------

    def _frame_issues(self, text: str) -> list[str]:
        return dt.frame_issues(text, self.trace, self.record["routes"], self.repo_exists)

    def _audit(self, worker: OpenCodeWorker, session_id: str, role: str) -> None:
        """Fail closed when a file tool of the turn touched a path outside the run worktree (owner, 2026-11-26)."""

        outside = worker.paths_outside_worktree(session_id)
        if outside is None:
            self.record.setdefault("audit_unavailable", []).append({"role": role, "session_id": session_id})
            return
        if outside:
            raise HarnessError(
                "design_read_outside_worktree",
                f"The {role} turn used paths outside the run worktree: {outside[:5]}.",
                run_id=self.run_id,
            )

    def _add_design(self, attempts: list[dict[str, object]], kind: str, design_ref: str, stem: str) -> None:
        text = self.designer.read(design_ref)
        artifact = self._archive(f"{stem}_a{len(attempts) + 1}.md", text)
        attempts.append({"kind": kind, "artifact": artifact, "sha256": _sha(text)})
        self.record["current_design"] = artifact
        self._write_record()

    def _current_design(self) -> str:
        return str(self.record["current_design"])

    def _reviewed(
        self,
        worker: OpenCodeWorker,
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
            receipt["files"] = {ref: _sha(worker.read(ref)) for ref in bound}
            return replace(completion, content=json.dumps(receipt, sort_keys=True))

        completion = ReviewableLlmClient(
            SimpleNamespace(complete_with_metadata=provide),
            store=self.store,
            run_id=self.run_id,
            review=self.review_mode,
            call_prefix=prefix,
            execution_dir=f"design_execution/{prefix}",
            error_prefix="design_markdown",
            label=label,
        ).complete_with_metadata(system=label, prompt=prompt)
        receipt = json.loads(completion.content)
        for ref, digest in receipt["files"].items():
            if _sha(worker.read(ref)) != digest:
                raise HarnessError("design_markdown_changed", f"{ref} changed after the {label} turn was reviewed.", run_id=self.run_id)
        return receipt

    def _read_record(self) -> dict[str, object]:
        if not self.record_path.exists():
            return {"version": "design_markdown_flow_v1", "flow": DESIGN_FLOW_VERSION, "source": "harness"}
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessError("design_session_not_resumable", "The Markdown design record is unreadable.", run_id=self.run_id) from exc
        if not isinstance(record, dict) or record.get("flow") != DESIGN_FLOW_VERSION:
            raise HarnessError("design_session_not_resumable", "The Markdown design record is invalid.", run_id=self.run_id)
        return record

    def _write_record(self) -> None:
        self.store.write_json_artifact(self.run_id, FLOW_RECORD, self.record)

    def _archive(self, filename: str, text: str) -> str:
        # Bytes, not text mode: the archive must equal the reviewed file exactly, line endings included.
        path = self.store.artifact_path(self.run_id, filename)
        data = text.encode("utf-8")
        if path.exists():
            if path.read_bytes() != data:
                raise HarnessError("design_revision_conflict", f"{filename} already exists with different content.", run_id=self.run_id)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.store.relative(path)

    def _artifact_text(self, relative: str) -> str:
        return self.store.artifact_path(self.run_id, Path(relative).name).read_bytes().decode("utf-8")


def _fix_statuses(text: str) -> dict[str, str]:
    table = re.search(r"^## Fix checks\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    statuses = {}
    for line in (table.group(1) if table else "").splitlines():
        cells = [cell.strip(" *") for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"F-\d+", cells[0]):
            statuses[cells[0]] = cells[1].lower()
    return statuses


def _template(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8").rstrip()


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
