"""Kiểm thử catalog nguồn chỉ đọc và vòng tool của Requirement Refiner."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from sdlc_harness.agents.requirements import RequirementModelV2Refiner, RequirementSourceCatalog
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.sources.fixtures import UseCaseSource
from sdlc_harness.storage.runtime import RuntimeStore


class ScriptedLlmClient:
    def __init__(self, completions: list[LlmCompletion]):
        self.completions = list(completions)
        self.settings = SimpleNamespace(llm_structured_max_tokens=4096)
        self.calls: list[dict[str, object]] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        return self.completions.pop(0)


def completion(payload: dict[str, object]) -> LlmCompletion:
    return LlmCompletion(content=json.dumps(payload, ensure_ascii=False), finish_reason="stop", usage={})


class RequirementSourceCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = "Alpha first\nbeta target\nGamma third\nbeta fourth"
        self.entry = {
            "id": "SRC-1",
            "type": "dataset",
            "source_ref": "dataset:test:UC1",
            "locator": "dataset:test:UC1",
            "revision": None,
            "artifact_ref": ".runtime/runs/run_tools/artifacts/use_case_packet_v1.md",
            "fingerprint": "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        }

    def catalog(self, *, text: str | None = None) -> RequirementSourceCatalog:
        return RequirementSourceCatalog(
            run_id="run_tools",
            sources=[self.entry],
            source_text_by_id={"SRC-1": self.text if text is None else text},
        )

    def test_list_search_and_read_are_bounded_and_deterministic(self) -> None:
        catalog = self.catalog()

        listed = catalog.list_sources()
        searched = catalog.search(query="BETA", max_results=1)
        read = catalog.read(source_id="SRC-1", start_line=2, line_count=500)

        self.assertEqual(listed["sources"][0]["line_count"], 4)
        self.assertEqual(len(searched["matches"]), 1)
        self.assertTrue(searched["truncated"])
        self.assertEqual(searched["matches"][0]["line_start"], 1)
        self.assertEqual(read["line_start"], 2)
        self.assertEqual(read["line_end"], 4)
        self.assertTrue(read["end_of_source"])

    def test_unknown_source_and_invalid_range_fail_as_structured_results(self) -> None:
        catalog = self.catalog()

        unknown = catalog.read(source_id="SRC-X")
        invalid = catalog.read(source_id="SRC-1", start_line=0)
        invalid_ids = catalog.search(query="beta", source_ids=["SRC-X"])

        self.assertEqual(unknown["error_code"], "requirement_source_not_allowed")
        self.assertEqual(invalid["error_code"], "requirement_source_range_invalid")
        self.assertEqual(invalid_ids["error_code"], "requirement_source_not_allowed")

    def test_catalog_rejects_snapshot_fingerprint_drift(self) -> None:
        with self.assertRaises(HarnessError) as context:
            self.catalog(text=self.text + " changed")
        self.assertEqual(context.exception.error_code, "source_snapshot_corrupted")

    def test_catalog_rejects_empty_source_and_line_larger_than_read_budget(self) -> None:
        for text in ("   ", "x" * 12_001):
            entry = deepcopy(self.entry)
            entry["fingerprint"] = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            with self.subTest(length=len(text)), self.assertRaises(HarnessError) as context:
                RequirementSourceCatalog(
                    run_id="run_tools",
                    sources=[entry],
                    source_text_by_id={"SRC-1": text},
                )
            self.assertEqual(context.exception.error_code, "requirement_source_catalog_invalid")

    def test_catalog_rejects_incomplete_source_metadata(self) -> None:
        entry = deepcopy(self.entry)
        entry.pop("artifact_ref")
        with self.assertRaises(HarnessError) as context:
            RequirementSourceCatalog(
                run_id="run_tools",
                sources=[entry],
                source_text_by_id={"SRC-1": self.text},
            )
        self.assertEqual(context.exception.error_code, "requirement_source_catalog_invalid")

    def test_catalog_returns_defensive_source_copies(self) -> None:
        catalog = self.catalog()
        sources = catalog.sources
        sources[0]["source_ref"] = "changed"
        self.assertEqual(catalog.sources[0]["source_ref"], "dataset:test:UC1")


class RequirementSourceLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        source_path = root / "fixtures" / "etour" / "UC2.txt"
        cls.source = UseCaseSource(
            dataset="eTour",
            use_case="UC2",
            source_ref="dataset_use_case:eTour:UC2",
            source_path=source_path,
            text=source_path.read_text(encoding="utf-8").strip(),
        )
        cls.payload = json.loads(
            (root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )

    def action(self, name: str, arguments: dict[str, object]) -> LlmCompletion:
        return completion({"action": name, "arguments": arguments, "summary": name})

    def candidate(self, *, observation_ref: str = "OBS-1") -> LlmCompletion:
        payload = deepcopy(self.payload)
        for evidence in payload["evidence"]:
            evidence.pop("source_id", None)
            evidence["observation_ref"] = observation_ref
        return self.action("complete", {"requirement_model": payload})

    def test_complete_before_observation_is_rejected_then_loop_can_finish(self) -> None:
        client = ScriptedLlmClient(
            [
                self.candidate(),
                self.action("source.list", {}),
                self.action("source.read", {"source_id": "SRC-1"}),
                self.candidate(),
            ]
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            result = RequirementModelV2Refiner(client, store=store).refine(
                self.source,
                run_id="run_tools",
                source_artifact_ref=".runtime/runs/run_tools/artifacts/use_case_packet_v1.md",
            )
            journal = json.loads((root / result.source_journal_path).read_text(encoding="utf-8"))
            manifest = json.loads((root / result.source_manifest_path).read_text(encoding="utf-8"))

        self.assertTrue(result.final_report.ok)
        self.assertFalse(result.repaired)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(journal["status"], "completed")
        self.assertEqual(journal["calls"][0]["result"]["error_code"], "requirement_source_observation_required")
        self.assertNotIn("text", journal["calls"][2]["result"])
        self.assertEqual(journal["calls"][2]["result"]["observation_id"], "OBS-1")
        self.assertEqual(manifest["max_actions"], 12)

    def test_invalid_observation_reference_gets_one_candidate_repair(self) -> None:
        client = ScriptedLlmClient(
            [
                self.action("source.list", {}),
                self.action("source.read", {"source_id": "SRC-1"}),
                self.candidate(observation_ref="OBS-999"),
                self.candidate(),
            ]
        )
        result = RequirementModelV2Refiner(client).refine(
            self.source,
            run_id="run_tools",
            source_artifact_ref=".runtime/runs/run_tools/artifacts/use_case_packet_v1.md",
        )
        self.assertTrue(result.repaired)
        self.assertEqual([item.parse_status for item in result.attempts], ["schema_failed", "parsed"])

    def test_action_budget_fails_closed(self) -> None:
        client = ScriptedLlmClient([self.action("source.list", {}) for _ in range(3)])
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client, max_actions=3).refine(
                self.source,
                run_id="run_tools",
                source_artifact_ref=".runtime/runs/run_tools/artifacts/use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_tool_action_limit_exceeded")

    def test_refiner_rejects_catalog_from_another_run(self) -> None:
        artifact_ref = ".runtime/runs/run_tools/artifacts/use_case_packet_v1.md"
        fingerprint = "sha256:" + hashlib.sha256(self.source.text.encode("utf-8")).hexdigest()
        catalog = RequirementSourceCatalog(
            run_id="another_run",
            sources=[
                {
                    "id": "SRC-1",
                    "type": "dataset",
                    "source_ref": self.source.source_ref,
                    "locator": self.source.locator,
                    "revision": None,
                    "artifact_ref": artifact_ref,
                    "fingerprint": fingerprint,
                }
            ],
            source_text_by_id={"SRC-1": self.source.text},
        )
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(ScriptedLlmClient([])).refine(
                self.source,
                run_id="run_tools",
                source_artifact_ref=artifact_ref,
                source_catalog=catalog,
            )
        self.assertEqual(context.exception.error_code, "requirement_source_catalog_invalid")


if __name__ == "__main__":
    unittest.main()
