"""Application service dùng chung cho HTTP API và CLI operator."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .commands import ApplicationCommand
from ..core.config import Settings, load_settings, repo_root as default_repo_root
from ..core.errors import HarnessError
from ..events import current_event_context, event_scope
from ..profiles import (
    public_allowed_actions,
    public_run_summary,
    read_profile_snapshot,
    require_project_profile_for_start,
    require_run_command_allowed,
)
from ..sources.fixtures import source_ref_for
from ..sources.jira import jira_source_ref_for
from ..storage.runtime import RuntimeStore
from ..workspaces.manager import RunWorkspaceManager
from ..workflows.approval import CheckpointApprovalWorkflow
from ..workflows.phase1 import PhaseOneWorkflow
from ..workflows.phase2 import PhaseTwoWorkflow
from ..workflows.phase3 import PhaseThreeWorkflow
from ..workflows.phase4 import PhaseFourWorkflow
from ..workflows.target_repo import TargetRepoPreparationWorkflow
from ..workflows.workspace_adoption import WorkspaceAdoptionWorkflow
from ..workflows.workspace_cleanup import WorkspaceCleanupWorkflow


class ApplicationService:
    """Dựng settings và dispatch command tới workflow sở hữu nghiệp vụ."""

    def __init__(self, *, root: Path | None = None):
        self.root = (root or default_repo_root()).resolve()

    def settings_for(self, command: ApplicationCommand) -> Settings:
        """Dựng Settings theo precedence hiện hành với override của transport."""

        return load_settings(
            root=self.root,
            runtime_root=command.runtime_root,
            overrides=dict(command.settings_overrides),
        )

    def execute(self, command: ApplicationCommand) -> dict[str, object]:
        """Chạy một command và trả nguyên workflow payload.

        Transport chịu trách nhiệm chuyển HarnessError/exception thành CLI hoặc
        HTTP response. Service không in stdout và không format câu trả lời Niko.
        """

        settings = self.settings_for(command)
        if current_event_context() is None:
            run_id = str(command.arguments.get("run_id") or "").strip() or None
            with event_scope(
                runtime_root=settings.runtime_root,
                command=command.name,
                run_id=run_id,
            ):
                return self._execute_with_locks(command, settings=settings)
        return self._execute_with_locks(command, settings=settings)

    def _execute_with_locks(
        self,
        command: ApplicationCommand,
        *,
        settings: Settings,
    ) -> dict[str, object]:
        name = command.name
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        if name in {"start-or-get-run", "start-from-jira"}:
            require_project_profile_for_start(project_profile_id=settings.project_profile_id)
            source_ref = self._source_ref(command)
            with store.source_lock(source_ref):
                active_state = store.find_active_run(source_ref)
                if active_state is not None:
                    active_run_id = str(active_state.get("run_id") or "")
                    require_run_command_allowed(
                        active_state,
                        command_name=name,
                        run_id=active_run_id,
                    )
                return self._execute_unlocked(command, settings=settings)
        if name in {
            "sync-run",
            "continue-run",
            "revalidate-implementation",
            "revalidate-implementation-plan",
            "revalidate-design",
            "revalidate-requirement",
            "clarify-more",
            "challenge-assumption",
            "approve-checkpoint",
            "answer-clarification",
            "adopt-run-workspace",
            "cleanup-run-workspace",
        }:
            run_id = self._required_text(command, "run_id")
            with store.run_lock(run_id):
                state = store.read_state(run_id)
                require_run_command_allowed(state, command_name=name, run_id=run_id)
                run_settings = self._settings_for_run(
                    settings=settings,
                    state=state,
                    run_id=run_id,
                    command_name=name,
                )
                response = self._execute_unlocked(command, settings=run_settings)
                return self._project_run_response(state=state, response=response)
        if name == "get-status":
            run_id = self._required_text(command, "run_id")
            state = store.read_state(run_id)
            response = self._execute_unlocked(command, settings=settings)
            return self._project_run_response(state=state, response=response)
        if name == "experiment-design-consensus":
            source_run_id = self._required_text(command, "source_run_id")
            state = store.read_state(source_run_id)
            require_run_command_allowed(
                state,
                command_name=name,
                run_id=source_run_id,
            )
        return self._execute_unlocked(command, settings=settings)

    @staticmethod
    def _project_run_response(
        *,
        state: dict[str, object],
        response: dict[str, object],
    ) -> dict[str, object]:
        """Áp retirement policy lên payload public mà không sửa state đã lưu."""

        projected = dict(response)
        if "allowed_next_actions" in projected:
            projected["allowed_next_actions"] = public_allowed_actions(
                state,
                projected["allowed_next_actions"],
            )
        if "summary_for_user" in projected:
            projected["summary_for_user"] = public_run_summary(
                state,
                projected["summary_for_user"],
            )
        return projected

    @staticmethod
    def _settings_for_run(
        *,
        settings: Settings,
        state: dict[str, object],
        run_id: str,
        command_name: str,
    ) -> Settings:
        """Gắn execution identity theo snapshot đã lưu, không theo config hiện tại."""

        snapshot = read_profile_snapshot(state, run_id=run_id)
        if snapshot is None:
            if command_name == "cleanup-run-workspace" or (
                command_name == "sync-run"
                and str(state.get("status") or "").startswith("code_review")
            ):
                return ApplicationService._settings_for_legacy_workspace(
                    settings=settings,
                    run_id=run_id,
                )
            return settings
        return replace(
            settings,
            project_profile_id=snapshot.project_profile_id,
            target_repo_remote=snapshot.target_remote,
            target_base_branch=snapshot.target_base_branch,
        )

    @staticmethod
    def _settings_for_legacy_workspace(*, settings: Settings, run_id: str) -> Settings:
        """Dùng identity đã persist để sync/cleanup workspace legacy an toàn."""

        record = RunWorkspaceManager(settings).read(run_id)
        canonical_repo = Path(record.canonical_repo).resolve()
        worktree_path = Path(record.worktree_path).resolve()
        base_prefix = "origin/"
        if canonical_repo == settings.repo_root.resolve():
            raise HarnessError(
                "target_repo_is_harness",
                "Legacy workspace canonical repository points to SDLC_Harness itself.",
                run_id=run_id,
            )
        if worktree_path.name != run_id or worktree_path.parent == worktree_path:
            raise HarnessError(
                "workspace_path_invalid",
                "Legacy workspace path does not match its owning run_id.",
                run_id=run_id,
            )
        if not record.base_ref.startswith(base_prefix) or not record.base_ref[len(base_prefix) :]:
            raise HarnessError(
                "workspace_branch_mismatch",
                "Legacy workspace base_ref must be an origin branch.",
                run_id=run_id,
            )
        return replace(
            settings,
            project_profile_id=None,
            target_repo_path=canonical_repo,
            target_worktree_root=worktree_path.parent,
            target_repo_remote=record.remote,
            target_base_branch=record.base_ref[len(base_prefix) :],
        )

    def _execute_unlocked(
        self,
        command: ApplicationCommand,
        *,
        settings: Settings,
    ) -> dict[str, object]:
        """Dispatch after the shared mutation lock, when one is required."""

        name = command.name

        if name == "start-or-get-run":
            return PhaseOneWorkflow(settings).start_or_get_run(
                dataset=self._required_text(command, "dataset"),
                use_case=self._required_text(command, "use_case"),
                requested_by=self._required_text(command, "requested_by"),
            )
        if name == "start-from-jira":
            return PhaseOneWorkflow(settings).start_from_jira(
                issue_key=self._required_text(command, "issue_key"),
                requested_by=self._required_text(command, "requested_by"),
            )
        if name == "get-status":
            return PhaseOneWorkflow(settings).get_status(
                run_id=self._required_text(command, "run_id")
            )
        if name == "sync-run":
            run_id = self._required_text(command, "run_id")
            phase_one = PhaseOneWorkflow(settings)
            state = phase_one.store.read_state(run_id)
            if str(state.get("status") or "").startswith("code_review"):
                return PhaseFourWorkflow(settings).sync_code_review(run_id=run_id)
            return phase_one.sync_run(run_id=run_id)
        if name == "continue-run":
            run_id = self._required_text(command, "run_id")
            phase_one = PhaseOneWorkflow(settings)
            state = phase_one.store.read_state(run_id)
            if str(state.get("status") or "") in {
                "requirement_drafting",
                "requirement_paused",
            }:
                return phase_one.continue_requirement_run(run_id=run_id)
            if str(state.get("status") or "") == "repo_discovery_paused":
                return PhaseTwoWorkflow(settings).continue_repo_discovery(run_id=run_id)
            if str(state.get("status") or "") == "design_paused":
                return PhaseTwoWorkflow(settings).continue_design(run_id=run_id)
            return PhaseFourWorkflow(settings).continue_until_implementation_checkpoint(
                run_id=run_id
            )
        if name == "revalidate-implementation":
            return PhaseFourWorkflow(settings).revalidate_implementation(
                run_id=self._required_text(command, "run_id")
            )
        if name == "revalidate-implementation-plan":
            return PhaseThreeWorkflow(settings).revalidate_implementation_plan(
                run_id=self._required_text(command, "run_id")
            )
        if name == "revalidate-design":
            return PhaseTwoWorkflow(settings).revalidate_design(
                run_id=self._required_text(command, "run_id")
            )
        if name == "revalidate-requirement":
            return PhaseOneWorkflow(settings).revalidate_requirement(
                run_id=self._required_text(command, "run_id")
            )
        if name == "clarify-more":
            return PhaseOneWorkflow(settings).clarify_more(
                run_id=self._required_text(command, "run_id")
            )
        if name == "challenge-assumption":
            return PhaseOneWorkflow(settings).challenge_assumption(
                run_id=self._required_text(command, "run_id"),
                assumption_id=self._required_text(command, "assumption_id"),
                instruction=self._required_text(command, "instruction"),
                challenged_by=self._required_text(command, "challenged_by"),
            )
        if name == "experiment-design-consensus":
            return PhaseTwoWorkflow(settings).experiment_design_consensus(
                source_run_id=self._required_text(command, "source_run_id"),
                experiment_root=Path(self._required_text(command, "experiment_root")),
                llm_call_budget=int(self._required_text(command, "llm_call_budget")),
            )
        if name == "approve-checkpoint":
            return CheckpointApprovalWorkflow(settings).approve_checkpoint(
                run_id=self._required_text(command, "run_id"),
                checkpoint_id=self._optional_text(command, "checkpoint_id"),
            )
        if name == "answer-clarification":
            return PhaseOneWorkflow(settings).answer_clarification(
                run_id=self._required_text(command, "run_id"),
                question_id=self._required_text(command, "question_id"),
                answer=self._required_text(command, "answer"),
                answered_by=self._required_text(command, "answered_by"),
            )
        if name == "prepare-target-repo":
            return TargetRepoPreparationWorkflow(settings).prepare(
                init_codegraph=bool(command.arguments.get("init_codegraph", False))
            )
        if name == "adopt-run-workspace":
            return WorkspaceAdoptionWorkflow(settings).adopt(
                run_id=self._required_text(command, "run_id")
            )
        if name == "cleanup-run-workspace":
            return WorkspaceCleanupWorkflow(settings).cleanup(
                run_id=self._required_text(command, "run_id")
            )
        raise HarnessError("unknown_command", f"Unknown command: {name}")

    def _source_ref(self, command: ApplicationCommand) -> str:
        if command.name == "start-from-jira":
            return jira_source_ref_for(self._required_text(command, "issue_key"))
        return source_ref_for(
            self._required_text(command, "dataset"),
            self._required_text(command, "use_case"),
        )

    @staticmethod
    def _required_text(command: ApplicationCommand, key: str) -> str:
        value = command.arguments.get(key)
        if value is None or not str(value).strip():
            raise HarnessError(
                "invalid_command_argument",
                f"Command {command.name} requires argument: {key}",
            )
        return str(value)

    @staticmethod
    def _optional_text(command: ApplicationCommand, key: str) -> str | None:
        value = command.arguments.get(key)
        if value is None:
            return None
        return str(value)
