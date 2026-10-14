# Phase 4.7 - QA/Tester Agent

Tài liệu này chốt hướng triển khai lát QA/Tester Agent sau khi Phase 4 đã đạt
`implementation_traced`. Mục tiêu là kiểm hành vi acceptance/system-level từ use
case, không thay thế Verification Runner và không mở Draft PR.

## Vai Trò

QA/Tester Agent là tầng kiểm hành vi sau khi code đã qua:

```text
Developer.act/repair
-> Verification Runner compile/unit test pass
-> Validator code trace hybrid rule + LLM pass
-> status=implementation_traced
-> QA/Tester Agent
```

Ranh giới quan trọng:

- QA/Tester không sửa code trực tiếp.
- QA/Tester không sửa requirement, design hoặc implementation plan đã duyệt.
- QA/Tester không thay thế unit test report của Verification Runner.
- QA/Tester chỉ sinh/chạy acceptance hoặc system-level scenarios và ghi evidence.
- Commit, Draft PR và sync Phase 4 vẫn là lát sau khi QA pass.

## Hai Vòng Lặp

Phase 4 có hai vòng lặp khác nhau, không được gộp nhầm:

```text
Vòng nhỏ: Code -> Verification Runner -> Validator
```

Vòng nhỏ kiểm code/test có compile, unit test pass và trace được về
requirement/design hay không. Nếu Validator thấy thiếu link do code/test, workflow
quay lại Developer.repair rồi chạy lại Verification Runner và Validator. Khi vòng
nhỏ pass, state mới đạt `implementation_traced`.

```text
Vòng lớn: Code -> Verification Runner -> Validator -> QA/Tester
```

Vòng lớn kiểm hành vi theo use case sau khi vòng nhỏ đã pass. Nếu QA/Tester fail
do code/test behavior, workflow quay lại Developer.repair, sau đó bắt buộc chạy
lại đầy đủ Verification Runner -> Validator -> QA/Tester. QA không được bỏ qua
Validator vì code vừa sửa có thể làm hỏng trace đã pass trước đó.

Nói ngắn gọn: Validator là lead kỹ thuật giữ trace requirement/design/code/test;
QA/Tester là lớp kiểm hành vi cuối của use case trước khi cho phép sang commit,
Draft PR và sync Phase 4.

## Input Bắt Buộc

Workflow chỉ được chạy QA/Tester khi state hiện tại là `implementation_traced`.
Input tối thiểu:

```text
latest_artifacts.requirement_packet
latest_artifacts.design_artifact
latest_artifacts.developer_work_packet
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
quality_reports.implementation_traceability.ok=true
```

Nếu run từng qua Developer repair, dùng artifact version mới nhất trong
`latest_artifacts`, không đọc lại `developer_work_packet_v1` theo cách cố định.

QA/Tester có thể đọc thêm:

- `formal_use_case_v1.txt` nếu có;
- acceptance criteria trong requirement packet;
- scenario/exception/postcondition từ requirement packet;
- code/test summary từ `requirement_design_code_trace_v{n}.json`;
- target repo branch hiện tại để chạy scenario nếu toolchain hỗ trợ.

## Output Bắt Buộc

Artifact chính là:

```text
.runtime/runs/<run_id>/artifacts/qa_test_report_v1.md
```

Report phải có tối thiểu:

```text
status: pass | fail | skipped
summary
scenarios
evidence
failure_feedback_for_developer
limits_or_skipped_reason
```

`status=passed` chỉ được dùng khi QA/Tester có đủ evidence rằng acceptance hoặc
system-level behavior khớp use case. Nếu chưa có cách chạy acceptance thật, report
phải ghi `skipped` kèm lý do, không được giả pass để mở PR.

State khi QA pass:

```text
current_phase=qa_testing
status=qa_tested
latest_artifacts.qa_test_report
quality_reports.qa_acceptance.ok=true
pending_checkpoint=null
allowed_next_actions=[get_status, cancel_run]
```

`qa_tested` là trạng thái trung gian đề xuất cho lát QA V1. Lát GitHub sync sau đó
mới chuyển sang `code_review_ready` khi commit, Draft PR và sync Phase 4 hoàn tất.

## Behavior V1 Đã Triển Khai Trong Slice 4.7A/4.7B

QA nhận compact packet theo hướng TraceDev: accepted requirement/use case, code
hiện tại theo `allowed_paths`, Verification Runner report và traceability summary.
Design/plan/full trace chỉ giữ reference path để tránh cắt JSON.

Sau khi apply test dưới `src/test/java/`, workflow không chỉ gọi command baseline.
Nó truyền danh sách file QA bắt buộc cho Verification Runner; runner suy ra fully
qualified Java class và execute từng class. QA chỉ pass khi cả baseline command và
tất cả required QA classes cùng pass.

Nếu test fail và còn `SDLC_QA_REPAIR_LIMIT`, workflow ghi
`qa_repair_feedback_v{n}.json`, chuyển `qa_repair_needed`, rồi vòng tiếp theo chạy
Developer.repair -> Verification Runner -> Validator -> QA. JSON lỗi và `skipped`
vẫn block thay vì tự sửa code.

## Behavior V1 Nền Tảng

### Fake Mode

Fake mode dùng cho unit test và smoke test không gọi LLM:

- đọc requirement packet, code hiện tại, Verification Runner report và trace summary;
- sinh một `qa_test_report_v1.md` deterministic;
- có thể pass khi input đủ và traceability đã pass;
- có thể forced-fail trong test bằng fixture/fake client để kiểm repair path.

### LLM/DeepSeek Mode

LLM mode đọc context đã duyệt và trả output có cấu trúc để writer chuyển thành
Markdown report. Prompt phải yêu cầu model:

- kiểm acceptance/system behavior, không review lại toàn bộ code như Validator;
- chỉ dùng requirement đã duyệt, code hiện tại, Verification Runner report và trace summary làm nguồn chính;
- không đề xuất sửa requirement/design âm thầm;
- phân biệt `failed` vì code behavior sai và `blocked` vì requirement/design nền
  mâu thuẫn hoặc thiếu thông tin.

Nếu LLM trả output không parse được, workflow block bằng lỗi rõ ràng, không coi
QA là pass.

## Failure Handling

Ở slice sau, nếu QA fail do code/test behavior thiếu hoặc sai:

```text
status=qa_repair_needed
latest_artifacts.qa_test_report
quality_reports.qa_acceptance.ok=false
feedback scope=code_or_test
allowed_next_actions=[continue_run, get_status, cancel_run]
```

Vòng tiếp theo:

```text
Developer.repair
-> Verification Runner
-> Validator code trace
-> QA/Tester Agent
```

Nếu QA fail do approved requirement/design mâu thuẫn, mơ hồ hoặc không thể kiểm:

```text
status=blocked
last_error.error_code=qa_requires_human_or_design_review
```

Không ép Developer vá code vòng quanh khi lỗi thật sự nằm ở artifact nền.

Nếu QA bị skipped:

- chưa được chuyển sang `code_review_ready`;
- phải ghi rõ lý do skipped;
- Niko sau này chỉ được báo "QA chưa đủ điều kiện chạy", không báo pass.

## State Và Config Đề Xuất

Thêm giới hạn vòng repair riêng cho QA:

```text
SDLC_QA_REPAIR_LIMIT
CLI: --qa-repair-limit
```

Default đề xuất: `1` vòng ở V1 để tránh loop quá rộng. Giới hạn này độc lập với
`SDLC_IMPLEMENTATION_REPAIR_LIMIT` vì QA fail là tầng sau Validator trace.

State trung gian đề xuất:

```text
qa_tested
qa_repair_needed
```

`code_review_ready` chỉ xuất hiện sau lát GitHub sync:

```text
qa_tested
-> commit/Draft PR/sync Jira-Confluence-GitHub
-> code_review_ready
```

## Test Và Acceptance Cho Lát QA

- Unit tests cover QA pass/fail, invalid JSON, repair pass, repair-limit block và
  required QA class execution.
- Baseline command exit `0` không đủ: required QA class fail phải trả `failed`.
- QA `skipped` không được chuyển `code_review_ready`.
- Live DeepSeek run `run_live_qa_repair_20261014_03` đã chứng minh nhánh fail ->
  `qa_repair_needed` -> Developer repair -> Validator -> QA pass trong một vòng.
- Live `skipped` case vẫn chưa chạy; đây là hardening case riêng, không phủ nhận
  repair loop đã được kiểm chứng.

## Current Slice 4.7A/4.7B Contract - 2026-10-14

```text
implementation_traced
-> build compact QA packet
-> QA generates Java acceptance/system tests as JSON
-> validate/apply paths under src/test/java/
-> baseline Verification Runner command
-> execute every required QA-generated Java class
-> pass: qa_tested
-> fail: qa_repair_needed -> Developer.repair -> Runner -> Validator -> QA
-> limit exhausted: blocked/qa_repair_limit_exceeded
```

Artifacts gồm `qa_generated_tests_v{n}.json`, `qa_test_execution_v{n}.md`,
`qa_test_report_v{n}.md` và khi fail có `qa_repair_feedback_v{n}.json`.

Live evidence `run_live_qa_repair_20261014_03` xác nhận class QA fail thật với
assertion cancel không được delete; Developer sửa trong một vòng, Validator pass,
class QA tái sinh được execute và pass. Commit/Draft PR không thuộc slice này.
