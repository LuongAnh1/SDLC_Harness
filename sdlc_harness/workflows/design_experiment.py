"""Isolated progressive-gate experiment for Phase 2 design validation.

This workflow is deliberately outside the production checkpoint path.  It
reads a frozen Requirement V2 snapshot, writes only under an experiment root,
never calls a sync adapter, and caps all LLM calls with a hard budget.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from ..agents.designer import DesignerAgent, parse_design_model
from ..agents.validator import DesignFinding, DesignValidator, ValidatorAgent
from ..agents.validator.traceability import TraceabilityResult
from ..artifacts import read_frozen_requirement_input
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..llm.json_utils import parse_first_json_object
from ..storage.runtime import RuntimeStore, utc_now_iso


SEMANTIC_CATEGORIES = {
    "behavior_order",
    "terminal_state",
    "cross_diagram_contract",
    "data_consistency",
    "transaction_boundary",
    "unsupported_assumption",
}
CATEGORY_PRIORITY = {
    "terminal_state": 0,
    "behavior_order": 1,
    "data_consistency": 2,
    "cross_diagram_contract": 3,
    "transaction_boundary": 4,
    "unsupported_assumption": 5,
}


class BudgetedLlmClient:
    """Count and hard-limit calls while preserving the LlmClient interface."""

    def __init__(self, base: Any, *, limit: int):
        self.base = base
        self.settings = base.settings
        self.limit = max(1, int(limit))
        self.calls: list[dict[str, object]] = []

    @property
    def used(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def require(self, count: int, *, category: str) -> None:
        if self.remaining < count:
            raise HarnessError(
                "design_llm_budget_exhausted",
                f"Need {count} {category} call(s), but only {self.remaining}/{self.limit} remain.",
            )

    def complete(self, *, system: str, prompt: str) -> str:
        self._reserve(system)
        return self.base.complete(system=system, prompt=prompt)

    def complete_with_metadata(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> Any:
        self._reserve(system)
        return self.base.complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def complete_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> Any:
        system = next((str(item.get("content") or "") for item in messages if item.get("role") == "system"), "")
        self._reserve(system)
        return self.base.complete_messages(messages=messages, json_mode=json_mode, max_tokens=max_tokens)

    def summary(self) -> dict[str, object]:
        counts = Counter(str(item["category"]) for item in self.calls)
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "by_category": dict(sorted(counts.items())),
            "calls": self.calls,
        }

    def _reserve(self, system: str) -> None:
        if self.used >= self.limit:
            raise HarnessError(
                "design_llm_budget_exhausted",
                f"Design experiment reached its {self.limit}-call LLM budget.",
            )
        category = _call_category(system)
        self.calls.append({"ordinal": self.used + 1, "category": category})


@dataclass(frozen=True)
class SemanticCandidate:
    candidate_id: str
    claim_id: str
    requirement_id: str
    category: str
    message: str
    expected_change: str
    design_refs: list[str]
    support_count: int
    trial_ids: list[str]

    @property
    def key(self) -> str:
        return f"{self.claim_id}|{self.category}"

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "claim_id": self.claim_id,
            "requirement_id": self.requirement_id,
            "category": self.category,
            "message": self.message,
            "expected_change": self.expected_change,
            "design_refs": self.design_refs,
            "support_count": self.support_count,
            "trial_ids": self.trial_ids,
        }


class ProgressiveDesignReviewEngine:
    """Shared progressive review engine used by production and isolated experiments."""

    def __init__(
        self,
        settings: Settings,
        *,
        experiment_root: Path | None = None,
        llm_call_budget: int = 16,
        llm_client: Any | None = None,
    ):
        self.settings = settings
        self.source_store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.store = (
            RuntimeStore(repo_root=settings.repo_root, runtime_root=experiment_root)
            if experiment_root is not None
            else None
        )
        self.llm = BudgetedLlmClient(llm_client or LlmClient(settings), limit=llm_call_budget)
        self.validation_trials = max(1, int(settings.design_validation_trials))
        self.designer = DesignerAgent(self.llm)
        self.trace_validator = ValidatorAgent(self.llm)
        self.design_validator = DesignValidator(self.llm)

    def run(self, *, source_run_id: str) -> dict[str, object]:
        if self.store is None:
            raise HarnessError(
                "design_experiment_root_required",
                "An experiment root is required for an isolated design experiment.",
            )
        source_state = self.source_store.read_state(source_run_id)
        approved = read_frozen_requirement_input(
            store=self.source_store,
            state=source_state,
            run_id=source_run_id,
        )
        source_paths = self._source_paths(source_run_id, approved)
        source_before = _hash_paths(source_paths)
        run_id = _experiment_run_id()
        copied = self._copy_requirement_snapshot(run_id, source_paths)
        state: dict[str, object] = {
            "ok": True,
            "run_id": run_id,
            "source_run_id": source_run_id,
            "source_ref": source_state.get("source_ref"),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "current_phase": "design_experiment",
            "status": "design_experiment_drafting",
            "pending_checkpoint": None,
            "allowed_next_actions": [],
            "last_sync": {"status": "disabled", "mode": "none", "attempted": False},
            "last_error": None,
            "latest_artifacts": copied,
            "experiment": {
                "version": "progressive_design_validation_v1",
                "source_run_id": source_run_id,
                "validation_trials": self.validation_trials,
                "repair_limit": min(3, max(0, int(self.settings.design_repair_limit))),
                "llm_call_budget": self.llm.limit,
                "gate_order": ["deterministic_preflight", "traceability", "semantic_claims", "bounded_adjudication"],
                "sync_disabled": True,
                "trajectory": [],
            },
            "summary_for_user": "Running isolated progressive design validation.",
        }
        self.store.write_state(state)
        try:
            self.llm.require(2, category="designer_draft")
            draft = self.designer.draft(
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                canonical_requirement=approved.canonical_model,
            )
            self._write_text(run_id, "designer_analysis_v1.md", draft.analysis_report)
            self._run_gates(state=state, approved=approved, design_model=draft.design_model)
        except HarnessError as exc:
            self._finish_error(state, exc)
        except Exception as exc:  # experiment boundary must always leave a report
            self._finish_error(state, HarnessError("design_experiment_failed", str(exc), run_id=run_id))
        finally:
            source_after = _hash_paths(source_paths)
            experiment = _dict(state.get("experiment"))
            experiment["source_integrity"] = {
                "unchanged": source_before == source_after,
                "before": source_before,
                "after": source_after,
            }
            experiment["llm_calls"] = self.llm.summary()
            state["experiment"] = experiment
            state["updated_at"] = utc_now_iso()
            self._write_report(state)
            self.store.write_state(state)
        return _response(state)

    def _run_gates(self, *, state: dict[str, object], approved: Any, design_model: str) -> None:
        run_id = str(state["run_id"])
        repair_limit = int(_dict(state["experiment"])["repair_limit"])
        pending_semantic: list[SemanticCandidate] = []
        seen_feedback: set[str] = set()
        for repair_iteration in range(repair_limit + 1):
            version = repair_iteration + 1
            if repair_iteration:
                state["status"] = "design_experiment_repairing"
            design_ref = self._write_text(run_id, f"design_artifact_v{version}.md", design_model)
            parsed = parse_design_model(design_model)
            self.store.write_json_artifact(run_id, f"class_graph_v{version}.json", parsed.class_graph)
            self.store.write_json_artifact(run_id, f"sequence_graph_v{version}.json", parsed.sequence_graph)

            preflight = self.design_validator.preflight(
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )
            if preflight:
                selected = preflight[:3]
                outcome = self._record_cycle(state, version, "deterministic_preflight", selected, [], design_ref)
                if not self._can_repair(repair_iteration, repair_limit, outcome, seen_feedback):
                    return
                design_model = self._repair(approved, design_model, _finding_feedback(selected))
                pending_semantic = []
                continue

            self.llm.require(2, category="traceability")
            traceability = self.trace_validator.construct_traceability(
                requirement_packet=approved.requirement_packet,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                canonical_requirement=approved.canonical_model,
            )
            self._write_trace(run_id, version, traceability)
            trace_findings = _missing_link_findings(traceability)
            if trace_findings:
                selected = trace_findings[:3]
                outcome = self._record_cycle(state, version, "traceability", selected, [], design_ref)
                if not self._can_repair(repair_iteration, repair_limit, outcome, seen_feedback):
                    return
                design_model = self._repair(approved, design_model, _finding_feedback(selected))
                pending_semantic = []
                continue

            if pending_semantic:
                self.llm.require(1, category="semantic_targeted_verification")
                unresolved = self.verify_repair(
                    pending_semantic,
                    design_model=design_model,
                    class_graph=parsed.class_graph,
                    sequence_graph=parsed.sequence_graph,
                )
                if unresolved:
                    selected = unresolved[:2]
                    outcome = self._record_cycle(state, version, "semantic_targeted_verification", selected, [], design_ref)
                    if not self._can_repair(repair_iteration, repair_limit, outcome, seen_feedback):
                        return
                    design_model = self._repair(approved, design_model, _candidate_feedback(selected))
                    pending_semantic = selected
                    continue
                pending_semantic = []

            self.llm.require(self.validation_trials, category="semantic_claim_reviews")
            claims = _build_claim_matrix(traceability)
            confirmed, disputed, trial_report = self.semantic_trials(
                claims=claims,
                requirement_model=approved.canonical_model.to_dict(),
                design_model=design_model,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )
            if not bool(trial_report.get("complete")):
                self._record_cycle(
                    state,
                    version,
                    "semantic_trials_inconclusive",
                    [],
                    [],
                    design_ref,
                    trial_report,
                )
                state.update(
                    status="design_experiment_inconclusive",
                    summary_for_user=(
                        "Experiment stopped because it could not collect the requested "
                        "number of valid semantic trials within the bounded retry budget."
                    ),
                )
                return
            if confirmed:
                selected = confirmed[:2]
                outcome = self._record_cycle(
                    state, version, "semantic_consensus", selected, disputed, design_ref, trial_report
                )
                if not self._can_repair(repair_iteration, repair_limit, outcome, seen_feedback):
                    return
                design_model = self._repair(approved, design_model, _candidate_feedback(selected))
                pending_semantic = selected
                continue

            if disputed:
                candidates = sorted(
                    disputed,
                    key=lambda item: (CATEGORY_PRIORITY.get(item.category, 99), item.key),
                )[:3]
                self.llm.require(1, category="semantic_adjudication")
                supported, uncertain, adjudication = self.adjudicate(
                    candidates,
                    requirement_model=approved.canonical_model.to_dict(),
                    design_model=design_model,
                )
                if supported:
                    selected = supported[:2]
                    outcome = self._record_cycle(
                        state,
                        version,
                        "bounded_adjudication",
                        selected,
                        uncertain,
                        design_ref,
                        {**trial_report, "adjudication": adjudication},
                    )
                    if not self._can_repair(repair_iteration, repair_limit, outcome, seen_feedback):
                        return
                    design_model = self._repair(approved, design_model, _candidate_feedback(selected))
                    pending_semantic = selected
                    continue
                if uncertain:
                    self._record_cycle(
                        state,
                        version,
                        "bounded_adjudication",
                        [],
                        uncertain,
                        design_ref,
                        {**trial_report, "adjudication": adjudication},
                    )
                    state.update(
                        status="design_experiment_risk_review",
                        summary_for_user=f"Experiment stopped with {len(uncertain)} adjudication risk(s).",
                    )
                    return

            self._record_cycle(state, version, "passed", [], [], design_ref, trial_report)
            state.update(
                status="design_experiment_passed",
                summary_for_user=f"Progressive validation converged at design V{version}.",
            )
            return

    def semantic_trials(
        self,
        *,
        claims: list[dict[str, object]],
        requirement_model: dict[str, object],
        design_model: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
    ) -> tuple[list[SemanticCandidate], list[SemanticCandidate], dict[str, object]]:
        trials: list[dict[str, object]] = []
        invalid_trials: list[dict[str, object]] = []
        assessments: list[tuple[str, dict[str, object]]] = []
        max_attempts = self.validation_trials * 2
        attempt = 0
        while len(trials) < self.validation_trials and attempt < max_attempts:
            missing_trials = self.validation_trials - len(trials)
            if self.llm.remaining < missing_trials:
                break
            attempt += 1
            attempt_id = f"attempt-{attempt}"
            try:
                payload = self._complete_json(
                    system="You are Progressive Design Semantic Judge. Return strict JSON only.",
                    prompt=_semantic_prompt(
                        claims=claims,
                        requirement_model=requirement_model,
                        design_model=design_model,
                        class_graph=class_graph,
                        sequence_graph=sequence_graph,
                    ),
                )
                normalized = _validate_assessments(payload, claims)
            except HarnessError as exc:
                if exc.error_code != "design_semantic_review_failed":
                    raise
                invalid_trials.append(
                    {
                        "attempt_id": attempt_id,
                        "error_code": exc.error_code,
                        "message": exc.message,
                    }
                )
                continue
            trial_id = f"trial-{len(trials) + 1}"
            trials.append({"trial_id": trial_id, "assessments": normalized})
            assessments.extend((trial_id, item) for item in normalized)
        candidates = _aggregate_assessments(assessments)
        quorum = self.validation_trials // 2 + 1
        confirmed = [item for item in candidates if item.support_count >= quorum]
        disputed = [item for item in candidates if item.support_count < quorum]
        return confirmed, disputed, {
            "requested_trials": self.validation_trials,
            "valid_trials": len(trials),
            "quorum": quorum,
            "confirmed_count": len(confirmed),
            "disputed_count": len(disputed),
            "complete": len(trials) == self.validation_trials,
            "attempt_count": attempt,
            "max_attempts": max_attempts,
            "invalid_trial_count": len(invalid_trials),
            "invalid_trials": invalid_trials,
            "trials": trials,
        }

    def verify_repair(
        self,
        candidates: list[SemanticCandidate],
        *,
        design_model: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
    ) -> list[SemanticCandidate]:
        payload = self._complete_json(
            system="You are Progressive Design Targeted Verifier. Return strict JSON only.",
            prompt=_targeted_prompt(candidates, design_model, class_graph, sequence_graph),
        )
        raw = payload.get("results")
        if not isinstance(raw, list):
            raise HarnessError("design_semantic_review_failed", "Targeted verifier must return a results array.")
        by_id = {item.candidate_id: item for item in candidates}
        seen: set[str] = set()
        unresolved: list[SemanticCandidate] = []
        for item in raw:
            if not isinstance(item, dict):
                raise HarnessError("design_semantic_review_failed", "Targeted verifier returned a non-object result.")
            candidate_id = str(item.get("candidate_id") or "")
            verdict = str(item.get("verdict") or "")
            if candidate_id not in by_id or candidate_id in seen or verdict not in {"resolved", "unresolved", "uncertain"}:
                raise HarnessError("design_semantic_review_failed", "Targeted verifier changed the approved candidate set.")
            raw_refs = item.get("evidence_refs")
            evidence_refs = [str(value) for value in raw_refs] if isinstance(raw_refs, list) else []
            allowed_refs = set(by_id[candidate_id].design_refs)
            if any(value not in allowed_refs for value in evidence_refs):
                raise HarnessError("design_semantic_review_failed", "Targeted verifier cited evidence outside the candidate.")
            if verdict != "uncertain" and allowed_refs and not evidence_refs:
                raise HarnessError("design_semantic_review_failed", "Targeted verifier verdict lacks bounded design evidence.")
            seen.add(candidate_id)
            if verdict != "resolved":
                unresolved.append(by_id[candidate_id])
        if seen != set(by_id):
            raise HarnessError("design_semantic_review_failed", "Targeted verifier omitted an approved candidate.")
        return unresolved

    def adjudicate(
        self,
        candidates: list[SemanticCandidate],
        *,
        requirement_model: dict[str, object],
        design_model: str,
    ) -> tuple[list[SemanticCandidate], list[SemanticCandidate], dict[str, object]]:
        payload = self._complete_json(
            system="You are Progressive Design Adjudicator. Return strict JSON only.",
            prompt=_adjudication_prompt(candidates, requirement_model, design_model),
        )
        raw = payload.get("decisions")
        if not isinstance(raw, list):
            raise HarnessError("design_semantic_review_failed", "Adjudicator must return a decisions array.")
        by_id = {item.candidate_id: item for item in candidates}
        decisions: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise HarnessError("design_semantic_review_failed", "Adjudicator returned a non-object decision.")
            candidate_id = str(item.get("candidate_id") or "")
            decision = str(item.get("decision") or "")
            if candidate_id not in by_id or candidate_id in decisions or decision not in {"supported", "refuted", "uncertain"}:
                raise HarnessError("design_semantic_review_failed", "Adjudicator changed the bounded candidate set.")
            candidate = by_id[candidate_id]
            raw_requirement_refs = item.get("requirement_refs")
            requirement_refs = [str(value) for value in raw_requirement_refs] if isinstance(raw_requirement_refs, list) else []
            raw_design_refs = item.get("design_refs")
            design_refs = [str(value) for value in raw_design_refs] if isinstance(raw_design_refs, list) else []
            if any(value != candidate.requirement_id for value in requirement_refs):
                raise HarnessError("design_semantic_review_failed", "Adjudicator cited a requirement outside the candidate.")
            if any(value not in set(candidate.design_refs) for value in design_refs):
                raise HarnessError("design_semantic_review_failed", "Adjudicator cited design evidence outside the candidate.")
            if decision == "supported" and (not requirement_refs or not design_refs):
                raise HarnessError("design_semantic_review_failed", "Supported adjudication lacks bounded evidence.")
            decisions[candidate_id] = decision
        if set(decisions) != set(by_id):
            raise HarnessError("design_semantic_review_failed", "Adjudicator omitted a bounded candidate.")
        supported = [by_id[key] for key, value in decisions.items() if value == "supported"]
        uncertain = [by_id[key] for key, value in decisions.items() if value == "uncertain"]
        return supported, uncertain, {"candidates": [item.to_dict() for item in candidates], "decisions": raw}

    def _repair(self, approved: Any, design_model: str, feedback: str) -> str:
        self.llm.require(1, category="designer_repair")
        return self.designer.update_design(
            requirement_packet=approved.requirement_packet,
            formal_use_case=approved.formal_use_case,
            current_design=design_model,
            audit_report=feedback,
            canonical_requirement=approved.canonical_model,
        )

    def _can_repair(
        self,
        repair_iteration: int,
        repair_limit: int,
        fingerprint: str,
        seen: set[str],
    ) -> bool:
        if fingerprint in seen:
            raise HarnessError("design_repair_stalled", "The selected repair set repeated without convergence.")
        seen.add(fingerprint)
        if repair_iteration >= repair_limit:
            raise HarnessError("design_repair_exhausted", "Progressive design repair reached its configured limit.")
        return True

    def _record_cycle(
        self,
        state: dict[str, object],
        version: int,
        stage: str,
        selected: list[Any],
        disputed: list[Any],
        design_ref: str,
        details: dict[str, object] | None = None,
    ) -> str:
        selected_payload = [_item_dict(item) for item in selected]
        disputed_payload = [_item_dict(item) for item in disputed]
        keys = sorted(_item_key(item) for item in selected)
        entry = {
            "version": f"v{version}",
            "stage": stage,
            "selected_for_repair": selected_payload,
            "disputed": disputed_payload,
            "fingerprint": hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest() if keys else "",
            "llm_calls_used": self.llm.used,
            "details": details or {},
        }
        experiment = _dict(state.get("experiment"))
        trajectory = list(experiment.get("trajectory") or [])
        trajectory.append(entry)
        experiment["trajectory"] = trajectory
        state["experiment"] = experiment
        latest = _dict(state.get("latest_artifacts"))
        latest["design_artifact"] = design_ref
        report_path = self.store.write_json_artifact(str(state["run_id"]), f"validation_cycle_v{version}.json", entry)
        latest["validation_cycle"] = self.store.relative(report_path)
        state["latest_artifacts"] = latest
        self.store.write_state(state)
        return entry["fingerprint"]

    def _write_trace(self, run_id: str, version: int, trace: TraceabilityResult) -> None:
        self.store.write_json_artifact(
            run_id,
            f"traceability_v{version}.json",
            {
                "requirement_graph": trace.requirement_graph,
                "class_trace": trace.class_trace,
                "sequence_trace": trace.sequence_trace,
                "missing_links": trace.missing_links,
                "mode": trace.mode,
            },
        )

    def _complete_json(self, *, system: str, prompt: str) -> dict[str, object]:
        completion = self.llm.complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=True,
            max_tokens=int(getattr(self.settings, "llm_structured_max_tokens", 8192)),
        )
        try:
            return parse_first_json_object(str(completion.content))
        except json.JSONDecodeError as exc:
            raise HarnessError("design_semantic_review_failed", "Progressive validator returned invalid JSON.") from exc

    def _source_paths(self, source_run_id: str, approved: Any) -> dict[str, Path]:
        refs = dict(approved.approved_artifacts)
        source_ref = str(approved.canonical_model.source.get("artifact_ref") or "")
        if source_ref:
            refs["source_snapshot"] = source_ref
        paths = {"state": self.source_store.state_path(source_run_id)}
        if self.source_store.active_runs_path.exists():
            paths["active_runs"] = self.source_store.active_runs_path
        for key, ref in refs.items():
            path = (self.source_store.repo_root / ref).resolve()
            if not path.is_file():
                raise HarnessError("approved_requirement_not_found", f"Frozen source artifact is missing: {ref}")
            paths[key] = path
        return paths

    def _copy_requirement_snapshot(self, run_id: str, paths: dict[str, Path]) -> dict[str, str]:
        destination = self.store.run_dir(run_id) / "artifacts" / "requirement_snapshot"
        destination.mkdir(parents=True, exist_ok=True)
        copied: dict[str, str] = {}
        for key, source in paths.items():
            if key in {"state", "active_runs"}:
                continue
            target = destination / f"{key}__{source.name}"
            shutil.copy2(source, target)
            copied[key] = self.store.relative(target)
        return copied

    def _write_text(self, run_id: str, filename: str, content: str) -> str:
        path = self.store.artifact_path(run_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return self.store.relative(path)

    def _finish_error(self, state: dict[str, object], error: HarnessError) -> None:
        status = {
            "design_llm_budget_exhausted": "design_experiment_budget_exhausted",
            "design_repair_stalled": "design_experiment_stalled",
            "design_repair_exhausted": "design_experiment_exhausted",
            "design_semantic_review_failed": "design_experiment_inconclusive",
        }.get(error.error_code, "design_experiment_failed")
        state.update(
            ok=False,
            status=status,
            last_error=HarnessError(error.error_code, error.message, run_id=str(state["run_id"])).to_response(),
            summary_for_user=f"Progressive design experiment stopped: {error.message}",
        )

    def _write_report(self, state: dict[str, object]) -> None:
        run_id = str(state["run_id"])
        report = {
            "version": "progressive_design_experiment_report_v1",
            "run_id": run_id,
            "source_run_id": state.get("source_run_id"),
            "status": state.get("status"),
            "last_error": state.get("last_error"),
            "sync": state.get("last_sync"),
            "experiment": state.get("experiment"),
        }
        json_path = self.store.write_json_artifact(run_id, "experiment_report.json", report)
        experiment = _dict(state.get("experiment"))
        integrity = _dict(experiment.get("source_integrity"))
        calls = _dict(experiment.get("llm_calls"))
        lines = [
            "# Progressive Design Validation Experiment",
            "",
            f"- Status: `{state.get('status')}`",
            f"- Source run: `{state.get('source_run_id')}`",
            f"- Source unchanged: `{integrity.get('unchanged')}`",
            f"- Sync attempted: `False`",
            f"- LLM calls: `{calls.get('used', 0)}/{calls.get('limit', self.llm.limit)}`",
            "",
            "## Trajectory",
            "",
        ]
        for item in list(experiment.get("trajectory") or []):
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('version')}: `{item.get('stage')}`; "
                    f"repair={len(item.get('selected_for_repair') or [])}; "
                    f"disputed={len(item.get('disputed') or [])}."
                )
        markdown_ref = self._write_text(run_id, "experiment_report.md", "\n".join(lines))
        latest = _dict(state.get("latest_artifacts"))
        latest["experiment_report"] = self.store.relative(json_path)
        latest["experiment_report_markdown"] = markdown_ref
        state["latest_artifacts"] = latest


def _build_claim_matrix(trace: TraceabilityResult) -> list[dict[str, object]]:
    class_links = _links_by_requirement(trace.class_trace)
    sequence_links = _links_by_requirement(trace.sequence_trace)
    claims: list[dict[str, object]] = []
    for raw in list(trace.requirement_graph.get("requirements") or []):
        if not isinstance(raw, dict):
            continue
        requirement_id = str(raw.get("id") or "")
        if not requirement_id:
            continue
        claims.append(
            {
                "claim_id": f"CLAIM-{requirement_id}",
                "requirement_id": requirement_id,
                "section": str(raw.get("section") or ""),
                "content": str(raw.get("content") or ""),
                "allowed_design_refs": sorted(class_links.get(requirement_id, set()) | sequence_links.get(requirement_id, set())),
            }
        )
    return claims


def _links_by_requirement(trace_items: list[dict[str, object]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in trace_items:
        requirement_id = str(item.get("requirement") or "")
        for link in list(item.get("links") or []):
            if isinstance(link, dict) and str(link.get("target") or ""):
                result.setdefault(requirement_id, set()).add(str(link["target"]))
    return result


def _validate_assessments(payload: dict[str, object], claims: list[dict[str, object]]) -> list[dict[str, object]]:
    raw = payload.get("assessments")
    if not isinstance(raw, list):
        raise HarnessError("design_semantic_review_failed", "Semantic judge must return an assessments array.")
    known = {str(item["claim_id"]): item for item in claims}
    seen: set[str] = set()
    normalized: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HarnessError("design_semantic_review_failed", "Semantic assessment must be an object.")
        claim_id = str(item.get("claim_id") or "")
        verdict = str(item.get("verdict") or "")
        category = str(item.get("category") or "")
        if claim_id not in known or claim_id in seen or verdict not in {"satisfied", "violated", "uncertain"}:
            raise HarnessError("design_semantic_review_failed", "Semantic judge changed or duplicated the claim set.")
        if verdict == "violated" and category not in SEMANTIC_CATEGORIES:
            raise HarnessError("design_semantic_review_failed", "Violated semantic claim has an unsupported category.")
        if verdict != "violated":
            category = "none"
        allowed = set(str(value) for value in known[claim_id].get("allowed_design_refs") or [])
        raw_refs = item.get("design_refs")
        refs = [str(value) for value in raw_refs] if isinstance(raw_refs, list) else []
        invalid_refs = [value for value in refs if value not in allowed]
        if invalid_refs:
            raise HarnessError(
                "design_semantic_review_failed",
                (
                    f"Semantic judge cited evidence outside fixed claim {claim_id}: "
                    f"invalid_refs={invalid_refs[:5]!r}; allowed_refs={sorted(allowed)[:10]!r}."
                ),
            )
        if verdict == "violated" and not refs:
            raise HarnessError("design_semantic_review_failed", "Violated semantic claim lacks bounded design evidence.")
        normalized.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "category": category,
                "message": str(item.get("message") or ""),
                "expected_change": str(item.get("expected_change") or ""),
                "design_refs": refs,
            }
        )
        seen.add(claim_id)
    if seen != set(known):
        raise HarnessError("design_semantic_review_failed", "Semantic judge omitted one or more fixed claims.")
    return normalized


def _aggregate_assessments(assessments: list[tuple[str, dict[str, object]]]) -> list[SemanticCandidate]:
    groups: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for trial_id, item in assessments:
        if item["verdict"] != "violated":
            continue
        key = f"{item['claim_id']}|{item['category']}"
        groups.setdefault(key, []).append((trial_id, item))
    candidates: list[SemanticCandidate] = []
    for ordinal, (key, votes) in enumerate(sorted(groups.items()), start=1):
        representative = max((item for _trial, item in votes), key=lambda item: len(str(item.get("message") or "")))
        claim_id = str(representative["claim_id"])
        requirement_id = claim_id.removeprefix("CLAIM-")
        candidates.append(
            SemanticCandidate(
                candidate_id=f"CAND-{ordinal:03d}",
                claim_id=claim_id,
                requirement_id=requirement_id,
                category=str(representative["category"]),
                message=str(representative.get("message") or "Semantic contradiction."),
                expected_change=str(representative.get("expected_change") or "Correct the cited design behavior."),
                design_refs=[str(value) for value in list(representative.get("design_refs") or [])],
                support_count=len(votes),
                trial_ids=[trial_id for trial_id, _item in votes],
            )
        )
    return candidates


def _missing_link_findings(trace: TraceabilityResult) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for diagram, values in trace.missing_links.items():
        for raw in values:
            requirement_id = str(raw.get("requirement") or "")
            findings.append(
                DesignFinding(
                    finding_id=f"TRACE-{len(findings) + 1:03d}",
                    category="traceability_missing",
                    severity="blocking",
                    message=f"{requirement_id} has no valid {diagram.replace('_', ' ')} link.",
                    expected_change="Add a real UML element and trace it to this requirement.",
                    requirement_ids=[requirement_id],
                    subject_ref=f"{diagram}:{requirement_id}",
                    origin="deterministic",
                )
            )
    return findings


def _semantic_prompt(**context: object) -> str:
    return """Progressive Semantic Claim Review

Judge every fixed claim exactly once. Do not add, remove, merge, or rename claims.
Return one JSON object only:
{"assessments":[{"claim_id":"CLAIM-ID","verdict":"satisfied|violated|uncertain","category":"behavior_order|terminal_state|cross_diagram_contract|data_consistency|transaction_boundary|unsupported_assumption|none","message":"specific evidence-based reason","expected_change":"specific correction or empty","design_refs":["one of allowed_design_refs"]}]}

A violation must cite a behavior required by the claim and evidence already present in its allowed_design_refs. For every assessment, copy design_refs only from that same claim's allowed_design_refs, byte for byte. Never cite a ref borrowed from another claim or merely visible in the full graphs. If no allowed ref supports the verdict, return an empty design_refs list and use uncertain. Architectural preferences are satisfied or uncertain, never violations. `none` is required for satisfied/uncertain.

INPUT:
""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _targeted_prompt(
    candidates: list[SemanticCandidate],
    design_model: str,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
) -> str:
    return """Progressive Targeted Repair Verification

Verify only the listed candidates against the revised design. Return each candidate exactly once and add no candidate.
{"results":[{"candidate_id":"CAND-001","verdict":"resolved|unresolved|uncertain","evidence_refs":["existing design ref"],"reason":"short reason"}]}

INPUT:
""" + json.dumps(
        {
            "candidates": [item.to_dict() for item in candidates],
            "design_model": design_model,
            "class_graph": class_graph,
            "sequence_graph": sequence_graph,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _adjudication_prompt(
    candidates: list[SemanticCandidate],
    requirement_model: dict[str, object],
    design_model: str,
) -> str:
    return """Progressive Bounded Semantic Adjudication

Decide only the supplied minority candidates. Return each candidate exactly once and invent no candidate.
{"decisions":[{"candidate_id":"CAND-001","decision":"supported|refuted|uncertain","requirement_refs":["stable requirement id"],"design_refs":["existing ref"],"reason":"short evidence-based reason"}]}

INPUT:
""" + json.dumps(
        {
            "candidates": [item.to_dict() for item in candidates],
            "requirement_model": requirement_model,
            "design_model": design_model,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _finding_feedback(findings: list[DesignFinding]) -> str:
    return "\n".join(
        f"- [{item.finding_id}] {item.message} Required correction: {item.expected_change}"
        for item in findings[:3]
    )


def _candidate_feedback(candidates: list[SemanticCandidate]) -> str:
    return "\n".join(
        f"- [{item.candidate_id}] Requirement {item.requirement_id}: {item.message} Required correction: {item.expected_change}"
        for item in candidates[:2]
    )


def _item_dict(item: Any) -> dict[str, object]:
    value = item.to_dict()
    return value if isinstance(value, dict) else {}


def _item_key(item: Any) -> str:
    return str(getattr(item, "key", getattr(item, "canonical_key", "")))


def _hash_paths(paths: dict[str, Path]) -> dict[str, str]:
    return {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in sorted(paths.items())}


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _response(state: dict[str, object]) -> dict[str, object]:
    return {
        "ok": bool(state.get("ok")),
        "run_id": state.get("run_id"),
        "source_run_id": state.get("source_run_id"),
        "status": state.get("status"),
        "summary_for_user": state.get("summary_for_user"),
        "latest_artifacts": state.get("latest_artifacts"),
        "last_error": state.get("last_error"),
        "last_sync": state.get("last_sync"),
        "experiment": state.get("experiment"),
    }


def _call_category(system: str) -> str:
    lowered = system.casefold()
    if "targeted verifier" in lowered:
        return "semantic_targeted_verification"
    if "adjudicator" in lowered:
        return "semantic_adjudication"
    if "semantic judge" in lowered:
        return "semantic_claim_review"
    if "traceability" in lowered:
        return "traceability"
    if "update_design" in lowered:
        return "designer_repair"
    if "designer" in lowered:
        return "designer_draft"
    return "other"


def _experiment_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"experiment_{stamp}_{uuid4().hex[:8]}"


class ProgressiveDesignExperiment(ProgressiveDesignReviewEngine):
    """Isolated artifact-writing facade over the shared progressive review engine."""
