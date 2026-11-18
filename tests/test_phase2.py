"""Kiểm thử Phase 2 V0 cho contract approve checkpoint -> design checkpoint."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.application import ApplicationCommand, ApplicationService
from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.designer import (
    DesignerToolLoopExecutor,
    new_vertical_design_graph,
)
from sdlc_harness.agents.validator import DesignFinding
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.sync.atlassian import real_sync_design_checkpoint
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow, _design_repair_stalled
from tests._support import GitRepositoryTemplate, RuntimeDirectoryTemplate, populate_typescript_target


def design_finding(name: str, *, excerpt: str = "") -> DesignFinding:
    return DesignFinding(
        finding_id=f"SEM-{name}",
        category="cross_diagram_contract",
        severity="blocking",
        message=f"Problem {name}",
        expected_change=f"Fix {name}",
        requirement_ids=["REQ-1"],
        diagram_ids=["sequence_diagram_1"],
        evidence=[{"source": "design", "ref": f"contract:{name}", "excerpt": excerpt}],
    )


class PhaseTwoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.target_repo_template = GitRepositoryTemplate(populate_typescript_target)
        cls.runtime_template_temp = tempfile.TemporaryDirectory()
        cls.phase1_target = cls.target_repo_template.materialize(
            Path(cls.runtime_template_temp.name) / "target"
        )
        runtime_root = Path(cls.runtime_template_temp.name) / ".runtime"
        settings = load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "16",
                "SDLC_TARGET_REPO_PATH": str(cls.phase1_target.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(cls.phase1_target.remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_TARGET_WORKTREE_ROOT": str(Path(cls.runtime_template_temp.name) / "worktrees"),
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        cls.phase1_run_id = str(created["run_id"])
        cls.phase1_runtime_template = RuntimeDirectoryTemplate(runtime_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.phase1_runtime_template.close()
        cls.runtime_template_temp.cleanup()
        cls.target_repo_template.close()

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.target = self.target_repo_template.materialize(Path(self.temp_dir.name) / "target")
        suffix = hashlib.sha256(self._testMethodName.encode("utf-8")).hexdigest()[:8]
        self.run_id = f"run_20261020_000000_{suffix}"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "16",
                "SDLC_TARGET_REPO_PATH": str(self.target.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(self.target.remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_TARGET_WORKTREE_ROOT": str(Path(self.temp_dir.name) / "worktrees"),
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_phase1_run(self) -> dict[str, object]:
        self.phase1_runtime_template.materialize(
            self.runtime_root,
            replacements={
                self.phase1_run_id: self.run_id,
                str(self.phase1_target.canonical): str(self.target.canonical),
                self.phase1_target.canonical.as_posix(): self.target.canonical.as_posix(),
                str(self.phase1_target.remote.resolve()): str(self.target.remote.resolve()),
                self.phase1_target.remote.resolve().as_posix(): self.target.remote.resolve().as_posix(),
            },
        )
        return PhaseOneWorkflow(self.settings).get_status(run_id=self.run_id)

    def _create_phase1_run_fresh(self) -> dict[str, object]:
        return PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

    def test_blocked_session_graph_uses_session_ordinal_when_versions_diverge(self) -> None:
        workflow = PhaseTwoWorkflow(self.settings)
        graph = new_vertical_design_graph(
            run_id=self.run_id,
            snapshot_binding={"requirement_digest": "sha256:req"},
        )
        session_dir = (
            workflow.store.run_dir(self.run_id)
            / "designer_execution"
            / "design_v11"
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "designer_loop_session_v1.json").write_text(
            json.dumps({"graph": graph}),
            encoding="utf-8",
        )

        recovered = workflow._blocked_session_graph(
            state={"designer_execution": {"design_version": 10, "session_ordinal": 11}},
            run_id=self.run_id,
        )

        self.assertEqual(recovered, graph)

    def test_blocked_session_graph_prefers_persisted_validator_input(self) -> None:
        workflow = PhaseTwoWorkflow(self.settings)
        graph = new_vertical_design_graph(
            run_id=self.run_id,
            snapshot_binding={"requirement_digest": "sha256:req"},
        )
        graph_path = (
            workflow.store.run_dir(self.run_id)
            / "design_validator_execution"
            / "design_v11"
            / "vertical_design_graph_input.json"
        )
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps(graph), encoding="utf-8")

        recovered = workflow._blocked_session_graph(
            state={
                "designer_execution": {
                    "design_version": 11,
                    "session_ordinal": 12,
                    "validation_graph_path": workflow.store.relative(graph_path),
                }
            },
            run_id=self.run_id,
        )

        self.assertEqual(recovered, graph)

    def _target_overrides(self) -> dict[str, str]:
        return {
            "SDLC_TARGET_REPO_PATH": str(self.target.canonical),
            "SDLC_TARGET_REPO_REMOTE": str(self.target.remote.resolve()),
            "SDLC_TARGET_BASE_BRANCH": "main",
            "SDLC_TARGET_WORKTREE_ROOT": str(Path(self.temp_dir.name) / "worktrees"),
        }

    def test_settings_reads_design_repair_limit(self) -> None:
        self.assertEqual(self.settings.design_repair_limit, 2)
        self.assertEqual(self.settings.design_llm_call_budget, 16)
        self.assertEqual(self.settings.designer_partition_action_budget, 12)
        self.assertEqual(self.settings.designer_scenario_repair_action_budget, 4)

    def test_sliced_enforce_persists_review_plan_and_reaches_design_checkpoint(self) -> None:
        settings = replace(
            self.settings,
        )
        created = self._create_phase1_run()

        response = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        plan_ref = response["latest_artifacts"]["design_semantic_review_plan"]
        plan = json.loads((self.repo_root / plan_ref).read_text(encoding="utf-8"))
        self.assertEqual(plan["schema_version"], "semantic_review_plan_v2")
        self.assertGreater(plan["coverage"]["task_count"], 0)
        report_ref = response["latest_artifacts"]["design_validation_report"]
        report = json.loads((self.repo_root / report_ref).read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "design_validation_report_v5_sliced")
        self.assertEqual(report["semantic_mode"], "design_validation_sliced_v1")
        self.assertEqual(self.settings.designer_finalization_action_budget, 4)
        self.assertEqual(self.settings.designer_global_action_hard_cap, 128)
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_DESIGN_REPAIR_LIMIT": "0",
                "SDLC_DESIGNER_PARTITION_ACTION_BUDGET": "9",
                "SDLC_DESIGNER_SCENARIO_REPAIR_ACTION_BUDGET": "5",
                "SDLC_DESIGNER_FINALIZATION_ACTION_BUDGET": "3",
                "SDLC_DESIGNER_GLOBAL_ACTION_HARD_CAP": "144",
            },
        )
        self.assertEqual(settings.design_repair_limit, 0)
        self.assertEqual(settings.designer_partition_action_budget, 9)
        self.assertEqual(settings.designer_scenario_repair_action_budget, 5)
        self.assertEqual(settings.designer_finalization_action_budget, 3)
        self.assertEqual(settings.designer_global_action_hard_cap, 144)

    def test_step_review_decision_survives_design_runtime_reset(self) -> None:
        review = {
            "enabled": True,
            "call_id": "designer-0001",
            "decision": "approve",
            "note": "",
        }
        workflow = PhaseTwoWorkflow(self.settings, designer_step_review=review)

        workflow._reset_design_runtime()

        self.assertEqual(
            workflow.designer_tool_loop.step_review,
            {
                "enabled": True,
                "review_input": True,
                "review_output": True,
                "call_id": "designer-0001",
                "decision": "approve",
                "note": "",
            },
        )

    def test_step_review_defaults_come_from_runtime_settings(self) -> None:
        settings = replace(
            self.settings,
            design_step_review_enabled=True,
            design_step_review_input_enabled=False,
            design_step_review_output_enabled=True,
        )

        workflow = PhaseTwoWorkflow(settings)

        self.assertEqual(
            workflow.designer_step_review,
            {"enabled": True, "review_input": False, "review_output": True},
        )

    def test_restart_design_uses_approved_requirement_and_current_design_contract(self) -> None:
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        review = {"enabled": True, "review_input": True, "review_output": True}
        paused = PhaseTwoWorkflow(
            self.settings,
            designer_step_review=review,
        ).approve_checkpoint(run_id=run_id)
        self.assertEqual(paused["status"], "design_paused")
        approved_artifacts = dict(
            PhaseTwoWorkflow(self.settings).store.read_state(run_id)["approved_artifacts"]
        )
        first_ordinal = int(paused["designer_execution"]["session_ordinal"])

        restart_settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                **self._target_overrides(),
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            },
        )
        restarted = PhaseTwoWorkflow(
            restart_settings,
            designer_step_review=review,
        ).restart_design(run_id=run_id)

        self.assertEqual(restarted["status"], "design_paused")
        restarted_state = PhaseTwoWorkflow(restart_settings).store.read_state(run_id)
        self.assertEqual(restarted_state["approved_artifacts"], approved_artifacts)
        self.assertEqual(restarted["designer_contract_version"], "vertical_design_graph_v2")
        self.assertEqual(restarted["designer_uiux_mode"], "enforce")
        self.assertEqual(restarted["designer_uiux_contract_version"], "uiux_pro_max_search_v1")
        second_ordinal = int(restarted["designer_execution"]["session_ordinal"])
        self.assertGreater(second_ordinal, first_ordinal)
        run_dir = PhaseTwoWorkflow(restart_settings).store.run_dir(run_id)
        self.assertTrue((run_dir / "designer_execution" / f"design_v{first_ordinal}").is_dir())
        self.assertTrue((run_dir / "designer_execution" / f"design_v{second_ordinal}").is_dir())
        self.assertIn("restart_design", restarted["allowed_next_actions"])

    def _continue_through_service(self, run_id: str) -> dict[str, object]:
        overrides = {
            **self._target_overrides(),
            "SDLC_LLM_MODE": "fake",
            "SDLC_SYNC_MODE": "mock",
            "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            "SDLC_DESIGN_REPAIR_LIMIT": "2",
            "SDLC_DESIGN_LLM_CALL_BUDGET": "16",
        }
        return ApplicationService(root=self.repo_root).execute(
            ApplicationCommand(
                name="continue-run",
                arguments={"run_id": run_id},
                settings_overrides=overrides,
                runtime_root=self.runtime_root,
            )
        )

    def _assert_interrupted_resume(self, run_id: str, *, from_status: str, handler: str) -> None:
        recoveries = PhaseTwoWorkflow(self.settings).store.read_state(run_id).get("recoveries") or []
        self.assertEqual(
            [(item["code"], item["from_status"], item["handler"]) for item in recoveries],
            [("interrupted_resume", from_status, handler)],
        )

    def test_designer_loop_killed_mid_session_resumes_from_continue_run(self) -> None:
        run_id = str(self._create_phase1_run()["run_id"])
        with patch.object(DesignerToolLoopExecutor, "_execute_fake", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        state = PhaseTwoWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["status"], "design_investigating")
        session_dir = PhaseTwoWorkflow(self.settings).store.run_dir(run_id) / "designer_execution"
        self.assertTrue(any(session_dir.glob("design_v*/designer_loop_session_v1.json")))

        resumed = self._continue_through_service(run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")
        self._assert_interrupted_resume(run_id, from_status="design_investigating", handler="continue_design")

    def test_repo_discovery_killed_mid_way_resumes_from_continue_run(self) -> None:
        run_id = str(self._create_phase1_run()["run_id"])
        with patch("sdlc_harness.workflows.repo_discovery.build_repo_landscape", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        self.assertEqual(PhaseTwoWorkflow(self.settings).store.read_state(run_id)["status"], "repo_discovery")

        resumed = self._continue_through_service(run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")
        self._assert_interrupted_resume(run_id, from_status="repo_discovery", handler="continue_repo_discovery")

    def test_design_restart_killed_mid_way_restarts_again_from_continue_run(self) -> None:
        run_id = str(self._create_phase1_run()["run_id"])
        review = {"enabled": True, "review_input": True, "review_output": True}
        paused = PhaseTwoWorkflow(self.settings, designer_step_review=review).approve_checkpoint(run_id=run_id)
        self.assertEqual(paused["status"], "design_paused")
        with patch.object(PhaseTwoWorkflow, "_run_vertical_design", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                PhaseTwoWorkflow(self.settings).restart_design(run_id=run_id)
        self.assertEqual(PhaseTwoWorkflow(self.settings).store.read_state(run_id)["status"], "design_restarting")

        resumed = self._continue_through_service(run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")
        self._assert_interrupted_resume(run_id, from_status="design_restarting", handler="restart_design")

    def test_restart_design_rejects_non_paused_run(self) -> None:
        created = self._create_phase1_run()

        with self.assertRaises(HarnessError) as context:
            PhaseTwoWorkflow(self.settings).restart_design(run_id=str(created["run_id"]))

        self.assertEqual(context.exception.error_code, "design_restart_not_allowed")

    def test_restart_design_recovers_run_blocked_before_any_design_graph(self) -> None:
        run_id = str(self._create_phase1_run()["run_id"])
        capped = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                **self._target_overrides(),
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGNER_GLOBAL_ACTION_HARD_CAP": "1",
            },
        )
        blocked = PhaseTwoWorkflow(capped).approve_checkpoint(run_id=run_id)
        self.assertEqual(blocked["status"], "design_blocked")
        self.assertEqual(blocked["last_error"]["error_code"], "designer_action_budget_hard_cap_exceeded")
        with self.assertRaises(HarnessError) as context:
            PhaseTwoWorkflow(self.settings).revalidate_design(run_id=run_id)
        self.assertEqual(context.exception.error_code, "vertical_design_graph_missing")

        restarted = PhaseTwoWorkflow(self.settings).restart_design(run_id=run_id)

        self.assertEqual(restarted["status"], "waiting_design_approval")

    def test_production_flow_blocks_safely_when_tight_budget_cannot_converge(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                **self._target_overrides(),
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "2",
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        response = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        # Two calls cannot finish the sliced semantic review: the run fails closed instead of reaching a checkpoint.
        self.assertEqual(response["status"], "design_blocked")
        self.assertEqual(response["last_error"]["error_code"], "design_semantic_review_inconclusive")
        self.assertIn("revalidate_design", response["allowed_next_actions"])
        self.assertIsNone(response["pending_checkpoint"])
        # A blocked run that already has a Design graph keeps it: restart is refused, revalidate is the way on.
        with self.assertRaises(HarnessError) as context:
            PhaseTwoWorkflow(settings).restart_design(run_id=str(created["run_id"]))
        self.assertEqual(context.exception.error_code, "design_restart_not_allowed")

    def test_repair_stalls_when_canonical_blockers_repeat_after_graph_change(self) -> None:
        self.assertTrue(
            _design_repair_stalled(
                prior_digest="sha256:v1",
                current_digest="sha256:v2",
                prior_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
                current_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
            )
        )
        self.assertFalse(
            _design_repair_stalled(
                prior_digest="sha256:v1",
                current_digest="sha256:v2",
                prior_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
                current_blocker_keys=frozenset({"terminal_state|sc-4"}),
            )
        )


    def test_approve_checkpoint_creates_design_checkpoint(self) -> None:
        created = self._create_phase1_run_fresh()
        response = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertTrue(response["ok"])
        self.assertFalse(response["created_new_run"])
        self.assertEqual(response["current_phase"], "design_review")
        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["pending_checkpoint"]["type"], "design_approval")
        self.assertEqual(response["last_sync"]["phase"], "design_checkpoint")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("design_artifact", response["latest_artifacts"])
        self.assertIn("requirement_graph", response["latest_artifacts"])
        self.assertIn("requirement_design_trace", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["design_traceability"]["ok"])
        self.assertEqual(response["quality_reports"]["design_traceability"]["mode"], "traceability_v2")
        self.assertIn("không phát hiện missing link", response["quality_reports"]["design_traceability"]["summary"])

        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertTrue((run_dir / "artifacts" / "designer_analysis_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "class_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_design_trace_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "missing_design_links_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "design_quality_report_v1.md").exists())
        self.assertTrue((run_dir / "sync" / "design-jira-comment.md").exists())
        self.assertTrue((run_dir / "sync" / "design-confluence-page.md").exists())
        mock_jira = (run_dir / "sync" / "design-jira-comment.md").read_text(encoding="utf-8")
        self.assertIn("## Repository Landscape", mock_jira)
        self.assertIn("Inventory: 5 component(s)", mock_jira)
        class_graph = json.loads((run_dir / "artifacts" / "class_diagram_graph_v1.json").read_text(encoding="utf-8"))
        class_names = {node["className"] for node in class_graph["nodes"]}
        self.assertIn("CulturalHeritage", class_names)
        sequence_graph = json.loads((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").read_text(encoding="utf-8"))
        first_sequence = sequence_graph["sequenceDiagrams"][0]
        self.assertTrue(first_sequence["nodes"])
        trace = json.loads((run_dir / "artifacts" / "requirement_design_trace_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["mode"], "traceability_v2")
        self.assertIn("class_trace", trace)
        self.assertIn("sequence_trace", trace)
        missing = json.loads((run_dir / "artifacts" / "missing_design_links_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(missing["mode"], "traceability_v2")
        self.assertEqual(missing["missing_count"], 0)
        self.assertIn("class_diagram", missing["missing_links"])
        report = json.loads((run_dir / "sync" / "tool_sync_report_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["checkpoint"]["type"], "design_approval")

    def test_repo_discovery_pause_can_resume_before_design(self) -> None:
        created = self._create_phase1_run_fresh()
        run_id = str(created["run_id"])
        workflow = PhaseTwoWorkflow(self.settings)
        real_create = workflow.repo_discovery.workspace_manager.create_for_run
        attempts = 0

        def flaky_create(current_run_id: str):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HarnessError("workspace_fetch_failed", "temporary fetch failure", run_id=current_run_id)
            return real_create(current_run_id)

        with patch.object(
            workflow.repo_discovery.workspace_manager,
            "create_for_run",
            side_effect=flaky_create,
        ):
            paused = workflow.approve_checkpoint(run_id=run_id)
            resumed = workflow.continue_repo_discovery(run_id=run_id)

        self.assertEqual(paused["status"], "repo_discovery_paused")
        self.assertEqual(paused["allowed_next_actions"], ["continue_run", "get_status"])
        self.assertEqual(paused["last_error"]["error_code"], "workspace_fetch_failed")
        self.assertEqual(resumed["status"], "waiting_design_approval")
        self.assertIn("repo_landscape", resumed["latest_artifacts"])
        self.assertIn("run_workspace", resumed["latest_artifacts"])
        self.assertEqual(resumed["quality_reports"]["repo_discovery"]["status"], "passed")


    def test_projection_tampering_is_rejected_before_design_state_mutation(self) -> None:
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        state_path = self.runtime_root / "runs" / run_id / "state.json"
        state_before = json.loads(state_path.read_text(encoding="utf-8"))
        requirement_path = self.repo_root / str(state_before["latest_artifacts"]["requirement_packet"])
        requirement_path.write_text(
            "# Requirement Packet V2\n\n## Notes\nTampered projection.",
            encoding="utf-8",
        )

        with self.assertRaises(HarnessError) as caught:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=run_id)

        self.assertEqual(caught.exception.error_code, "requirement_projection_mismatch")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), state_before)
        self.assertFalse((self.runtime_root / "runs" / run_id / "sync" / "tool_sync_report_v2.json").exists())


    def test_sync_run_resyncs_design_checkpoint(self) -> None:
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertIn("sync_run", approved["allowed_next_actions"])

        synced = PhaseOneWorkflow(self.settings).sync_run(run_id=str(approved["run_id"]))

        self.assertEqual(synced["run_id"], approved["run_id"])
        self.assertEqual(synced["last_sync"]["phase"], "design_checkpoint")
        self.assertEqual(synced["last_sync"]["status"], "mock_synced")
        self.assertIn("review design", synced["summary_for_user"])

    def test_real_sync_design_writes_jira_and_confluence_targets(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if "/comment" in url:
                return {"id": "10002"}
            if url.endswith("/wiki/api/v2/pages"):
                return {"id": "20002", "_links": {"webui": "/spaces/SDLC/pages/20002/Design"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(approved["run_id"]),
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["phase"], "design_checkpoint")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        jira_payload = calls[0][2]
        self.assertIsNotNone(jira_payload)
        self.assertNotIn(
            str(self.runtime_root).replace("\\", "\\\\"),
            json.dumps(jira_payload, ensure_ascii=False),
        )
        self.assertIn("Design review", jira_payload["body"]["content"][0]["content"][0]["text"])
        jira_blocks = jira_payload["body"]["content"]
        self.assertTrue(any(block["type"] == "codeBlock" for block in jira_blocks))
        self.assertTrue(
            any(
                block["type"] == "heading" and block["content"][0]["text"] == "Repository landscape"
                for block in jira_blocks
            )
        )
        self.assertTrue(
            any(
                block["type"] == "heading" and block["content"][0]["text"] == "Diagrams"
                for block in jira_blocks
            )
        )
        confluence_payload = calls[1][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>SDLC Harness - UC1 - Design Checkpoint</h1>", confluence_body)
        self.assertIn("SDLC-HARNESS-CHECKPOINT::design::dataset_use_case:eTour:UC1", confluence_body)
        self.assertIn("<h2>Phụ lục UML</h2>", confluence_body)
        self.assertIn("<h2>Repository Landscape</h2>", confluence_body)
        self.assertIn("<h3>Class Diagram</h3>", confluence_body)
        self.assertIn("<h3>Sequence Diagram</h3>", confluence_body)
        self.assertIn("<ac:adf-extension>", confluence_body)
        self.assertIn("plantuml-confluence", confluence_body)
        self.assertIn('key="diagram-code"', confluence_body)
        self.assertIn('ac:name="code"', confluence_body)
        self.assertNotIn("<ac:image", confluence_body)
        self.assertIn("@startuml", confluence_body)
        self.assertIn("<h2>Quality Report</h2>", confluence_body)
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["mode"], "real")
        self.assertEqual(report["targets"][0]["operation"], "created")
        self.assertEqual(report["targets"][1]["operation"], "created")
        self.assertNotIn("diagram_images", report["targets"][1])

    def test_real_sync_design_creates_targets_even_when_phase1_report_exists(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        run_id = str(approved["run_id"])
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / run_id / "sync"
        (sync_dir / "tool_sync_report_v1.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "jira", "status": "synced", "external_id": "10001"},
                        {"tool": "confluence", "status": "synced", "external_id": "20001"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "POST" and "/comment" in url:
                return {"id": "10002"}
            if method == "POST" and url.endswith("/wiki/api/v2/pages"):
                return {"id": "20002", "_links": {"webui": "/spaces/SDLC/pages/20002/Design"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=run_id,
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][0]["operation"], "created")
        self.assertEqual(report["targets"][1]["operation"], "created")

    def test_real_sync_design_records_partial_failure(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            if "/comment" in url:
                return {"id": "10002"}
            raise RuntimeError("boom")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(approved["run_id"]),
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "partial_failed")

    def test_approve_checkpoint_rejects_wrong_checkpoint_id(self) -> None:
        created = self._create_phase1_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(
                run_id=str(created["run_id"]),
                checkpoint_id="checkpoint_requirement_wrong",
            )

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_revalidate_design_creates_next_version_without_restarting_requirement(self) -> None:
        created = self._create_phase1_run()
        initial = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        response = PhaseTwoWorkflow(self.settings).revalidate_design(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))
        self.assertTrue(response["latest_artifacts"]["design_validation_report"].endswith("design_validation_report_v2.json"))
        self.assertEqual(response["quality_reports"]["design_validation"]["versions"], ["v1", "v2"])
        self.assertNotEqual(initial["pending_checkpoint"]["artifact_ref"], response["pending_checkpoint"]["artifact_ref"])

    def test_revalidate_paused_design_supersedes_stale_call_and_validates_existing_graph_first(self) -> None:
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        workflow = PhaseTwoWorkflow(self.settings)
        workflow.approve_checkpoint(run_id=run_id)
        run_dir = workflow.store.run_dir(run_id)
        review_dir = run_dir / "designer_execution" / "design_v99" / "input_step_review"
        review_dir.mkdir(parents=True)
        exchange_path = review_dir / "exchange_designer-input-0001-a01.json"
        stale_call = {
            "call_id": "designer-input-0001-a01",
            "ordinal": 1,
            "attempt": 1,
            "status": "input_pending",
        }
        exchange_path.write_text(json.dumps(stale_call), encoding="utf-8")
        (review_dir / "step_review_session_v1.json").write_text(
            json.dumps({"calls": [stale_call]}),
            encoding="utf-8",
        )
        state = workflow.store.read_state(run_id)
        state.update(
            {
                "current_phase": "design_investigating",
                "status": "design_paused",
                "designer_repair_feedback": [
                    {
                        "id": "STALE-001",
                        "category": "obsolete_policy",
                        "severity": "blocking",
                    }
                ],
                "designer_execution": {
                    "status": "review_required",
                    "session_ordinal": 99,
                    "design_version": 2,
                    "pending_step_review": {
                        "call_id": "designer-input-0001-a01-input",
                        "exchange_path": str(exchange_path),
                        "review_stage": "input",
                    },
                },
            }
        )
        workflow.store.write_state(state)

        with patch.object(
            DesignerToolLoopExecutor,
            "execute",
            side_effect=AssertionError("Designer must not run before revalidation"),
        ):
            paused = PhaseTwoWorkflow(
                self.settings,
                designer_step_review={"enabled": True, "review_input": True},
            ).revalidate_design(run_id=run_id)

        self.assertEqual(paused["status"], "design_paused", paused)
        self.assertEqual(paused["last_error"]["error_code"], "design_validator_step_review_required")
        latest_state = workflow.store.read_state(run_id)
        self.assertEqual(latest_state["designer_repair_feedback"], [])
        pending = latest_state["designer_execution"]["validator_step_review"]
        self.assertIn("design_validator_execution", str(pending["exchange_path"]))
        stale_session = json.loads(
            (review_dir / "step_review_session_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stale_session["calls"][0]["status"], "superseded")

        with patch.object(
            DesignerToolLoopExecutor,
            "execute",
            side_effect=AssertionError("Designer must not run while resuming Validator"),
        ):
            paused_output = PhaseTwoWorkflow(
                self.settings,
                designer_step_review={
                    "enabled": True,
                    "review_input": True,
                    "call_id": pending["call_id"],
                    "decision": "approve",
                },
            ).continue_design(run_id=run_id)

        self.assertEqual(paused_output["status"], "design_paused", paused_output)
        output_review = workflow.store.read_state(run_id)["designer_execution"][
            "validator_step_review"
        ]
        self.assertEqual(output_review["review_stage"], "output")

    def test_supersede_pending_designer_output_uses_loop_session(self) -> None:
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        workflow = PhaseTwoWorkflow(self.settings)
        run_dir = workflow.store.run_dir(run_id)
        review_dir = run_dir / "designer_execution" / "design_v99"
        review_dir.mkdir(parents=True)
        exchange_path = review_dir / "designer_exchange_designer-0001.json"
        exchange_path.write_text(
            json.dumps({"call_id": "designer-0001", "status": "review_required"}),
            encoding="utf-8",
        )
        session_path = review_dir / "designer_loop_session_v1.json"
        session_path.write_text(
            json.dumps(
                {
                    "status": "review_required",
                    "pending_step_review": {"call_id": "designer-0001"},
                }
            ),
            encoding="utf-8",
        )
        state = workflow.store.read_state(run_id)
        state["designer_execution"] = {
            "pending_step_review": {
                "call_id": "designer-0001",
                "exchange_path": workflow.store.relative(exchange_path),
            }
        }

        workflow._supersede_pending_design_step_review(state=state, run_id=run_id)

        exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
        self.assertEqual(exchange["status"], "superseded")
        self.assertEqual(exchange["superseded_reason"], "design_revalidation_policy_changed")
        self.assertIsNone(session["pending_step_review"])
        self.assertEqual(session["status"], "superseded")

    def test_design_validator_step_review_pauses_and_replays_approved_output(self) -> None:
        settings = self.settings
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)

        paused = PhaseTwoWorkflow(
            settings,
            designer_step_review={"enabled": True, "review_input": False},
        ).revalidate_design(run_id=run_id)

        self.assertEqual(paused["status"], "design_paused", paused)
        self.assertEqual(paused["last_error"]["error_code"], "design_validator_step_review_required")
        state = PhaseTwoWorkflow(settings).store.read_state(run_id)
        pending = state["designer_execution"]["validator_step_review"]
        self.assertEqual(pending["call_id"], "design-validator-0001-a01")
        resumed = PhaseTwoWorkflow(
            settings,
            designer_step_review={
                "enabled": True,
                "call_id": pending["call_id"],
                "decision": "approve",
                "note": "",
            },
        ).continue_design(run_id=run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")
        self.assertTrue(resumed["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))

    def test_design_validator_can_review_input_before_provider_call(self) -> None:
        settings = self.settings
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)

        paused_input = PhaseTwoWorkflow(
            settings,
            designer_step_review={"enabled": True, "review_input": True},
        ).revalidate_design(run_id=run_id)

        self.assertEqual(paused_input["status"], "design_paused", paused_input)
        state = PhaseTwoWorkflow(settings).store.read_state(run_id)
        input_review = state["designer_execution"]["validator_step_review"]
        self.assertEqual(input_review["review_stage"], "input")
        self.assertTrue(str(input_review["call_id"]).endswith("-input"))
        exchange = json.loads((self.repo_root / str(input_review["exchange_path"])).read_text(encoding="utf-8"))
        self.assertEqual(exchange["status"], "input_pending")
        self.assertEqual(exchange["raw_completion"], "")

        paused_output = PhaseTwoWorkflow(
            settings,
            designer_step_review={
                "enabled": True,
                "review_input": True,
                "call_id": input_review["call_id"],
                "decision": "approve",
            },
        ).continue_design(run_id=run_id)

        self.assertEqual(paused_output["status"], "design_paused", paused_output)
        state = PhaseTwoWorkflow(settings).store.read_state(run_id)
        output_review = state["designer_execution"]["validator_step_review"]
        self.assertEqual(output_review["review_stage"], "output")
        self.assertFalse(str(output_review["call_id"]).endswith("-input"))

        resumed = PhaseTwoWorkflow(
            settings,
            designer_step_review={
                "enabled": True,
                "review_input": True,
                "call_id": output_review["call_id"],
                "decision": "approve",
            },
        ).continue_design(run_id=run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")

    def test_revalidate_design_advances_past_interrupted_unpublished_validation(self) -> None:
        settings = self.settings
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)

        first = PhaseTwoWorkflow(
            settings,
            designer_step_review={"enabled": True, "review_input": True},
        ).revalidate_design(run_id=run_id)
        self.assertEqual(first["status"], "design_paused", first)

        workflow = PhaseTwoWorkflow(settings)
        state = workflow.store.read_state(run_id)
        old_review = dict(state["designer_execution"]["validator_step_review"])
        state["status"] = "design_validating"
        state["current_phase"] = "design_validating"
        state["designer_execution"]["status"] = "validating_existing"
        workflow.store.write_state(state)

        second = PhaseTwoWorkflow(
            settings,
            designer_step_review={"enabled": True, "review_input": True},
        ).revalidate_design(run_id=run_id)

        self.assertEqual(second["status"], "design_paused", second)
        recovered = workflow.store.read_state(run_id)
        self.assertEqual(recovered["designer_execution"]["design_version"], 3)
        self.assertIn(
            "design_validator_execution/design_v3/",
            str(recovered["designer_execution"]["validator_step_review"]["exchange_path"]).replace("\\", "/"),
        )
        old_exchange = json.loads(
            (self.repo_root / str(old_review["exchange_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(old_exchange["status"], "superseded")

    def test_revalidate_design_recovers_feedback_from_latest_finding_ledger(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state["designer_repair_feedback"] = []
        ledger_ref = state["latest_artifacts"]["design_finding_ledger"]
        ledger_path = self.repo_root / str(ledger_ref)
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["blocking_findings"] = [
            {
                "id": "CONS-RETRY",
                "category": "behavior_order",
                "severity": "blocking",
                "message": "The previous semantic blocker must reach Designer revalidation.",
            }
        ]
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        feedback = workflow._persisted_design_feedback(state=state, run_id=run_id)

        self.assertEqual([item["id"] for item in feedback], ["CONS-RETRY"])

    def test_revalidate_design_recovers_failed_phase3_component_routing(self) -> None:
        created = self._create_phase1_run()
        designed = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        state = workflow.store.read_state(str(created["run_id"]))
        design_path = self.repo_root / str(designed["latest_artifacts"]["design_artifact"])
        design_path.write_text(
            design_path.read_text(encoding="utf-8").replace(
                "class CulturalHeritage <<contracts>>",
                "class CulturalHeritage",
            ),
            encoding="utf-8",
        )
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(
                    "implementation_target_unclassified",
                    "CulturalHeritage has no component.",
                    run_id=str(created["run_id"]),
                ).to_response(),
            }
        )
        workflow.store.write_state(state)

        response = workflow.revalidate_design(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["designer_contract_version"], "vertical_design_graph_v2")
        self.assertIn("revalidate_design", response["allowed_next_actions"])

    def test_revalidate_design_supersedes_pending_phase3_exchange(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "implementation_plan_step_review_required",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
            }
        )
        workflow.store.write_state(state)
        execution_root = workflow.store.run_dir(run_id) / "implementation_plan_execution"
        approved_call = {
            "call_id": "implementation-plan-0001-a01",
            "ordinal": 1,
            "attempt": 1,
            "status": "approved",
            "raw_completion": "{}",
        }
        pending_call = {
            "call_id": "implementation-plan-0002-a01",
            "ordinal": 2,
            "attempt": 1,
            "status": "pending",
            "raw_completion": "{}",
        }
        execution_root.mkdir(parents=True, exist_ok=True)
        (execution_root / "step_review_session_v1.json").write_text(
            json.dumps({"calls": [approved_call, pending_call]}, ensure_ascii=False), encoding="utf-8"
        )
        (execution_root / "exchange_implementation-plan-0001-a01.json").write_text(
            json.dumps(approved_call, ensure_ascii=False), encoding="utf-8"
        )
        (execution_root / "exchange_implementation-plan-0002-a01.json").write_text(
            json.dumps(pending_call, ensure_ascii=False), encoding="utf-8"
        )

        response = workflow.revalidate_design(run_id=run_id)

        self.assertEqual(response["status"], "waiting_design_approval")
        session = json.loads(
            (execution_root / "step_review_session_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual([item["status"] for item in session["calls"]], ["superseded", "superseded"])
        self.assertTrue(
            all(item["superseded_reason"] == "upstream_design_revalidation" for item in session["calls"])
        )

    def test_revalidate_design_supersedes_unapproved_plan_checkpoint(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state["latest_artifacts"].update(
            {
                "implementation_plan": "artifacts/implementation_plan_v1.md",
                "implementation_plan_structured": "artifacts/implementation_plan_structured_v1.json",
                "implementation_plan_review": "artifacts/implementation_plan_review_v1.json",
            }
        )
        state["quality_reports"]["implementation_plan_review"] = {"ok": True}
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "waiting_implementation_plan_review",
                "pending_checkpoint": {
                    "checkpoint_id": f"checkpoint_implementation_plan_{run_id}",
                    "type": "implementation_plan_review",
                },
            }
        )
        workflow.store.write_state(state)
        execution_root = workflow.store.run_dir(run_id) / "implementation_plan_execution"
        call = {
            "call_id": "implementation-plan-0001-a01",
            "ordinal": 1,
            "attempt": 1,
            "status": "approved",
            "raw_completion": "{}",
        }
        execution_root.mkdir(parents=True, exist_ok=True)
        (execution_root / "step_review_session_v1.json").write_text(
            json.dumps({"calls": [call]}, ensure_ascii=False), encoding="utf-8"
        )
        (execution_root / "exchange_implementation-plan-0001-a01.json").write_text(
            json.dumps(call, ensure_ascii=False), encoding="utf-8"
        )

        response = workflow.revalidate_design(run_id=run_id)

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["pending_checkpoint"]["type"], "design_approval")
        self.assertNotIn("implementation_plan", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_structured", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_review", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_review", response["quality_reports"])
        session = json.loads(
            (execution_root / "step_review_session_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(session["calls"][0]["status"], "superseded")
        self.assertEqual(
            session["calls"][0]["superseded_reason"],
            "upstream_design_revalidation",
        )


    def test_cli_revalidate_design_returns_json_contract(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        stdout = io.StringIO()

        with patch.dict(os.environ, self._target_overrides()), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "revalidate-design",
                    "--run-id",
                    str(created["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--sync-mode",
                    "mock",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "waiting_design_approval")
        self.assertIn("revalidate_design", payload["allowed_next_actions"])


    def test_approve_checkpoint_rejects_missing_pending_checkpoint(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        state["pending_checkpoint"] = None
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "no_pending_checkpoint")

    def test_approve_checkpoint_rejects_non_requirement_status(self) -> None:
        created = self._create_phase1_run()
        phase2 = PhaseTwoWorkflow(self.settings)
        phase2.approve_checkpoint(run_id=str(created["run_id"]))

        with self.assertRaises(HarnessError) as ctx:
            phase2.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_approve_checkpoint_rejects_missing_requirement_artifact(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("requirement_packet")
        state["latest_artifacts"] = latest
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_requirement_not_found")

    def test_cli_approve_checkpoint_returns_json(self) -> None:
        created = self._create_phase1_run()
        stdout = io.StringIO()
        with patch.dict(os.environ, self._target_overrides()), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "approve-checkpoint",
                    "--run-id",
                    str(created["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "waiting_design_approval")
        self.assertEqual(payload["last_sync"]["phase"], "design_checkpoint")


class RepairSessionOrdinalTests(unittest.TestCase):
    def _session(self, run_dir: Path, ordinal: int, *, status: str, feedback: list[dict[str, object]]) -> None:
        session_dir = run_dir / "designer_execution" / f"design_v{ordinal}"
        session_dir.mkdir(parents=True)
        (session_dir / "designer_loop_session_v1.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        (session_dir / "designer_loop_manifest_v1.json").write_text(
            json.dumps({"authority": {"repair_feedback": feedback}}), encoding="utf-8"
        )

    def test_next_repair_never_reuses_a_completed_session_built_for_other_feedback(self) -> None:
        from sdlc_harness.workflows.phase2 import _unused_repair_session_ordinal

        old = [{"id": "runtime-sc-3:SEM-001", "subject_ref": "VDN-a"}]
        new = [{"id": "runtime-sc-3:SEM-001", "subject_ref": "VDE-b"}]
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            self.assertEqual(_unused_repair_session_ordinal(run_dir, ordinal=18, repair_feedback=new), 18)
            self._session(run_dir, 18, status="completed", feedback=old)
            self.assertEqual(_unused_repair_session_ordinal(run_dir, ordinal=18, repair_feedback=new), 19)
            # Re-entering the same completed repair (same feedback) keeps its session.
            self.assertEqual(_unused_repair_session_ordinal(run_dir, ordinal=18, repair_feedback=old), 18)
            # A paused or blocked session is resumable; drift checks stay with the Designer manifest.
            self._session(run_dir, 19, status="paused", feedback=old)
            self.assertEqual(_unused_repair_session_ordinal(run_dir, ordinal=19, repair_feedback=new), 19)


if __name__ == "__main__":
    unittest.main()
