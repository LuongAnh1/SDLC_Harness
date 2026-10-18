"""Application service dùng chung cho CLI và HTTP API tương lai."""

from __future__ import annotations

from pathlib import Path

from .commands import ApplicationCommand
from ..core.config import Settings, load_settings, repo_root as default_repo_root
from ..core.errors import HarnessError
from ..sources.fixtures import source_ref_for
from ..sources.jira import jira_source_ref_for
from ..storage.runtime import RuntimeStore
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
        name = command.name
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        if name in {"start-or-get-run", "start-from-jira"}:
            source_ref = self._source_ref(command)
            with store.source_lock(source_ref):
                return self._execute_unlocked(command, settings=settings)
        if name in {
            "sync-run",
            "continue-run",
            "revalidate-implementation",
            "revalidate-implementation-plan",
            "revalidate-design",
            "approve-checkpoint",
            "answer-clarification",
            "adopt-run-workspace",
            "cleanup-run-workspace",
        }:
            run_id = self._required_text(command, "run_id")
            with store.run_lock(run_id):
                return self._execute_unlocked(command, settings=settings)
        return self._execute_unlocked(command, settings=settings)

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
            return PhaseFourWorkflow(settings).continue_until_implementation_checkpoint(
                run_id=self._required_text(command, "run_id")
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
