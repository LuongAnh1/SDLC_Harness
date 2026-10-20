"""Contract tests cho Verification Runner dùng project profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.artifacts.writers import _test_report_markdown
from sdlc_harness.profiles import (
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    VerificationStagePolicy,
    build_planning_path_policy,
    build_qa_test_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.verification import (
    VerificationResult,
    VerificationRunner,
    VerificationStageResult,
    resolve_verification_stage_specs,
)


class ProfiledVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)
        (self.target / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "typecheck": "tsc --noEmit",
                        "test:unit": "vitest run",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.policy = build_verification_stage_policy(
            project=get_project_profile("typescript_fullstack_v1")
        )
        project = get_project_profile("typescript_fullstack_v1")
        self.qa_policy = build_qa_test_policy(
            planning_policy=build_planning_path_policy(
                project=project,
                language=get_language_profile(project.language_profile_id),
            )
        )
        self.packet = {
            "run_id": "run_profiled_verification",
            "schema_version": PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
            "verification_stage_ids": ["typecheck", "unit"],
            "test_intents": [
                {"stage_id": "typecheck", "purpose": "Check TypeScript contracts."},
                {"stage_id": "unit", "purpose": "Run unit tests."},
            ],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_registry_resolves_exact_order_and_rejects_drift(self) -> None:
        specs = resolve_verification_stage_specs(self.policy)
        self.assertEqual([item.stage_id for item in specs], ["typecheck", "unit"])
        self.assertEqual(specs[0].argv, ("npm", "run", "typecheck"))
        self.assertEqual(specs[1].argv, ("npm", "run", "test:unit"))
        self.assertEqual(specs[1].required_test_framework, "vitest")
        self.assertEqual(
            specs[1].machine_reporter_args,
            ("--reporter=json", "--outputFile={report_path}"),
        )

        drifted = VerificationStagePolicy(
            project_profile_id=self.policy.project_profile_id,
            policy_version=self.policy.policy_version,
            stage_ids=("unit", "typecheck"),
        )
        with self.assertRaises(HarnessError) as caught:
            resolve_verification_stage_specs(drifted)
        self.assertEqual(caught.exception.error_code, "verification_stage_policy_invalid")

        unsupported = VerificationStagePolicy(
            project_profile_id="unknown_profile_v1",
            policy_version=self.policy.policy_version,
            stage_ids=("typecheck", "unit"),
        )
        with self.assertRaises(HarnessError):
            resolve_verification_stage_specs(unsupported)

        wrong_version = VerificationStagePolicy(
            project_profile_id=self.policy.project_profile_id,
            policy_version="verification_stage_policy_v999",
            stage_ids=self.policy.stage_ids,
        )
        with self.assertRaises(HarnessError):
            resolve_verification_stage_specs(wrong_version)

    def test_runner_executes_fixed_stages_in_order_with_restricted_environment(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr="")
        with (
            patch.dict(
                os.environ,
                {
                    "Path": "safe-path",
                    "SystemRoot": "C:/Windows",
                    "NODE_OPTIONS": "--require malicious.js",
                    "SDLC_SECRET": "private",
                },
                clear=True,
            ),
            patch(
                "sdlc_harness.verification.runner.shutil.which",
                return_value="C:/npm.cmd",
            ) as which,
            patch("sdlc_harness.verification.runner.subprocess.run", return_value=completed) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.planned_stage_ids, ("typecheck", "unit"))
        self.assertEqual(result.executed_stage_ids, ("typecheck", "unit"))
        self.assertEqual([item.stage_id for item in result.stage_results], ["typecheck", "unit"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(which.call_args_list[0].kwargs["path"], "safe-path")
        self.assertEqual(run.call_args_list[0].args[0], ["C:/npm.cmd", "run", "typecheck"])
        self.assertEqual(run.call_args_list[1].args[0], ["C:/npm.cmd", "run", "test:unit"])
        for call in run.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["cwd"], str(self.target.resolve()))
            self.assertEqual(call.kwargs["timeout"], 120)
            self.assertEqual(call.kwargs["env"]["CI"], "1")
            self.assertNotIn("NODE_OPTIONS", call.kwargs["env"])
            self.assertNotIn("SDLC_SECRET", call.kwargs["env"])

    def test_runner_stops_after_first_failed_stage(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="type error")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner.subprocess.run", return_value=failed) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.executed_stage_ids, ("typecheck",))
        self.assertEqual(result.stderr, "type error")
        self.assertEqual(run.call_count, 1)

    def test_runner_records_timeout_and_stops(self) -> None:
        timeout = subprocess.TimeoutExpired(cmd=["npm"], timeout=120, output="partial", stderr="slow")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner.subprocess.run", side_effect=timeout) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.executed_stage_ids, ("typecheck",))
        self.assertIn("partial", result.stdout)
        self.assertEqual(run.call_count, 1)

    def test_runner_records_process_start_failure(self) -> None:
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner.subprocess.run",
                side_effect=OSError("executable disappeared"),
            ) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.executed_stage_ids, ("typecheck",))
        self.assertIn("executable disappeared", result.stderr)
        self.assertEqual(run.call_count, 1)

    def test_runner_fails_preflight_when_script_or_npm_is_missing(self) -> None:
        (self.target / "package.json").write_text(
            json.dumps({"scripts": {"typecheck": "tsc --noEmit"}}),
            encoding="utf-8",
        )
        with patch("sdlc_harness.verification.runner.subprocess.run") as run:
            missing_script = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )
        self.assertIn("test:unit", missing_script.reason)
        run.assert_not_called()

        (self.target / "package.json").write_text(
            json.dumps({"scripts": {"typecheck": "tsc", "test:unit": "vitest run"}}),
            encoding="utf-8",
        )
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value=None),
            patch("sdlc_harness.verification.runner.subprocess.run") as run,
        ):
            missing_npm = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )
        self.assertIn("npm", missing_npm.reason)
        run.assert_not_called()

    def test_runner_rejects_missing_policy_stage_drift_and_raw_execution_fields(self) -> None:
        with (
            patch("sdlc_harness.verification.runner.subprocess.run") as run,
            self.assertRaises(HarnessError),
        ):
            VerificationRunner().run(target_path=self.target, work_packet=self.packet)
        run.assert_not_called()

        drifted_packet = {**self.packet, "verification_stage_ids": ["unit", "typecheck"]}
        with self.assertRaises(HarnessError):
            VerificationRunner().run(
                target_path=self.target,
                work_packet=drifted_packet,
                verification_policy=self.policy,
            )

        raw_packet = {
            **self.packet,
            "test_intents": [{"stage_id": "typecheck", "command": "echo unsafe"}],
        }
        with (
            patch("sdlc_harness.verification.runner.subprocess.run") as run,
            self.assertRaises(HarnessError),
        ):
            VerificationRunner().run(
                target_path=self.target,
                work_packet=raw_packet,
                verification_policy=self.policy,
            )
        run.assert_not_called()

    def test_profiled_qa_executes_exact_required_vitest_file(self) -> None:
        qa_path = "apps/api/src/generated.test.ts"
        qa_file = self.target / qa_path
        qa_file.parent.mkdir(parents=True)
        qa_file.write_text("import { it } from 'vitest';\n", encoding="utf-8")
        def run_with_report(argv, **kwargs):
            del kwargs
            if "test:unit" in argv:
                report_arg = next(item for item in argv if item.startswith("--outputFile="))
                Path(report_arg.split("=", 1)[1]).write_text(
                    json.dumps(
                        {
                            "success": True,
                            "testResults": [
                                {
                                    "name": str(qa_file.resolve()),
                                    "assertionResults": [
                                        {"status": "passed"},
                                        {"status": "passed"},
                                    ],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="passed", stderr="")

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner.subprocess.run",
                side_effect=run_with_report,
            ) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=[qa_path],
                verification_policy=self.policy,
                qa_policy=self.qa_policy,
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.selected_test_files, (qa_path,))
        self.assertEqual(result.verified_test_files, (qa_path,))
        unit_argv = run.call_args_list[1].args[0]
        self.assertEqual(unit_argv[:5], ["npm", "run", "test:unit", "--", "--reporter=json"])
        self.assertTrue(unit_argv[5].startswith("--outputFile="))
        self.assertEqual(unit_argv[6], qa_path)
        self.assertEqual(
            result.stage_results[1].argv,
            (
                "npm",
                "run",
                "test:unit",
                "--",
                "--reporter=json",
                "--outputFile=<temporary-vitest-json-report>",
                qa_path,
            ),
        )

    def test_profiled_qa_fails_without_exact_vitest_json_evidence(self) -> None:
        qa_path = "apps/api/src/generated.test.ts"
        qa_file = self.target / qa_path
        qa_file.parent.mkdir(parents=True)
        qa_file.write_text("import { it } from 'vitest';\n", encoding="utf-8")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr="")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner.subprocess.run", return_value=completed),
        ):
            missing = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=[qa_path],
                verification_policy=self.policy,
                qa_policy=self.qa_policy,
            )
        self.assertEqual(missing.status, "failed")
        self.assertEqual(missing.verified_test_files, ())
        self.assertIn("machine-readable JSON report", missing.reason)

        def run_with_wrong_report(argv, **kwargs):
            del kwargs
            if "test:unit" in argv:
                report_arg = next(item for item in argv if item.startswith("--outputFile="))
                Path(report_arg.split("=", 1)[1]).write_text(
                    json.dumps(
                        {
                            "success": True,
                            "testResults": [
                                {
                                    "name": str(
                                        (self.target / "apps/api/src/other.test.ts").resolve()
                                    ),
                                    "assertionResults": [{"status": "passed"}],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="passed", stderr="")

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner.subprocess.run",
                side_effect=run_with_wrong_report,
            ),
        ):
            mismatched = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=[qa_path],
                verification_policy=self.policy,
                qa_policy=self.qa_policy,
            )
        self.assertEqual(mismatched.status, "failed")
        self.assertEqual(mismatched.verified_test_files, ("apps/api/src/other.test.ts",))
        self.assertIn("does not match the exact selected", mismatched.reason)

    def test_profiled_qa_keeps_selected_but_not_verified_when_typecheck_fails(self) -> None:
        qa_path = "apps/api/src/generated.test.ts"
        qa_file = self.target / qa_path
        qa_file.parent.mkdir(parents=True)
        qa_file.write_text("import { it } from 'vitest';\n", encoding="utf-8")
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="type error")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner.subprocess.run", return_value=failed) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=[qa_path],
                verification_policy=self.policy,
                qa_policy=self.qa_policy,
            )
        self.assertEqual(result.selected_test_files, (qa_path,))
        self.assertEqual(result.verified_test_files, ())
        self.assertEqual(result.executed_stage_ids, ("typecheck",))
        self.assertEqual(run.call_count, 1)

    def test_profiled_qa_records_executed_file_when_vitest_assertion_fails(self) -> None:
        qa_path = "apps/api/src/generated.test.ts"
        qa_file = self.target / qa_path
        qa_file.parent.mkdir(parents=True)
        qa_file.write_text("import { it } from 'vitest';\n", encoding="utf-8")

        def run_with_failed_report(argv, **kwargs):
            del kwargs
            if "test:unit" in argv:
                report_arg = next(item for item in argv if item.startswith("--outputFile="))
                Path(report_arg.split("=", 1)[1]).write_text(
                    json.dumps(
                        {
                            "success": False,
                            "testResults": [
                                {
                                    "name": str(qa_file.resolve()),
                                    "assertionResults": [{"status": "failed"}],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=1,
                    stdout="failed",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="passed", stderr="")

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner.subprocess.run",
                side_effect=run_with_failed_report,
            ),
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=[qa_path],
                verification_policy=self.policy,
                qa_policy=self.qa_policy,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.selected_test_files, (qa_path,))
        self.assertEqual(result.verified_test_files, (qa_path,))
        self.assertIn("unsuccessful", result.reason)

    def test_profiled_qa_required_files_need_policy_and_existing_safe_paths(self) -> None:
        with self.assertRaises(HarnessError) as missing_policy:
            VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                required_test_files=["apps/api/src/generated.test.ts"],
                verification_policy=self.policy,
            )
        self.assertEqual(missing_policy.exception.error_code, "qa_test_policy_invalid")

        for path, error_code in (
            ("--reporter=verbose", "qa_test_out_of_scope"),
            ("tests/e2e/generated.spec.ts", "qa_test_out_of_scope"),
            ("apps/api/src/missing.test.ts", "qa_test_file_missing"),
        ):
            with self.subTest(path=path), self.assertRaises(HarnessError) as caught:
                VerificationRunner().run(
                    target_path=self.target,
                    work_packet=self.packet,
                    required_test_files=[path],
                    verification_policy=self.policy,
                    qa_policy=self.qa_policy,
                )
            self.assertEqual(caught.exception.error_code, error_code)

    def test_report_renders_each_stage_evidence(self) -> None:
        stage = VerificationStageResult(
            stage_id="typecheck",
            status="passed",
            argv=("npm", "run", "typecheck"),
            cwd=".",
            timeout_seconds=120,
            exit_code=0,
            duration_seconds=0.25,
            stdout="types ok",
            stderr="",
            reason="Verification stage completed successfully: typecheck",
        )
        result = VerificationResult(
            status="passed",
            command="",
            cwd=str(self.target),
            exit_code=0,
            duration_seconds=0.25,
            stdout="types ok",
            stderr="",
            reason="All profile-owned verification stages completed successfully.",
            planned_stage_ids=("typecheck", "unit"),
            executed_stage_ids=("typecheck",),
            stage_results=(stage,),
        )

        markdown = _test_report_markdown(result.to_dict())

        self.assertIn("## Verification Stages", markdown)
        self.assertIn("### typecheck", markdown)
        self.assertIn('[\"npm\", \"run\", \"typecheck\"]', markdown)
        self.assertIn("- Timeout: `120` seconds", markdown)


if __name__ == "__main__":
    unittest.main()
