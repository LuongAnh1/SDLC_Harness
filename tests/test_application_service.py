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
                "--step-review",
                "--step-review-call-id",
                "plan-1-input",
                "--step-review-decision",
                "approve",
                "--step-review-note",
                "looks good",
                "--sync-mode",
                "real",
                "--runtime-root",
                ".runtime-test",
            ]
        )

        command = command_from_args(args)

        self.assertEqual(command.name, "continue-run")
        self.assertEqual(
            command.arguments,
            {
                "run_id": "run_1",
                "step_review": True,
                "step_review_input": False,
                "step_review_output": False,
                "step_review_call_id": "plan-1-input",
                "step_review_decision": "approve",
                "step_review_note": "looks good",
            },
        )
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
            settings_overrides=overrides
            or {
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            },
            runtime_root=self.runtime_root,
        )

    def write_state(
        self,
        run_id: str = "run_1",
        *,
        profiled: bool = True,
        **changes: object,
    ) -> None:
        state: dict[str, object] = {
            "run_id": run_id,
            "status": "waiting_design_approval",
            "latest_artifacts": {},
            "designer_contract_version": "vertical_design_graph_v1",
            "designer_contract_version": "vertical_design_graph_v1",
        }
        if profiled:
            state["profile_snapshot"] = build_profile_snapshot(
                project_profile_id="typescript_fullstack_v1",
                target_remote="https://github.com/example/target.git",
                target_base_branch="main",
            ).to_dict()
        state.update(changes)
        RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root).write_state(state)

    def test_get_status_dispatches_to_phase_one(self) -> None:
        self.write_state()
        response = {"ok": True, "run_id": "run_1", "status": "waiting_design_approval"}
        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.get_status.return_value = response

            result = self.service.execute(self.command("get-status", {"run_id": "run_1"}))

        self.assertEqual(result, response)
        workflow_type.return_value.get_status.assert_called_once_with(run_id="run_1")
        settings = workflow_type.call_args.args[0]
        self.assertEqual(settings.runtime_root, self.runtime_root.resolve())

    def test_get_status_rejects_run_id_path_traversal_before_reading_state(self) -> None:
        escaped = self.runtime_root / "escaped" / "state.json"
        escaped.parent.mkdir(parents=True)
        escaped.write_text('{"run_id":"escaped","status":"waiting_design_approval"}', encoding="utf-8")

        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            with self.assertRaises(HarnessError) as context:
                self.service.execute(self.command("get-status", {"run_id": "../escaped"}))

        self.assertEqual(context.exception.error_code, "invalid_path_parameter")
        workflow_type.assert_not_called()

    def test_get_status_projects_legacy_retirement_without_mutating_state(self) -> None:
        self.write_state(
            profiled=False,
            allowed_next_actions=["approve_checkpoint", "sync_run", "cancel_run", "get_status"],
            summary_for_user="Approve this checkpoint.",
        )
        before = RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root).state_path("run_1").read_text(
            encoding="utf-8"
        )
        response = {
            "ok": True,
            "run_id": "run_1",
            "allowed_next_actions": ["approve_checkpoint", "sync_run", "cancel_run", "get_status"],
            "summary_for_user": "Approve this checkpoint.",
        }
        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.get_status.return_value = response
            result = self.service.execute(self.command("get-status", {"run_id": "run_1"}))

        self.assertEqual(result["allowed_next_actions"], ["sync_run", "get_status"])
        self.assertIn("legacy_java_v1 đã retire", result["summary_for_user"])
        after = RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root).state_path("run_1").read_text(
            encoding="utf-8"
        )
        self.assertEqual(after, before)

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


    def test_start_blank_profile_uses_typescript_default_before_dispatch(self) -> None:
        command = self.command(
            "start-or-get-run",
            {"dataset": "eTour", "use_case": "UC1", "requested_by": "owner"},
            overrides={"SDLC_LLM_MODE": "fake", "SDLC_PROJECT_PROFILE": ""},
        )

        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            workflow_type.return_value.start_or_get_run.return_value = {"ok": True}
            response = self.service.execute(command)

        self.assertTrue(response["ok"])
        settings = workflow_type.call_args.args[0]
        self.assertEqual(settings.project_profile_id, "typescript_fullstack_v1")

    def test_start_rejects_active_legacy_run_for_same_source(self) -> None:
        source_ref = "dataset_use_case:eTour:UC1"
        self.write_state(profiled=False, source_ref=source_ref)
        store = RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root)
        store.register_active_run(source_ref, "run_1")

        with patch("sdlc_harness.application.service.PhaseOneWorkflow") as workflow_type:
            with self.assertRaises(HarnessError) as context:
                self.service.execute(
                    self.command(
                        "start-or-get-run",
                        {"dataset": "eTour", "use_case": "UC1", "requested_by": "owner"},
                    )
                )

        self.assertEqual(context.exception.error_code, "legacy_execution_profile_retired")
        workflow_type.assert_not_called()

    def test_legacy_run_blocks_artifact_producing_commands_before_dispatch(self) -> None:
        self.write_state(profiled=False)
        cases = [
            ("answer-clarification", {"run_id": "run_1", "question_id": "Q-1", "answer": "A", "answered_by": "owner"}),
            ("approve-checkpoint", {"run_id": "run_1"}),
            ("continue-run", {"run_id": "run_1"}),
            ("revalidate-design", {"run_id": "run_1"}),
            ("restart-design", {"run_id": "run_1"}),
            ("revalidate-requirement", {"run_id": "run_1"}),
            ("revalidate-implementation-plan", {"run_id": "run_1"}),
            ("revalidate-implementation", {"run_id": "run_1"}),
            ("adopt-run-workspace", {"run_id": "run_1"}),
        ]

        with (
            patch("sdlc_harness.application.service.PhaseOneWorkflow") as phase_one,
            patch("sdlc_harness.application.service.PhaseTwoWorkflow") as phase_two,
            patch("sdlc_harness.application.service.PhaseThreeWorkflow") as phase_three,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four,
            patch("sdlc_harness.application.service.CheckpointApprovalWorkflow") as approval,
            patch("sdlc_harness.application.service.WorkspaceAdoptionWorkflow") as adoption,
        ):
            for name, arguments in cases:
                with self.subTest(command=name):
                    with self.assertRaises(HarnessError) as context:
                        self.service.execute(self.command(name, arguments))
                    self.assertEqual(context.exception.error_code, "legacy_execution_profile_retired")

        for workflow_type in (phase_one, phase_two, phase_three, phase_four, approval, adoption):
            workflow_type.assert_not_called()


    def test_legacy_non_code_review_sync_remains_available(self) -> None:
        self.write_state(profiled=False)
        phase_one = Mock()
        phase_one.store.read_state.return_value = {"status": "waiting_design_approval"}
        phase_one.sync_run.return_value = {"ok": True}

        with patch("sdlc_harness.application.service.PhaseOneWorkflow", return_value=phase_one):
            result = self.service.execute(self.command("sync-run", {"run_id": "run_1"}))

        self.assertTrue(result["ok"])
        phase_one.sync_run.assert_called_once_with(run_id="run_1")

    def test_legacy_code_review_sync_uses_persisted_workspace_identity(self) -> None:
        self.write_state(profiled=False, status="code_review_ready")
        record = Mock(
            canonical_repo=str(self.root.parent / "SDLC_Target_eTour"),
            worktree_path=str(self.root.parent / "SDLC_Target_eTour-worktrees" / "run_1"),
            remote="https://github.com/example/legacy.git",
            base_ref="origin/legacy-main",
        )
        phase_one = Mock()
        phase_one.store.read_state.return_value = {"status": "code_review_ready"}

        with (
            patch("sdlc_harness.application.service.RunWorkspaceManager") as manager_type,
            patch("sdlc_harness.application.service.PhaseOneWorkflow", return_value=phase_one),
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as workflow_type,
        ):
            manager_type.return_value.read.return_value = record
            workflow_type.return_value.sync_code_review.return_value = {"ok": True}
            result = self.service.execute(self.command("sync-run", {"run_id": "run_1"}))

        self.assertTrue(result["ok"])
        settings = workflow_type.call_args.args[0]
        self.assertIsNone(settings.project_profile_id)
        self.assertEqual(settings.target_repo_path, Path(record.canonical_repo).resolve())
        self.assertEqual(settings.target_repo_remote, record.remote)
        self.assertEqual(settings.target_base_branch, "legacy-main")
        workflow_type.return_value.sync_code_review.assert_called_once_with(run_id="run_1")

    def test_legacy_cleanup_uses_persisted_workspace_identity(self) -> None:
        self.write_state(profiled=False, status="cancelled")
        record = Mock(
            canonical_repo=str(self.root.parent / "SDLC_Target_eTour"),
            worktree_path=str(self.root.parent / "SDLC_Target_eTour-worktrees" / "run_1"),
            remote="https://github.com/example/legacy.git",
            base_ref="origin/legacy-main",
        )

        with (
            patch("sdlc_harness.application.service.RunWorkspaceManager") as manager_type,
            patch("sdlc_harness.application.service.WorkspaceCleanupWorkflow") as workflow_type,
        ):
            manager_type.return_value.read.return_value = record
            workflow_type.return_value.cleanup.return_value = {"ok": True}
            result = self.service.execute(
                self.command("cleanup-run-workspace", {"run_id": "run_1"})
            )

        self.assertTrue(result["ok"])
        settings = workflow_type.call_args.args[0]
        self.assertIsNone(settings.project_profile_id)
        self.assertEqual(settings.target_repo_path, Path(record.canonical_repo).resolve())
        self.assertEqual(settings.target_worktree_root, Path(record.worktree_path).resolve().parent)
        self.assertEqual(settings.target_repo_remote, record.remote)
        self.assertEqual(settings.target_base_branch, "legacy-main")

    def test_legacy_workspace_identity_fails_closed_before_cleanup_dispatch(self) -> None:
        self.write_state(profiled=False, status="cancelled")
        record = Mock(
            canonical_repo=str(self.root.parent / "SDLC_Target_eTour"),
            worktree_path=str(self.root.parent / "SDLC_Target_eTour-worktrees" / "another-run"),
            remote="https://github.com/example/legacy.git",
            base_ref="origin/main",
        )

        with (
            patch("sdlc_harness.application.service.RunWorkspaceManager") as manager_type,
            patch("sdlc_harness.application.service.WorkspaceCleanupWorkflow") as workflow_type,
        ):
            manager_type.return_value.read.return_value = record
            with self.assertRaises(HarnessError) as context:
                self.service.execute(
                    self.command("cleanup-run-workspace", {"run_id": "run_1"})
                )

        self.assertEqual(context.exception.error_code, "workspace_path_invalid")
        workflow_type.assert_not_called()

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
            workflow_type.return_value.revalidate_requirement.return_value = {"ok": True}
            workflow_type.return_value.clarify_more.return_value = {"ok": True}

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
            self.service.execute(self.command("revalidate-requirement", {"run_id": "run_1"}))
            self.service.execute(self.command("clarify-more", {"run_id": "run_1"}))

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
        workflow_type.return_value.revalidate_requirement.assert_called_once_with(run_id="run_1")
        workflow_type.return_value.clarify_more.assert_called_once_with(run_id="run_1")

    def test_phase_two_three_four_commands_dispatch_to_current_workflows(self) -> None:
        self.write_state()
        with (
            patch("sdlc_harness.application.service.PhaseTwoWorkflow") as phase_two,
            patch("sdlc_harness.application.service.PhaseThreeWorkflow") as phase_three,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four,
        ):
            phase_two.return_value.revalidate_design.return_value = {"ok": True}
            phase_two.return_value.restart_design.return_value = {"ok": True}
            phase_three.return_value.revalidate_implementation_plan.return_value = {"ok": True}
            phase_four.return_value.continue_until_implementation_checkpoint.return_value = {"ok": True}
            phase_four.return_value.revalidate_implementation.return_value = {"ok": True}

            self.service.execute(self.command("revalidate-design", {"run_id": "run_1"}))
            self.service.execute(self.command("restart-design", {"run_id": "run_1"}))
            self.service.execute(
                self.command("revalidate-implementation-plan", {"run_id": "run_1"})
            )
            self.service.execute(self.command("continue-run", {"run_id": "run_1"}))
            self.service.execute(
                self.command("revalidate-implementation", {"run_id": "run_1"})
            )

        phase_two.return_value.revalidate_design.assert_called_once_with(run_id="run_1")
        phase_two.return_value.restart_design.assert_called_once_with(run_id="run_1")
        phase_three.return_value.revalidate_implementation_plan.assert_called_once_with(
            run_id="run_1"
        )
        phase_four.return_value.continue_until_implementation_checkpoint.assert_called_once_with(
            run_id="run_1"
        )
        phase_four.return_value.revalidate_implementation.assert_called_once_with(run_id="run_1")

    def test_continue_run_passes_developer_input_output_review_to_phase_four(self) -> None:
        self.write_state(status="implementation_ready", current_phase="implementation_preparing")
        arguments = {
            "run_id": "run_1",
            "step_review": True,
            "step_review_input": False,
            "step_review_output": False,
            "step_review_call_id": None,
            "step_review_decision": None,
            "step_review_note": None,
        }
        with (
            patch("sdlc_harness.application.service.PhaseOneWorkflow") as phase_one_type,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four_type,
        ):
            phase_one_type.return_value.store.read_state.return_value = {
                "status": "implementation_ready"
            }
            phase_four_type.return_value.continue_until_implementation_checkpoint.return_value = {
                "ok": True
            }

            self.service.execute(self.command("continue-run", arguments))

        self.assertEqual(
            phase_four_type.call_args.kwargs["developer_step_review"],
            {"enabled": True, "review_input": True, "review_output": True},
        )

    def test_continue_run_routes_requirement_pause_to_phase_one(self) -> None:
        self.write_state(
            status="requirement_paused",
            current_phase="requirement_drafting",
            allowed_next_actions=["continue_run", "get_status", "cancel_run"],
        )
        response = {"ok": True, "run_id": "run_1", "status": "waiting_requirement_approval"}
        with (
            patch(
                "sdlc_harness.application.service.PhaseOneWorkflow.continue_requirement_run",
                return_value=response,
            ) as phase_one_continue,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four,
        ):
            result = self.service.execute(self.command("continue-run", {"run_id": "run_1"}))

        self.assertEqual(result, response)
        phase_one_continue.assert_called_once_with(run_id="run_1")
        phase_four.return_value.continue_until_implementation_checkpoint.assert_not_called()

    def test_continue_run_routes_repo_discovery_pause_to_phase_two(self) -> None:
        self.write_state(
            status="repo_discovery_paused",
            current_phase="repo_discovery",
            allowed_next_actions=["continue_run", "get_status"],
        )
        response = {"ok": True, "run_id": "run_1", "status": "waiting_design_approval"}
        with (
            patch(
                "sdlc_harness.application.service.PhaseTwoWorkflow.continue_repo_discovery",
                return_value=response,
            ) as phase_two_continue,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four,
        ):
            result = self.service.execute(self.command("continue-run", {"run_id": "run_1"}))

        self.assertEqual(result, response)
        phase_two_continue.assert_called_once_with(run_id="run_1")
        phase_four.return_value.continue_until_implementation_checkpoint.assert_not_called()

    def test_continue_run_routes_design_step_review_decision_to_phase_two(self) -> None:
        self.write_state(
            status="design_paused",
            current_phase="design_investigating",
            allowed_next_actions=["continue_run", "get_status"],
        )
        response = {"ok": True, "run_id": "run_1", "status": "design_paused"}
        arguments = {
            "run_id": "run_1",
            "step_review_call_id": "designer-0001-input",
            "step_review_decision": "approve",
            "step_review_note": "looks good",
        }
        with (
            patch("sdlc_harness.application.service.PhaseOneWorkflow") as phase_one_type,
            patch("sdlc_harness.application.service.PhaseTwoWorkflow") as phase_two_type,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four_type,
        ):
            phase_one_type.return_value.store.read_state.return_value = {"status": "design_paused"}
            phase_two_type.return_value.continue_design.return_value = response

            result = self.service.execute(self.command("continue-run", arguments))

        self.assertEqual(result, response)
        self.assertEqual(
            phase_two_type.call_args.kwargs["designer_step_review"],
            {
                "enabled": True,
                "call_id": "designer-0001-input",
                "decision": "approve",
                "note": "looks good",
            },
        )
        phase_two_type.return_value.continue_design.assert_called_once_with(run_id="run_1")
        phase_four_type.return_value.continue_until_implementation_checkpoint.assert_not_called()

    def test_continue_run_routes_implementation_plan_step_review_to_phase_three(self) -> None:
        self.write_state(
            status="implementation_plan_step_review_required",
            current_phase="implementation_planning",
            allowed_next_actions=["continue_run", "get_status", "cancel_run"],
        )
        response = {"ok": True, "run_id": "run_1", "status": "implementation_plan_step_review_required"}
        arguments = {
            "run_id": "run_1",
            "step_review_call_id": "plan-1-input",
            "step_review_decision": "approve",
            "step_review_note": "looks good",
        }
        with (
            patch("sdlc_harness.application.service.PhaseOneWorkflow") as phase_one_type,
            patch("sdlc_harness.application.service.PhaseThreeWorkflow") as phase_three_type,
            patch("sdlc_harness.application.service.PhaseFourWorkflow") as phase_four_type,
        ):
            phase_one_type.return_value.store.read_state.return_value = {
                "status": "implementation_plan_step_review_required"
            }
            phase_three_type.return_value.continue_implementation_plan_step_review.return_value = response

            result = self.service.execute(self.command("continue-run", arguments))

        self.assertEqual(result, response)
        phase_three_type.assert_called_once()
        self.assertEqual(
            phase_three_type.call_args.kwargs["plan_step_review"],
            {
                "enabled": True,
                "call_id": "plan-1-input",
                "decision": "approve",
                "note": "looks good",
            },
        )
        phase_three_type.return_value.continue_implementation_plan_step_review.assert_called_once_with(
            run_id="run_1"
        )
        phase_four_type.return_value.continue_until_implementation_checkpoint.assert_not_called()

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
        self.write_state()
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
