# Phase 4 State Và Artifact

Tài liệu này chốt state, artifact và checkpoint tối thiểu cho Phase 4. Phase 4
bắt đầu khi implementation plan đã được duyệt.

## State Machine

```text
waiting_implementation_plan_review
  -> implementation_ready
  -> implementation_writing
  -> implementation_written
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
| `implementation_ready` | Implementation plan đã được duyệt và `developer_work_packet_v1.json` đã sẵn sàng; chưa sửa code. | Harness slice tiếp theo |
| `implementation_writing` | Đang thao tác target repo, tạo branch và code change. | Harness |
| `implementation_written` | Code change đã được apply vào target repo; chưa test/commit/PR. | Harness slice tiếp theo |
| `implementation_testing` | Đang chạy Verification Runner, code traceability check, hoặc vòng repair Developer nếu compile/unit/trace fail. | Harness |
| `code_review_ready` | Draft PR và report đã sẵn sàng để human review. | Niko + human |
| `blocked` | Thiếu plan/design, trace fail, GitHub/test lỗi cần can thiệp. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

## Artifact Tối Thiểu

| Artifact | Mục đích |
| --- | --- |
| `developer_work_packet_v1.json` | Gói đầu vào máy đọc được cho Developer.act, lấy từ implementation plan đã duyệt. |
| `code_change_report_v1.md` | Tóm tắt branch, file đổi, lý do đổi; commit xuất hiện ở slice sau khi test/trace pass. |
| `test_report_v1.md` | Kết quả Verification Runner cho compile/unit test command của target repo, ghi rõ pass/fail/skipped. |
| `requirement_design_code_trace_v1.json` | Trace requirement -> design -> file/class/function/test. |
| `qa_test_report_v1.md` | Optional/future artifact cho QA/Tester Agent: acceptance/system-level test từ use case, không thay thế unit test report. |
| `tool_sync_report_v4.json` | Kết quả sync GitHub/Jira/Confluence của Phase 4. |

Artifact dùng version như các phase trước. Nếu repair/rerun code xảy ra trong
giới hạn Phase 4, tạo `v2`, `v3`; không ghi đè file cũ.

## State JSON Sau Khi Pass

Sau Slice 4.1:

```text
current_phase=implementation_preparing
status=implementation_ready
latest_artifacts.developer_work_packet
pending_checkpoint=null
allowed_next_actions=[get_status, cancel_run]
summary_for_user
```

Sau Slice 4.2:

```text
current_phase=implementation_writing
status=implementation_written
latest_artifacts.code_change_report
pending_checkpoint=null
allowed_next_actions=[continue_run, get_status, cancel_run]
summary_for_user
```

Sau Slice 4.3 nếu compile/unit test pass:

```text
current_phase=implementation_testing
status=implementation_tested
latest_artifacts.code_change_report
latest_artifacts.test_report
pending_checkpoint=null
allowed_next_actions=[get_status, cancel_run]
summary_for_user
```

Sau Slice 4.3 nếu compile/unit test fail hoặc skipped:

```text
current_phase=implementation_testing
status=blocked
latest_artifacts.test_report
last_error.error_code=test_failed | test_skipped
allowed_next_actions=[get_status, cancel_run]
summary_for_user
```

Sau Phase 4 đầy đủ:

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
- compile/unit test fail và không còn repair path an toàn về Developer;
- Validator không nối được approved requirement/design với code/unit test;
- Validator phát hiện approved design mâu thuẫn/không triển khai được; trường hợp này phải block hoặc reopen design checkpoint có human approval, không tự sửa design âm thầm trong Phase 4;
- QA/Tester Agent phát hiện acceptance/system behavior fail quá giới hạn repair;
- GitHub sync fail toàn bộ.

`last_error` phải đủ rõ để Niko nói lại cho anh biết đang kẹt ở đâu.

