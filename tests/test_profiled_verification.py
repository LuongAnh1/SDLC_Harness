"""Contract tests cho Verification Runner dùng project profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.parse

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.artifacts.writers import _test_report_markdown
from sdlc_harness.profiles import (
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    QA_TEST_POLICY_VERSION,
    UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    VERIFICATION_STAGE_POLICY_VERSION,
    VerificationStagePolicy,
    build_planning_path_policy,
    build_qa_test_policy,
    build_verification_stage_policy,
    build_verification_stage_policy_version,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.agents.developer.closure_contract import CLOSURE_DEVELOPER_STAGE_IDS
from sdlc_harness.profiles import RUNTIME_CLOSURE_STAGE_POLICY_VERSION, build_runtime_closure_stage_policy
from sdlc_harness.verification import (
    RUNTIME_PROBE_SPEC_PATH,
    RuntimeProbeRun,
    VerificationResult,
    VerificationRunner,
    VerificationStageResult,
    resolve_verification_preparation_spec,
    resolve_verification_preparation_specs,
    resolve_verification_stage_specs,
)
from sdlc_harness.verification.runner import (
    _playwright_verified_test_files,
    _run_bounded,
    _runtime_probe_outcomes,
)


class RunBoundedTest(unittest.TestCase):
    """Real processes: a stage timeout must end the grandchild that holds the output pipes."""

    def test_returns_output_and_exit_code_without_a_shell(self) -> None:
        with patch("sdlc_harness.verification.runner.subprocess.Popen", wraps=subprocess.Popen) as popen:
            completed = _run_bounded(
                [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                cwd=os.getcwd(),
                env=dict(os.environ),
                timeout=30,
            )
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertEqual((completed.returncode, completed.stdout.strip(), completed.stderr.strip()), (3, "out", "err"))

    def test_timeout_kills_the_grandchild_that_holds_the_pipes(self) -> None:
        # The parent prints, starts a grandchild that inherits stdout, then sleeps; plain
        # subprocess.run would block ~60s on the grandchild's pipe after killing the parent.
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            _run_bounded([sys.executable, "-c", script], cwd=os.getcwd(), env=dict(os.environ), timeout=2)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("started", caught.exception.output or "")


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
        preparation = resolve_verification_preparation_spec(self.policy)
        self.assertIsNotNone(preparation)
        self.assertEqual(preparation.preparation_id, "dependency_setup")
        self.assertEqual(preparation.argv, ("npm", "ci"))
        preparations = resolve_verification_preparation_specs(self.policy)
        self.assertEqual(
            [item.preparation_id for item in preparations],
            ["dependency_setup", "generated_source_setup"],
        )
        self.assertEqual(preparations[1].argv, ("npm", "run", "db:generate"))
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

    def test_full_stack_policy_runs_real_stage_order_and_proves_playwright_file(self) -> None:
        project = get_project_profile("typescript_fullstack_v1")
        policy = build_verification_stage_policy_version(
            project=project,
            policy_version=VERIFICATION_STAGE_POLICY_VERSION,
        )
        self.assertEqual(
            policy.metadata()["stage_ownership"],
            {
                "database_ready": "harness_global",
                "migrate_and_seed": "harness_global",
                "typecheck": "developer_intent",
                "unit": "developer_intent",
                "api_database_integration": "harness_global",
                "frontend_build": "harness_global",
                "service_health": "harness_global",
                "browser_e2e": "qa_intent",
                "cleanup": "harness_global",
            },
        )
        qa_policy = build_qa_test_policy(
            planning_policy=self.qa_policy.planning_policy,
            policy_version=QA_TEST_POLICY_VERSION,
        )
        scripts = {
            "db:up": "db up",
            "db:migrate": "db migrate",
            "db:seed": "db seed",
            "typecheck": "tsc",
            "test:unit": "vitest run",
            "db:smoke": "db smoke",
            "build:web": "vite build",
            "verify:bootstrap": "verify bootstrap",
            "test:e2e": "playwright test",
            "db:down": "db down",
        }
        (self.target / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
        qa_path = "tests/e2e/approved-flow.spec.ts"
        qa_file = self.target / qa_path
        qa_file.parent.mkdir(parents=True)
        qa_file.write_text('import { test } from "@playwright/test";\n', encoding="utf-8")
        packet = {
            "run_id": "run_full_stack_browser",
            "schema_version": UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
            "verification_stage_ids": list(policy.stage_ids),
            "test_intents": [{"stage_id": "typecheck", "purpose": "Compile."}],
            "qa_test_intents": [{"stage_id": "browser_e2e", "target_paths": [qa_path]}],
        }

        def run_with_playwright_report(argv, **kwargs):
            del kwargs
            if "test:e2e" in argv:
                report = self.target / "playwright-report/results.json"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    json.dumps(
                        {
                            "suites": [
                                {
                                    "file": qa_path,
                                    "specs": [
                                        {
                                            "title": "approved flow",
                                            "tests": [{"results": [{"status": "passed"}]}],
                                        }
                                    ],
                                }
                            ],
                            "stats": {"unexpected": 0},
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="passed", stderr="")

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner._run_bounded",
                side_effect=run_with_playwright_report,
            ) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=packet,
                required_test_files=[qa_path],
                verification_policy=policy,
                qa_policy=qa_policy,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.executed_stage_ids, policy.stage_ids)
        self.assertEqual(result.selected_test_files, (qa_path,))
        self.assertEqual(result.verified_test_files, (qa_path,))
        self.assertIn(["npm", "run", "db:seed"], [call.args[0] for call in run.call_args_list])
        self.assertEqual(run.call_args_list[-1].args[0], ["npm", "run", "db:down"])
        self.assertTrue(run.call_args_list[0].kwargs["env"]["COMPOSE_PROJECT_NAME"].startswith("sdlc_"))

    def test_runner_prepares_dependencies_once_per_lockfile(self) -> None:
        (self.target / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr="")
        runner = VerificationRunner()
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=completed) as run,
        ):
            first = runner.run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )
            second = runner.run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(first.status, "passed")
        self.assertEqual(first.preparation_results[0].reason, "Verification preparation completed successfully.")
        self.assertIn("current inputs", second.preparation_results[0].reason)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(run.call_args_list[0].args[0], ["npm", "ci"])
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "run", "typecheck"])
        marker = self.target / "node_modules" / ".sdlc-harness-dependency-setup.json"
        self.assertTrue(marker.is_file())

    def test_runner_refreshes_generated_source_when_schema_changes(self) -> None:
        (self.target / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        (self.target / "packages/db/prisma").mkdir(parents=True)
        (self.target / "packages/db/package.json").write_text("{}\n", encoding="utf-8")
        (self.target / "packages/db/prisma7.config.ts").write_text("export default {};\n", encoding="utf-8")
        schema = self.target / "packages/db/prisma/schema.prisma"
        schema.write_text("model First { id String @id }\n", encoding="utf-8")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr="")

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=completed) as run,
        ):
            first = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )
            second = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )
            schema.write_text("model Second { id String @id }\n", encoding="utf-8")
            third = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(first.status, "passed")
        self.assertEqual(second.status, "passed")
        self.assertEqual(third.status, "passed")
        self.assertEqual(
            [item.stage_id for item in first.preparation_results],
            ["dependency_setup", "generated_source_setup"],
        )
        self.assertEqual(run.call_count, 9)
        self.assertEqual(run.call_args_list[0].args[0], ["npm", "ci"])
        self.assertEqual(run.call_args_list[1].args[0], ["npm", "run", "db:generate"])
        self.assertEqual(run.call_args_list[6].args[0], ["npm", "run", "db:generate"])
        marker = self.target / "node_modules" / ".sdlc-harness-generated-source-setup.json"
        self.assertTrue(marker.is_file())

    def test_runner_reports_generated_source_failure_as_implementation(self) -> None:
        (self.target / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        (self.target / "packages/db/prisma").mkdir(parents=True)
        (self.target / "packages/db/package.json").write_text("{}\n", encoding="utf-8")
        (self.target / "packages/db/prisma7.config.ts").write_text("export default {};\n", encoding="utf-8")
        (self.target / "packages/db/prisma/schema.prisma").write_text(
            'datasource db { provider = "postgresql" url = env("DATABASE_URL") }\n',
            encoding="utf-8",
        )
        installed = subprocess.CompletedProcess(args=[], returncode=0, stdout="installed", stderr="")
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Prisma schema validation failed at schema.prisma:1",
            stderr="npm error Lifecycle script failed",
        )

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch(
                "sdlc_harness.verification.runner._run_bounded",
                side_effect=[installed, failed],
            ) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_kind, "implementation")
        self.assertEqual(result.executed_stage_ids, ())
        self.assertEqual(
            [item.stage_id for item in result.preparation_results],
            ["dependency_setup", "generated_source_setup"],
        )
        self.assertEqual(run.call_count, 2)
        self.assertIn("Prisma schema", result.stdout)

    def test_runner_classifies_dependency_network_failure_as_environment(self) -> None:
        (self.target / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="npm error ENOTFOUND registry.npmjs.org",
        )
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=failed) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_kind, "environment")
        self.assertEqual(result.executed_stage_ids, ())
        self.assertEqual(result.preparation_results[0].stage_id, "dependency_setup")
        self.assertEqual(run.call_count, 1)

    def test_runner_keeps_source_lifecycle_failure_repairable(self) -> None:
        (self.target / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Prisma schema validation failed at schema.prisma:8",
            stderr="npm error Lifecycle script failed",
        )
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=failed),
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.failure_kind, "implementation")
        self.assertIn("Prisma schema", result.stdout)

    def test_runner_classifies_missing_nested_tool_as_environment(self) -> None:
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="'tsc' is not recognized as an internal or external command",
        )
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=failed),
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
            )

        self.assertEqual(result.failure_kind, "environment")

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
            patch("sdlc_harness.verification.runner._run_bounded", return_value=completed) as run,
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
            # shell=False is fixed inside _run_bounded (see RunBoundedTest).
            self.assertEqual(call.kwargs["cwd"], str(self.target.resolve()))
            self.assertEqual(call.kwargs["timeout"], 120)
            self.assertEqual(call.kwargs["env"]["CI"], "1")
            self.assertNotIn("NODE_OPTIONS", call.kwargs["env"])
            self.assertNotIn("SDLC_SECRET", call.kwargs["env"])

    def test_developer_unit_runner_executes_only_requested_approved_stage(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr="")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=completed) as run,
        ):
            result = VerificationRunner().run_stage(
                target_path=self.target,
                work_packet=self.packet,
                stage_id="typecheck",
                verification_policy=self.policy,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.planned_stage_ids, ("typecheck",))
        self.assertEqual(result.executed_stage_ids, ("typecheck",))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["npm", "run", "typecheck"])

    def test_runner_stops_after_first_failed_stage(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="type error")
        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", return_value=failed) as run,
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
            patch("sdlc_harness.verification.runner._run_bounded", side_effect=timeout) as run,
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
                "sdlc_harness.verification.runner._run_bounded",
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
        with patch("sdlc_harness.verification.runner._run_bounded") as run:
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
            patch("sdlc_harness.verification.runner._run_bounded") as run,
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
            patch("sdlc_harness.verification.runner._run_bounded") as run,
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
            patch("sdlc_harness.verification.runner._run_bounded") as run,
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
                "sdlc_harness.verification.runner._run_bounded",
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
            patch("sdlc_harness.verification.runner._run_bounded", return_value=completed),
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
                "sdlc_harness.verification.runner._run_bounded",
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
            patch("sdlc_harness.verification.runner._run_bounded", return_value=failed) as run,
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
                                    "assertionResults": [
                                        {
                                            "status": "failed",
                                            "fullName": "delete flow restores state after interruption",
                                            "failureMessages": ["expected true to be false"],
                                        }
                                    ],
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
                "sdlc_harness.verification.runner._run_bounded",
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
        self.assertEqual(result.failure_kind, "implementation")
        self.assertEqual(result.selected_test_files, (qa_path,))
        self.assertEqual(result.verified_test_files, (qa_path,))
        self.assertIn("delete flow restores state after interruption", result.reason)
        self.assertIn("expected true to be false", result.reason)

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
        preparation = VerificationStageResult(
            stage_id="dependency_setup",
            status="passed",
            argv=("npm", "ci"),
            cwd=".",
            timeout_seconds=300,
            exit_code=0,
            duration_seconds=1.5,
            stdout="installed",
            stderr="",
            reason="Dependency preparation completed successfully.",
        )
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
            preparation_results=(preparation,),
        )

        markdown = _test_report_markdown(result.to_dict())

        self.assertIn("## Verification Stages", markdown)
        self.assertIn("## Verification Preparation", markdown)
        self.assertIn("### dependency_setup", markdown)
        self.assertIn("### typecheck", markdown)
        self.assertIn('[\"npm\", \"run\", \"typecheck\"]', markdown)
        self.assertIn("- Timeout: `120` seconds", markdown)


CLOSURE_SCRIPTS = {
    "db:up": "db up",
    "db:migrate": "db migrate",
    "db:seed": "db seed",
    "typecheck": "tsc",
    "test:unit": "vitest run",
    "db:smoke": "db smoke",
    "build:web": "vite build",
    "verify:bootstrap": "verify bootstrap",
    "test:e2e": "playwright test",
    "db:down": "db down",
}


def probe_report(*probes: tuple[str, str, dict[str, object] | None]) -> dict[str, object]:
    """Playwright JSON for the Harness probe spec: (title, status, annotation payload)."""

    specs = []
    for title, status, observation in probes:
        test: dict[str, object] = {"results": [{"status": status}]}
        if observation is not None:
            test["annotations"] = [{"type": "runtime_probe", "description": json.dumps(observation)}]
        specs.append({"title": title, "tests": [test]})
    unexpected = sum(1 for _, status, _ in probes if status != "passed")
    return {"suites": [{"file": RUNTIME_PROBE_SPEC_PATH, "specs": specs}], "stats": {"unexpected": unexpected}}


class RuntimeClosureVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)
        (self.target / "package.json").write_text(json.dumps({"scripts": CLOSURE_SCRIPTS}), encoding="utf-8")
        spec = self.target / RUNTIME_PROBE_SPEC_PATH
        spec.parent.mkdir(parents=True)
        spec.write_text('import { test } from "@playwright/test";\n', encoding="utf-8")
        self.project = get_project_profile("typescript_fullstack_v1")
        self.policy = build_runtime_closure_stage_policy(project=self.project)
        self.packet = {
            "run_id": "run_runtime_closure_verification",
            "schema_version": UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
            "verification_stage_ids": list(self.policy.stage_ids),
            "test_intents": [{"stage_id": "typecheck", "purpose": "Closure changes type-check."}],
            "qa_test_intents": [],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_closure(self, report, runtime_probe, *, fail_stage: str = "", fail_stderr: str = ""):
        def fake_run(argv, **kwargs):
            del kwargs
            if "test:e2e" in argv and report is not None:
                path = self.target / "playwright-report/results.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report), encoding="utf-8")
            failed = (fail_stage and fail_stage in argv) or (
                "test:e2e" in argv and report is not None and report["stats"]["unexpected"]
            )
            stderr = fail_stderr if fail_stage and fail_stage in argv else ""
            return subprocess.CompletedProcess(args=argv, returncode=1 if failed else 0, stdout="", stderr=stderr)

        with (
            patch("sdlc_harness.verification.runner.shutil.which", return_value="npm"),
            patch("sdlc_harness.verification.runner._run_bounded", side_effect=fake_run) as run,
        ):
            result = VerificationRunner().run(
                target_path=self.target,
                work_packet=self.packet,
                verification_policy=self.policy,
                runtime_probe=runtime_probe,
            )
        return result, run

    def test_closure_policy_order_ownership_and_registry(self) -> None:
        self.assertEqual(self.policy.policy_version, RUNTIME_CLOSURE_STAGE_POLICY_VERSION)
        self.assertEqual(
            self.policy.stage_ids,
            (
                "database_ready",
                "migrate_and_seed",
                "typecheck",
                "unit",
                "api_database_integration",
                "frontend_build",
                "service_health",
                "runtime_probe",
                "cleanup",
            ),
        )
        ownership = self.policy.metadata()["stage_ownership"]
        self.assertEqual(ownership["runtime_probe"], "harness_global")
        self.assertEqual(
            tuple(stage for stage, owner in ownership.items() if owner == "developer_intent"),
            CLOSURE_DEVELOPER_STAGE_IDS,
        )
        self.assertNotIn("runtime_probe", self.project.verification_stage_ids)  # catalog untouched
        specs = {spec.stage_id: spec for spec in resolve_verification_stage_specs(self.policy)}
        self.assertEqual(specs["runtime_probe"].argv, ("npm", "run", "test:e2e"))
        self.assertFalse(specs["runtime_probe"].developer_selectable)
        self.assertTrue(specs["cleanup"].always_run)
        self.assertIn("runtime_probe", self.policy.prompt_context()["stage_capabilities"])
        # The legacy snapshot policy is unchanged.
        self.assertEqual(build_verification_stage_policy(project=self.project).stage_ids, ("typecheck", "unit"))

    def test_runtime_probe_runs_only_the_harness_spec_with_run_scoped_stand_in_env(self) -> None:
        report = probe_report(
            ("RP-1", "failed", {"probe_id": "RP-1", "actual_status": 500, "actual_items": None, "response_body_excerpt": "boom"}),
            ("RP-2", "passed", {"probe_id": "RP-2", "actual_status": 200, "actual_items": 3, "response_body_excerpt": "[1,2,3]"}),
        )
        result, run = self.run_closure(
            report, RuntimeProbeRun(probe_ids=("RP-1", "RP-2"), stand_in_env_keys=("LEDGER_URL",))
        )

        calls = [call.args[0] for call in run.call_args_list]
        probe_index = calls.index(["npm", "run", "test:e2e", "--", RUNTIME_PROBE_SPEC_PATH])
        probe_env = run.call_args_list[probe_index].kwargs["env"]
        self.assertRegex(probe_env["LEDGER_URL"], r"^http://127\.0\.0\.1:4[0-9]{4}$")
        self.assertIn("DATABASE_URL", probe_env)
        for index, call in enumerate(run.call_args_list):
            if index != probe_index:
                self.assertNotIn("LEDGER_URL", call.kwargs["env"])
        self.assertEqual(calls[-1], ["npm", "run", "db:down"])  # cleanup still runs after the failed stage
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_kind, "implementation")
        stage = next(item for item in result.stage_results if item.stage_id == "runtime_probe")
        # Every probe is read even though the first one failed.
        self.assertEqual([item["status"] for item in stage.probe_results], ["failed", "passed"])
        self.assertEqual(stage.probe_results[0]["observation"]["actual_status"], 500)
        self.assertIn("probe_results", stage.to_dict())

    def test_runtime_probe_report_missing_a_probe_is_a_policy_failure(self) -> None:
        report = probe_report(("RP-1", "passed", None))
        result, _ = self.run_closure(report, RuntimeProbeRun(probe_ids=("RP-1", "RP-2")))

        stage = next(item for item in result.stage_results if item.stage_id == "runtime_probe")
        self.assertEqual(stage.status, "failed")
        self.assertEqual(stage.failure_kind, "policy")
        self.assertIn("RP-2", stage.reason)

    def test_runtime_probe_passes_when_every_probe_passes(self) -> None:
        report = probe_report(("RP-1", "passed", {"probe_id": "RP-1", "actual_status": 204}))
        result, _ = self.run_closure(report, RuntimeProbeRun(probe_ids=("RP-1",)))
        self.assertEqual(result.status, "passed", result.reason)
        self.assertEqual(result.executed_stage_ids, self.policy.stage_ids)

    def test_report_files_resolve_against_playwright_root_dir(self) -> None:
        # Real Playwright JSON: files are relative to config.rootDir (testDir), nested in describe suites.
        root_dir = (self.target / "tests/e2e").as_posix()
        probe_payload = probe_report(("RP-1", "passed", {"probe_id": "RP-1", "actual_status": 200}))
        specs = probe_payload["suites"][0].pop("specs")
        probe_payload["suites"][0].update(
            {"file": "runtime-closure.probe.spec.ts", "specs": [], "suites": [{"title": "group", "specs": specs}]}
        )
        probe_payload["config"] = {"rootDir": root_dir}
        report_path = self.target / "probe-results.json"
        report_path.write_text(json.dumps(probe_payload), encoding="utf-8")
        outcomes, error = _runtime_probe_outcomes(
            report_path=report_path, target_root=self.target, probe_ids=("RP-1",)
        )
        self.assertEqual(error, "")
        self.assertEqual([item["status"] for item in outcomes], ["passed"])

        qa_path = "tests/e2e/approved-flow.spec.ts"
        (self.target / qa_path).write_text('import { test } from "@playwright/test";\n', encoding="utf-8")
        qa_payload = {
            "config": {"rootDir": root_dir},
            "suites": [
                {
                    "file": "approved-flow.spec.ts",
                    "specs": [{"title": "flow", "tests": [{"results": [{"status": "passed"}]}]}],
                }
            ],
            "stats": {"unexpected": 0},
        }
        report_path.write_text(json.dumps(qa_payload), encoding="utf-8")
        verified, qa_error, _ = _playwright_verified_test_files(
            report_path=report_path, target_root=self.target, selected_test_files=(qa_path,)
        )
        self.assertEqual((verified, qa_error), ((qa_path,), ""))

        with tempfile.TemporaryDirectory() as outside:
            qa_payload["config"] = {"rootDir": Path(outside).as_posix()}
            report_path.write_text(json.dumps(qa_payload), encoding="utf-8")
            _, outside_error, _ = _playwright_verified_test_files(
                report_path=report_path, target_root=self.target, selected_test_files=(qa_path,)
            )
        self.assertIn("outside the target repository", outside_error)

    def test_run_scoped_database_env_names_the_database_compose_creates(self) -> None:
        report = probe_report(("RP-1", "passed", {"probe_id": "RP-1", "actual_status": 204}))
        _, run = self.run_closure(report, RuntimeProbeRun(probe_ids=("RP-1",)))

        scoped = [call.kwargs["env"] for call in run.call_args_list if "DATABASE_URL" in call.kwargs["env"]]
        self.assertTrue(scoped)
        for env in scoped:
            url = urllib.parse.urlsplit(env["DATABASE_URL"])
            self.assertEqual(url.username, env["POSTGRES_USER"])
            self.assertEqual(url.password, env["POSTGRES_PASSWORD"])
            self.assertEqual(url.path.lstrip("/"), env["POSTGRES_DB"])
            self.assertEqual(str(url.port), env["SDLC_POSTGRES_PORT"])

    def test_database_authentication_failure_is_environment(self) -> None:
        result, run = self.run_closure(
            None,
            RuntimeProbeRun(probe_ids=("RP-1",)),
            fail_stage="db:migrate",
            fail_stderr="Error: P1000: Authentication failed against database server, the provided database "
            "credentials for `postgres` are not valid.",
        )
        self.assertEqual((result.status, result.failure_kind), ("failed", "environment"))
        self.assertEqual(run.call_args_list[-1].args[0], ["npm", "run", "db:down"])

    def test_runtime_probe_without_probes_does_not_start_playwright(self) -> None:
        result, run = self.run_closure(None, RuntimeProbeRun(probe_ids=()))
        self.assertEqual(result.status, "passed", result.reason)
        self.assertNotIn(
            ["npm", "run", "test:e2e", "--", RUNTIME_PROBE_SPEC_PATH],
            [call.args[0] for call in run.call_args_list],
        )

    def test_runtime_probe_fails_closed_on_missing_spec_and_unsafe_inputs(self) -> None:
        for runtime_probe in (
            None,
            RuntimeProbeRun(probe_ids=("RP-1",), stand_in_env_keys=("DATABASE_URL",)),
            RuntimeProbeRun(probe_ids=("RP-1",), stand_in_env_keys=("PATH",)),
            RuntimeProbeRun(probe_ids=("RP-1",), stand_in_env_keys=("NODE_OPTIONS",)),
            RuntimeProbeRun(probe_ids=("RP-1",), stand_in_env_keys=("POSTGRES_PASSWORD",)),
            RuntimeProbeRun(probe_ids=("RP-1",), stand_in_env_keys=("lower_case",)),
            RuntimeProbeRun(probe_ids=("X-1",)),
            RuntimeProbeRun(probe_ids=("RP-1", "RP-1")),
        ):
            with self.subTest(runtime_probe=runtime_probe), self.assertRaises(HarnessError) as caught:
                self.run_closure(None, runtime_probe)
            self.assertEqual(caught.exception.error_code, "verification_stage_policy_invalid")

        (self.target / RUNTIME_PROBE_SPEC_PATH).unlink()
        result, run = self.run_closure(None, RuntimeProbeRun(probe_ids=("RP-1",)))
        self.assertEqual((result.status, result.failure_kind), ("failed", "policy"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
