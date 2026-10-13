# Phase 3 State Và Artifact

Tài liệu này chốt state, artifact và checkpoint tối thiểu cho Phase 3. Mục tiêu
không phải mô tả toàn bộ implementation chi tiết, mà chốt contract để Niko và
tool sync đọc được tiến độ sau khi design đã duyệt.

## State Machine

Phase 3 bắt đầu từ:

```text
current_phase=design_review
status=waiting_design_approval
pending_checkpoint.type=design_approval
```

Khi người dùng duyệt design checkpoint:

```text
waiting_design_approval
  -> implementation_planning
  -> waiting_implementation_plan_review
```

State phụ:

```text
blocked
cancelled
```

## Ý Nghĩa State

| State | Ý nghĩa | Ai kích hoạt bước tiếp theo |
| --- | --- | --- |
| `implementation_planning` | Đang lập kế hoạch sửa/sinh code từ design đã duyệt. | Harness |
| `waiting_implementation_plan_review` | Implementation plan đã sinh, chờ human/Niko duyệt trước khi sửa target repo. | Niko + human |
| `blocked` | Thiếu input, plan thiếu trace, plan bịa scope hoặc target repo chưa an toàn. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

State `implementation_writing`, `implementation_testing` và `code_review_ready`
thuộc Phase 4.

## Artifact Tối Thiểu

| Artifact | Mục đích |
| --- | --- |
| `implementation_plan_v1.md` | Kế hoạch implement dựa trên requirement/design đã duyệt. |
| `implementation_plan_review_v1.json` | Kết quả Validator review plan: coverage, traceability, executability, scope. |
| `tool_sync_report_v3.json` | Kết quả sync Confluence/Jira của Phase 3. |

Artifact dùng version như các phase trước. Nếu Developer.plan rerun theo feedback
của Validator, tạo `v2`, `v3`; không ghi đè file cũ.

`implementation_plan_v1` không thay đổi requirement/design graph. Nó chỉ tham
chiếu graph đã duyệt và ánh xạ sang kế hoạch code dự kiến.

## State JSON Sau Khi Pass

Sau khi Phase 3 pass:

```text
current_phase=implementation_planning
status=waiting_implementation_plan_review
latest_artifacts.implementation_plan
quality_reports.implementation_plan_review
pending_checkpoint.type=implementation_plan_review
pending_checkpoint.artifact_ref=implementation_plan_v1
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
last_sync.phase=implementation_plan_review
summary_for_user
```

Checkpoint `implementation_plan_review` nghĩa là human cần review kế hoạch trước
khi Phase 4 được phép sửa target repo.

## Last Sync Phase 3

`tool_sync_report_v3.json` phải có tối thiểu:

```text
version=tool_sync_report_v3
phase=implementation_plan_review
status=synced | partial_failed | failed
mode=mock | real
targets=[
  jira,
  confluence
]
```

Target Confluence lưu implementation plan đầy đủ. Target Jira ghi comment ngắn
kèm link Confluence và `run_id`. GitHub chưa có branch/commit/PR ở Phase 3.

## Blocked State

Phase 3 phải `blocked` thay vì giả vờ pass trong các trường hợp:

- thiếu approved design artifact;
- không xác định được target repo;
- plan bỏ sót design element quan trọng;
- plan không có mapping từ design sang planned code target;
- plan bịa thêm scope ngoài requirement/design;
- plan không có test intent hoặc target path đủ rõ;
- Jira/Confluence sync fail toàn bộ.

`last_error` phải đủ rõ để Niko nói lại cho anh biết đang kẹt ở đâu.

