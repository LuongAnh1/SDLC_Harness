"""Xác thực thiết kế hybrid cho Phase 2.

Traceability agent xác định các requirement đã duyệt có được biểu diễn trong
diagram hay không. Module này bổ sung quality gate còn thiếu: contract graph
tất định cùng một lượt LLM semantic review về thứ tự, trạng thái và bảo đảm
transaction có điều kiện.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
import json
from itertools import combinations
import re
import time
from typing import Any

from ...core.config import phase2_llm_call_options
from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired
from .semantic_review import (
    SEMANTIC_REVIEW_MODES,
    build_semantic_review_plan,
    materialize_task_context,
    review_node_view,
    relation_index,
)
from .traceability import TraceabilityResult


FINDING_IDENTITY_VERSION = "design_finding_identity_v2"


@dataclass(frozen=True)
class DesignFinding:
    """One actionable design issue with evidence."""

    finding_id: str
    category: str
    severity: str
    message: str
    expected_change: str
    claim_ids: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    diagram_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    subject_ref: str = ""
    support_count: int = 1
    trial_ids: list[str] = field(default_factory=list)
    origin: str = "semantic"
    repair_owner: str = "designer"
    repairability: str = "repairable"
    confidence: float = 1.0
    snapshot_fingerprint: str = ""
    review_task_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.finding_id,
            "identity_version": FINDING_IDENTITY_VERSION,
            "canonical_key": self.canonical_key,
            "evidence_signature": self.evidence_signature,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "expected_change": self.expected_change,
            "claim_ids": self.claim_ids,
            "requirement_ids": self.requirement_ids,
            "diagram_ids": self.diagram_ids,
            "evidence": self.evidence,
            "subject_ref": self.subject_ref,
            "support_count": self.support_count,
            "trial_ids": self.trial_ids,
            "origin": self.origin,
            "repair_owner": self.repair_owner,
            "repairability": self.repairability,
            "confidence": self.confidence,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "review_task_ids": self.review_task_ids,
        }

    @property
    def canonical_key(self) -> str:
        return _canonical_finding_key(self)

    @property
    def evidence_signature(self) -> str:
        return _finding_evidence_signature(self)


@dataclass(frozen=True)
class DesignValidationResult:
    """Combined traceability, deterministic, and semantic design review."""

    traceability: TraceabilityResult
    blocking_findings: list[DesignFinding]
    warnings: list[DesignFinding]
    rule_review: dict[str, object]
    llm_review: dict[str, object]
    repair_iterations: int = 0
    mode: str = "design_validation_hybrid_v2"
    semantic_consensus: dict[str, object] = field(default_factory=dict)
    disputed_findings: list[DesignFinding] = field(default_factory=list)
    inconclusive: bool = False
    semantic_review_plan: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking_findings and not self.inconclusive

    @property
    def feedback(self) -> str:
        if not self.blocking_findings:
            return ""
        lines = ["# Design Validation Feedback", ""]
        for finding in self.blocking_findings:
            refs = ", ".join(finding.requirement_ids) or "design contract"
            lines.extend(
                [
                    f"## {finding.finding_id}: {finding.category}",
                    f"- Requirements: {refs}",
                    f"- Problem: {finding.message}",
                    f"- Required correction: {finding.expected_change}",
                    "",
                ]
            )
        return "\n".join(lines).strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "version": (
                "design_validation_report_v3_consensus"
                if self.mode == "design_validation_consensus_v1"
                else "design_validation_report_v2"
            ),
            "ok": self.ok,
            "mode": self.mode,
            "blocking_count": len(self.blocking_findings),
            "warning_count": len(self.warnings),
            "blocking_findings": [item.to_dict() for item in self.blocking_findings],
            "warnings": [item.to_dict() for item in self.warnings],
            "disputed_findings": [item.to_dict() for item in self.disputed_findings],
            "inconclusive": self.inconclusive,
            "semantic_consensus": self.semantic_consensus,
            "semantic_review_plan": self.semantic_review_plan,
            "rule_review": self.rule_review,
            "llm_review": self.llm_review,
            "repair_iterations": self.repair_iterations,
            "traceability": {
                "ok": self.traceability.ok,
                "mode": self.traceability.mode,
                "missing_links": self.traceability.missing_links,
            },
            "designer_feedback": self.feedback,
        }


class DesignValidator:
    """Validate parsed UML with deterministic rules and semantic LLM review."""

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def preflight(
        self,
        *,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
    ) -> list[DesignFinding]:
        """Run deterministic graph checks before paying for traceability/semantic review."""

        return _structural_findings(class_graph=class_graph, sequence_graph=sequence_graph)

    def build_review_plan(
        self,
        *,
        canonical_requirement: object | None,
        vertical_design_graph: dict[str, object] | None,
    ) -> dict[str, object]:
        canonical = _canonical_requirement_dict(canonical_requirement)
        return build_semantic_review_plan(
            claims=_semantic_review_claims(canonical),
            vertical_design_graph=vertical_design_graph,
        )

    def review(
        self,
        *,
        requirement_packet: str,
        design_artifact: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        traceability: TraceabilityResult,
        canonical_requirement: object | None = None,
        vertical_trace: dict[str, object] | None = None,
        vertical_design_graph: dict[str, object] | None = None,
        repair_iterations: int = 0,
        validation_trials: int = 1,
        semantic_review_mode: str = "legacy",
        semantic_review_plan: dict[str, object] | None = None,
        semantic_call_budget: int | None = None,
        semantic_task_cache: Path | None = None,
        semantic_task_cache_version: int = 0,
    ) -> DesignValidationResult:
        if semantic_review_mode not in SEMANTIC_REVIEW_MODES:
            raise HarnessError(
                "design_semantic_review_mode_invalid",
                f"Unsupported Design semantic review mode: {semantic_review_mode}",
            )
        if semantic_review_mode != "legacy":
            plan = semantic_review_plan or self.build_review_plan(
                canonical_requirement=canonical_requirement,
                vertical_design_graph=vertical_design_graph,
            )
            sliced_calls = len(_dict_items(plan.get("tasks"))) + 2
            if semantic_review_mode == "sliced_enforce":
                return self._sliced_review(
                    class_graph=class_graph,
                    sequence_graph=sequence_graph,
                    traceability=traceability,
                    canonical_requirement=canonical_requirement,
                    vertical_trace=vertical_trace,
                    vertical_design_graph=vertical_design_graph,
                    repair_iterations=repair_iterations,
                    review_plan=plan,
                    call_budget=semantic_call_budget,
                    task_cache=SemanticTaskCache(semantic_task_cache, version=semantic_task_cache_version) if semantic_task_cache else None,
                )
            legacy = self.review(
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                traceability=traceability,
                canonical_requirement=canonical_requirement,
                vertical_trace=vertical_trace,
                vertical_design_graph=vertical_design_graph,
                repair_iterations=repair_iterations,
                validation_trials=validation_trials,
                semantic_review_mode="legacy",
            )
            shadow: DesignValidationResult | None = None
            if semantic_call_budget is not None and max(1, int(validation_trials)) + sliced_calls > semantic_call_budget:
                shadow_payload: dict[str, object] = {
                    "ok": False,
                    "inconclusive": True,
                    "skipped": True,
                    "error_code": "design_semantic_review_budget_exceeded",
                    "summary": "Sliced shadow review did not fit the configured Design LLM call budget.",
                    "planned_calls": sliced_calls,
                }
            else:
                try:
                    shadow = self._sliced_review(
                        class_graph=class_graph,
                        sequence_graph=sequence_graph,
                        traceability=traceability,
                        canonical_requirement=canonical_requirement,
                        vertical_trace=vertical_trace,
                        vertical_design_graph=vertical_design_graph,
                        repair_iterations=repair_iterations,
                        review_plan=plan,
                        call_budget=sliced_calls,
                        task_cache=SemanticTaskCache(semantic_task_cache, version=semantic_task_cache_version) if semantic_task_cache else None,
                    )
                    shadow_payload = shadow.to_dict()
                except LlmStepReviewRequired:
                    raise
                except HarnessError as exc:
                    shadow_payload = {
                        "ok": False,
                        "inconclusive": True,
                        "error": exc.to_response(),
                        "summary": "Sliced shadow review failed; legacy remains authoritative.",
                    }
            legacy_keys = {item.canonical_key for item in legacy.blocking_findings}
            shadow_keys = {item.canonical_key for item in shadow.blocking_findings} if shadow is not None else set()
            shadow_inconclusive = shadow is None or shadow.inconclusive
            return replace(
                legacy,
                mode="design_validation_sliced_shadow_v1",
                llm_review={
                    "ok": legacy.ok and not shadow_inconclusive,
                    "summary": (
                        "Sliced shadow review did not complete; legacy findings are retained but cannot trigger "
                        "repair or checkpoint."
                        if shadow_inconclusive
                        else "Legacy semantic review remains authoritative; sliced review is observational."
                    ),
                    "authoritative": legacy.llm_review,
                    "sliced_shadow": shadow_payload,
                    "comparison": {
                        "legacy_ok": legacy.ok,
                        "sliced_ok": shadow.ok if shadow is not None else None,
                        "legacy_blocking_count": len(legacy.blocking_findings),
                        "sliced_blocking_count": len(shadow.blocking_findings) if shadow is not None else None,
                        "legacy_only_blockers": sorted(legacy_keys - shadow_keys),
                        "sliced_only_blockers": sorted(shadow_keys - legacy_keys),
                    },
                },
                inconclusive=legacy.inconclusive or shadow_inconclusive,
                semantic_review_plan=plan,
            )

        canonical = _canonical_requirement_dict(canonical_requirement)
        review_claims = _semantic_review_claims(canonical)
        valid_requirement_ids = {
            requirement_id
            for claim in review_claims
            for requirement_id in _string_items(claim.get("requirement_ids"))
        }
        valid_requirement_ids.update(
            str(claim.get("scenario_id") or "")
            for claim in review_claims
            if str(claim.get("scenario_id") or "")
        )
        valid_subject_refs = _vertical_design_subject_refs(vertical_design_graph)
        rule_findings = _rule_findings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            traceability=traceability,
        )
        rule_payload = {
            "ok": not any(item.severity == "blocking" for item in rule_findings),
            "findings": [item.to_dict() for item in rule_findings],
        }
        requested_trials = max(1, int(validation_trials))
        trial_payloads: list[dict[str, object]] = []
        valid_trials: list[tuple[str, dict[str, object], list[DesignFinding]]] = []
        for index in range(requested_trials):
            trial_id = f"trial-{index + 1}"
            try:
                payload = self._semantic_review(
                    class_graph=class_graph,
                    sequence_graph=sequence_graph,
                    traceability=traceability,
                    canonical_requirement=canonical_requirement,
                    vertical_trace=vertical_trace,
                    vertical_design_graph=vertical_design_graph,
                    rule_review=rule_payload,
                    review_claims=review_claims,
                )
                _validate_semantic_assessments(
                    payload,
                    claims=review_claims,
                    valid_subject_refs=valid_subject_refs,
                )
                normalized = _normalize_llm_findings(
                    payload,
                    valid_claim_ids={str(item.get("claim_id") or "") for item in review_claims},
                    valid_requirement_ids=valid_requirement_ids,
                    valid_subject_refs=valid_subject_refs,
                )
                if requested_trials > 1:
                    normalized = _prepare_consensus_trial(normalized, trial_id=trial_id)
                trial_payloads.append(
                    {
                        "trial_id": trial_id,
                        "ok": True,
                        "review": payload,
                        "canonical_keys": sorted({item.canonical_key for item in normalized}),
                    }
                )
                valid_trials.append((trial_id, payload, normalized))
            except LlmStepReviewRequired:
                raise
            except HarnessError as exc:
                if requested_trials == 1:
                    raise
                trial_payloads.append(
                    {
                        "trial_id": trial_id,
                        "ok": False,
                        "error": exc.to_response(),
                        "canonical_keys": [],
                    }
                )

        if requested_trials == 1:
            llm_payload = valid_trials[0][1]
            llm_findings = valid_trials[0][2]
            findings = _deduplicate_findings([*rule_findings, *llm_findings])
            return DesignValidationResult(
                traceability=traceability,
                blocking_findings=[item for item in findings if item.severity == "blocking"],
                warnings=[item for item in findings if item.severity == "warning"],
                rule_review=rule_payload,
                llm_review=llm_payload,
                repair_iterations=repair_iterations,
            )

        consensus = _aggregate_semantic_trials(
            requested_trials=requested_trials,
            valid_trials=valid_trials,
        )
        semantic_findings = [*consensus["blocking_findings"], *consensus["warnings"]]
        findings = _deduplicate_findings([*rule_findings, *semantic_findings])
        semantic_payload = {
            "ok": not consensus["blocking_findings"] and len(valid_trials) == requested_trials,
            "summary": "Semantic findings aggregated by majority vote.",
            "trials": trial_payloads,
            **consensus["metrics"],
        }
        return DesignValidationResult(
            traceability=traceability,
            blocking_findings=[item for item in findings if item.severity == "blocking"],
            warnings=[item for item in findings if item.severity == "warning"],
            rule_review=rule_payload,
            llm_review=semantic_payload,
            repair_iterations=repair_iterations,
            mode="design_validation_consensus_v1",
            semantic_consensus=consensus["metrics"],
            disputed_findings=consensus["disputed_findings"],
            inconclusive=len(valid_trials) != requested_trials,
        )

    def _sliced_review(
        self,
        *,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        traceability: TraceabilityResult,
        canonical_requirement: object | None,
        vertical_trace: dict[str, object] | None,
        vertical_design_graph: dict[str, object] | None,
        repair_iterations: int,
        review_plan: dict[str, object],
        call_budget: int | None,
        task_cache: "SemanticTaskCache | None" = None,
    ) -> DesignValidationResult:
        canonical = _canonical_requirement_dict(canonical_requirement)
        claims = _semantic_review_claims(canonical)
        tasks = _dict_items(review_plan.get("tasks"))
        planned_calls = len(tasks) + 2  # coherence plus at most one targeted adjudication
        rule_findings = _rule_findings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            traceability=traceability,
        )
        rule_payload = {
            "ok": not any(item.severity == "blocking" for item in rule_findings),
            "findings": [item.to_dict() for item in rule_findings],
        }
        if call_budget is not None and planned_calls > call_budget:
            return self._sliced_inconclusive(
                traceability=traceability,
                rule_findings=rule_findings,
                rule_payload=rule_payload,
                repair_iterations=repair_iterations,
                review_plan=review_plan,
                error={
                    "error_code": "design_semantic_review_budget_exceeded",
                    "message": (
                        f"Sliced semantic review needs up to {planned_calls} calls but the budget is {call_budget}."
                    ),
                },
            )

        valid_requirement_ids = {
            requirement_id
            for claim in claims
            for requirement_id in _string_items(claim.get("requirement_ids"))
        }
        valid_requirement_ids.update(
            str(claim.get("scenario_id") or "")
            for claim in claims
            if str(claim.get("scenario_id") or "")
        )
        valid_subject_refs = _vertical_design_subject_refs(vertical_design_graph)
        trace_view = _vertical_trace_review_view(vertical_trace)
        task_results: list[dict[str, object]] = []
        semantic_findings: list[DesignFinding] = []
        runtime_blockers: dict[str, list[str]] = {}
        skipped_calls = 0
        retry_calls = 0
        metric_start = len(_llm_call_metrics(self.llm_client))
        try:
            for task in tasks:
                task_id = str(task.get("task_id") or "")
                task_type = str(task.get("task_type") or "")
                scenario_ids = _string_items(task.get("scenario_ids"))
                primary_ids = set(_string_items(task.get("primary_claim_ids")))
                context_ids = set(_string_items(task.get("context_claim_ids")))
                task_claims = [claim for claim in claims if str(claim.get("claim_id") or "") in primary_ids]
                blocked_by = sorted(
                    finding_id for scenario_id in scenario_ids for finding_id in runtime_blockers.get(scenario_id, [])
                )
                if task_type == "scenario_behavior" and blocked_by:
                    # Harness gate: behavior is not reviewed on a runtime path already known to be blocked.
                    skipped_calls += 1
                    task_results.append(
                        {
                            "task_id": task_id,
                            "task_type": task_type,
                            "status": "deferred_runtime_blocked",
                            "ok": False,
                            "summary": "Behavior review deferred until the scenario runtime blockers are repaired.",
                            "assessments": [
                                {
                                    "claim_id": claim_id,
                                    "status": "deferred",
                                    "design_refs": [],
                                    "blocked_by": blocked_by,
                                    "reason": "Scenario runtime review reported blocking findings.",
                                }
                                for claim_id in sorted(primary_ids)
                            ],
                            "findings": [],
                            "deferred_claim_ids": sorted(primary_ids),
                        }
                    )
                    continue
                task_context = materialize_task_context(
                    task,
                    claims=claims,
                    vertical_design_graph=vertical_design_graph,
                    vertical_trace=trace_view,
                )

                cache_key = (
                    semantic_task_cache_key(self._semantic_task_prompt(task_context=task_context, rule_review=rule_payload))
                    if task_cache is not None
                    else ""
                )
                cached_payload = task_cache.get(cache_key) if task_cache is not None else None

                def review_task(correction: str) -> tuple[dict[str, object], list[DesignFinding]]:
                    task_payload = (
                        dict(cached_payload)
                        if cached_payload is not None and not correction
                        else self._semantic_task_review(
                            task_context=task_context,
                            rule_review=rule_payload,
                            correction=correction,
                        )
                    )
                    _validate_semantic_assessments(
                        task_payload,
                        claims=task_claims,
                        valid_subject_refs=valid_subject_refs,
                    )
                    return task_payload, _normalize_llm_findings(
                        task_payload,
                        valid_claim_ids=primary_ids | context_ids,
                        valid_requirement_ids=valid_requirement_ids,
                        valid_subject_refs=valid_subject_refs,
                    )

                (payload, normalized), retries = _with_contract_retry(review_task)
                cache_hit = cached_payload is not None and retries == 0
                if cache_hit:
                    skipped_calls += 1
                else:
                    retry_calls += retries
                    if task_cache is not None:
                        task_cache.put(cache_key, payload, task_id=task_id)
                normalized = [
                    replace(
                        finding,
                        finding_id=f"{task_id}:{finding.finding_id}",
                        review_task_ids=[task_id],
                    )
                    for finding in normalized
                ]
                semantic_findings.extend(normalized)
                if task_type == "scenario_runtime":
                    blocking_ids = [finding.finding_id for finding in normalized if finding.severity == "blocking"]
                    if blocking_ids:
                        for scenario_id in scenario_ids:
                            runtime_blockers.setdefault(scenario_id, []).extend(blocking_ids)
                task_results.append(
                    {
                        "task_id": task_id,
                        "task_type": task_type,
                        "status": "reused_identical_input" if cache_hit else "reviewed",
                        "contract_retries": retries,
                        "ok": bool(payload.get("ok")),
                        "summary": str(payload.get("summary") or ""),
                        "assessments": _dict_items(payload.get("assessments")),
                        "findings": [finding.to_dict() for finding in normalized],
                        "deferred_claim_ids": [],
                    }
                )

            shared_surfaces = _dict_items(review_plan.get("shared_surfaces"))
            shared_node_ids = {
                node_id
                for surface in shared_surfaces
                for node_id in _string_items(surface.get("node_ids"))
            }
            shared_surface_nodes = [
                review_node_view(node, task_type="coherence")
                for node in _dict_items(_dict_value(vertical_design_graph).get("nodes"))
                if str(node.get("id") or "") in shared_node_ids
            ]

            def review_coherence(correction: str) -> tuple[dict[str, object], list[DesignFinding]]:
                coherence_payload = self._semantic_coherence_review(
                    task_results=task_results,
                    relationship_index=relation_index(vertical_design_graph),
                    claims=claims,
                    shared_surfaces=shared_surfaces,
                    shared_surface_nodes=shared_surface_nodes,
                    correction=correction,
                )
                return coherence_payload, _normalize_llm_findings(
                    coherence_payload,
                    valid_claim_ids={str(claim.get("claim_id") or "") for claim in claims},
                    valid_requirement_ids=valid_requirement_ids,
                    valid_subject_refs=valid_subject_refs,
                )

            (coherence, coherence_findings), retries = _with_contract_retry(review_coherence)
            retry_calls += retries
            coherence_findings = [
                replace(
                    finding,
                    finding_id=f"coherence:{finding.finding_id}",
                    review_task_ids=["coherence"],
                )
                for finding in coherence_findings
            ]
            semantic_findings.extend(coherence_findings)
            conflicts = _dict_items(coherence.get("conflicts"))
            adjudication: dict[str, object] = {}
            actual_calls = len(tasks) - skipped_calls + 1 + retry_calls
            if conflicts:
                existing_ids = {finding.finding_id for finding in semantic_findings}

                def review_adjudication(correction: str) -> tuple[dict[str, object], list[DesignFinding]]:
                    adjudication_payload = self._semantic_adjudication_review(
                        conflicts=conflicts,
                        findings=[finding.to_dict() for finding in semantic_findings],
                        claims=claims,
                        correction=correction,
                    )
                    unknown_drops = set(_string_items(adjudication_payload.get("drop_finding_ids"))) - existing_ids
                    if unknown_drops:
                        raise HarnessError(
                            "design_semantic_review_failed",
                            f"Design semantic adjudication tried to drop unknown findings {sorted(unknown_drops)}.",
                        )
                    return adjudication_payload, _normalize_llm_findings(
                        adjudication_payload,
                        valid_claim_ids={str(claim.get("claim_id") or "") for claim in claims},
                        valid_requirement_ids=valid_requirement_ids,
                        valid_subject_refs=valid_subject_refs,
                    )

                (adjudication, adjudicated), retries = _with_contract_retry(review_adjudication)
                actual_calls += retries
                drop_ids = set(_string_items(adjudication.get("drop_finding_ids")))
                semantic_findings = [finding for finding in semantic_findings if finding.finding_id not in drop_ids]
                semantic_findings.extend(
                    replace(
                        finding,
                        finding_id=f"adjudication:{finding.finding_id}",
                        review_task_ids=["adjudication"],
                    )
                    for finding in adjudicated
                )
                actual_calls += 1
        except LlmStepReviewRequired:
            raise
        except HarnessError as exc:
            return self._sliced_inconclusive(
                traceability=traceability,
                rule_findings=rule_findings,
                rule_payload=rule_payload,
                repair_iterations=repair_iterations,
                review_plan=review_plan,
                error=exc.to_response(),
                task_results=task_results,
                call_metrics=_llm_call_metrics(self.llm_client)[metric_start:],
            )

        findings = _deduplicate_findings([*rule_findings, *semantic_findings])
        call_metrics = _llm_call_metrics(self.llm_client)[metric_start:]
        return DesignValidationResult(
            traceability=traceability,
            blocking_findings=[item for item in findings if item.severity == "blocking"],
            warnings=[item for item in findings if item.severity == "warning"],
            rule_review=rule_payload,
            llm_review={
                "ok": not any(item.severity == "blocking" for item in semantic_findings),
                "summary": "Sliced specialist reviews completed with a compact coherence pass.",
                "tasks": task_results,
                "coherence": coherence,
                "adjudication": adjudication,
                "planned_calls": planned_calls,
                "actual_calls": actual_calls,
                "call_metrics": call_metrics,
                "usage": _summarize_call_metrics(call_metrics),
                "coverage": review_plan.get("coverage") or {},
                "runtime_blocked_scenarios": sorted(runtime_blockers),
            },
            repair_iterations=repair_iterations,
            mode="design_validation_sliced_v1",
            semantic_consensus={
                "requested_trials": 1,
                "valid_trials": 1,
                "quorum": 1,
                "consistency_score": 1.0,
                "llm_calls": {"planned": planned_calls, "actual": actual_calls},
            },
            semantic_review_plan=review_plan,
        )

    def _sliced_inconclusive(
        self,
        *,
        traceability: TraceabilityResult,
        rule_findings: list[DesignFinding],
        rule_payload: dict[str, object],
        repair_iterations: int,
        review_plan: dict[str, object],
        error: dict[str, object],
        task_results: list[dict[str, object]] | None = None,
        call_metrics: list[dict[str, object]] | None = None,
    ) -> DesignValidationResult:
        return DesignValidationResult(
            traceability=traceability,
            blocking_findings=[item for item in rule_findings if item.severity == "blocking"],
            warnings=[item for item in rule_findings if item.severity == "warning"],
            rule_review=rule_payload,
            llm_review={
                "ok": False,
                "inconclusive": True,
                "summary": "Sliced semantic review did not complete; Design cannot reach checkpoint.",
                "error": error,
                "tasks": task_results or [],
                "call_metrics": call_metrics or [],
                "usage": _summarize_call_metrics(call_metrics or []),
                "coverage": review_plan.get("coverage") or {},
            },
            repair_iterations=repair_iterations,
            mode="design_validation_sliced_v1",
            inconclusive=True,
            semantic_review_plan=review_plan,
        )

    def _semantic_task_review(
        self,
        *,
        correction: str = "",
        task_context: dict[str, object],
        rule_review: dict[str, object],
    ) -> dict[str, object]:
        prompt = self._semantic_task_prompt(task_context=task_context, rule_review=rule_review)
        return _semantic_json_completion(self.llm_client, prompt=_with_contract_correction(prompt, correction))

    @staticmethod
    def _semantic_task_prompt(*, task_context: dict[str, object], rule_review: dict[str, object]) -> str:
        task = _dict_value(task_context.get("task"))
        task_type = str(task.get("task_type") or "")
        header, scope, task_rules = _SEMANTIC_TASK_PROMPTS.get(task_type, _SEMANTIC_TASK_PROMPTS["cross_cutting"])
        return f"""{header}

Review only the assigned task. Primary claims require exactly one assessment each. Context claims may be cited by
findings but must not be assessed. Review obligations are validation coverage, not new Requirements.

{scope}

Return one JSON object only:
{{"ok":true,"summary":"short summary","assessments":[{{"claim_id":"exact supplied id","status":"satisfied|missing|contradicted|uncertain","design_refs":["exact VDN-* or VDE-* id"],"reason":"evidence-backed decision"}}],"findings":[{{"id":"SEM-001","category":"behavior_order|terminal_state|cross_diagram_contract|runtime_boundary_feasibility|decision_input_sufficiency|runtime_topology|data_consistency|transaction_boundary|negative_constraint|unsupported_assumption|ui_state_coverage|ui_journey_coherence|ui_runtime_input_binding|interaction_feedback|accessibility|responsive_attachment|authentication_contract|authorization_contract","severity":"blocking|warning","message":"specific issue","expected_change":"what must become true, stated on existing node/edge ids","claim_ids":["exact supplied claim id"],"requirement_ids":["exact Requirement id"],"diagram_ids":[],"subject_ref":"exact VDN-* or VDE-* id","evidence":[{{"source":"requirement|design","ref":"exact id","excerpt":"short evidence"}}]}}]}}

{_PACKET_FIELD_GUIDE}

{_SEVERITY_GUIDE}

Rules:
- Never invent a Requirement or Design reference. Every blocker cites a stable Requirement id and exact Design ref.
- A non-satisfied primary claim requires a finding. A satisfied claim requires exact Design evidence.
- expected_change states what must become true, in terms of existing node and edge ids. Do not prescribe a design,
  and do not introduce a system, service or dependency that neither the approved Requirement nor the packet contains.
{task_rules}

Task focus:
{task.get("focus")}

Assigned review packet:
{json.dumps(task_context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

{_DETERMINISTIC_REVIEW_NOTE}
{json.dumps(rule_review, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}
""".strip()

    def _semantic_coherence_review(
        self,
        *,
        correction: str = "",
        task_results: list[dict[str, object]],
        relationship_index: dict[str, object],
        claims: list[dict[str, object]],
        shared_surfaces: list[dict[str, object]],
        shared_surface_nodes: list[dict[str, object]],
    ) -> dict[str, object]:
        prompt = f"""Design Semantic Coherence Review v2

Review the specialist summaries and compact relationship index for contradictions across tasks. Do not reassess every
claim and do not invent missing graph detail. Return one JSON object only:
{{"ok":true,"summary":"short summary","findings":[],"conflicts":[{{"finding_ids":["exact existing finding id"],"claim_ids":["exact claim id"],"reason":"why adjudication is needed"}}]}}

Emit a new finding only for a cross-task defect with exact Requirement and Design evidence. Use the same finding schema
as the specialists; its expected_change states what must become true on existing node and edge ids, without
prescribing a design or introducing a system the Requirement and graph do not contain. Put genuine disagreement or blocking uncertainty in conflicts; cosmetic wording differences are not
conflicts. Pay special attention to a state or event being treated incompatibly across runtime, UI and data tasks.
Review each shared surface across its scenarios as one coherent contract for state, actions, feedback, layout, focus,
inputs and availability; visual taste alone is only a warning. Never assess or raise a finding on a claim listed as
deferred in a specialist result, and do not re-report a runtime blocker already present.

Claims:
{json.dumps(claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Specialist results:
{json.dumps(task_results, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Relationship index:
{json.dumps(relationship_index, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

shared_surfaces:
{json.dumps(shared_surfaces, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Full shared surface node contracts:
{json.dumps(shared_surface_nodes, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}
""".strip()
        payload = _semantic_json_completion(self.llm_client, prompt=_with_contract_correction(prompt, correction))
        if not isinstance(payload.get("conflicts", []), list):
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic coherence response must contain a conflicts array.",
            )
        return payload

    def _semantic_adjudication_review(
        self,
        *,
        correction: str = "",
        conflicts: list[dict[str, object]],
        findings: list[dict[str, object]],
        claims: list[dict[str, object]],
    ) -> dict[str, object]:
        prompt = f"""Design Semantic Adjudication v1

Resolve only the supplied conflicts. Return one JSON object only:
{{"ok":true,"summary":"short resolution","drop_finding_ids":["exact existing finding id"],"findings":[]}}

Keep an existing finding unless exact Requirement and Design evidence disproves it. New findings use the specialist
finding schema and exact supplied references. Do not reassess unrelated claims.

Conflicts:
{json.dumps(conflicts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Existing findings:
{json.dumps(findings, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Claims:
{json.dumps(claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}
""".strip()
        payload = _semantic_json_completion(self.llm_client, prompt=_with_contract_correction(prompt, correction))
        if not isinstance(payload.get("drop_finding_ids", []), list):
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic adjudication response must contain a drop_finding_ids array.",
            )
        return payload

    def _semantic_review(
        self,
        *,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        traceability: TraceabilityResult,
        canonical_requirement: object | None,
        vertical_trace: dict[str, object] | None,
        vertical_design_graph: dict[str, object] | None,
        rule_review: dict[str, object],
        review_claims: list[dict[str, object]],
    ) -> dict[str, object]:
        canonical = _canonical_requirement_dict(canonical_requirement)
        prompt = f"""Design Semantic Review v3

Review the approved Requirement against the canonical Vertical Design Graph.

Source-of-truth order:
1. Canonical Requirement review claims define required behavior.
2. Runtime-bound Vertical Design Graph defines the Design.
3. Requirement-to-Design mapping and deterministic review provide structural evidence.
4. Class and sequence graphs are derived compatibility projections. They may expose a contradiction but never override items 1-3.

Review every claim exactly once. Do not infer a missing behavior only because a derived projection omits detail that is present in the canonical Vertical Design Graph. A node or edge that only repeats a required outcome is not evidence of an executable mechanism or causal order.

Return one JSON object only:
{{
  "ok": true,
  "summary": "short summary",
  "assessments": [
    {{
      "claim_id": "SC-1:MF-1",
      "status": "satisfied|missing|contradicted|uncertain",
      "design_refs": ["exact VDN-* or VDE-* id"],
      "reason": "short evidence-backed decision"
    }}
  ],
  "findings": [
    {{
      "id": "SEM-001",
      "category": "behavior_order|terminal_state|cross_diagram_contract|runtime_boundary_feasibility|decision_input_sufficiency|runtime_topology|data_consistency|transaction_boundary|negative_constraint|unsupported_assumption|ui_state_coverage|ui_journey_coherence|ui_runtime_input_binding|interaction_feedback|accessibility|responsive_attachment|authentication_contract|authorization_contract",
      "severity": "blocking|warning",
      "message": "specific contradiction or ambiguity",
      "expected_change": "specific UML correction",
      "claim_ids": ["SC-1:MF-1"],
      "requirement_ids": ["MAIN-1"],
      "diagram_ids": ["sequence_diagram_1"],
      "subject_ref": "exact VDN-* or VDE-* id",
      "evidence": [{{"source": "requirement|design", "ref": "stable id", "excerpt": "short exact evidence"}}]
    }}
  ]
}}

Assessment contract:
- Return exactly one assessment for every supplied claim_id and no invented claim IDs.
- satisfied assessments require at least one exact Design node/edge ID in design_refs.
- missing, contradicted, and uncertain assessments require a finding bound through claim_ids.
- Finding requirement_ids and subject_ref must use exact IDs supplied below.
- For finding evidence with source "requirement", ref must be an exact Requirement ID from requirement_ids
  (for example "MF-1" or "AC-3"), never a composite claim_id such as "SC-1:MF-1".
- For finding evidence with source "design" or "design_graph", ref must be an exact VDN-* or VDE-* ID supplied below.

Blocking policy:
- Block a required behavior that is missing, contradicted, or ordered so its guard cannot prevent the action.
- Block a sequence call whose receiver contract is inconsistent with the class design.
- Block success, cancellation, and failure paths that reach the wrong terminal state.
- Block persistence before required human confirmation only when neither the UI contract nor the interaction path represents that confirmation before the write.
- Do not require a preliminary backend verification round trip unless the approved requirement explicitly requires it.
- Reusing one UI owner across main and alternative scenarios is valid when each projection keeps its own behavior contract.
- Verify that every runtime edge is executable. A direct call may stay within one application or target an importable
  shared package; a call into another application requires an explicit API/transport surface.
- For every decision, guard, branch, and exception outcome, verify that the receiving node has enough information to
  decide it through an input/boundary mapping, a response or error event, an already-selected control-flow path, or
  local runtime state. Do not require a dedicated boolean when the control flow already identifies the condition.
  Block when the Design names a decision but models no source from which the receiver can make it.
- Do not prescribe one fixed UI -> API -> business-rule -> data chain. Fan-out, caller-local rules, dedicated error
  handlers, and other topologies are valid when every required surface is reachable and the declared path is executable.
- A business rule does not imply backend placement. When a scenario marks API as not applicable, do not invent an
  endpoint to connect UI to a backend service; report the placement/boundary contradiction so the Designer can keep
  local logic in the caller application or an importable shared package.
- Validate causal order: a condition may justify behavior only after a modeled runtime surface or edge can observe it.
  A broken transport cannot also carry a new request or response, and naming an error scenario or projection does not
  prove when the error becomes known.
- A promised rollback, unchanged record, atomic outcome or safe retry needs an executable transaction boundary,
  compensation, idempotency or reconciliation path. Repeating the promised outcome in node, edge or return prose is
  not mechanism evidence; block when Requirement or Design promises the guarantee but no such mechanism is modeled.
- Do not demand those mechanisms when neither Requirement nor Design promises the guarantee.
- For each Vertical Design UI node, review its experience contract: component structure, observable feedback,
  accessibility, responsive behavior, and focus flow must agree with the approved interaction and states.
- Block a UI experience issue only when it omits or contradicts approved behavior, makes a required state or
  outcome unobservable, or makes the approved interaction impossible. Visual polish and aesthetic preferences
  are warnings unless the approved requirement explicitly makes them mandatory.
- UI/UX guidance references are design evidence, not new Requirements; never promote a recommendation into a blocker by itself.
- Treat approved negative constraints as behavior: inspect transitions, states, feedback, outcomes, and edges for a
  contradiction instead of relying on keyword-specific deterministic rules.
- Review authentication_required, authentication_source, and authentication Requirement refs against the full approved
  preconditions and flows. Keep actor authentication separate from role/permission authorization; do not decide either
  from field labels alone. Review authorization_required against the approved Requirement as the typed Design decision.
- Review focus and announcement behavior semantically. A live/status announcement may announce without receiving
  keyboard focus; block focus movement only when the modeled target makes an approved interaction inaccessible or
  targets an element that no longer exists.
{_holistic_ui_review_instruction(vertical_design_graph)}- Never invent a requirement. Every semantic blocker must cite a stable requirement id and design evidence.
- Give every finding an exact Vertical Design node or edge ID as subject_ref.
- An uncertainty without evidence is a warning, not a blocker.

Canonical Requirement review packet:
{json.dumps(_canonical_requirement_review_view(canonical, review_claims), ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Canonical Vertical Design Graph semantic view:
{json.dumps(_vertical_design_review_view(vertical_design_graph), ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Requirement-to-Design mapping view:
{json.dumps(_vertical_trace_review_view(vertical_trace), ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Compatibility class projection:
{json.dumps(class_graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Compatibility sequence projection:
{json.dumps(sequence_graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}

Deterministic review:
{json.dumps(rule_review, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}
""".strip()
        raw = _complete_json(
            self.llm_client,
            system="You are SDLC Harness Design Validator. Return strict JSON only.",
            prompt=prompt,
        )
        try:
            payload = parse_first_json_object(raw, expected_keys={"ok", "findings"})
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic validator did not return parseable JSON.",
            ) from exc
        if not isinstance(payload.get("findings"), list):
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic validator response must contain a findings array.",
            )
        payload["ok"] = bool(payload.get("ok"))
        payload["summary"] = str(payload.get("summary") or "").strip()
        return payload


def _rule_findings(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    traceability: TraceabilityResult,
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    counter = 0
    for diagram_name, missing in traceability.missing_links.items():
        for item in missing:
            req_id = str(item.get("requirement") or "")
            counter += 1
            findings.append(
                DesignFinding(
                    finding_id=f"TRACE-{counter:03d}",
                    category="traceability_missing",
                    severity="blocking",
                    message=f"Requirement {req_id} has no {diagram_name.replace('_', ' ')} link.",
                    expected_change="Add the UML element and trace target that implement this approved requirement.",
                    requirement_ids=[req_id] if req_id else [],
                    evidence=[{"source": "requirement", "ref": req_id, "excerpt": str(item.get("content") or "")}],
                    subject_ref=f"{diagram_name}:{req_id}",
                    origin="deterministic",
                )
            )
    findings.extend(_structural_findings(class_graph=class_graph, sequence_graph=sequence_graph))
    return findings


def _structural_findings(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    counter = 0

    def add(category: str, message: str, expected: str, *, diagram: str = "", evidence: str = "") -> None:
        nonlocal counter
        counter += 1
        findings.append(
            DesignFinding(
                finding_id=f"RULE-{counter:03d}",
                category=category,
                severity="blocking",
                message=message,
                expected_change=expected,
                diagram_ids=[diagram] if diagram else [],
                evidence=[
                    {"source": "design_graph", "ref": f"{diagram}:{evidence}", "excerpt": evidence}
                ]
                if evidence
                else [],
                subject_ref=f"{diagram}:{evidence}" if evidence else diagram,
                origin="deterministic",
            )
        )

    classes = _class_symbols(class_graph)
    for diagram in _dict_items(sequence_graph.get("sequenceDiagrams")):
        diagram_id = str(diagram.get("id") or "sequence_diagram")
        for issue in _dict_items(diagram.get("parseIssues")):
            add(
                "control_flow_structure",
                str(issue.get("message") or "Invalid sequence control flow."),
                "Balance and correctly nest the PlantUML control fragments.",
                diagram=diagram_id,
                evidence=str(issue.get("line") or ""),
            )
        nodes = {
            str(item.get("id") or ""): item
            for item in _dict_items(diagram.get("nodes"))
            if str(item.get("id") or "")
        }
        for message in _dict_items(diagram.get("messages")):
            if str(message.get("type") or "") == "return":
                continue
            message_text = str(message.get("message") or "")
            operation = _operation_name(message_text)
            if not operation:
                continue
            target_id = str(message.get("to") or "")
            target_node = nodes.get(target_id, {})
            if str(target_node.get("type") or "").lower() == "actor":
                continue
            matches = _matching_classes(target_id, str(target_node.get("label") or ""), classes)
            event_id = str(message.get("id") or "")
            if not matches:
                add(
                    "unresolved_lifeline",
                    f"Sequence receiver `{target_id}` cannot be resolved to a class or interface.",
                    "Add or rename the class/interface and lifeline so their ownership is explicit.",
                    diagram=diagram_id,
                    evidence=event_id or str(message.get("message") or ""),
                )
                continue
            if len(matches) == 1 and operation not in classes[matches[0]]:
                add(
                    "cross_diagram_contract",
                    f"Sequence calls `{target_id}.{operation}` but `{matches[0]}` does not declare that operation.",
                    f"Declare `{operation}` on `{matches[0]}` or change the sequence call to an existing operation.",
                    diagram=diagram_id,
                    evidence=event_id or str(message.get("message") or ""),
                )
                continue
    return findings


def _canonical_requirement_dict(value: object | None) -> dict[str, object]:
    if value is not None and hasattr(value, "to_dict"):
        value = value.to_dict()
    return dict(value) if isinstance(value, dict) else {}


def _semantic_review_claims(canonical: dict[str, object]) -> list[dict[str, object]]:
    if not canonical:
        return []
    records: dict[str, dict[str, object]] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            record_id = str(value.get("id") or "").strip()
            if record_id:
                current = records.get(record_id)
                if current is None or len(_record_statement(value)) > len(_record_statement(current)):
                    records[record_id] = value
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(canonical)
    claims: list[dict[str, object]] = []
    claimed_requirements: set[str] = set()
    scenarios = _dict_items(_dict_value(canonical.get("scenario_contract")).get("scenarios"))
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "").strip()
        if not scenario_id:
            continue
        refs = _merged_string_values(
            [_string_items(scenario.get("requirement_refs")), _string_items(scenario.get("acceptance_refs"))]
        )
        for requirement_id in refs:
            claimed_requirements.add(requirement_id)
            claims.append(
                {
                    "claim_id": f"{scenario_id}:{requirement_id}",
                    "claim_type": "requirement",
                    "scenario_id": scenario_id,
                    "requirement_ids": [requirement_id],
                    "statement": _record_statement(records.get(requirement_id, {})) or requirement_id,
                }
            )
        for surface, raw_contract in sorted(_dict_value(scenario.get("surfaces")).items()):
            contract = _dict_value(raw_contract)
            if str(contract.get("status") or "") != "required":
                continue
            claims.append(
                {
                    "claim_id": f"{scenario_id}:surface:{surface}",
                    "claim_type": "required_surface",
                    "scenario_id": scenario_id,
                    "requirement_ids": _string_items(contract.get("requirement_refs")),
                    "surface": str(surface),
                    "statement": str(contract.get("reason") or f"{surface} is required.").strip(),
                }
            )
    for section in ("preconditions", "postconditions", "quality_requirements"):
        for record in _dict_items(canonical.get(section)):
            requirement_id = str(record.get("id") or "").strip()
            if not requirement_id or requirement_id in claimed_requirements:
                continue
            claims.append(
                {
                    "claim_id": f"GLOBAL:{requirement_id}",
                    "claim_type": section.rstrip("s"),
                    "scenario_id": "",
                    "requirement_ids": [requirement_id],
                    "statement": _record_statement(record) or requirement_id,
                }
            )
    return claims


def _record_statement(record: dict[str, object]) -> str:
    parts = []
    for key in ("text", "trigger", "action", "outcome", "target", "name", "reason", "description"):
        value = record.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            parts.append(str(value).strip())
    return " ".join(parts)


def _canonical_requirement_review_view(
    canonical: dict[str, object], claims: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": "design_semantic_requirement_view_v1",
        "use_case": canonical.get("use_case") or {},
        "claims": claims,
        "assumptions": canonical.get("assumptions") or [],
        "open_questions": canonical.get("open_questions") or [],
        "decisions": canonical.get("decisions") or [],
    }


def _vertical_design_review_view(value: dict[str, object] | None) -> dict[str, object]:
    graph = _dict_value(value)
    node_keys = (
        "id",
        "scenario_id",
        "component_id",
        "surface",
        "kind",
        "name",
        "semantic_key",
        "disposition",
        "requirement_refs",
        "contract",
        "surface_contract",
        "runtime_binding",
    )
    edge_keys = (
        "id",
        "scenario_id",
        "order",
        "from",
        "to",
        "kind",
        "label",
        "terminal",
        "branch_group",
        "requirement_refs",
        "boundary_contract",
    )
    return {
        "schema_version": "vertical_design_semantic_view_v1",
        "graph_digest": graph.get("artifact_digest"),
        "nodes": [
            {key: node[key] for key in node_keys if key in node}
            for node in _dict_items(graph.get("nodes"))
        ],
        "edges": [
            {key: edge[key] for key in edge_keys if key in edge}
            for edge in _dict_items(graph.get("edges"))
        ],
        "unresolved_items": graph.get("unresolved_items") or [],
        **({"ui_repo_context": _ui_repo_context_review_view(graph)} if _ui_contract_v2(graph) else {}),
    }


def _ui_contract_v2(graph: dict[str, object]) -> bool:
    binding = _dict_value(graph.get("snapshot_binding"))
    return binding.get("designer_ui_contract_version") == "ui_experience_contract_v2"


def _ui_repo_context_review_view(graph: dict[str, object]) -> dict[str, object]:
    context = _dict_value(_dict_value(graph.get("snapshot_binding")).get("ui_repo_context"))
    return {
        "entrypoint": _dict_value(context.get("entrypoint")).get("ref"),
        "composition_chain": [
            {"ref": hop.get("ref"), "renders": hop.get("renders"), "passes_props": hop.get("passes_props")}
            for hop in _dict_items(context.get("composition_chain"))
        ],
        "data_clients": [item.get("ref") for item in _dict_items(context.get("data_clients"))],
        "auth": context.get("auth"),
        "styles": context.get("styles"),
        "verification": context.get("verification"),
    }


def _holistic_ui_review_instruction(vertical_design_graph: dict[str, object] | None) -> str:
    if not _ui_contract_v2(_dict_value(vertical_design_graph)):
        return ""
    return (
        "- UI contract v2: review each screen as one journey. Nodes sharing a screen_id must tell one consistent story "
        "across scenarios (ui_journey_coherence). runtime_states must make loading, empty, error and success observable "
        "when the approved behavior can reach them; not_applicable needs a reason that matches the Requirement. "
        "focus_policies must give one deterministic target per event. Using ui_repo_context, check that every "
        "required_input classification is plausible: external_use_case must match an approved precondition, and a "
        "list-driven or authenticated screen marked bound must have a real provider (ui_runtime_input_binding). "
        "Block only when a required behavior or state becomes unreachable or unobservable; visual taste stays a warning. "
        "For role=composition_wiring, runtime_surface.forwarded_inputs is the authoritative per-screen propagation "
        "contract; required_inputs lists only inputs consumed by the AS-IS hop. Do not demand screen inputs be "
        "duplicated in both fields.\n"
    )


def _vertical_trace_review_view(value: dict[str, object] | None) -> dict[str, object]:
    trace = _dict_value(value)
    nodes = {str(item.get("id") or ""): item for item in _dict_items(trace.get("nodes"))}
    mappings = []
    for edge in _dict_items(trace.get("edges")):
        if str(edge.get("kind") or "") not in {"realizes", "depends_on"}:
            continue
        source_id = str(edge.get("from") or "")
        target_id = str(edge.get("to") or "")
        mappings.append(
            {
                "kind": str(edge.get("kind") or ""),
                "from": source_id,
                "from_kind": str(nodes.get(source_id, {}).get("kind") or ""),
                "to": target_id,
                "to_kind": str(nodes.get(target_id, {}).get("kind") or ""),
            }
        )
    return {
        "schema_version": "vertical_trace_semantic_mapping_v1",
        "graph_digest": trace.get("graph_digest") or trace.get("artifact_digest"),
        "mappings": mappings,
    }


def _vertical_design_subject_refs(value: dict[str, object] | None) -> set[str]:
    graph = _dict_value(value)
    return {
        str(item.get("id") or "")
        for item in [*_dict_items(graph.get("nodes")), *_dict_items(graph.get("edges"))]
        if str(item.get("id") or "")
    }


def _validate_semantic_assessments(
    payload: dict[str, object],
    *,
    claims: list[dict[str, object]],
    valid_subject_refs: set[str],
) -> None:
    expected = {str(item.get("claim_id") or "") for item in claims}
    assessments = _dict_items(payload.get("assessments"))
    received = [str(item.get("claim_id") or "") for item in assessments]
    if len(received) != len(set(received)) or set(received) != expected:
        raise HarnessError(
            "design_semantic_review_failed",
            "Design semantic validator must assess every supplied claim exactly once.",
        )
    findings_by_claim = {
        claim_id
        for finding in _dict_items(payload.get("findings"))
        for claim_id in _string_items(finding.get("claim_ids"))
    }
    for assessment in assessments:
        claim_id = str(assessment.get("claim_id") or "")
        status = str(assessment.get("status") or "").strip().lower()
        if status not in {"satisfied", "missing", "contradicted", "uncertain"}:
            raise HarnessError(
                "design_semantic_review_failed",
                f"Design semantic assessment {claim_id} has an invalid status.",
            )
        design_refs = _string_items(assessment.get("design_refs"))
        if any(ref not in valid_subject_refs for ref in design_refs):
            raise HarnessError(
                "design_semantic_review_failed",
                f"Design semantic assessment {claim_id} cites unknown Design refs "
                f"{[ref for ref in design_refs if ref not in valid_subject_refs]}.",
            )
        if status == "satisfied" and not design_refs:
            raise HarnessError(
                "design_semantic_review_failed",
                f"Satisfied Design semantic assessment {claim_id} requires Design evidence.",
            )
        if status != "satisfied" and claim_id not in findings_by_claim:
            raise HarnessError(
                "design_semantic_review_failed",
                f"Non-satisfied Design semantic assessment {claim_id} requires an actionable finding.",
            )


def _normalize_llm_findings(
    payload: dict[str, object],
    *,
    valid_claim_ids: set[str] | None = None,
    valid_requirement_ids: set[str] | None = None,
    valid_subject_refs: set[str] | None = None,
) -> list[DesignFinding]:
    strict_claim_ids = {item for item in (valid_claim_ids or set()) if item}
    strict_requirement_ids = {item for item in (valid_requirement_ids or set()) if item}
    strict_subject_refs = {item for item in (valid_subject_refs or set()) if item}
    findings: list[DesignFinding] = []
    for index, raw in enumerate(_dict_items(payload.get("findings")), start=1):
        severity = str(raw.get("severity") or "warning").strip().lower()
        if severity not in {"blocking", "warning"}:
            severity = "warning"
        claim_ids = _string_items(raw.get("claim_ids"))
        requirement_ids = _string_items(raw.get("requirement_ids"))
        if strict_claim_ids and (not claim_ids or any(item not in strict_claim_ids for item in claim_ids)):
            raise HarnessError(
                "design_semantic_review_failed",
                f"Design semantic finding {raw.get('id')} contains a missing or unknown claim_id: {claim_ids}.",
            )
        if strict_requirement_ids and any(item not in strict_requirement_ids for item in requirement_ids):
            raise HarnessError(
                "design_semantic_review_failed",
                f"Design semantic finding {raw.get('id')} contains an unknown Requirement ID: "
                f"{[item for item in requirement_ids if item not in strict_requirement_ids]}.",
            )
        if severity == "blocking" and not requirement_ids:
            severity = "warning"
        evidence = []
        for item in _dict_items(raw.get("evidence")):
            evidence.append(
                {
                    "source": str(item.get("source") or "").strip(),
                    "ref": str(item.get("ref") or "").strip(),
                    "excerpt": str(item.get("excerpt") or "").strip()[:500],
                }
            )
        diagram_ids = _string_items(raw.get("diagram_ids"))
        subject_ref = str(raw.get("subject_ref") or "").strip()
        if not subject_ref:
            subject_ref = _subject_ref_from_evidence(evidence, diagram_ids)
        if strict_subject_refs and subject_ref not in strict_subject_refs:
            raise HarnessError(
                "design_semantic_review_failed",
                f"Design semantic finding {raw.get('id')} subject_ref {subject_ref!r} is not an exact Vertical Design "
                "node or edge ID.",
            )
        if strict_requirement_ids or strict_subject_refs:
            for item in evidence:
                source = item["source"]
                ref = item["ref"]
                if source == "requirement" and ref not in strict_requirement_ids:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        f"Design semantic finding {raw.get('id')} cites an unknown Requirement evidence ref {ref!r}.",
                    )
                if source in {"design", "design_graph"} and ref not in strict_subject_refs:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        f"Design semantic finding {raw.get('id')} cites an unknown Design evidence ref {ref!r}.",
                    )
                if source not in {"requirement", "design", "design_graph"}:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        f"Design semantic finding {raw.get('id')} evidence source {source!r} is unsupported.",
                    )
        findings.append(
            DesignFinding(
                finding_id=str(raw.get("id") or f"SEM-{index:03d}").strip(),
                category=str(raw.get("category") or "semantic_consistency").strip(),
                severity=severity,
                message=str(raw.get("message") or "Semantic design issue.").strip(),
                expected_change=str(raw.get("expected_change") or "Correct the design consistently.").strip(),
                claim_ids=claim_ids,
                requirement_ids=requirement_ids,
                diagram_ids=diagram_ids,
                evidence=evidence,
                subject_ref=subject_ref,
            )
        )
    if not bool(payload.get("ok")) and not findings:
        raise HarnessError(
            "design_semantic_review_failed",
            "Design semantic validator rejected the design without actionable findings.",
        )
    return findings


def _prepare_consensus_trial(findings: list[DesignFinding], *, trial_id: str) -> list[DesignFinding]:
    """Normalize one semantic vote and ensure one vote per canonical subject."""

    prepared: list[DesignFinding] = []
    for finding in findings:
        severity = finding.severity
        if severity == "blocking" and (not finding.requirement_ids or not finding.subject_ref):
            severity = "warning"
        subject_ref = finding.subject_ref
        if not _normalize_reference(subject_ref):
            # An unlocated observation cannot vote with another trial. Preserve it
            # as disputed evidence under a trial-local identity instead.
            subject_ref = f"unlocated:{trial_id}:{finding.finding_id}"
        prepared.append(
            DesignFinding(
                finding_id=finding.finding_id,
                category=finding.category,
                severity=severity,
                message=finding.message,
                expected_change=finding.expected_change,
                claim_ids=finding.claim_ids,
                requirement_ids=finding.requirement_ids,
                diagram_ids=finding.diagram_ids,
                evidence=finding.evidence,
                subject_ref=subject_ref,
                support_count=1,
                trial_ids=[trial_id],
                origin="semantic",
            )
        )
    unique: dict[str, DesignFinding] = {}
    for finding in prepared:
        current = unique.get(finding.canonical_key)
        if current is None or _evidence_score(finding) > _evidence_score(current):
            unique[finding.canonical_key] = finding
    return list(unique.values())


def _aggregate_semantic_trials(
    *,
    requested_trials: int,
    valid_trials: list[tuple[str, dict[str, object], list[DesignFinding]]],
) -> dict[str, object]:
    """Majority-vote semantic findings while retaining minority observations."""

    quorum = requested_trials // 2 + 1
    groups: dict[str, list[tuple[str, DesignFinding]]] = {}
    trial_key_sets: list[tuple[str, set[str]]] = []
    for trial_id, _payload, findings in valid_trials:
        key_set = {item.canonical_key for item in findings}
        trial_key_sets.append((trial_id, key_set))
        for finding in findings:
            groups.setdefault(finding.canonical_key, []).append((trial_id, finding))

    blockers: list[DesignFinding] = []
    warnings: list[DesignFinding] = []
    disputed: list[DesignFinding] = []
    for ordinal, (key, votes) in enumerate(sorted(groups.items()), start=1):
        blocker_votes = [(trial_id, item) for trial_id, item in votes if item.severity == "blocking"]
        representative = max((item for _trial, item in votes), key=_evidence_score)
        if len(blocker_votes) >= quorum:
            severity = "blocking"
            target = blockers
        elif len(votes) >= quorum:
            severity = "warning"
            target = warnings
        else:
            severity = representative.severity
            target = disputed
        target.append(
            DesignFinding(
                finding_id=f"CONS-{ordinal:03d}",
                category=representative.category,
                severity=severity,
                message=representative.message,
                expected_change=representative.expected_change,
                claim_ids=_merged_string_values(item.claim_ids for _trial, item in votes),
                requirement_ids=_merged_string_values(item.requirement_ids for _trial, item in votes),
                diagram_ids=_merged_string_values(item.diagram_ids for _trial, item in votes),
                evidence=_merged_evidence(item.evidence for _trial, item in votes),
                subject_ref=representative.subject_ref,
                support_count=len({trial_id for trial_id, _item in votes}),
                trial_ids=sorted({trial_id for trial_id, _item in votes}),
                origin="semantic",
            )
        )

    pairwise = []
    for (left_id, left), (right_id, right) in combinations(trial_key_sets, 2):
        union = left | right
        score = 1.0 if not union else len(left & right) / len(union)
        pairwise.append(
            {
                "left_trial": left_id,
                "right_trial": right_id,
                "jaccard": round(score, 4),
            }
        )
    consistency = (
        round(sum(float(item["jaccard"]) for item in pairwise) / len(pairwise), 4)
        if pairwise
        else (1.0 if len(valid_trials) == requested_trials else 0.0)
    )
    metrics = {
        "requested_trials": requested_trials,
        "valid_trials": len(valid_trials),
        "quorum": quorum,
        "consensus_blocking_count": len(blockers),
        "consensus_warning_count": len(warnings),
        "disputed_count": len(disputed),
        "pairwise_jaccard": pairwise,
        "consistency_score": consistency,
    }
    return {
        "blocking_findings": blockers,
        "warnings": warnings,
        "disputed_findings": disputed,
        "metrics": metrics,
    }


def _subject_ref_from_evidence(evidence: list[dict[str, str]], diagram_ids: list[str]) -> str:
    for item in evidence:
        if item.get("source") in {"design", "design_graph"} and item.get("ref"):
            return str(item["ref"]).strip()
    return diagram_ids[0] if len(diagram_ids) == 1 else ""


def _canonical_finding_key(finding: DesignFinding) -> str:
    """Stable issue identity: exact semantic category plus exact Design location."""

    return "|".join(
        [
            finding.category.strip().casefold(),
            _normalize_reference(finding.subject_ref),
        ]
    )


def _finding_evidence_signature(finding: DesignFinding) -> str:
    payload = {
        "requirement_ids": sorted({item.strip().casefold() for item in finding.requirement_ids if item.strip()}),
        "diagram_ids": sorted({item.strip().casefold() for item in finding.diagram_ids if item.strip()}),
        "message": finding.message.strip(),
        "expected_change": finding.expected_change.strip(),
        "evidence": sorted(
            (
                {
                    "source": str(item.get("source") or "").strip(),
                    "ref": str(item.get("ref") or "").strip(),
                    "excerpt": str(item.get("excerpt") or "").strip(),
                }
                for item in finding.evidence
            ),
            key=lambda item: (item["source"], item["ref"], item["excerpt"]),
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _merged_string_values(values: Any) -> list[str]:
    merged: dict[str, str] = {}
    for group in values:
        for item in group:
            text = str(item).strip()
            if text:
                merged.setdefault(text.casefold(), text)
    return [merged[key] for key in sorted(merged)]


def _merged_evidence(values: Any) -> list[dict[str, str]]:
    merged: dict[tuple[str, str, str], dict[str, str]] = {}
    for group in values:
        for item in group:
            normalized = {
                "source": str(item.get("source") or "").strip(),
                "ref": str(item.get("ref") or "").strip(),
                "excerpt": str(item.get("excerpt") or "").strip(),
            }
            key = (
                normalized["source"].casefold(),
                normalized["ref"].casefold(),
                normalized["excerpt"].casefold(),
            )
            merged.setdefault(key, normalized)
    return [merged[key] for key in sorted(merged)]


def _normalize_reference(value: str) -> str:
    return re.sub(r"[^a-z0-9:_-]", "", value.strip().casefold())


def _evidence_score(finding: DesignFinding) -> tuple[int, int, int]:
    return (len(finding.evidence), len(finding.message), len(finding.expected_change))


def _class_symbols(class_graph: dict[str, object]) -> dict[str, set[str]]:
    symbols: dict[str, set[str]] = {}
    for node in _dict_items(class_graph.get("nodes")):
        name = str(node.get("className") or node.get("id") or "").strip()
        if not name:
            continue
        symbols[name] = {
            str(method.get("name") or "").strip()
            for method in _dict_items(node.get("methods"))
            if str(method.get("name") or "").strip()
        }
    return symbols


def _matching_classes(node_id: str, label: str, classes: dict[str, set[str]]) -> list[str]:
    candidates = {_normalize_symbol(node_id), _normalize_symbol(label)} - {""}
    return [name for name in classes if _normalize_symbol(name) in candidates]


def _operation_name(message: str) -> str:
    match = re.match(r"\s*([A-Za-z_][\w]*)\s*\(", message)
    return match.group(1) if match else ""


def _normalize_symbol(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _deduplicate_findings(findings: list[DesignFinding]) -> list[DesignFinding]:
    result: list[DesignFinding] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in findings:
        key = (item.category, item.message.casefold(), tuple(item.requirement_ids))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _complete_json(llm_client: Any, *, system: str, prompt: str) -> str:
    complete_with_metadata = getattr(llm_client, "complete_with_metadata", None)
    if callable(complete_with_metadata):
        settings = getattr(llm_client, "settings", None)
        max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
        started = time.monotonic()
        completion = complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=True,
            **phase2_llm_call_options(
                settings,
                "design_validator",
                fallback_max_tokens=max_tokens,
            ),
        )
        _record_llm_call_metric(
            llm_client,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            finish_reason=str(completion.finish_reason or ""),
            usage=dict(completion.usage or {}),
        )
        raw = str(completion.content or "").strip()
        if str(completion.finish_reason or "").strip().lower() == "length":
            raise HarnessError(
                "design_semantic_review_truncated",
                "Design semantic validator exhausted its token budget before returning complete JSON.",
            )
        if not raw:
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic validator returned an empty response.",
            )
        return raw
    started = time.monotonic()
    raw = str(llm_client.complete(system=system, prompt=prompt))
    _record_llm_call_metric(
        llm_client,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        finish_reason="",
        usage={},
    )
    return raw


def _llm_call_metrics(llm_client: Any) -> list[dict[str, object]]:
    value = getattr(llm_client, "_design_validator_call_metrics", [])
    return value if isinstance(value, list) else []


def _record_llm_call_metric(
    llm_client: Any,
    *,
    duration_ms: float,
    finish_reason: str,
    usage: dict[str, object],
) -> None:
    metrics = _llm_call_metrics(llm_client)
    if not hasattr(llm_client, "_design_validator_call_metrics"):
        try:
            setattr(llm_client, "_design_validator_call_metrics", metrics)
        except (AttributeError, TypeError):
            return
    metrics.append(
        {
            "ordinal": len(metrics) + 1,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
            "usage": usage,
        }
    )


def _summarize_call_metrics(metrics: list[dict[str, object]]) -> dict[str, object]:
    usage_keys = {key for metric in metrics for key in _dict_value(metric.get("usage"))}
    totals = {
        key: sum(
            int(_dict_value(metric.get("usage")).get(key) or 0)
            for metric in metrics
            if isinstance(_dict_value(metric.get("usage")).get(key), (int, float))
        )
        for key in sorted(usage_keys)
    }
    return {
        "call_count": len(metrics),
        "duration_ms": round(sum(float(metric.get("duration_ms") or 0) for metric in metrics), 3),
        "tokens": totals,
    }


_PACKET_FIELD_GUIDE = """
Packet field guide (Harness-owned meanings; do not reinterpret them):
- task.primary_claim_ids are assessed once each; context claims are background that findings may cite.
- task.coverage_keys list what this call answers for: claim:<id> and boundary:<edge id>.
- surface_contract.runtime_surface.required_inputs are values a surface consumes. source=external_use_case or
  external_dependency means the approved Requirement supplies the value from outside this use case (see
  approved_context); provided_by names the in-app entry point that hands it over. A declared external input is an
  accepted dependency, not a missing provider.
- runtime_surface.runnable_status: bound = runnable on repository evidence; externally_blocked = runnable once its
  declared external inputs exist. Neither value is a defect by itself.
- runtime_surface.role=composition_wiring with forwarded_inputs: an existing component that passes those inputs down
  the composition chain. It forwards them; it does not have to produce them.
- edges[].boundary_contract.input_mapping is consumer field -> provider field. It only declares forwarding: the
  consumer side must hold each value through a request field, an inbound mapping, a modeled response or error event,
  its own declared state or transition, or a declared external input. response_mapping and error_mapping flow back to
  the consumer.
- branch_entries are Harness-derived from exact state names. For a resolved entry the branch starts in
  parent_entry_state: parent_transitions_taken_before_entry have happened and parent_transitions_not_taken_at_entry
  have not, so values and events those transitions would produce do not exist yet. An unresolved entry means the
  Design does not state where the branch starts; do not assume one.
- approved_context holds approved Requirement preconditions and the Requirement text behind each declared external
  input. Treat them as facts the scenario may rely on.
""".strip()

_SEVERITY_GUIDE = """
Severity:
- blocking: the packet proves the defect. For runtime, a step on the modeled path cannot execute as declared: an event
  cannot occur, a value a decision tests does not exist at that point, or a promised guarantee has no mechanism. For a
  claim, the Design omits or contradicts what the claim requires.
- warning: the packet leaves the issue uncertain, or the issue does not stop the path or the claim (quality, wording,
  an error branch outside the active path).
- Decide from the packet alone. An invented blocker and a missed blocker each cost a full repair cycle.
""".strip()

_DETERMINISTIC_REVIEW_NOTE = """
Deterministic review: before this call the Harness already enforced graph schema and stable IDs, edge endpoints,
partition coverage, boundary_contract shape, composition forwarded-input propagation, branch parent wiring and
Requirement-to-Design trace coverage. The object below adds class/sequence projection traceability and structure.
None of these checks judges whether a value exists at the right time or whether behavior satisfies a claim, so ok=true
is not evidence that this Design is semantically sound:
""".strip()

_RUNTIME_RULES = """
- Check that every modeled transport, response and error event can actually occur. A cross-application call requires a
  real transport/API surface; a broken transport cannot also carry a request or response.
- Validate causal order: a condition may justify behavior only after a modeled runtime surface or edge can observe it;
  naming an error scenario does not prove when the error becomes known.
- Every value a decision tests must exist on this path when the decision runs (see input_mapping in the field guide).
  A tested value that no modeled surface, declared state or declared external input produces is a
  decision_input_sufficiency defect even when an input_mapping names it.
- Transitions that leave the same `from` state are alternative branches unless an edge or transition orders them;
  taking one branch does not mean a sibling branch's request, write or response has happened. A branch that starts
  before an external action (per branch_entries) needs no mechanism to undo or cancel that action.
- A promised rollback, unchanged record, atomic outcome or safe retry needs an executable transaction, idempotency or
  reconciliation mechanism; when neither Requirement nor Design promises it, do not require one.
- For role=composition_wiring, runtime_surface.forwarded_inputs is the authoritative propagation contract; a missing
  forwarded input is a structural defect already reported by deterministic review.
""".strip()

_BEHAVIOR_RULES = """
- Verify behavior order, guards and terminal states against each primary claim, using branch_entries for branch timing.
- Transitions that leave the same `from` state are alternative branches unless an edge or transition orders them.
- The UI experience contract (states, feedback, focus, accessibility, responsive layout) must agree with the approved
  interaction and make every required state and outcome observable. Visual taste alone is a warning.
- Authentication and authorization are separate decisions; an uncertainty without evidence is a warning.
""".strip()

_GLOBAL_RULES = """
- Judge preconditions, postconditions, quality requirements and negative constraints across every scenario they govern,
  including the scenarios where they must not apply.
- Authentication and authorization are separate decisions; an uncertainty without evidence is a warning.
- A surface reused by several scenarios must tell one consistent story about actions, state, feedback and inputs when
  a global claim depends on it. Visual taste alone is a warning.
""".strip()

_SEMANTIC_TASK_PROMPTS: dict[str, tuple[str, str, str]] = {
    "cross_cutting": (
        "Design Semantic Global Review v2",
        """
Global-task scope:
- Scenario paths and their scenario-bound boundary contracts are reviewed in separate scenario calls. Do not report
  their runtime feasibility, behavior order or scenario-only claims here.
- Assess only the primary global claims. Scenario nodes, edges and context claims are evidence for judging those global
  claims; cite them only when a global claim itself is missing or contradicted.
- Review a boundary contract for runtime feasibility only when coverage_keys lists it as boundary:<edge id>.
""".strip(),
        _GLOBAL_RULES,
    ),
    "scenario_runtime": (
        "Design Semantic Runtime Review v2",
        """
Runtime-task scope:
- This task has no primary claims: return an empty assessments array. Context claims explain the intended scenario
  behavior and may be cited by findings.
- Report only runtime feasibility defects of the active scenario path. Behavior, UX and Requirement satisfaction are
  reviewed in a separate call that runs only when this runtime review has no blocking finding.
- Paths assigned to other scenario ids are reviewed separately.
""".strip(),
        _RUNTIME_RULES,
    ),
    "scenario_behavior": (
        "Design Semantic Scenario Review v2",
        """
Scenario-task scope:
- Evaluate the active scenario in this packet. Paths assigned to other scenario ids are intentionally reviewed in
  separate specialist calls, so their absence here is not a Design defect.
- Do not discard a shared acceptance criterion or rule. Assess every obligation it places on the active scenario;
  defer only evidence that requires executing another scenario to that scenario's separate review.
- Do not report another scenario path as missing from this bounded packet.
- This scenario's runtime path already passed a separate runtime review without blockers.
""".strip(),
        _BEHAVIOR_RULES,
    ),
}


_GRAPH_DIGEST_FIELD = re.compile(r'"graph_digest":"[^"]*",?')


def semantic_task_cache_key(prompt: str) -> str:
    """Identity of a task review input; the whole-graph digest is dropped because it changes on any unrelated edit."""

    # shortcut: the key ignores the provider model; clear the cache file when the Validator model changes.
    return "sha256:" + hashlib.sha256(_GRAPH_DIGEST_FIELD.sub("", prompt).encode("utf-8")).hexdigest()


class SemanticTaskCache:
    """Validated semantic task results of one run, reused only for a byte-identical task input.

    Only results of earlier validation versions are reused: step review replays the current version by call order,
    so a result produced during the current version must keep flowing through the provider client.
    """

    def __init__(self, path: Path, *, version: int) -> None:
        self.path = path
        self.version = version

    def _entries(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload.get("entries") if isinstance(payload.get("entries"), dict) else {}

    def get(self, key: str) -> dict[str, object] | None:
        entry = self._entries().get(key)
        if not isinstance(entry, dict) or entry.get("validation_version") == self.version:
            return None
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: dict[str, object], *, task_id: str) -> None:
        entries = self._entries()
        entries[key] = {"task_id": task_id, "validation_version": self.version, "payload": payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"schema_version": "semantic_task_cache_v1", "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)


def _with_contract_retry(review: Any) -> tuple[Any, int]:
    """Run one semantic review call; retry it once when its output breaks the Harness contract."""

    correction = ""
    for attempt in range(2):
        try:
            return review(correction), attempt
        except LlmStepReviewRequired:
            raise
        except HarnessError as exc:
            if exc.error_code != "design_semantic_review_failed" or attempt:
                raise
            correction = exc.message
    raise AssertionError("unreachable")


def _with_contract_correction(prompt: str, correction: str) -> str:
    if not correction:
        return prompt
    return (
        f"{prompt}\n\nHarness contract correction:\nYour previous response to this same request was rejected before "
        f"use: {correction}\nReturn one complete corrected JSON object for the same request. Cite only exact IDs that "
        "appear in the supplied packet."
    )


def _semantic_json_completion(llm_client: Any, *, prompt: str) -> dict[str, object]:
    raw = _complete_json(
        llm_client,
        system="You are SDLC Harness Design Validator. Return strict JSON only.",
        prompt=prompt,
    )
    try:
        payload = parse_first_json_object(raw, expected_keys={"ok", "findings"})
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "design_semantic_review_failed",
            "Design semantic validator did not return parseable JSON.",
        ) from exc
    if not isinstance(payload.get("findings"), list):
        raise HarnessError(
            "design_semantic_review_failed",
            "Design semantic validator response must contain a findings array.",
        )
    payload["ok"] = bool(payload.get("ok"))
    payload["summary"] = str(payload.get("summary") or "").strip()
    return payload
