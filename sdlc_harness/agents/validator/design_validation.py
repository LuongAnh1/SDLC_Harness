"""Xác thực thiết kế hybrid cho Phase 2.

Traceability agent xác định các requirement đã duyệt có được biểu diễn trong
diagram hay không. Module này bổ sung quality gate còn thiếu: contract graph
tất định cùng một lượt LLM semantic review về thứ tự, trạng thái và bảo đảm
transaction có điều kiện.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from itertools import combinations
import re
from typing import Any

from ...core.config import phase2_llm_call_options
from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired
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

    def preflight(
        self,
        *,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
    ) -> list[DesignFinding]:
        """Run deterministic graph checks before paying for traceability/semantic review."""

        return _structural_findings(class_graph=class_graph, sequence_graph=sequence_graph)

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
    ) -> DesignValidationResult:
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

Review every claim exactly once. Do not infer a missing behavior only because a derived projection omits detail that is present in the canonical Vertical Design Graph.

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
      "category": "behavior_order|terminal_state|cross_diagram_contract|data_consistency|transaction_boundary|unsupported_assumption|ui_state_coverage|interaction_feedback|accessibility|responsive_attachment|authorization_contract",
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

Blocking policy:
- Block a required behavior that is missing, contradicted, or ordered so its guard cannot prevent the action.
- Block a sequence call whose receiver contract is inconsistent with the class design.
- Block success, cancellation, and failure paths that reach the wrong terminal state.
- Block persistence before required human confirmation only when neither the UI contract nor the interaction path represents that confirmation before the write.
- Do not require a preliminary backend verification round trip unless the approved requirement explicitly requires it.
- Block transaction/rollback ambiguity only when the approved requirement or the design itself explicitly promises atomicity, all-or-nothing behavior, or a single consistency boundary.
- If transaction behavior is merely an architectural improvement not required by either input, emit a warning.
- For each Vertical Design UI node, review its experience contract: component structure, observable feedback,
  accessibility, responsive behavior, and focus flow must agree with the approved interaction and states.
- Block a UI experience issue only when it omits or contradicts approved behavior, makes a required state or
  outcome unobservable, or makes the approved interaction impossible. Visual polish and aesthetic preferences
  are warnings unless the approved requirement explicitly makes them mandatory.
- UI/UX guidance references are design evidence, not new Requirements; never promote a recommendation into a blocker by itself.
- Review authorization against the approved Requirement. Use authorization_required as the typed Design decision; do not infer it from prose labels.
- Never invent a requirement. Every semantic blocker must cite a stable requirement id and design evidence.
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
    }


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
    if not claims:
        return
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
                f"Design semantic assessment {claim_id} cites an unknown Design ref.",
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
                "Design semantic finding contains a missing or unknown claim_id.",
            )
        if strict_requirement_ids and any(item not in strict_requirement_ids for item in requirement_ids):
            raise HarnessError(
                "design_semantic_review_failed",
                "Design semantic finding contains an unknown Requirement ID.",
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
                "Design semantic finding subject_ref is not an exact Vertical Design node or edge ID.",
            )
        if strict_requirement_ids or strict_subject_refs:
            for item in evidence:
                source = item["source"]
                ref = item["ref"]
                if source == "requirement" and ref not in strict_requirement_ids:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        "Design semantic finding cites an unknown Requirement evidence ref.",
                    )
                if source in {"design", "design_graph"} and ref not in strict_subject_refs:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        "Design semantic finding cites an unknown Design evidence ref.",
                    )
                if source not in {"requirement", "design", "design_graph"}:
                    raise HarnessError(
                        "design_semantic_review_failed",
                        "Design semantic finding evidence source is unsupported.",
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
        return str(completion.content)
    return str(llm_client.complete(system=system, prompt=prompt))
