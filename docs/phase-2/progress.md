# Phase 2 Progress

Tài liệu này ghi lại tiến độ triển khai Phase 2 theo từng lát nhỏ. Không đánh
dấu một mục là xong nếu mới chỉ có tài liệu mà chưa có code/test tương ứng.

## 2026-10-13 - V0 Approve Requirement Sang Design Checkpoint

Đã làm:

- Thêm CLI `approve-checkpoint --run-id ... [--checkpoint-id ...]`.
- Thêm workflow Phase 2 V0 để chỉ chạy khi run đang ở
  `waiting_requirement_approval`.
- Đọc `latest_artifacts.requirement_packet` làm approved requirement input.
- Ghi `approved_artifacts.requirement_packet` vào state.
- Sinh bộ artifact design V0 bằng fake/deterministic Designer/Validator:
  `designer_analysis_v1.md`, `design_artifact_v1.md`,
  `class_diagram_graph_v1.json`, `sequence_diagram_graph_v1.json`,
  `requirement_design_trace_v1.json`, `missing_design_links_v1.json`,
  `design_quality_report_v1.md`.
- Tạo mock sync Phase 2: `design-jira-comment.md`,
  `design-confluence-page.md`, `tool_sync_report_v2.json`.
- Chuyển state sang `current_phase=design_review`,
  `status=waiting_design_approval`, `pending_checkpoint.type=design_approval`.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test*.py" -v`: 34 test pass.
- Smoke CLI với runtime tạm:
  `start-or-get-run --llm-mode fake` rồi
  `approve-checkpoint --llm-mode fake` trả
  `approve_status=waiting_design_approval`, `sync_status=mock_synced`.

Chưa làm trong lát này:

- Chưa gọi LLM Designer thật.
- Chưa parse PlantUML thành graph thật.
- Chưa chạy Validator traceability thật theo TraceDev.
- Chưa có repair loop Designer.update_design.
- Chưa real sync Jira/Confluence cho design checkpoint.
- Chưa nối Niko route duyệt checkpoint sang CLI `approve-checkpoint`.
