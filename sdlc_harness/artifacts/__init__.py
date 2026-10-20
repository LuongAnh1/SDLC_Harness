"""Đọc, ghi và render các artifact có contract của workflow.

Package quản lý định dạng lưu trữ cho requirement, source snapshot, work packet
và report Phase 4. Nó không quyết định khi nào artifact được tạo hoặc duyệt;
workflow sở hữu vòng đời, còn ``storage`` sở hữu state/runtime layout.
"""

from .requirement_v2 import (
    read_requirement_model_v2,
    read_source_snapshot_v2,
    write_requirement_v2_artifacts,
    write_requirement_v2_revision_artifacts,
    write_source_snapshot_v2,
)
from .requirement_input import (
    ApprovedRequirementInput,
    CanonicalRequirementInput,
    read_approved_requirement_input,
    read_frozen_requirement_input,
)
from .writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_llm_metadata,
    write_phase4_developer_work_packet,
    write_phase4_developer_raw_response,
    write_phase4_qa_repair_feedback,
    write_phase4_repair_feedback,
    write_phase4_test_report,
    write_use_case_packet,
)

__all__ = [
    "ApprovedRequirementInput",
    "CanonicalRequirementInput",
    "read_approved_requirement_input",
    "read_frozen_requirement_input",
    "read_requirement_model_v2",
    "read_source_snapshot_v2",
    "write_requirement_v2_artifacts",
    "write_requirement_v2_revision_artifacts",
    "write_source_snapshot_v2",
    "write_phase4_code_change_report",
    "write_phase4_code_trace_report",
    "write_phase4_developer_llm_metadata",
    "write_phase4_developer_work_packet",
    "write_phase4_developer_raw_response",
    "write_phase4_qa_repair_feedback",
    "write_phase4_repair_feedback",
    "write_phase4_test_report",
    "write_use_case_packet",
]
