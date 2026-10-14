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
     (Verification Runner -> Validator code trace)
  -> implementation_repair_needed
     (Developer repair -> Verification Runner -> Validator; có thể lặp trong giới hạn)
  -> implementation_traced
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
| `implementation_testing` | Umbrella state hiện tại cho compile/unit test, Validator code trace, Developer repair loop khi compile/unit/trace fail, và QA/Tester acceptance/system test sau khi trace ổn. | Harness |
| `implementation_repair_needed` | Validator code trace fail vì code/test thiếu link có thể sửa; `implementation_repair_feedback_v{n}.json` đã sẵn sàng để `continue-run` gọi Developer.repair. | Harness |
| `implementation_traced` | Compile/unit test và Validator code trace hybrid rule + LLM đã pass ổn định sau repair loop nếu có; chưa chạy QA/Tester Agent, chưa commit/PR. | Harness slice tiếp theo |
| `code_review_ready` | Draft PR, test report, trace report, QA report và sync report đã sẵn sàng để human review. | Niko + human |
| `blocked` | Thiếu plan/design, trace fail, GitHub/test lỗi cần can thiệp. | Niko + human |
| `cancelled` | Run bị dừng có chủ đích. | Human/Niko |

## Artifact Tối Thiểu

| Artifact | Mục đích |
| --- | --- |
| `developer_work_packet_v1.json` | Gói đầu vào máy đọc được cho Developer.act, lấy từ implementation plan đã duyệt. |
| `developer_work_packet_v{n}.json` | Snapshot đã refresh trước vòng Developer.repair thứ `n-1`; giữ nguyên scope nhưng đọc lại code/test thật trong target repo. |
| `developer_repair_raw_response_v{n}.txt` | Raw response từ LLM Developer.repair nếu có, dùng để debug parse/logic khi repair khó hội tụ. |
| `code_change_report_v{n}.md` | Tóm tắt branch, file đổi, lý do đổi; commit xuất hiện ở slice sau khi test/trace/QA pass. |
| `test_report_v{n}.md` | Kết quả Verification Runner cho compile/unit test command của target repo, ghi rõ pass/fail/skipped. |
| `requirement_design_code_trace_v{n}.json` | Trace requirement -> design -> file/class/function/test, gồm static evidence và LLM graph review bắt buộc. |
| `implementation_repair_feedback_v{n}.json` | Feedback máy đọc được từ Validator cho Developer repair code/test; chỉ xuất hiện khi trace fail có thể sửa ở code/test. |
| `qa_test_report_v1.md` | Artifact bắt buộc trước `code_review_ready`: QA/Tester Agent kiểm acceptance/system-level behavior từ use case, không thay thế unit test report. |
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
allowed_next_actions=[continue_run, get_status, cancel_run]
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

Sau Slice 4.3 nếu compile/unit test pass. Đây mới là trạng thái code đã chạy test
baseline, chưa phải `code_review_ready` vì còn thiếu Validator code trace,
QA/Tester report, commit/Draft PR và sync Phase 4:

```text
current_phase=implementation_testing
status=implementation_tested
latest_artifacts.code_change_report
latest_artifacts.test_report
pending_checkpoint=null
allowed_next_actions=[get_status, cancel_run]
summary_for_user
```

Sau Slice 4.4 nếu Validator code trace hybrid rule + LLM pass. Nếu trace fail do
code/test thiếu, Slice 4.5 phải repair trước và quay lại Verification Runner ->
Validator. Đây vẫn chưa phải `code_review_ready` vì còn thiếu QA/Tester report,
commit/Draft PR và sync Phase 4:

```text
current_phase=implementation_testing
status=implementation_traced
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
quality_reports.implementation_traceability
pending_checkpoint=null
allowed_next_actions=[continue_run, get_status, cancel_run]
summary_for_user
```

`quality_reports.implementation_traceability` phải ghi `mode=hybrid_rule_llm_code_trace_v1`,
`llm_required=true`, `llm_review_ok=true` và confidence nếu model trả về. Nếu LLM
review phát hiện semantic gap do code/test, state phải là
`implementation_repair_needed` thay vì `implementation_traced`; lỗi không
repair được ở code/test mới `blocked`.

Sau Slice 4.5 nếu Validator fail vì code/test thiếu link có thể sửa, workflow
không còn dùng `blocked` chung chung. Nó dừng ở state trung gian để Developer
repair ở Slice 4.6:

```text
current_phase=implementation_repair
status=implementation_repair_needed
latest_artifacts.requirement_design_code_trace
latest_artifacts.implementation_repair_feedback
quality_reports.implementation_traceability
quality_reports.implementation_repair
implementation_repair_iterations=0
pending_checkpoint=null
last_error=null
allowed_next_actions=[continue_run, get_status, cancel_run]
summary_for_user
```

`continue-run` từ state này gọi Developer.repair một vòng. Nếu repair pass test
và Validator trace, state quay về `implementation_traced`. Nếu vẫn còn missing
link repairable và còn lượt, workflow ghi feedback version mới và giữ
`implementation_repair_needed`. Nếu hết lượt, test fail/skipped, thiếu design
mapping, thiếu artifact nền, LLM review lỗi hệ thống hoặc lỗi không thể quy về
code/test, state chuyển `blocked` với `last_error` rõ.

Trước khi gọi Developer.repair, workflow phải refresh `developer_work_packet_v{n}`
từ target repo hiện tại. Điều này bảo đảm `current_files` không còn là snapshot
cũ từ trước khi Developer.act/repair ghi code. `latest_artifacts.developer_work_packet`
phải trỏ tới packet version mới nhất trong vòng repair.

Nếu state đã chuyển `blocked` vì
`last_error.error_code=implementation_repair_limit_exceeded`, workflow có một đường
vận hành riêng là `revalidate-implementation`. Đường này dùng khi Validator hoặc
evidence logic vừa được sửa và cần trace lại code hiện tại bằng logic mới. Nó tạo
artifact version mới cho `developer_work_packet`, `test_report` và
`requirement_design_code_trace`, nhưng không tăng `implementation_repair_iterations`
vì chưa có Developer.repair mới. Nếu trace còn repairable và limit mới còn lượt,
state quay về `implementation_repair_needed`; nếu trace pass, state chuyển
`implementation_traced`.

Sau Slice 4.6 nếu Developer.repair pass:

```text
current_phase=implementation_testing
status=implementation_traced
latest_artifacts.code_change_report=code_change_report_v{n}.md
latest_artifacts.test_report=test_report_v{n}.md
latest_artifacts.requirement_design_code_trace=requirement_design_code_trace_v{n}.json
latest_artifacts.developer_work_packet=developer_work_packet_v{n}.json
quality_reports.implementation_traceability
quality_reports.implementation_repair.needed=false
implementation_repair_iterations>=1
pending_checkpoint=null
last_error=null
allowed_next_actions=[continue_run, get_status, cancel_run]
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
latest_artifacts.qa_test_report
quality_reports.implementation_traceability
quality_reports.qa_acceptance
pending_checkpoint.type=code_review
pending_checkpoint.artifact_ref=code_change_report_v1
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
last_sync.phase=code_review
summary_for_user
```

Checkpoint `code_review` nghĩa là human cần review PR/code/test report. Nó không
có nghĩa PR đã được merge.

## Last Sync Phase 4

Trước khi ghi state đầy đủ ở trên, Harness phải có cả `test_report_v1.md`,
`requirement_design_code_trace_v1.json` và `qa_test_report_v1.md` pass hoặc block
rõ lý do. Không được tạo Draft PR/code review checkpoint chỉ vì compile/unit test
pass.

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
- LLM graph review trong Validator trả `ok=false` vì lỗi không thể quy về repair
  code/test an toàn;
- Validator phát hiện approved design mâu thuẫn/không triển khai được; trường hợp này phải block hoặc reopen design checkpoint có human approval, không tự sửa design âm thầm trong Phase 4;
- QA/Tester Agent phát hiện acceptance/system behavior fail quá giới hạn repair;
- GitHub sync fail toàn bộ.

`last_error` phải đủ rõ để Niko nói lại cho anh biết đang kẹt ở đâu.

