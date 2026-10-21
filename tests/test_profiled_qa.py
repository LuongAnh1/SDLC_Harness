"""Contract tests cho QA generation/application theo project profile."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.tester import (
    QaGeneratedTestSuite,
    QaTesterAgent,
    QaTesterLlmError,
)
from sdlc_harness.artifacts.writers import (
    _qa_execution_report_markdown,
    _qa_test_report_markdown,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_qa_test_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.verification import VerificationResult
from sdlc_harness.workflows.phase4 import (
    _apply_qa_generated_tests,
    _code_review_publication_paths,
    _qa_code_paths,
    _qa_result_from_generated_execution,
)


class RecordingQaLlm:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        del system
        self.prompts.append(prompt)
        return json.dumps(self.payload)


class ProfiledQaTest(unittest.TestCase):
    def setUp(self) -> None:
        project = get_project_profile("typescript_fullstack_v1")
        planning = build_planning_path_policy(
            project=project,
            language=get_language_profile(project.language_profile_id),
        )
        self.planning_policy = planning
        self.qa_policy = build_qa_test_policy(planning_policy=planning)
        self.context = {
            "run_id": "run_profiled_qa",
            "issue_key": "SDLC-7",
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [{"id": "REQ-1", "content": "Keep the result visible."}],
            "code_under_test": {
                "files": [
                    {
                        "path": "apps/api/src/FeatureService.ts",
                        "exists": True,
                        "content": "export class FeatureService {}",
                    }
                ]
            },
            "context_budget": {"section_count": 3},
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_policy_accepts_only_profiled_colocated_vitest_paths(self) -> None:
        accepted = (
            "apps/web/src/Feature.test.tsx",
            "apps/api/src/Feature.test.ts",
            "packages/contracts/src/Feature.test.ts",
            "packages/db/src/Feature.test.ts",
        )
        for path in accepted:
            with self.subTest(path=path):
                self.assertEqual(self.qa_policy.normalize_test_path(path), path)

        rejected = (
            "src/test/java/FeatureTest.java",
            "tests/e2e/feature.spec.ts",
            "apps/api/src/Feature.ts",
            "apps/api/src/Feature.test.tsx",
            "apps/api/src/../Feature.test.ts",
            "apps\\api\\src\\Feature.test.ts",
            "/apps/api/src/Feature.test.ts",
            "package.json",
        )
        for path in rejected:
            with self.subTest(path=path):
                self.assertEqual(self.qa_policy.normalize_test_path(path), "")

    def test_profiled_allowed_paths_feed_qa_context_and_publication_manifest(self) -> None:
        work_packet = {
            "output_contract": {
                "allowed_paths": [
                    "packages/contracts/src/CulturalHeritage.ts",
                    "apps/api/src/CulturalHeritageService.ts",
                ]
            }
        }

        self.assertEqual(
            _qa_code_paths(
                work_packet=work_packet,
                run_id="run_profiled_qa",
                planning_policy=self.planning_policy,
            ),
            [
                "packages/contracts/src/CulturalHeritage.ts",
                "apps/api/src/CulturalHeritageService.ts",
            ],
        )
        self.assertEqual(
            _code_review_publication_paths(
                work_packet=work_packet,
                qa_generated={
                    "test_files": [
                        {"path": "apps/api/src/CulturalHeritageAcceptance.test.ts"}
                    ]
                },
                run_id="run_profiled_qa",
                planning_policy=self.planning_policy,
            ),
            [
                "apps/api/src/CulturalHeritageAcceptance.test.ts",
                "apps/api/src/CulturalHeritageService.ts",
                "packages/contracts/src/CulturalHeritage.ts",
            ],
        )

    def test_profiled_allowed_paths_still_reject_paths_outside_editable_roots(self) -> None:
        work_packet = {
            "output_contract": {
                "allowed_paths": ["package.json"],
            }
        }

        with self.assertRaises(HarnessError) as caught:
            _qa_code_paths(
                work_packet=work_packet,
                run_id="run_profiled_qa",
                planning_policy=self.planning_policy,
            )
        self.assertEqual(caught.exception.error_code, "developer_act_unsafe_path")

    def test_fake_profiled_qa_generates_vitest_and_profile_metadata(self) -> None:
        generated = QaTesterAgent().generate_tests(
            context=self.context,
            qa_policy=self.qa_policy,
        )

        self.assertEqual(generated.generation_profile["framework"], "vitest")
        self.assertEqual(generated.test_files[0]["path"], "apps/api/src/GeneratedQaAcceptance.test.ts")
        self.assertIn('from "vitest"', generated.test_files[0]["content"])
        self.assertEqual(generated.scenarios[0]["requirement_ids"], ["REQ-1"])

    def test_profiled_prompt_uses_vitest_and_rejects_java_shape(self) -> None:
        path = "apps/api/src/FeatureAcceptance.test.ts"
        llm = RecordingQaLlm(
            {
                "summary": "Vitest acceptance",
                "test_files": [
                    {
                        "path": path,
                        "content": 'import { it, expect } from "vitest"; it("works", () => expect(true).toBe(true));',
                        "scenarios": ["works"],
                    }
                ],
                "scenarios": [
                    {
                        "name": "works",
                        "expected_result": "works",
                        "test_file": path,
                        "risk": "",
                        "requirement_ids": ["REQ-1"],
                    }
                ],
                "risks": [],
            }
        )

        generated = QaTesterAgent(llm).generate_tests(
            context=self.context,
            qa_policy=self.qa_policy,
        )

        self.assertEqual(generated.test_files[0]["path"], path)
        self.assertIn("TypeScript Vitest", llm.prompts[0])
        self.assertNotIn("src/test/java", llm.prompts[0])
        self.assertNotIn("complete Java test source", llm.prompts[0])

    def test_profiled_parser_rejects_out_of_policy_or_unbound_scenario_path(self) -> None:
        invalid_payloads = (
            {
                "summary": "bad Java path",
                "test_files": [{"path": "src/test/java/Bad.java", "content": "bad", "scenarios": ["bad"]}],
                "scenarios": [
                    {
                        "name": "bad",
                        "expected_result": "bad",
                        "test_file": "src/test/java/Bad.java",
                        "requirement_ids": ["REQ-1"],
                    }
                ],
            },
            {
                "summary": "wrong scenario reference",
                "test_files": [
                    {
                        "path": "apps/api/src/Good.test.ts",
                        "content": 'import { it } from "vitest";',
                        "scenarios": ["bad"],
                    }
                ],
                "scenarios": [
                    {
                        "name": "bad",
                        "expected_result": "bad",
                        "test_file": "apps/api/src/Other.test.ts",
                        "requirement_ids": ["REQ-1"],
                    }
                ],
            },
        )
        for payload in invalid_payloads:
            with self.subTest(summary=payload["summary"]), self.assertRaises(QaTesterLlmError):
                QaTesterAgent(RecordingQaLlm(payload)).generate_tests(
                    context=self.context,
                    qa_policy=self.qa_policy,
                )

    def test_profiled_apply_prevents_overwrite_but_allows_same_run_retry(self) -> None:
        path = "apps/api/src/GeneratedQaAcceptance.test.ts"
        generated = self._generated(path=path, content="first")
        applied = _apply_qa_generated_tests(
            generated=generated,
            target_path=self.target,
            run_id="run_profiled_qa",
            qa_policy=self.qa_policy,
        )
        self.assertEqual(applied[0]["path"], path)

        retried = _apply_qa_generated_tests(
            generated=self._generated(path=path, content="second"),
            target_path=self.target,
            run_id="run_profiled_qa",
            qa_policy=self.qa_policy,
            prior_qa_paths={path},
        )
        self.assertEqual(retried[0]["operation"], "create_or_update")
        self.assertEqual((self.target / path).read_text(encoding="utf-8"), "second\n")

        foreign_path = "apps/api/src/Existing.test.ts"
        foreign = self.target / foreign_path
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("developer-owned\n", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            _apply_qa_generated_tests(
                generated=self._generated(path=foreign_path, content="overwrite"),
                target_path=self.target,
                run_id="run_profiled_qa",
                qa_policy=self.qa_policy,
            )
        self.assertEqual(caught.exception.error_code, "qa_test_existing_file_conflict")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "developer-owned\n")

    def test_profiled_qa_requires_exact_selected_and_verified_evidence(self) -> None:
        path = "apps/api/src/GeneratedQaAcceptance.test.ts"
        generated = self._generated(path=path, content="test")
        applied = [{"path": path, "operation": "create_or_update", "scenarios": ["scenario"]}]
        passed = VerificationResult(
            status="passed",
            command="",
            cwd=str(self.target),
            exit_code=0,
            duration_seconds=0.1,
            stdout="passed",
            stderr="",
            reason="passed",
            selected_test_files=(path,),
            verified_test_files=(path,),
        )
        result = _qa_result_from_generated_execution(
            generated=generated,
            execution=passed,
            execution_path="qa_execution.md",
            generated_path="qa_generated.json",
            applied_tests=applied,
            qa_policy=self.qa_policy,
        )
        self.assertTrue(result.ok)

        missing_evidence = VerificationResult(
            status="passed",
            command="",
            cwd=str(self.target),
            exit_code=0,
            duration_seconds=0.1,
            stdout="passed",
            stderr="",
            reason="passed",
            selected_test_files=(path,),
            verified_test_files=(),
        )
        failed = _qa_result_from_generated_execution(
            generated=generated,
            execution=missing_evidence,
            execution_path="qa_execution.md",
            generated_path="qa_generated.json",
            applied_tests=applied,
            qa_policy=self.qa_policy,
        )
        self.assertFalse(failed.ok)
        self.assertIn("does not prove", failed.summary)

    def test_profiled_qa_reports_render_framework_and_machine_evidence(self) -> None:
        path = "apps/api/src/GeneratedQaAcceptance.test.ts"
        stage = {
            "stage_id": "unit",
            "status": "passed",
            "argv": ["npm", "run", "test:unit", "--", path],
            "cwd": ".",
            "timeout_seconds": 120,
            "selected_test_files": [path],
            "verified_test_files": [path],
        }
        payload = {
            "generation_profile": self.qa_policy.metadata(),
            "selected_test_files": [path],
            "verified_test_files": [path],
            "stage_results": [stage],
        }

        qa_report = _qa_test_report_markdown(payload)
        execution_report = _qa_execution_report_markdown(payload)

        self.assertIn("- Test framework: `vitest`", qa_report)
        self.assertIn(f'- Selected test files: `["{path}"]`', qa_report)
        self.assertIn(f'- Verified test files: `["{path}"]`', qa_report)
        self.assertIn("## Verification Stages", execution_report)
        self.assertIn("### unit", execution_report)
        self.assertIn(f'- Verified test files: `["{path}"]`', execution_report)

    def _generated(self, *, path: str, content: str) -> QaGeneratedTestSuite:
        return QaGeneratedTestSuite(
            summary="generated",
            test_files=[{"path": path, "content": content, "scenarios": ["scenario"]}],
            scenarios=[
                {
                    "name": "scenario",
                    "expected_result": "passes",
                    "test_file": path,
                    "risk": "",
                    "requirement_ids": ["REQ-1"],
                }
            ],
            risks=[],
            generation_profile=self.qa_policy.metadata(),
        )


if __name__ == "__main__":
    unittest.main()
