"""Kiểm thử contract và atomic mutation của Requirement repair delta."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from sdlc_harness.agents.requirements import normalize_and_validate_requirement_model_v2
from sdlc_harness.agents.requirements.delta_v1 import (
    DELTA_SCHEMA_VERSION,
    MAX_DELTA_OPERATIONS,
    apply_requirement_delta,
    requirement_candidate_hash,
)
from sdlc_harness.core.errors import HarnessError


class RequirementDeltaV1Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source_text = (root / "fixtures" / "etour" / "UC2.txt").read_text(encoding="utf-8")
        cls.payload = json.loads(
            (root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(
                encoding="utf-8"
            )
        )

    def model(self, payload: dict[str, object] | None = None):
        result = normalize_and_validate_requirement_model_v2(
            deepcopy(payload or self.payload),
            {"SRC-1": self.source_text},
        )
        self.assertTrue(result.report.ok)
        return result.model

    def delta(self, model, *operations: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": DELTA_SCHEMA_VERSION,
            "base_candidate_sha256": requirement_candidate_hash(model),
            "operations": list(operations),
        }

    @property
    def observations(self) -> dict[str, dict[str, object]]:
        return {"OBS-1": {"source_id": "SRC-1", "text": self.source_text}}

    def apply(self, model, delta):
        return apply_requirement_delta(
            base_model=model,
            raw_delta=delta,
            observations=self.observations,
            source_text_by_id={"SRC-1": self.source_text},
        )

    def test_update_entity_and_surface_preserves_identity(self) -> None:
        model = self.model()
        result = self.apply(
            model,
            self.delta(
                model,
                {"op": "entity.update", "target_id": "SC-1", "fields": {"name": "Successful insert"}},
                {
                    "op": "surface.update",
                    "scenario_id": "SC-1",
                    "surface": "ui",
                    "fields": {"reason": "The existing form exposes the complete successful flow."},
                },
            ),
        )

        scenario = result.model.scenario_contract["scenarios"][0]
        self.assertEqual(scenario["id"], "SC-1")
        self.assertEqual(scenario["name"], "Successful insert")
        self.assertEqual(
            scenario["surfaces"]["ui"]["reason"],
            "The existing form exposes the complete successful flow.",
        )

    def test_add_assigns_ids_and_rewrites_temp_references(self) -> None:
        model = self.model()
        quote = "Inserts a new cultural object in the system."
        result = self.apply(
            model,
            self.delta(
                model,
                {
                    "op": "entity.add",
                    "collection": "quality_requirements",
                    "temp_ref": "tmp:quality",
                    "value": {
                        "text": "The insertion requirement remains traceable to its approved source.",
                        "status": "explicit",
                        "evidence_refs": ["tmp:evidence"],
                        "open_question_refs": [],
                    },
                },
                {
                    "op": "entity.add",
                    "collection": "evidence",
                    "temp_ref": "tmp:evidence",
                    "value": {
                        "location": "description",
                        "quote": quote,
                        "observation_ref": "OBS-1",
                    },
                },
            ),
        )

        self.assertEqual(result.model.evidence[-1]["id"], "E-14")
        self.assertEqual(result.model.evidence[-1]["source_id"], "SRC-1")
        self.assertEqual(result.model.quality_requirements[-1]["id"], "QR-1")
        self.assertEqual(result.model.quality_requirements[-1]["evidence_refs"], ["E-14"])

    def test_remove_existing_entity(self) -> None:
        payload = deepcopy(self.payload)
        payload["quality_requirements"] = [
            {
                "id": "QR-1",
                "text": "Keep the source traceable.",
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            }
        ]
        model = self.model(payload)

        result = self.apply(
            model,
            self.delta(model, {"op": "entity.remove", "target_id": "QR-1"}),
        )

        self.assertEqual(result.model.quality_requirements, [])

    def test_stale_hash_is_rejected(self) -> None:
        model = self.model()
        delta = self.delta(model, {"op": "entity.update", "target_id": "SC-1", "fields": {"name": "x"}})
        delta["base_candidate_sha256"] = "sha256:" + "0" * 64

        with self.assertRaises(HarnessError) as context:
            self.apply(model, delta)
        self.assertEqual(context.exception.error_code, "requirement_delta_stale")

    def test_protected_field_is_rejected(self) -> None:
        model = self.model()
        with self.assertRaises(HarnessError) as context:
            self.apply(
                model,
                self.delta(
                    model,
                    {"op": "entity.update", "target_id": "SC-1", "fields": {"id": "SC-99"}},
                ),
            )
        self.assertEqual(context.exception.error_code, "requirement_delta_policy_violation")

    def test_duplicate_temp_ref_is_rejected(self) -> None:
        model = self.model()
        operation = {
            "op": "entity.add",
            "collection": "quality_requirements",
            "temp_ref": "tmp:item",
            "value": {
                "text": "Traceable.",
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            },
        }
        with self.assertRaises(HarnessError) as context:
            self.apply(model, self.delta(model, operation, deepcopy(operation)))
        self.assertEqual(context.exception.error_code, "requirement_delta_invalid")

    def test_operation_budget_is_enforced(self) -> None:
        model = self.model()
        operations = [
            {"op": "entity.update", "target_id": "SC-1", "fields": {"name": str(index)}}
            for index in range(MAX_DELTA_OPERATIONS + 1)
        ]
        with self.assertRaises(HarnessError) as context:
            self.apply(model, self.delta(model, *operations))
        self.assertEqual(context.exception.error_code, "requirement_delta_invalid")

    def test_validation_failure_is_atomic(self) -> None:
        model = self.model()
        before = model.to_dict()
        with self.assertRaises(HarnessError) as context:
            self.apply(
                model,
                self.delta(
                    model,
                    {
                        "op": "entity.update",
                        "target_id": "SC-1",
                        "fields": {"requirement_refs": ["MF-999"]},
                    },
                ),
            )
        self.assertEqual(context.exception.error_code, "requirement_delta_validation_failed")
        self.assertEqual(model.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
