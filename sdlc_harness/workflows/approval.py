"""Dispatcher duyệt checkpoint theo state hiện tại của run (bảng checkpoint ở workflows/graph.py)."""

from __future__ import annotations

from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore
from .graph import APPROVAL_REFUSALS, PHASE_FOUR, PHASE_THREE, PHASE_TWO, approval_for
from .phase2 import PhaseTwoWorkflow
from .phase3 import PhaseThreeWorkflow
from .phase4 import PhaseFourWorkflow



class CheckpointApprovalWorkflow:
    """Chọn workflow đúng khi application service nhận `approve_checkpoint`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        if status in APPROVAL_REFUSALS:
            error_code, message = APPROVAL_REFUSALS[status]
            raise HarnessError(error_code, message, run_id=run_id)
        approval = approval_for(status)
        if approval is None:
            raise HarnessError(
                "checkpoint_not_current",
                f"Run does not have an approvable checkpoint: {run_id}",
                run_id=run_id,
            )
        workflows = {PHASE_TWO: PhaseTwoWorkflow, PHASE_THREE: PhaseThreeWorkflow, PHASE_FOUR: PhaseFourWorkflow}
        workflow = workflows[approval.workflow](self.settings)
        return getattr(workflow, approval.method)(run_id=run_id, checkpoint_id=checkpoint_id)
