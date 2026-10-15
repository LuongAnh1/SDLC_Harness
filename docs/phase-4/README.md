# Phase 4 - Implementation Plan Đến Code/Test Và Draft PR

Phase 4 bắt đầu sau khi human/Niko duyệt checkpoint
`implementation_plan_review` của Phase 3. Mục tiêu của phase này là triển khai
code thật trong target repo, chạy test/validation, kiểm trace requirement ->
design -> code -> test, rồi mở Draft PR để human review.

Phase 4 mới là nơi GitHub trở thành tool sync chính. Phase 3 chỉ chuẩn bị plan và
kiểm target repo; Phase 4 mới tạo branch, commit và Draft PR.

## Trạng Thái Hiện Tại

Phase 4 đã khóa code/unit test tới Slice 4.8B. Sau khi Draft PR được publish,
Harness sync evidence về Jira/Confluence và mở checkpoint `code_review_ready`.
Developer.act sinh/apply code, Verification Runner chạy compile/unit, Validator kiểm trace bằng
rule + LLM, QA sinh acceptance test thật, và hai repair loop đều đã có giới hạn:

- vòng nhỏ Validator -> Developer qua `SDLC_IMPLEMENTATION_REPAIR_LIMIT`;
- vòng lớn QA -> Developer -> Runner -> Validator -> QA qua `SDLC_QA_REPAIR_LIMIT`.

Verification Runner hiện bắt buộc execute từng Java class do QA vừa sinh sau
command baseline. Việc chỉ compile file QA hoặc chạy một unit-test class cũ không
còn được tính là QA pass.

Slice 4.8A đã được live-test để commit target repo, push branch và tạo/cập nhật
Draft PR thật. Slice 4.8B cũng đã được live-test cho bước
`code_review_published -> code_review_ready`; re-sync cập nhật đúng Jira comment
và Confluence page cũ thay vì tạo bản trùng.

Trước khi mở approve Code Review và Phase 5, Slice 4.9 phải thay cơ chế một
working directory dùng chung bằng worktree-per-run. Contract triển khai nằm tại
`workspace-isolation/README.md`; checklist riêng chưa được tick vì code workspace
manager, lock, adoption và cleanup chưa tồn tại.

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

## Slice 4.7A Và 4.7B Đã Chốt

Luồng QA hiện tại:

```text
implementation_traced
-> compact QA packet
-> QA/Tester sinh Java acceptance/system tests
-> workflow apply test dưới src/test/java/
-> Verification Runner chạy command baseline
-> Verification Runner chạy trực tiếp từng QA-generated Java class
-> pass: qa_tested
-> fail còn lượt: qa_repair_needed
-> Developer.repair -> Runner -> Validator -> QA
-> hết lượt: blocked/qa_repair_limit_exceeded
```

QA LLM output lỗi hoặc bị cắt vẫn block và lưu raw response. QA `skipped` cũng
block, không tự đi vào Developer repair vì thiếu môi trường chạy không chứng minh
được lỗi nằm ở code.

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
-> publish an toàn đúng publication manifest lên GitHub
-> status=code_review_published
-> sync PR/test/trace summary về Jira và Confluence ở Slice 4.8B
-> status=code_review_ready
```

## Slice 4.8A - GitHub Publication

Slice này chỉ sở hữu code collaboration lane:

```text
qa_tested
-> dựng publication manifest từ Developer allowed_paths, QA test files và
   verification support scripts
-> từ chối mọi file dirty nằm ngoài manifest
-> stage đúng từng path, không dùng git add -A
-> tạo hoặc tái sử dụng commit có trailer SDLC-Run
-> push planned branch
-> tạo Draft PR mới hoặc cập nhật đúng Draft PR đang mở
-> ghi github_pr_body_v1.md và github_code_review_v1.json
-> status=code_review_published
```

Nếu commit/push/PR lỗi sau khi đã hoàn thành một phần, workflow giữ evidence trong
`github_code_review_v1.json`, chuyển sang `code_review_publish_failed` và cho phép
`continue_run` retry. Khi đã ở `code_review_published`, lần `continue_run` kế tiếp
không publish GitHub lần nữa mà sync checkpoint Code Review sang Jira/Confluence.
Re-sync dùng `sync-run` và giữ nguyên remote target theo marker `source_ref + phase`.

## Slice 4.8B - Code Review Checkpoint Sync

```text
code_review_published
-> status=code_review_syncing
-> đọc github_code_review + test report + Validator trace + QA report
-> sync một Jira comment và một Confluence page ổn định theo source_ref + phase
-> ghi tool_sync_report_v4.json
-> status=code_review_ready
-> pending_checkpoint.type=code_review
```

GitHub trong report v4 là target tham chiếu, không bị publish lại. Nếu một target
Atlassian lỗi, state chuyển `code_review_sync_failed`, giữ nguyên toàn bộ evidence
và chỉ cho retry bằng `sync-run`. Nếu report local bị mất, adapter tìm lại remote
target bằng marker. Nhiều target cùng marker hoặc nhiều legacy page phù hợp sẽ trả
`remote_sync_ambiguous`; Harness không tự chọn bừa và không tự xóa page/comment cũ.

Jira source loader loại cả marker mới và heading checkpoint legacy do Harness tạo.
Nhờ vậy output của run cũ không quay ngược vào source packet của run mới.

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

Code change report hiện cần nói rõ branch, file đổi, lý do đổi và phần
requirement/design nào được cover. Commit xuất hiện ở lát GitHub sync sau khi
compile/unit, Validator trace và QA/Tester đều đủ điều kiện.

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
  source/test hiện tại, Verification Runner report và trace summary để kiểm
  acceptance/system-level behavior và gửi failure report về Developer.

Phase 4 có hai vòng lặp: vòng nhỏ `Code -> Verification Runner -> Validator` để
khóa compile/unit/trace, và vòng lớn `Code -> Verification Runner -> Validator ->
QA/Tester` để khóa hành vi use case. Nếu QA fail, Developer sửa code rồi vẫn phải
chạy lại cả Verification Runner và Validator trước khi QA test lại.

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

## Đầu Ra Hiện Tại

```text
.runtime/runs/<run_id>/artifacts/developer_work_packet_v1.json
.runtime/runs/<run_id>/artifacts/developer_work_packet_v{n}.json
.runtime/runs/<run_id>/artifacts/code_change_report_v1.md
.runtime/runs/<run_id>/artifacts/test_report_v1.md
.runtime/runs/<run_id>/artifacts/requirement_design_code_trace_v1.json
.runtime/runs/<run_id>/artifacts/implementation_repair_feedback_v1.json
.runtime/runs/<run_id>/artifacts/qa_test_report_v1.md
.runtime/runs/<run_id>/sync/github_pr_body_v1.md
.runtime/runs/<run_id>/sync/github_code_review_v1.json
```

Khi run đạt `qa_tested`, state phải có:

```text
current_phase=qa_testing
status=qa_tested
latest_artifacts.developer_work_packet
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
latest_artifacts.qa_test_report
quality_reports.implementation_traceability
quality_reports.implementation_repair nếu từng repair
quality_reports.qa_acceptance
implementation_repair_iterations
pending_checkpoint=null
allowed_next_actions=[get_status, cancel_run]
```

## Đầu Ra Đầy Đủ Trước Code Review

Slice 4.8B đã sinh và live-test artifact sau:

```text
.runtime/runs/<run_id>/sync/tool_sync_report_v4.json
```

Và `state.json` lúc đó phải có:

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
allowed_next_actions=[get_status, sync_run, cancel_run]
summary_for_user
```

Checkpoint `code_review` nghĩa là human cần review PR/code/test/trace/QA report.
Nó không có nghĩa PR đã được merge. Nếu thiếu `qa_test_report_v1.md` hoặc QA chưa
pass/được block rõ ràng, workflow chưa được coi là `code_review_ready`.

Approve checkpoint ở lát sau chỉ được chuyển Draft PR thành Ready for review và
sync trạng thái; merge luôn do human thực hiện trực tiếp trên GitHub. Request
change thuộc Phase 5 và phải tiếp tục trên đúng worktree/branch/PR của run.

CLI side-effect mặc định `--sync-mode mock`, kể cả `.env` đang để real. Muốn ghi
Jira/Confluence thật phải truyền rõ `--sync-mode real`; Niko làm việc này qua
`NIKO_SDLC_SYNC_MODE`. Đây là guard chống một lần test CLI vô tình phát lại các
checkpoint cũ lên tool thật.

## Không Làm Trong Phase 4

- Không tạo GitHub Issue mới.
- Không merge PR tự động.
- Không xử lý request change động; đó là Phase 5.
- Không invalidate context ngoài tool; đó là Phase 6.
- Không mở đa ngôn ngữ. Java/eTour vẫn là target ban đầu để bám TraceDev.
- Không để Niko tự thao tác GitHub hoặc tự sửa code.
- Không dùng current branch của canonical target repo làm identity của run sau
  khi Slice 4.9 được triển khai.

## Tài Liệu Trong Folder Này

- `phase-4-checklist.md`: checklist triển khai code/test/Draft PR.
- `state-and-artifacts.md`: state machine và artifact Phase 4.
- `qa-tester-agent.md`: contract triển khai Slice 4.7 QA/Tester Agent trước khi
  mở commit/Draft PR.
- `github-code-collaboration.md`: policy dùng GitHub branch/commit/Draft PR.
- `workspace-isolation/`: kiến trúc, runtime contract, migration và checklist
  worktree-per-run trước Code Review approval/Phase 5.


## QA Slice 4.7A/4.7B Live Update - 2026-10-14

Lần live test đầu cho thấy một false positive quan trọng: `scripts/test.ps1`
compile toàn bộ Java nhưng chỉ execute `CulturalHeritageRepositoryTest`, nên file
`DeleteCulturalHeritageAcceptanceTest.java` do QA sinh chưa từng chạy. Harness đã
được sửa để truyền `required_test_files` cho Verification Runner và execute từng
class bằng `java -cp build <fully-qualified-class>` sau baseline command.

Regression test mới khóa hành vi: baseline exit `0` nhưng QA class ném exception
thì kết quả bắt buộc là `failed`.

Live DeepSeek run `run_live_qa_repair_20261014_03` trên runtime/target repo copy:

- Seed lỗi: cancel xóa cultural-heritage item dù use case yêu cầu giữ nguyên.
- QA sinh `DeleteCulturalHeritageAcceptanceTest.java`; class được execute thật và
  fail tại assertion `cancel must not delete the item`.
- State chuyển `implementation_traced -> qa_repair_needed` và ghi
  `qa_repair_feedback_v2.json`.
- Developer sửa `DeleteCulturalHeritageController.java` cùng flow test trong
  `allowed_paths`.
- Baseline verification pass, Validator rule + LLM pass, QA sinh/chạy lại class.
- State cuối `qa_tested`, `qa_repair_iterations=1`, `last_error=null`.

Đoạn trên là mốc lịch sử của Slice 4.7B. Slice 4.8A sau đó đã publish Draft PR và
Slice 4.8B đã sync code-review checkpoint về Jira/Confluence để chuyển sang
`code_review_ready`. Bước kế tiếp là Slice 4.9 worktree-per-run trước khi mở
approve Code Review và Phase 5.
