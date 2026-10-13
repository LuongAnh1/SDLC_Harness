# Phase 4 State Và Artifact

Tài liệu này chốt state, artifact và checkpoint tối thiểu cho Phase 4. Phase 4
bắt đầu khi implementation plan đã được duyệt.

## State Machine

```text
waiting_implementation_plan_review
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
| `implementation_writing` | Đang thao tác target repo, tạo branch và code change. | Harness |
| `implementation_testing` | Đang chạy compile/test/trace check. | Harness |
| `code_review_ready` | Draft PR và report đã sẵn sàng để human review. | Niko + human |
| `blocked` | Thiếu plan/design, trace fail, GitHub/test lỗi cần can thiệp. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

## Artifact Tối Thiểu

| Artifact | Mục đích |
| --- | --- |
| `code_change_report_v1.md` | Tóm tắt branch, commit, file đổi và lý do đổi. |
| `test_report_v1.md` | Compile/test/validation result, ghi rõ pass/fail/skipped. |
| `requirement_design_code_trace_v1.json` | Trace requirement -> design -> file/class/function/test. |
| `tool_sync_report_v4.json` | Kết quả sync GitHub/Jira/Confluence của Phase 4. |

Artifact dùng version như các phase trước. Nếu repair/rerun code xảy ra trong
giới hạn Phase 4, tạo `v2`, `v3`; không ghi đè file cũ.

## State JSON Sau Khi Pass

```text
current_phase=code_review
status=code_review_ready
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

## Last Sync Phase 4

`tool_sync_report_v4.json` phải có tối thiểu:

```text
version=tool_sync_report_v4
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

Phase 4 phải `blocked` thay vì giả vờ pass trong các trường hợp:

- thiếu approved implementation plan;
- không tạo được branch hoặc không ghi được code;
- test/compile fail và không có repair path an toàn;
- Validator không nối được requirement/design với code/test;
- GitHub sync fail toàn bộ.

`last_error` phải đủ rõ để Niko nói lại cho anh biết đang kẹt ở đâu.

