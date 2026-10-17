"""Hybrid design validation for Phase 2.

The traceability agent answers whether approved requirements are represented in
the diagrams.  This module adds the missing quality gate: deterministic graph
contracts plus an LLM semantic review of ordering, states, and conditional
transaction guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from itertools import combinations
import re
from typing import Any

from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from .traceability import TraceabilityResult


@dataclass(frozen=True)
class DesignFinding:
    """One actionable design issue with evidence."""

    finding_id: str
    category: str
    severity: str
    message: str
    expected_change: str
    requirement_ids: list[str] = field(default_factory=list)
    diagram_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    subject_ref: str = ""
    support_count: int = 1
    trial_ids: list[str] = field(default_factory=list)
    origin: str = "semantic"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "expected_change": self.expected_change,
            "requirement_ids": self.requirement_ids,
            "diagram_ids": self.diagram_ids,
            "evidence": self.evidence,
            "subject_ref": self.subject_ref,
            "support_count": self.support_count,
            "trial_ids": self.trial_ids,
            "origin": self.origin,
        }

    @property
    def canonical_key(self) -> str:
        return _canonical_finding_key(self)


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

    def review(
        self,
        *,
        requirement_packet: str,
        design_artifact: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        traceability: TraceabilityResult,
        canonical_requirement: object | None = None,
        repair_iterations: int = 0,
        validation_trials: int = 1,
    ) -> DesignValidationResult:
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
                    requirement_packet=requirement_packet,
                    design_artifact=design_artifact,
                    class_graph=class_graph,
                    sequence_graph=sequence_graph,
                    traceability=traceability,
                    canonical_requirement=canonical_requirement,
                    rule_review=rule_payload,
                )
                normalized = _normalize_llm_findings(payload)
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

    def _semantic_review(
        self,
        *,
        requirement_packet: str,
        design_artifact: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        traceability: TraceabilityResult,
        canonical_requirement: object | None,
        rule_review: dict[str, object],
    ) -> dict[str, object]:
        canonical = (
            canonical_requirement.to_dict()
            if canonical_requirement is not None and hasattr(canonical_requirement, "to_dict")
            else canonical_requirement
        )
        prompt = f"""Design Semantic Review v2

Compare the approved requirements with the complete class and sequence design.
The deterministic findings are evidence, but you must also examine behavior.

Return one JSON object only:
{{
  "ok": true,
  "summary": "short summary",
  "findings": [
    {{
      "id": "SEM-001",
      "category": "behavior_order|terminal_state|cross_diagram_contract|data_consistency|transaction_boundary|unsupported_assumption",
      "severity": "blocking|warning",
      "message": "specific contradiction or ambiguity",
      "expected_change": "specific UML correction",
      "requirement_ids": ["MAIN-1"],
      "diagram_ids": ["sequence_diagram_1"],
      "subject_ref": "sequence_diagram_1:event-or-fragment-id",
      "evidence": [{{"source": "requirement|design", "ref": "stable id", "excerpt": "short exact evidence"}}]
    }}
  ]
}}

Blocking policy:
- Block a required behavior that is missing, contradicted, or ordered so its guard cannot prevent the action.
- Block a sequence call whose receiver contract is inconsistent with the class design.
- Block success, cancellation, and failure paths that reach the wrong terminal state.
- Block transaction/rollback ambiguity only when the approved requirement or the design itself explicitly promises atomicity, all-or-nothing behavior, or a single consistency boundary.
- If transaction behavior is merely an architectural improvement not required by either input, emit a warning.
- Never invent a requirement. Every semantic blocker must cite a stable requirement id and design evidence.
- Give every finding a stable subject_ref naming the UML node, message, fragment, or class contract being judged.
- An uncertainty without evidence is a warning, not a blocker.

Canonical Requirement V2:
{json.dumps(canonical or {}, ensure_ascii=False, separators=(",", ":"))}

Requirement packet:
{_excerpt(requirement_packet, 7000)}

Design artifact:
{_excerpt(design_artifact, 9000)}

Class graph:
{json.dumps(class_graph, ensure_ascii=False, separators=(",", ":"))}

Sequence graph:
{json.dumps(sequence_graph, ensure_ascii=False, separators=(",", ":"))}

Traceability:
{json.dumps({"class_trace": traceability.class_trace, "sequence_trace": traceability.sequence_trace}, ensure_ascii=False, separators=(",", ":"))}

Deterministic review:
{json.dumps(rule_review, ensure_ascii=False, separators=(",", ":"))}
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


def _normalize_llm_findings(payload: dict[str, object]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for index, raw in enumerate(_dict_items(payload.get("findings")), start=1):
        severity = str(raw.get("severity") or "warning").strip().lower()
        if severity not in {"blocking", "warning"}:
            severity = "warning"
        requirement_ids = _string_items(raw.get("requirement_ids"))
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
        findings.append(
            DesignFinding(
                finding_id=str(raw.get("id") or f"SEM-{index:03d}").strip(),
                category=str(raw.get("category") or "semantic_consistency").strip(),
                severity=severity,
                message=str(raw.get("message") or "Semantic design issue.").strip(),
                expected_change=str(raw.get("expected_change") or "Correct the design consistently.").strip(),
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
        prepared.append(
            DesignFinding(
                finding_id=finding.finding_id,
                category=finding.category,
                severity=severity,
                message=finding.message,
                expected_change=finding.expected_change,
                requirement_ids=finding.requirement_ids,
                diagram_ids=finding.diagram_ids,
                evidence=finding.evidence,
                subject_ref=finding.subject_ref,
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
                requirement_ids=representative.requirement_ids,
                diagram_ids=representative.diagram_ids,
                evidence=representative.evidence,
                subject_ref=representative.subject_ref,
                support_count=len(votes),
                trial_ids=[trial_id for trial_id, _item in votes],
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
    values = [
        finding.category.strip().casefold(),
        ",".join(sorted(item.strip().casefold() for item in finding.requirement_ids)),
        ",".join(sorted(item.strip().casefold() for item in finding.diagram_ids)),
        _normalize_reference(finding.subject_ref),
    ]
    return "|".join(values)


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
    exact = [name for name in classes if _normalize_symbol(name) in candidates]
    if exact:
        return exact
    return [
        name
        for name in classes
        if any(
            _normalize_symbol(name).endswith(candidate) or candidate.endswith(_normalize_symbol(name))
            for candidate in candidates
            if len(candidate) >= 4
        )
    ]


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


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _excerpt(value: str, limit: int) -> str:
    text = value.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n... truncated ..."


def _complete_json(llm_client: Any, *, system: str, prompt: str) -> str:
    complete_with_metadata = getattr(llm_client, "complete_with_metadata", None)
    if callable(complete_with_metadata):
        settings = getattr(llm_client, "settings", None)
        max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
        completion = complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=True,
            max_tokens=max_tokens,
        )
        return str(completion.content)
    return str(llm_client.complete(system=system, prompt=prompt))
