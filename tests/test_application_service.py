"""Khóa application service chung trước khi thêm HTTP transport."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sdlc_harness.application import ApplicationCommand, ApplicationService
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.interfaces.cli import _settings_from_args, build_parser, command_from_args
from sdlc_harness.profiles import build_profile_snapshot
from sdlc_harness.storage.runtime import RuntimeStore


class ApplicationCommandTests(unittest.TestCase):
    def test_command_copies_input_mappings(self) -> None:
        arguments = {"run_id": "run_1"}
        overrides = {"SDLC_SYNC_MODE": "mock"}
        command = ApplicationCommand(
            name="get-status",
            arguments=arguments,
            settings_overrides=overrides,
        )

        arguments["run_id"] = "changed"
        overrides["SDLC_SYNC_MODE"] = "real"

        self.assertEqual(command.arguments["run_id"], "run_1")
        self.assertEqual(command.settings_overrides["SDLC_SYNC_MODE"], "mock")

    def test_cli_namespace_maps_arguments_and_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "continue-run",
                "--run-id",
                "run_1",
                "--llm-mode",
                "fake",
                "--implementation-repair-limit",
                "4",
                "--qa-repair-limit",
                "3",
                "--sync-mode",
                "real",
                "--runtime-root",
                ".runtime-test",
            ]
        )

        command = command_from_args(args)

        self.assertEqual(command.name, "continue-run")
        self.assertEqual(command.arguments, {"run_id": "run_1"})
        self.assertEqual(command.runtime_root, Path(".runtime-test"))
        self.assertEqual(
            dict(command.settings_overrides),
            {
                "SDLC_LLM_MODE": "fake",
                "SDLC_IMPLEMENTATION_REPAIR_LIMIT": "4",
                "SDLC_QA_REPAIR_LIMIT": "3",
                "SDLC_SYNC_MODE": "real",
            },
        )

    def test_experiment_keeps_source_runtime_and_cli_only_arguments(self) -> None:
        args = build_parser().parse_args(
            [
                "experiment-design-consensus",
                "--source-run-id",
                "run_source",
                "--source-runtime-root",
                ".source-runtime",
                "--experiment-root",
                ".experiments/custom",
                "--llm-call-budget",
                "12",
            ]
        )

        command = command_from_args(args)

        self.assertEqual(command.runtime_root, Path(".source-runtime"))
        self.assertEqual(command.arguments["source_run_id"], "run_source")
        self.assertEqual(command.arguments["experiment_root"], ".experiments/custom")
        self.assertEqual(command.arguments["llm_call_budget"], "12")

    def test_cli_sync_default_remains_mock_when_environment_is_real(self) -> None:
        args = build_parser().parse_args(
            ["start-from-jira", "--issue-key", "SDLC-1", "--requested-by", "test"]
        )
        with patch.dict(os.environ, {"SDLC_SYNC_MODE": "real"}):
            settings = _settings_from_args(args)
        self.assertEqual(settings.sync_mode, "mock")


class ApplicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.temp_dir = TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.service = ApplicationService(root=self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def command(
        self,
        name: str,
        arguments: dict[str, object],
        overrides: dict[str, str] | None = None,
    ) -> ApplicationCommand:
        return ApplicationCommand(
            name=name,
            arguments=arguments,
            settings_overrides=overrides or {"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"},
            runtime_root=self.runtime_root,
        )

    def write_state(self, run_id: str = "run_1", **changes: object) -> None:
        state: dict[str, object] = {
            "run_id": run_id,
            "status": "waiting_design_approval",
            "latest_artifacts": {},
        }
        state.update(changes)
        RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root).write_state(state)

    def test_get_status_dispatches_to_phase_one(self) -> None:
        response = {"ok": True, "run_id": "run_1", "status": "waiting_design_approval"}
        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.get_status.return_value = response

            result = self.service.execute(self.command("get-status", {"run_id": "run_1"}))

        self.assertEqual(result, response)
        workflow_type.return_value.get_status.assert_called_once_with(run_id="run_1")
        settings = workflow_type.call_args.args[0]
        self.assertEqual(settings.runtime_root, self.runtime_root.resolve())

    def test_sync_routes_code_review_state_to_phase_four(self) -> None:
        self.write_state(status="code_review_ready")
        phase_one = Mock()
        phase_one.store.read_state.return_value = {"status": "code_review_ready"}
        response = {"ok": True, "status": "code_review_ready"}
        with (
            patch("sdlc_harness.application.service.PhaseOneWorkflow", return_value=phase_one),
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four_type,
        ):
            phase_four_type.return_value.sync_code_review.return_value = response

            result = self.service.execute(self.command("sync-run", {"run_id": "run_1"}))

        self.assertEqual(result, response)
        phase_four_type.return_value.sync_code_review.assert_called_once_with(run_id="run_1")
        phase_one.sync_run.assert_not_called()

    def test_sync_routes_non_code_review_state_to_phase_one(self) -> None:
        self.write_state()
        phase_one = Mock()
        phase_one.store.read_state.return_value = {"status": "waiting_design_approval"}
        phase_one.sync_run.return_value = {"ok": True, "status": "waiting_design_approval"}
        with patch("sdlc_harness.application.service.PhaseOneWorkflow", return_value=phase_one):
            self.service.execute(self.command("sync-run", {"run_id": "run_1"}))

        phase_one.sync_run.assert_called_once_with(run_id="run_1")

    def test_experiment_dispatch_preserves_path_and_budget(self) -> None:
        with patch("sdlc_harness.application.service.PhaseTwoWorkflow") as workflow_type:
            workflow_type.return_value.experiment_design_consensus.return_value = {"ok": True}

            self.service.execute(
                self.command(
                    "experiment-design-consensus",
                    {
                        "source_run_id": "run_source",
                        "experiment_root": ".experiments/custom",
                        "llm_call_budget": "12",
                    },
                )
            )

        workflow_type.return_value.experiment_design_consensus.assert_called_once_with(
            source_run_id="run_source",
            experiment_root=Path(".experiments/custom"),
            llm_call_budget=12,
        )

    def test_prepare_target_repo_dispatches_operator_flag(self) -> None:
        with patch("sdlc_harness.application.service.TargetRepoPreparationWorkflow") as workflow_type:
            workflow_type.return_value.prepare.return_value = {"ok": True}

            self.service.execute(
                self.command("prepare-target-repo", {"init_codegraph": True})
            )

        workflow_type.return_value.prepare.assert_called_once_with(init_codegraph=True)

    def test_phase_one_write_commands_dispatch_all_arguments(self) -> None:
        self.write_state()
        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.start_or_get_run.return_value = {"ok": True}
            workflow_type.return_value.start_from_jira.return_value = {"ok": True}
            workflow_type.return_value.answer_clarification.return_value = {"ok": True}

            self.service.execute(
                self.command(
                    "start-or-get-run",
                    {"dataset": "eTour", "use_case": "UC1", "requested_by": "owner"},
                )
            )
            self.service.execute(
                self.command(
                    "start-from-jira",
                    {"issue_key": "SDLC-1", "requested_by": "owner"},
                )
            )
            self.service.execute(
                self.command(
                    "answer-clarification",
                    {
                        "run_id": "run_1",
                        "question_id": "Q-1",
                        "answer": "Use the approved rule.",
                        "answered_by": "owner",
                    },
                )
            )

        workflow_type.return_value.start_or_get_run.assert_called_once_with(
            dataset="eTour", use_case="UC1", requested_by="owner"
        )
        workflow_type.return_value.start_from_jira.assert_called_once_with(
            issue_key="SDLC-1", requested_by="owner"
        )
        workflow_type.return_value.answer_clarification.assert_called_once_with(
            run_id="run_1",
            question_id="Q-1",
            answer="Use the approved rule.",
            answered_by="owner",
        )

    def test_phase_two_three_four_commands_dispatch_to_current_workflows(self) -> None:
        self.write_state()
        with (
            patch("sdlc_harness.application.service.PhaseTwoWorkflow") as phase_two,
            patch("sdlc_harness.application.service.PhaseThreeWorkflow") as phase_three,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four,
        ):
            phase_two.return_value.revalidate_design.return_value = {"ok": True}
            phase_three.return_value.revalidate_implementation_plan.return_value = {"ok": True}
            phase_four.return_value.continue_until_implementation_checkpoint.return_value = {"ok": True}
            phase_four.return_value.revalidate_implementation.return_value = {"ok": True}

            self.service.execute(self.command("revalidate-design", {"run_id": "run_1"}))
            self.service.execute(
                self.command("revalidate-implementation-plan", {"run_id": "run_1"})
            )
            self.service.execute(self.command("continue-run", {"run_id": "run_1"}))
            self.service.execute(
                self.command("revalidate-implementation", {"run_id": "run_1"})
            )

        phase_two.return_value.revalidate_design.assert_called_once_with(run_id="run_1")
        phase_three.return_value.revalidate_implementation_plan.assert_called_once_with(
            run_id="run_1"
        )
        phase_four.return_value.continue_until_implementation_checkpoint.assert_called_once_with(
            run_id="run_1"
        )
        phase_four.return_value.revalidate_implementation.assert_called_once_with(run_id="run_1")

    def test_approval_and_workspace_commands_dispatch(self) -> None:
        self.write_state()
        with (
            patch("sdlc_harness.application.service.CheckpointApprovalWorkflow") as approval,
            patch("sdlc_harness.application.service.WorkspaceAdoptionWorkflow") as adoption,
            patch("sdlc_harness.application.service.WorkspaceCleanupWorkflow") as cleanup,
        ):
            approval.return_value.approve_checkpoint.return_value = {"ok": True}
            adoption.return_value.adopt.return_value = {"ok": True}
            cleanup.return_value.cleanup.return_value = {"ok": True}

            self.service.execute(
                self.command(
                    "approve-checkpoint",
                    {"run_id": "run_1", "checkpoint_id": "checkpoint_1"},
                )
            )
            self.service.execute(self.command("adopt-run-workspace", {"run_id": "run_1"}))
            self.service.execute(self.command("cleanup-run-workspace", {"run_id": "run_1"}))

        approval.return_value.approve_checkpoint.assert_called_once_with(
            run_id="run_1", checkpoint_id="checkpoint_1"
        )
        adoption.return_value.adopt.assert_called_once_with(run_id="run_1")
        cleanup.return_value.cleanup.assert_called_once_with(run_id="run_1")

    def test_run_actions_use_persisted_profile_identity_after_config_changes(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/snapshotted.git",
            target_base_branch="snapshot-main",
        )
        self.write_state(profile_snapshot=snapshot.to_dict())

        with patch("sdlc_harness.application.service.PhaseTwoWorkflow") as workflow_type:
            workflow_type.return_value.revalidate_design.return_value = {"ok": True}
            self.service.execute(
                self.command(
                    "revalidate-design",
                    {"run_id": "run_1"},
                    overrides={
                        "SDLC_LLM_MODE": "fake",
                        "SDLC_SYNC_MODE": "mock",
                        "SDLC_PROJECT_PROFILE": "",
                        "SDLC_TARGET_REPO_REMOTE": "https://github.com/example/current.git",
                        "SDLC_TARGET_BASE_BRANCH": "current-main",
                    },
                )
            )

        settings = workflow_type.call_args.args[0]
        self.assertEqual(settings.project_profile_id, "typescript_fullstack_v1")
        self.assertEqual(settings.target_repo_remote, "https://github.com/example/snapshotted.git")
        self.assertEqual(settings.target_base_branch, "snapshot-main")

    def test_invalid_profile_snapshot_blocks_mutation_before_workflow_dispatch(self) -> None:
        self.write_state(profile_snapshot={"version": "profile_snapshot_v2"})

        with patch("sdlc_harness.application.service.PhaseFourWorkflow") as workflow_type:
            with self.assertRaises(HarnessError) as context:
                self.service.execute(self.command("continue-run", {"run_id": "run_1"}))

        self.assertEqual(context.exception.error_code, "profile_snapshot_invalid")
        workflow_type.assert_not_called()

    def test_registry_mismatched_snapshot_blocks_mutation_before_workflow_dispatch(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        ).to_dict()
        snapshot["language_profile_schema"] = "language_profile_v2"
        self.write_state(profile_snapshot=snapshot)

        with patch("sdlc_harness.application.service.PhaseFourWorkflow") as workflow_type:
            with self.assertRaises(HarnessError) as context:
                self.service.execute(self.command("continue-run", {"run_id": "run_1"}))

        self.assertEqual(context.exception.error_code, "profile_snapshot_mismatch")
        workflow_type.assert_not_called()

    def test_unknown_command_raises_stable_error(self) -> None:
        with self.assertRaises(HarnessError) as context:
            self.service.execute(self.command("unknown", {}))
        self.assertEqual(context.exception.error_code, "unknown_command")

    def test_business_error_is_not_rewritten_by_service(self) -> None:
        failure = HarnessError("run_not_found", "Run not found", run_id="run_1")
        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.get_status.side_effect = failure
            with self.assertRaises(HarnessError) as context:
                self.service.execute(self.command("get-status", {"run_id": "run_1"}))
        self.assertIs(context.exception, failure)

    def test_run_mutations_share_runtime_lock(self) -> None:
        command = self.command("sync-run", {"run_id": "run_1"})
        settings = self.service.settings_for(command)
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

        with store.run_lock("run_1"):
            with self.assertRaises(HarnessError) as context:
                self.service.execute(command)

        self.assertEqual(context.exception.error_code, "run_action_locked")

    def test_start_commands_share_source_lock(self) -> None:
        command = self.command(
            "start-or-get-run",
            {"dataset": "eTour", "use_case": "UC1", "requested_by": "owner"},
        )
        settings = self.service.settings_for(command)
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

        with store.source_lock("dataset_use_case:eTour:UC1"):
            with self.assertRaises(HarnessError) as context:
                self.service.execute(command)

        self.assertEqual(context.exception.error_code, "source_action_locked")


if __name__ == "__main__":
    unittest.main()
