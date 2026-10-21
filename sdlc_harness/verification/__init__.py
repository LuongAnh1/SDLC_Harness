"""Verification runner cho Phase 4.

Package này chỉ sở hữu việc chạy command compile/unit test của target repo và
chuẩn hóa kết quả thành report máy đọc được. Nó không sinh test bằng LLM, không
sửa source và không thay vai QA/Tester Agent ở tầng acceptance/system test.
"""

from .registry import (
    VerificationPreparationSpec,
    VerificationStageSpec,
    resolve_verification_preparation_spec,
    resolve_verification_preparation_specs,
    resolve_verification_stage_specs,
)
from .runner import VerificationResult, VerificationRunner, VerificationStageResult

__all__ = [
    "VerificationResult",
    "VerificationRunner",
    "VerificationStageResult",
    "VerificationPreparationSpec",
    "VerificationStageSpec",
    "resolve_verification_preparation_spec",
    "resolve_verification_preparation_specs",
    "resolve_verification_stage_specs",
]
