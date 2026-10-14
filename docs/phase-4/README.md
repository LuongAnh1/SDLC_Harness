# Phase 4 - Implementation Plan Đến Code/Test Và Draft PR

Phase 4 bắt đầu sau khi human/Niko duyệt checkpoint
`implementation_plan_review` của Phase 3. Mục tiêu của phase này là triển khai
code thật trong target repo, chạy test/validation, kiểm trace requirement ->
design -> code -> test, rồi mở Draft PR để human review.

Phase 4 mới là nơi GitHub trở thành tool sync chính. Phase 3 chỉ chuẩn bị plan và
kiểm target repo; Phase 4 mới tạo branch, commit và Draft PR.

## Slice 4.1 Đã Chốt

Slice đầu của Phase 4 hiện chỉ làm bước chuyển tiếp an toàn:

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_implementation_plan_review
-> đọc implementation_plan_structured_v{n}
-> kiểm target repo vẫn sạch và có CodeGraph index
-> ghi developer_work_packet_v1.json
-> status=implementation_ready
```

Slice này chưa tạo branch, chưa sửa code, chưa commit và chưa mở PR. Mục đích
là biến implementation plan đã được duyệt thành input máy đọc được cho
Developer.act ở slice tiếp theo, thay vì để Developer parse Markdown hoặc
Confluence.

## Slice 4.2 Đã Chốt

Slice 4.2 chạy tiếp từ `implementation_ready`:

```text
continue-run(run_id)
-> đọc developer_work_packet_v1.json
-> kiểm target repo sạch
-> tạo/checkout planned branch
-> Developer.act sinh changed_files_json_v1
-> validate file nằm trong allowed_paths
-> apply file vào target repo
-> ghi code_change_report_v1.md
-> status=implementation_written
```

Slice này tạo code change thật nhưng vẫn chưa chạy test, chưa commit và chưa mở
PR. Working tree của target repo có thể dirty sau bước này để human review code
trực tiếp trước khi sang Verification Runner và Validator.

## Slice 4.3 Đã Chốt

Slice 4.3 chạy tiếp từ `implementation_written`:

```text
continue-run(run_id)
-> đọc developer_work_packet_v1.json
-> đọc code_change_report_v1.md
-> chạy Verification Runner trong target repo
-> ghi test_report_v1.md
-> nếu passed: status=implementation_tested
-> nếu failed/skipped: status=blocked với last_error rõ
```

Slice này chỉ chạy compile/unit test command của repo target. Nó chưa gọi
Validator code trace, chưa chạy QA/Tester Agent acceptance/system test, chưa
commit và chưa mở PR.

## Slice 4.4 Đã Chốt

Slice 4.4 chạy tiếp từ `implementation_tested`:

```text
continue-run(run_id)
-> đọc developer_work_packet_v1.json
-> đọc code_change_report_v1.md và test_report_v1.md
-> kiểm target repo cho phép dirty vì code đã được apply
-> Validator code trace V1 sinh static evidence design -> source/test file thực tế
-> bắt buộc gọi LLM/DeepSeek review graph requirement -> design -> code -> test
-> ghi requirement_design_code_trace_v1.json
-> nếu pass: status=implementation_traced
-> nếu fail: status=blocked với missing_links rõ
```

Slice này là hybrid rule + LLM gate. Rule-based evidence chỉ kiểm file/test/symbol
tối thiểu; LLM/DeepSeek review graph là bắt buộc để soi semantic gap mà rule có
thể bỏ sót. Đây là điểm nâng cấp của SDLC_Harness so với TraceDev gốc: pass chỉ
được tính khi cả static evidence và LLM graph review cùng ok. Theo xác nhận của
project owner, Validator được phép gửi source/test snippet giới hạn sang
LLM/DeepSeek để review code trước khi human review.

Slice này chưa chạy QA/Tester Agent, chưa commit và chưa mở PR.

Nếu Validator phát hiện missing link/semantic gap do code/test, Slice 4.5 chuyển
run sang `implementation_repair_needed` và ghi
`implementation_repair_feedback_v1.json`. Slice 4.6 dùng feedback đó để
Developer sửa code/test một vòng, rồi chạy lại Verification Runner và Validator.
QA/Tester Agent chỉ được chạy sau khi repair loop làm compile/unit test và
Validator trace cùng pass ổn định.

## Slice 4.5 Đã Chốt

Slice 4.5 là bước đệm hẹp trước repair loop tự động:

```text
continue-run(run_id)
-> chạy Validator code trace như Slice 4.4
-> nếu pass: status=implementation_traced
-> nếu fail vì thiếu link có thể sửa ở code/test:
   status=implementation_repair_needed
   latest_artifacts.implementation_repair_feedback
-> nếu fail vì input graph/artifact nền không đủ điều kiện repair: status=blocked
```

Artifact mới là `implementation_repair_feedback_v1.json`. Nó gom
`missing_links`, `llm_review.summary`, `llm_review.risks`, trace artifact, repo
branch và `allowed_paths` để slice sau giao lại cho Developer sửa code/test.
Slice này chỉ chuẩn hóa feedback. Slice 4.6 mới gọi Developer repair từ
`implementation_repair_needed`.

## Slice 4.6 Đã Chốt

Slice 4.6 chạy tiếp từ `implementation_repair_needed`:

```text
continue-run(run_id)
-> đọc developer_work_packet_v1.json
-> đọc implementation_repair_feedback_v{n}
-> kiểm số vòng repair bằng SDLC_IMPLEMENTATION_REPAIR_LIMIT
-> refresh code/test snapshot từ target repo và ghi developer_work_packet_v{n+1}.json
-> Developer.repair sửa code/test trong allowed_paths
-> ghi code_change_report_v{n+1}.md
-> chạy lại Verification Runner và ghi test_report_v{n+1}.md
-> nếu test pass: chạy lại Validator code trace + LLM review
-> nếu trace pass: status=implementation_traced
-> nếu trace vẫn thiếu link repairable và còn lượt: ghi feedback version mới
-> nếu hết lượt hoặc lỗi không repair được: status=blocked với last_error rõ
```

Slice này không sửa requirement, design hoặc implementation plan. Mỗi vòng repair
chỉ xử lý code/test trong `output_contract.allowed_paths`, không ghi đè artifact
cũ và không tự commit/mở PR. `SDLC_IMPLEMENTATION_REPAIR_LIMIT` hoặc CLI
`--implementation-repair-limit` điều khiển số vòng Developer.repair.

Từ bản sửa sau live test DeepSeek ngày 2026-10-14, mỗi vòng repair phải refresh
`DeveloperWorkPacket.current_files` từ target repo thật trước khi gọi
Developer.repair. `developer_work_packet_v1.json` vẫn là packet gốc sau khi duyệt
implementation plan; các vòng repair dùng `developer_work_packet_v2.json`,
`developer_work_packet_v3.json`, ... để tránh Developer sửa dựa trên snapshot cũ.
Nếu Developer.repair gọi LLM thật và có raw response, workflow lưu thêm
`developer_repair_raw_response_v{n}.txt` để debug parse/logic khi repair khó hội tụ.

Khi repair pass, `quality_reports.implementation_repair.needed=false` và
`implementation_repair_iterations` ghi số vòng đã chạy. Khi Verification Runner
fail sau repair, workflow block với `test_failed`/`test_skipped`; khi Validator
vẫn fail sau khi hết lượt, workflow block với
`implementation_repair_limit_exceeded`.

Nếu run đã `blocked` vì `implementation_repair_limit_exceeded` nhưng sau đó mình
sửa Validator/evidence logic, không sửa tay `state.json`. Dùng lệnh:

```bash
rtk python -m sdlc_harness.cli revalidate-implementation \
  --run-id <run_id> \
  --llm-mode deepseek \
  --implementation-repair-limit <new_limit>
```

Lệnh này không gọi Developer sửa code ngay. Nó refresh `DeveloperWorkPacket` từ
target repo hiện tại, chạy lại Verification Runner, chạy lại Validator code trace,
rồi:

- nếu trace pass: chuyển về `implementation_traced`;
- nếu còn gap repairable và limit mới còn lượt: ghi feedback version mới và chuyển
  về `implementation_repair_needed`;
- nếu vẫn hết lượt hoặc lỗi không repair được ở code/test: giữ `blocked` với
  `last_error` rõ.

## Luồng Phase 4

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_implementation_plan_review
-> chuẩn bị developer_work_packet_v1.json
-> continue-run khi state=implementation_ready
-> đọc approved implementation_plan_v1
-> đọc approved design graph và requirement-design trace
-> tạo branch trong target repo
-> Developer.act tạo/sửa code và unit test dựa trên plan
-> ghi code_change_report_v1
-> Verification Runner chạy compile/unit test command hoặc ghi skipped rõ ràng
-> ghi test_report_v1
-> Validator kiểm requirement -> design -> code -> test trace bằng static evidence + LLM graph review
-> ghi requirement_design_code_trace_v1.json
-> nếu code/unit test thiếu so với approved design: ghi implementation_repair_feedback_v1.json và dừng ở implementation_repair_needed
-> dùng feedback đó để Developer sửa code/test rồi lặp Verification Runner -> Validator trong giới hạn cấu hình
-> nếu approved design mâu thuẫn hoặc không triển khai được: blocked/reopen design có human approval, không tự sửa design âm thầm
-> QA/Tester Agent kiểm acceptance/system behavior từ use case khi baseline compile/unit/trace đã pass ổn định
-> nếu QA fail: feedback về Developer, rồi chạy lại unit test -> Validator -> QA trong giới hạn cấu hình
-> tạo Draft PR trên GitHub
-> sync PR/test/trace summary về Jira và Confluence
-> status=code_review_ready
```

Nếu code không cover design, compile/unit test fail không có repair path, QA fail
quá giới hạn, hoặc trace code-test thiếu nghiêm trọng, workflow phải `blocked`
với `last_error` rõ.

## Developer.act

Developer.act không được sinh code từ design trần. Đầu vào bắt buộc là:

- approved `implementation_plan_v{n}`;
- approved `design_artifact_v{n}`;
- class/sequence graph đã duyệt;
- requirement-design trace;
- target repo status đã được guard.

Code change report cần nói rõ branch, commit, file đổi, lý do đổi và phần
requirement/design nào được cover.

## Verification Runner, Validator Và QA Tester

Phase 4 tách rõ ba vai để gần thực tế doanh nghiệp hơn TraceDev gốc:

- Developer sinh/sửa code và unit test gần code.
- Verification Runner chỉ chạy command compile/unit test của target repo và tạo
  `test_report_v1.md`; nó không phải AI Tester và không tự sửa source.
- Validator kiểm traceability từ approved requirement/design sang code/unit test;
  LLM graph review là bắt buộc, rule-based evidence chỉ là lớp đầu.
- Developer repair loop đứng ngay sau Validator fail do code/test thiếu: Developer
  sửa code/test theo missing links, Verification Runner chạy lại, rồi Validator
  review lại. Khi vòng này pass ổn định, workflow mới được chuyển sang QA/Tester.
- QA/Tester Agent là tầng sau repair loop: đọc use case đã được Validator bảo vệ,
  sinh/chạy acceptance hoặc system-level test và gửi failure report về Developer.

`test_report_v1.md` của Verification Runner phải phân biệt rõ `passed`, `failed`
và `skipped`. Nếu máy chưa có JDK hoặc target repo chưa có test harness đủ mạnh,
report vẫn phải ghi skipped với lý do, không được trình bày như pass.

Validator tạo `requirement_design_code_trace_v1.json`, tối thiểu nối:

```text
requirement id
  -> design class/method/message
  -> source file/class/function
  -> test file/case nếu có
```

Report này phải ghi `mode=hybrid_rule_llm_code_trace_v1`, `llm_required=true` và
`llm_review`. Nếu LLM review trả `ok=false` hoặc phát hiện semantic gap do
code/test chưa cover approved design, workflow phải dừng ở
`implementation_repair_needed` và ghi feedback. Chỉ block khi lỗi không thể quy
về repair code/test an toàn.

Nếu trace fail do code/unit test chưa cover approved design, Developer phải repair
trong giới hạn cấu hình trước khi QA/Tester được chạy. Nếu fail do approved
design mâu thuẫn, mơ hồ hoặc không triển khai được, workflow phải `blocked` hoặc
mở nhánh quay về design checkpoint có human approval; Validator không được tự sửa
design trong Phase 4.

QA/Tester Agent cũng không sửa code trực tiếp. Nếu acceptance/system test fail,
Tester chỉ ghi failure evidence; Developer sửa code, Verification Runner chạy lại
unit test, Validator kiểm lại trace, rồi QA test lại.

## Đầu Ra Bắt Buộc

```text
.runtime/runs/<run_id>/artifacts/developer_work_packet_v1.json
.runtime/runs/<run_id>/artifacts/developer_work_packet_v{n}.json
.runtime/runs/<run_id>/artifacts/code_change_report_v1.md
.runtime/runs/<run_id>/artifacts/test_report_v1.md
.runtime/runs/<run_id>/artifacts/requirement_design_code_trace_v1.json
.runtime/runs/<run_id>/artifacts/implementation_repair_feedback_v1.json
.runtime/runs/<run_id>/artifacts/qa_test_report_v1.md
.runtime/runs/<run_id>/sync/tool_sync_report_v4.json
```

Và `state.json` phải có:

```text
current_phase=code_review
status=code_review_ready
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
latest_artifacts.qa_test_report
quality_reports.implementation_traceability
quality_reports.qa_acceptance
last_sync.phase=code_review
pending_checkpoint.type=code_review
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

Checkpoint `code_review` nghĩa là human cần review PR/code/test/trace/QA report.
Nó không có nghĩa PR đã được merge. Nếu thiếu `qa_test_report_v1.md` hoặc QA chưa
pass/được block rõ ràng, workflow chưa được coi là `code_review_ready`.

## Không Làm Trong Phase 4

- Không tạo GitHub Issue mới.
- Không merge PR tự động.
- Không xử lý request change động; đó là Phase 5.
- Không invalidate context ngoài tool; đó là Phase 6.
- Không mở đa ngôn ngữ. Java/eTour vẫn là target ban đầu để bám TraceDev.
- Không để Niko tự thao tác GitHub hoặc tự sửa code.

## Tài Liệu Trong Folder Này

- `phase-4-checklist.md`: checklist triển khai code/test/Draft PR.
- `state-and-artifacts.md`: state machine và artifact Phase 4.
- `github-code-collaboration.md`: policy dùng GitHub branch/commit/Draft PR.

