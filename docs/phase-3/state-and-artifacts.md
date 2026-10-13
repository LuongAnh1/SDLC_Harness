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
  -> implementation_writing
  -> implementation_testing
  -> code_review_ready
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
| `implementation_writing` | Đang thao tác target repo, tạo branch và code change. | Harness |
| `implementation_testing` | Đang chạy compile/test/trace check. | Harness |
| `code_review_ready` | Draft PR và report đã sẵn sàng để human review. | Niko + human |
| `blocked` | Thiếu input, trace fail, GitHub/test lỗi cần can thiệp. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

`review_ready` trong tài liệu Phase 0 được cụ thể hóa ở Phase 3 thành
`code_review_ready` để tránh nhập nhằng với design review.

## Artifact Tối Thiểu

| Artifact | Mục đích |
| --- | --- |
| `implementation_plan_v1.md` | Kế hoạch implement dựa trên requirement/design đã duyệt. |
| `code_change_report_v1.md` | Tóm tắt branch, commit, file đổi và lý do đổi. |
| `test_report_v1.md` | Compile/test/validation result, ghi rõ pass/fail/skipped. |
| `requirement_design_code_trace_v1.json` | Trace requirement -> design -> file/class/function/test. |
| `tool_sync_report_v3.json` | Kết quả sync GitHub/Jira/Confluence của Phase 3. |

Artifact dùng version như các phase trước. Nếu repair/rerun code xảy ra trong
giới hạn Phase 3, tạo `v2`, `v3`; không ghi đè file cũ.

Slice 1 hiện mới sinh `implementation_plan_v1.md` và ghi checkpoint
`waiting_implementation_plan_review`. Các artifact code/test/trace/sync V3 là
đích khóa Phase 3 đầy đủ ở các slice tiếp theo.

## State JSON Sau Khi Pass

Sau khi Phase 3 tạo được Draft PR và report:

```text
current_phase=code_review
status=code_review_ready
latest_artifacts.implementation_plan
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
quality_reports.implementation_traceability
pending_checkpoint.type=code_review
pending_checkpoint.artifact_ref=code_change_report_v1
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
last_sync.phase=code_review
summary_for_user
```

Checkpoint `code_review` nghĩa là human cần review PR/code/test report. Nó không
có nghĩa PR đã được merge.

## Last Sync Phase 3

`tool_sync_report_v3.json` phải có tối thiểu:

```text
version=tool_sync_report_v3
phase=code_review
status=synced | partial_failed | failed
mode=mock | real
targets=[
  github,
  jira,
  confluence
]
```

Target GitHub cần ghi branch, commit và PR URL nếu tạo được. Target Jira và
Confluence ghi comment/page đã được cập nhật để trỏ về PR.

## Blocked State

Phase 3 phải `blocked` thay vì giả vờ pass trong các trường hợp:

- thiếu approved design artifact;
- không xác định được target repo;
- không tạo được branch hoặc không ghi được code;
- test/compile fail và không có repair path an toàn;
- Validator không nối được requirement/design với code/test;
- GitHub sync fail toàn bộ.

`last_error` phải đủ rõ để Niko nói lại cho anh biết đang kẹt ở đâu.

