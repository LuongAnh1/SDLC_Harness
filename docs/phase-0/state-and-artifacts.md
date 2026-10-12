# State Machine Và Artifact Phase 0

Tài liệu này chốt state machine ban đầu và các artifact tối thiểu của SDLC Harness.

## State Machine Ban Đầu

```text
created
  -> requirement_drafting
  -> waiting_requirement_approval
  -> design_drafting
  -> waiting_design_approval
  -> coding
  -> review_ready
  -> done
```

Các state phụ:

```text
blocked
cancelled
```

## Ý Nghĩa State

| State | Ý nghĩa | Ai thường kích hoạt bước tiếp theo |
| --- | --- | --- |
| `created` | Run đã được tạo, chưa xử lý phase nào. | Harness |
| `requirement_drafting` | Đang tạo requirement packet từ input. | Harness |
| `waiting_requirement_approval` | Requirement packet đã sẵn sàng, chờ người dùng duyệt/sửa. | Niko + human |
| `design_drafting` | Đang tạo design artifact từ requirement đã duyệt. | Harness |
| `waiting_design_approval` | Design đã sẵn sàng, chờ người dùng duyệt/sửa. | Niko + human |
| `coding` | Đang tạo implementation artifact / code report. | Harness |
| `review_ready` | Có output code/test/trace để review. | Niko + human |
| `done` | Run đã hoàn thành. | Harness hoặc human |
| `blocked` | Thiếu thông tin hoặc lỗi cần can thiệp. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

## Artifact Tối Thiểu

| Artifact | Tạo ở phase | Mục đích |
| --- | --- | --- |
| `use_case_packet` | intake/load input | Chuẩn hóa input gốc, giữ source/evidence. |
| `requirement_packet` | requirement_drafting | Yêu cầu đã được làm rõ, acceptance criteria, open questions. |
| `design_artifact` | design_drafting | Thiết kế, diagram text/mermaid, component, decision. |
| `trace_graph` | sau mỗi phase chính | Nối requirement -> design -> code/test. |
| `implementation_artifact` | coding | Code plan, generated code summary hoặc diff summary. |
| `test_report` | coding/review | Compile/test/validation result. |
| `tool_sync_report` | sau mỗi sync | Ghi artifact nào đã đẩy lên tool/mock tool nào. |

## Versioning

Artifact luôn có version:

```text
requirement_packet_v1
requirement_packet_v2
design_artifact_v1
trace_graph_v1
trace_graph_v2
```

Quy tắc:

- Sửa requirement tạo requirement version mới và làm stale design/code/test phía sau.
- Sửa design tạo design version mới và làm stale code/test phía sau.
- Sửa code review giữ requirement/design, tạo implementation/test version mới.

## Checkpoint Ban Đầu

Phase 1 cần checkpoint đầu tiên:

```text
waiting_requirement_approval
```

Checkpoint này cần lưu:

```text
checkpoint_id
run_id
artifact_ref
summary_for_user
allowed_actions
created_at
status: pending | approved | changed | cancelled
```

Phase 2 thêm checkpoint:

```text
waiting_design_approval
```

## Trace Graph Tối Thiểu

Trace graph Phase 1 có thể rất đơn giản:

```text
source_use_case -> requirement_packet_v1
```

Phase 2 mở rộng:

```text
source_use_case -> requirement_packet_v1 -> design_artifact_v1
```

Phase 3 mở rộng:

```text
source_use_case -> requirement_packet_v1 -> design_artifact_v1 -> implementation_artifact_v1 -> test_report_v1
```

Không cần KG phức tạp ở Phase 0-1. Chỉ cần trace đủ để giải thích artifact này dựa trên nguồn nào.
