"""Điều phối vòng đời SDLC, checkpoint và chuyển trạng thái của run.

Các workflow Phase 1-4 kết nối source, agent, validator, workspace, artifact,
verification và sync theo thứ tự nghiệp vụ. Đây là nơi sở hữu flow; transport
và agent riêng lẻ không được tự chuyển phase hoặc bỏ qua checkpoint.
"""

from .phase1 import PhaseOneWorkflow
from .phase2 import PhaseTwoWorkflow
from .phase3 import PhaseThreeWorkflow
from .approval import CheckpointApprovalWorkflow

__all__ = ["CheckpointApprovalWorkflow", "PhaseOneWorkflow", "PhaseTwoWorkflow", "PhaseThreeWorkflow"]
