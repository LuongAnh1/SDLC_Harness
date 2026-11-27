"""H-OC Phase 3 (`plan_graph_v1`): build the Plan graph, run the one Planner turn, store the plan for the checkpoint.

The Harness reads the repository facts once (archived, so a resumed run plans from the same facts), builds the plan with
every `reliance` candidate assumed kept, and fails closed if any mechanical check (G0-G7) fails before an LLM is called.
The Planner (OpenCode, the worker model) keeps or drops each candidate and confirms a base option in one turn; format
problems get at most two fixes in the same session. The final plan is rebuilt with the kept edges and the confirmed
option, checked again and archived with its report and task diagram. Every turn runs under step review, and a turn that
touched a path outside the run worktree fails closed.

Plan `docs/maintenance/2026-11-27-h-oc-phase3-plan-graph.md` sections 3, 6, 10.3-10.6.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from types import SimpleNamespace

from ..agents.developer import plan_graph_v1 as pg
from ..agents.developer import plan_planner_v1 as pp
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmCompletion
from ..llm.opencode import OpenCodeWorker
from ..llm.step_review import ReviewableLlmClient
from ..repo_context.plan_facts import RepoFacts, repo_facts_for_profile
from ..storage.runtime import RuntimeStore

PLAN_FLOW_VERSION = "plan_graph_v1"
FLOW_RECORD = "plan_graph_flow_v1.json"
FACTS_ARTIFACT = "plan_repo_facts_v1.json"
CANDIDATES_ARTIFACT = "plan_graph_candidates_v1.json"
PLAN_ARTIFACT = "plan_graph_v1.json"
REPORT_ARTIFACT = "plan_graph_v1.md"
DIAGRAM_ARTIFACT = "plan_task_graph_v1.mmd"
PLANNER_TIMEOUT = 1800
FORMAT_REPAIR_LIMIT = 2


@dataclass(frozen=True)
class PlanGraphResult:
    plan: dict[str, object]
    artifacts: dict[str, str]


def run_plan_graph(
    *,
    settings: Settings,
    store: RuntimeStore,
    run_id: str,
    model: str,
    design_trace: dict[str, object],
    design_markdown: str,
    project_profile_id: str,
    k: int,
    step_review: dict[str, object],
) -> PlanGraphResult:
    return _PlanFlow(
        settings=settings, store=store, run_id=run_id, model=model, design_trace=design_trace,
        design_markdown=design_markdown, project_profile_id=project_profile_id, k=k, step_review=step_review,
    ).run()


class _PlanFlow:
    def __init__(
        self,
        *,
        settings: Settings,
        store: RuntimeStore,
        run_id: str,
        model: str,
        design_trace: dict[str, object],
        design_markdown: str,
        project_profile_id: str,
        k: int,
        step_review: dict[str, object],
    ):
        self.store, self.run_id = store, run_id
        self.worker = OpenCodeWorker(settings, run_id=run_id, model=model)
        self.review_mode = step_review if step_review.get("enabled") else {"review_input": False, "review_output": False}
        self.trace = design_trace
        self.design_markdown = design_markdown
        self.project_profile_id = project_profile_id
        self.k = k
        self.record_path = store.artifact_path(run_id, FLOW_RECORD)
        self.record = self._read_record()

    def run(self) -> PlanGraphResult:
        facts = self._facts()
        plan_all = pg.build_plan_graph(self.trace, facts, k=self.k, reliance=pp.assumed_reliance(facts.reliance_candidates))
        self._fail_on_checks(plan_all, "before the Planner")
        candidates_artifact = self._archive(CANDIDATES_ARTIFACT, json.dumps(plan_all, indent=2) + "\n")
        report = pg.plan_markdown(plan_all, title="Plan graph before the Planner (every reliance candidate assumed kept)")
        design_ref = self.worker.stage("plan-design", ".md", self.design_markdown)
        report_ref = self.worker.stage("plan-graph-report", ".md", report)
        output_ref = self.worker.local_ref(pp.PLANNER_OUTPUT_NAME)

        attempts: list[dict[str, object]] = self.record.setdefault("planner", [])
        if not attempts:
            prompt = pp.planner_prompt(design_ref=design_ref, report_ref=report_ref, output_ref=output_ref, plan_all=plan_all)
            self._turn("plan-planner", "Planner", prompt, output_ref, fresh=True)
            self._add_attempt(attempts, output_ref, plan_all)
        while attempts[-1]["issues"]:
            if len(attempts) > FORMAT_REPAIR_LIMIT:
                raise HarnessError(
                    "plan_planner_invalid",
                    "The Planner's answer still fails the Harness checks after format fixes: "
                    + "; ".join(attempts[-1]["issues"]),
                    run_id=self.run_id,
                )
            prompt = (
                f"The Harness checked your answer at {output_ref} mechanically and found these issues:\n"
                + "\n".join(f"- {issue}" for issue in attempts[-1]["issues"])
                + "\n\nFix only these and keep every other row and decision as it is. Keep the layout of the first "
                "instruction (## Reliance, ## Base, ## Stubs, ## Notes).\n\n"
                f"When you are done, reply with only this relative path: {output_ref}"
            )
            self._turn(f"plan-planner-fix-r{len(attempts) + 1}", "Planner format fix", prompt, output_ref, fresh=False)
            self._add_attempt(attempts, output_ref, plan_all)

        parsed = attempts[-1]["parsed"]
        option = pp.chosen_option(parsed, plan_all)
        kept = pp.kept_reliance(parsed, plan_all)
        plan = pg.build_plan_graph(self.trace, facts, k=self.k, reliance=kept, fixed_base=option["base"],
                                   fixed_stubs=option["stubs"])
        rows = parsed["rows"]
        plan["planner"] = {
            "output_artifact": attempts[-1]["artifact"],
            "attempts": len(attempts),
            "option": parsed["option"],
            "option_reason": parsed["option_reason"],
            "kept": sum(1 for row in rows if row["decision"] == "keep"),
            "dropped": sum(1 for row in rows if row["decision"] == "drop"),
            "added": sum(1 for row in rows if row["decision"] == "add"),
            "notes": parsed["notes"],
        }
        plan["inputs"] = {
            "design_trace_sha256": _sha(json.dumps(self.trace, sort_keys=True)),
            "design_markdown_sha256": _sha(self.design_markdown),
            "facts_artifact": self.record["facts_artifact"],
            "candidates_artifact": candidates_artifact,
        }
        self._fail_on_checks(plan, "after the Planner")
        artifacts = {
            "plan_graph_flow": self.store.relative(self.record_path),
            "plan_repo_facts": str(self.record["facts_artifact"]),
            "plan_graph_candidates": candidates_artifact,
            "plan_graph": self._archive(PLAN_ARTIFACT, json.dumps(plan, indent=2) + "\n"),
            "plan_graph_report": self._archive(
                REPORT_ARTIFACT, pg.plan_markdown(plan, title="Plan graph (H-OC Phase 3)") + _notes(plan)),
            "plan_task_graph": self._archive(DIAGRAM_ARTIFACT, pg.plan_mermaid(plan)),
        }
        self.record["final"] = artifacts
        self._write_record()
        return PlanGraphResult(plan=plan, artifacts=artifacts)

    # Steps ---------------------------------------------------------------------------------------------------------

    def _facts(self) -> RepoFacts:
        if "facts_artifact" not in self.record:
            facts = repo_facts_for_profile(
                self.worker.worktree(),
                {str(unit["id"]): list(unit.get("files") or []) for unit in self.trace.get("units") or []},
                project_profile_id=self.project_profile_id,
            )
            self.record["facts_artifact"] = self._archive(FACTS_ARTIFACT, json.dumps(facts.to_dict(), indent=2) + "\n")
            self._write_record()
        path = self.store.artifact_path(self.run_id, FACTS_ARTIFACT)
        return RepoFacts.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _fail_on_checks(self, plan: dict[str, object], when: str) -> None:
        issues = plan["checks"]["issues"]
        if issues:
            raise HarnessError(
                "plan_graph_invalid",
                f"The plan graph fails mechanical checks {when}: "
                + "; ".join(f"{issue['check']} {issue['message']}" for issue in issues[:8]),
                run_id=self.run_id,
            )

    def _add_attempt(self, attempts: list[dict[str, object]], output_ref: str, plan_all: dict[str, object]) -> None:
        text = self.worker.read(output_ref)
        parsed = pp.parse_planner(text)
        artifact = self._archive(f"plan_planner_a{len(attempts) + 1}.md", text)
        attempts.append({"artifact": artifact, "sha256": _sha(text), "parsed": parsed,
                         "issues": pp.planner_issues(parsed, plan_all)})
        self._write_record()

    def _turn(self, prefix: str, label: str, prompt: str, output_ref: str, *, fresh: bool) -> None:
        def call(prompt: str) -> tuple[LlmCompletion, dict[str, object], tuple[str, ...]]:
            if fresh:
                self.worker.remove(output_ref)  # A rejected first answer is rewritten from scratch.
            completion, session_id = self.worker.turn(
                task=prompt, timeout=PLANNER_TIMEOUT, role="planner", output_ref=output_ref, editable_refs=(output_ref,),
            )
            self._audit(session_id)
            return completion, {"output": output_ref}, (output_ref,)

        self._reviewed(prefix, label, prompt, call)

    def _audit(self, session_id: str) -> None:
        """Fail closed when a file tool of the turn touched a path outside the run worktree."""

        outside = self.worker.paths_outside_worktree(session_id)
        if outside is None:
            self.record.setdefault("audit_unavailable", []).append({"session_id": session_id})
            self._write_record()
            return
        if outside:
            raise HarnessError(
                "plan_read_outside_worktree",
                f"The Planner turn used paths outside the run worktree: {outside[:5]}.",
                run_id=self.run_id,
            )

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
            execution_dir=f"plan_execution/{prefix}",
            error_prefix="plan_graph",
            label=label,
        ).complete_with_metadata(system=label, prompt=prompt)
        receipt = json.loads(completion.content)
        for ref, digest in receipt["files"].items():
            if _sha(self.worker.read(ref)) != digest:
                raise HarnessError("plan_graph_changed", f"{ref} changed after the {label} turn was reviewed.",
                                   run_id=self.run_id)
        return receipt

    # Record --------------------------------------------------------------------------------------------------------

    def _read_record(self) -> dict[str, object]:
        if not self.record_path.exists():
            return {"version": "plan_graph_flow_v1", "flow": PLAN_FLOW_VERSION, "k": self.k}
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessError("plan_session_not_resumable", "The plan graph record is unreadable.", run_id=self.run_id) from exc
        if not isinstance(record, dict) or record.get("flow") != PLAN_FLOW_VERSION or record.get("k") != self.k:
            raise HarnessError("plan_session_not_resumable", "The plan graph record does not match this run.",
                               run_id=self.run_id)
        return record

    def _write_record(self) -> None:
        self.store.write_json_artifact(self.run_id, FLOW_RECORD, self.record)

    def _archive(self, filename: str, text: str) -> str:
        # Bytes, not text mode: the archive must equal what was checked, line endings included.
        path = self.store.artifact_path(self.run_id, filename)
        data = text.encode("utf-8")
        if path.exists():
            if path.read_bytes() != data:
                raise HarnessError("plan_revision_conflict", f"{filename} already exists with different content.",
                                   run_id=self.run_id)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.store.relative(path)


def _notes(plan: dict[str, object]) -> str:
    planner = plan.get("planner") or {}
    return (
        "\n## Planner\n\n"
        f"Kept {planner.get('kept')}, dropped {planner.get('dropped')}, added {planner.get('added')} reliance edges; "
        f"base option {planner.get('option')}: {planner.get('option_reason')}\n\n"
        f"Notes: {planner.get('notes') or 'none'}\n"
    )


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
