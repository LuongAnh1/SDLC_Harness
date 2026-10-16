"""Canonical data model for Requirement Model V2.

The V2 model is deliberately independent from PhaseOneWorkflow.  This module
only parses and serializes the wire contract; policy and semantic validation
live in validator_v2.py.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...core.errors import HarnessError


SCHEMA_VERSION = "requirement_model_v2"


class RequirementStatus(str, Enum):
    EXPLICIT = "explicit"
    HUMAN_DECISION = "human_decision"
    ASSUMPTION = "assumption"
    UNKNOWN = "unknown"


class QuestionStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class DecisionStatus(str, Enum):
    ACCEPTED = "accepted"
    INSUFFICIENT = "insufficient"
    SUPERSEDED = "superseded"


class ValidationStatus(str, Enum):
    VALID = "valid"
    CLARIFICATION_REQUIRED = "clarification_required"
    INVALID = "invalid"


@dataclass(frozen=True)
class RequirementModelV2:
    """Parsed root object for the Requirement Model V2 wire contract.

    Nested objects remain dictionaries because the JSON contract is expected to
    evolve during the V2 rollout. The root fields cannot be rebound, and parsing
    detaches all nested values so validation never mutates the caller's payload.
    """

    schema_version: str
    revision: int
    run_id: str
    source: dict[str, Any]
    use_case: dict[str, Any]
    preconditions: list[dict[str, Any]]
    flows: dict[str, list[dict[str, Any]]]
    postconditions: list[dict[str, Any]]
    business_rules: list[dict[str, Any]]
    acceptance_criteria: list[dict[str, Any]]
    quality_requirements: list[dict[str, Any]]
    assumptions: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    validation: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: object) -> "RequirementModelV2":
        if not isinstance(payload, dict):
            raise HarnessError(
                "requirement_model_v2_invalid",
                "Requirement Model V2 payload must be a JSON object.",
            )

        required = {
            "schema_version",
            "revision",
            "run_id",
            "source",
            "use_case",
            "preconditions",
            "flows",
            "postconditions",
            "business_rules",
            "acceptance_criteria",
            "quality_requirements",
            "assumptions",
            "open_questions",
            "decisions",
            "evidence",
            "validation",
        }
        missing = sorted(required.difference(payload))
        if missing:
            raise HarnessError(
                "requirement_model_v2_invalid",
                "Requirement Model V2 is missing root fields: " + ", ".join(missing),
            )

        object_fields = ("source", "use_case", "flows", "validation")
        list_fields = (
            "preconditions",
            "postconditions",
            "business_rules",
            "acceptance_criteria",
            "quality_requirements",
            "assumptions",
            "open_questions",
            "decisions",
            "evidence",
        )
        invalid_types = [name for name in object_fields if not isinstance(payload.get(name), dict)]
        invalid_types.extend(name for name in list_fields if not isinstance(payload.get(name), list))
        if not isinstance(payload.get("schema_version"), str):
            invalid_types.append("schema_version")
        if not isinstance(payload.get("run_id"), str):
            invalid_types.append("run_id")
        if not isinstance(payload.get("revision"), int) or isinstance(payload.get("revision"), bool):
            invalid_types.append("revision")
        if invalid_types:
            raise HarnessError(
                "requirement_model_v2_invalid",
                "Requirement Model V2 has invalid root field types: " + ", ".join(invalid_types),
            )

        try:
            return cls(
                schema_version=payload["schema_version"],
                revision=payload["revision"],
                run_id=payload["run_id"],
                source=deepcopy(payload["source"]),
                use_case=deepcopy(payload["use_case"]),
                preconditions=deepcopy(payload["preconditions"]),
                flows=deepcopy(payload["flows"]),
                postconditions=deepcopy(payload["postconditions"]),
                business_rules=deepcopy(payload["business_rules"]),
                acceptance_criteria=deepcopy(payload["acceptance_criteria"]),
                quality_requirements=deepcopy(payload["quality_requirements"]),
                assumptions=deepcopy(payload["assumptions"]),
                open_questions=deepcopy(payload["open_questions"]),
                decisions=deepcopy(payload["decisions"]),
                evidence=deepcopy(payload["evidence"]),
                validation=deepcopy(payload["validation"]),
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "requirement_model_v2_invalid",
                f"Requirement Model V2 cannot be parsed: {exc}",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "run_id": self.run_id,
            "source": deepcopy(self.source),
            "use_case": deepcopy(self.use_case),
            "preconditions": deepcopy(self.preconditions),
            "flows": deepcopy(self.flows),
            "postconditions": deepcopy(self.postconditions),
            "business_rules": deepcopy(self.business_rules),
            "acceptance_criteria": deepcopy(self.acceptance_criteria),
            "quality_requirements": deepcopy(self.quality_requirements),
            "assumptions": deepcopy(self.assumptions),
            "open_questions": deepcopy(self.open_questions),
            "decisions": deepcopy(self.decisions),
            "evidence": deepcopy(self.evidence),
            "validation": deepcopy(self.validation),
        }
