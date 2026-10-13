"""Dispatcher duyệt checkpoint theo state hiện tại của run."""

from __future__ import annotations

from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore
from .phase2 import PhaseTwoWorkflow
from .phase3 import PhaseThreeWorkflow
from .phase4 import PhaseFourWorkflow


class CheckpointApprovalWorkflow:
    """Chọn workflow đúng khi CLI/Niko gọi `approve-checkpoint`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        if status == "waiting_requirement_approval":
            return PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=run_id, checkpoint_id=checkpoint_id)
        if status == "waiting_design_approval":
            return PhaseThreeWorkflow(self.settings).approve_design_checkpoint(
                run_id=run_id,
                checkpoint_id=checkpoint_id,
            )
        if status == "waiting_implementation_plan_review":
            return PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(
                run_id=run_id,
                checkpoint_id=checkpoint_id,
            )
        raise HarnessError(
            "checkpoint_not_current",
            f"Run does not have an approvable checkpoint: {run_id}",
            run_id=run_id,
        )
