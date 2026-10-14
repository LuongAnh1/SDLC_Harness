# Phase 4 Checklist

Checklist này dùng khi triển khai Phase 4: approved implementation plan -> code,
test, trace và Draft PR. Không tick các mục này trong Phase 3.

## 1. Approval Contract

Mục đích: human/Niko có thể duyệt implementation plan checkpoint và kích hoạt
Phase 4 bằng JSON contract ổn định.

- [x] `approve-checkpoint` chỉ mở Phase 4 khi run đang `waiting_implementation_plan_review`.
- [x] Nếu checkpoint không phải `implementation_plan_review`, trả `checkpoint_not_current`.
- [x] Success trả JSON stdout, không trộn log tự do.
- [x] Error trả JSON `ok=false`.
- [x] `get-status` đọc được state Phase 4 mới.

## 2. Approved Plan Input

Mục đích: Phase 4 dùng plan đã duyệt, không tự lập kế hoạch lại.

- [x] Đọc approved `implementation_plan_v{n}`.
- [x] Đọc approved `design_artifact_v{n}`.
- [x] Đọc graph và requirement-design trace liên quan.
- [x] Nếu thiếu plan đã duyệt, trả lỗi rõ và không chạy Developer.act.

## 2.1 Developer Work Packet V1

Mục đích: đóng gói implementation plan đã duyệt thành input máy đọc được cho
Developer.act, tránh để bước code phải parse Markdown/Confluence.

- [x] Sinh `developer_work_packet_v1.json`.
- [x] Packet chứa `implementation_steps`, `proposed_files`, `design_mappings_by_file`, `test_intents` và `repo_evidence`.
- [x] Packet snapshot nội dung hiện tại của file mục tiêu đã tồn tại.
- [x] Packet có `output_contract` yêu cầu Developer trả changed/new files theo JSON.
- [x] Sau Slice 4.1, state=`implementation_ready` và chưa tạo branch/code/PR.

## 3. Git Target Repo

Mục đích: code sinh ra có nơi lưu trữ/review thật, tách khỏi repo harness.

- [x] Tạo branch theo issue/run.
- [x] Không thao tác khi target repo dirty.
- [ ] Commit có issue key.
- [ ] Không tạo GitHub Issue.
- [ ] Có Draft PR thật; mock/local report chỉ được đánh dấu là chưa khóa Phase 4.

## 4. Developer.act

Mục đích: tạo code change có thể review, dựa trên plan đã duyệt.

- [x] Tạo prompt/module Developer.act riêng cho Phase 4.
- [x] Developer đọc `developer_work_packet_v1.json` từ implementation plan + approved design graph.
- [x] Developer tạo/sửa code trong target repo.
- [x] Sinh `code_change_report_v1.md`.
- [x] Report liệt kê file đổi, branch và reasoning chính; commit để slice sau.
- [x] Code change đủ cụ thể để reviewer kiểm tra thủ công trước khi Draft PR.
- [x] DeveloperWorkPacket và Developer.act prompt có constraint: code/test/helper
  script phải chạy được trong repo path có khoảng trắng hoặc Unicode; dùng
  repo-relative paths, không hard-code absolute path, và dùng UTF-8 rõ ràng khi
  toolchain hỗ trợ.

## 5. Verification Runner Và Validation

Mục đích: code output có kiểm chứng compile/unit rõ ràng, kể cả khi test bị
skipped. Đây là test runner của repo target, không phải AI QA/Tester.

- [x] Tạo Verification Runner/test runner adapter.
- [x] Chạy build/unit test nếu target repo hỗ trợ.
- [x] Sinh `test_report_v1.md`.
- [x] Report phân biệt `passed`, `failed`, `skipped`.
- [x] Nếu test fail, workflow không báo pass.
- [x] Verification Runner phải chạy được trong repo path có khoảng trắng hoặc
  Unicode; mọi compile/test command nên dùng repo-relative paths và UTF-8 rõ ràng
  khi toolchain hỗ trợ.
- [ ] Nếu compile/unit test fail và còn vòng repair, feedback quay về Developer;
  sau repair phải chạy lại Verification Runner.

## 6. Code Traceability

Mục đích: chứng minh requirement/design đã đi tới code/test.

- [x] Sinh `requirement_design_code_trace_v1.json`.
- [x] Trace requirement -> design element -> source file/class/function.
- [x] Trace requirement -> test file/case nếu có.
- [x] Bắt buộc gọi LLM/DeepSeek review graph requirement -> design -> code -> test
  sau static evidence.
- [x] Trace report ghi `mode=hybrid_rule_llm_code_trace_v1`,
  `llm_required=true` và `llm_review`.
- [x] Nếu LLM review phát hiện semantic gap do code/test, workflow chuyển sang
  `implementation_repair_needed` dù static evidence pass.
- [x] Quyết định policy gửi raw code/test snippet cho LLM: project owner đã xác
  nhận cho phép Validator gửi snippet giới hạn sang DeepSeek để review.
- [x] Missing link được ghi riêng.
- [x] Nếu trace fail vì code/unit test thiếu so với approved design, ghi feedback
  máy đọc được cho Developer; vòng repair thật dùng feedback này chạy ở Slice 4.6.
- [x] Validator evidence gửi full content cho controller/test chính khi còn trong
  budget, và ghi metadata `content_truncated/chars_sent/chars_total` để LLM không
  nhầm evidence bị cắt.
- [x] Boolean response và notification/display side-effect trong sequence diagram
  được normalize thành evidence semantic, không ép Developer tạo literal symbol
  như `#true`, `#false` hoặc `display*Message`.
- [ ] Nếu trace fail vì approved design mâu thuẫn/không triển khai được,
  state=`blocked` hoặc reopen design checkpoint có human approval; không tự sửa
  design trong Phase 4.

## 6.1 Validator -> Developer Repair Loop

Mục đích: khi Validator lead phát hiện code/test thiếu link so với approved
design, workflow phải giao feedback lại Developer sửa trước khi chạy QA/Tester.

- [x] Chuyển `missing_links`, `llm_review.summary`, `llm_review.risks` và trace
  artifact thành feedback máy đọc được cho Developer.
- [x] Khi code trace fail nhưng lỗi nằm ở code/test, state chuyển thành
  `implementation_repair_needed` và ghi `implementation_repair_feedback_v1.json`.
- [x] `continue-run` từ `implementation_repair_needed` gọi Developer.repair một vòng
  thay vì trả `implementation_repair_not_implemented`.
- [x] Developer repair code/test trong `allowed_paths`, không tự sửa requirement
  hoặc approved design.
- [x] Trước mỗi vòng Developer.repair, workflow refresh `current_files` từ target
  repo thật và ghi `developer_work_packet_v{n}` để tránh dùng snapshot cũ.
- [x] Mỗi vòng repair ghi `code_change_report_v{n}`, `test_report_v{n}` và
  `requirement_design_code_trace_v{n}` riêng; không ghi đè artifact cũ.
- [x] Sau Developer repair, workflow chạy lại Verification Runner.
- [x] Nếu Verification Runner pass, workflow chạy lại Validator code trace + LLM
  review trước khi cho phép sang QA/Tester.
- [x] Repair loop tôn trọng giới hạn cấu hình; hết lượt vẫn fail thì
  state=`blocked` với `last_error` rõ.
- [x] `SDLC_IMPLEMENTATION_REPAIR_LIMIT` và CLI `--implementation-repair-limit`
  cấu hình số vòng Developer.repair.
- [x] Có lệnh `revalidate-implementation` cho run bị block bởi
  `implementation_repair_limit_exceeded`; lệnh này chạy lại Verification Runner +
  Validator bằng logic mới, không sửa tay runtime state.
- [x] Revalidate không reset `implementation_repair_iterations`; nếu tăng limit
  thì chỉ mở thêm số vòng còn lại và ghi artifact version mới, không ghi đè file
  cũ.
- [x] Nếu Validator xác định lỗi là `trace_mapping_issue` hoặc
  `evidence_incomplete`, workflow block bằng `validator_evidence_or_mapping_issue`
  và không ép Developer vá code vòng quanh.
- [ ] Nếu Validator xác định lỗi nằm ở approved design/requirement cấp nghiệp vụ,
  workflow block hoặc reopen checkpoint có human approval, không ép Developer vá
  code.

## 6.2 QA/Tester Agent Loop

Mục đích: kiểm acceptance/system behavior từ use case sau khi compile/unit,
Validator trace và repair loop đã pass ổn định. Đây mới là vai Tester/QA gần
thực tế doanh nghiệp.

- [ ] QA/Tester Agent đọc use case/requirement packet đã duyệt và code/test
  summary do Validator bảo vệ.
- [ ] QA/Tester Agent sinh hoặc chạy acceptance/system-level scenarios.
- [ ] Sinh `qa_test_report_v1.md` với `passed/failed/skipped` và evidence rõ.
- [ ] QA/Tester Agent không sửa code trực tiếp; chỉ ghi failure evidence.
- [ ] Nếu QA fail và còn vòng repair, feedback quay về Developer.
- [ ] Sau Developer repair, workflow chạy lại Verification Runner -> Validator ->
  QA/Tester Agent.
- [ ] Nếu QA fail quá giới hạn repair, state=`blocked` với `last_error` rõ.

## 7. Tool Sync Phase 4

Mục đích: context implementation được đẩy sang GitHub và sync ngược về Jira/
Confluence bởi SDLC Harness.

- [ ] Tạo hoặc cập nhật Draft PR trên GitHub.
- [ ] PR description có Jira link, Confluence link, `run_id`, summary, tests và trace.
- [ ] Sync Jira comment với PR link/status.
- [ ] Sync Confluence code review checkpoint.
- [ ] Ghi `tool_sync_report_v4.json` với `phase=code_review`.
- [ ] Re-sync không tạo PR/comment/page trùng nếu đã có external id.
- [ ] Partial failure không xóa artifact local.

## 8. Workflow State Và Niko Reporting

Mục đích: Niko báo được Phase 4 đang ở đâu mà không hiểu nội bộ Developer,
Verification Runner, Validator hoặc QA/Tester.

- [x] Ghi state `implementation_writing`.
- [x] Ghi state trung gian `implementation_written` sau khi apply code nhưng chưa test/commit/PR.
- [x] Ghi state `implementation_testing`.
- [x] Ghi state trung gian `implementation_traced` sau khi Validator code trace pass.
- [x] Ghi state trung gian `implementation_repair_needed` khi Validator fail do
  thiếu link có thể sửa ở code/test.
- [ ] Ghi state cuối `code_review_ready` khi pass.
- [x] `latest_artifacts` trỏ tới `test_report` mới nhất sau Verification Runner.
- [x] `latest_artifacts` trỏ tới report/trace mới nhất sau Validator code trace.
- [x] `latest_artifacts` trỏ tới `developer_work_packet` mới nhất sau mỗi vòng
  repair.
- [ ] `latest_artifacts` trỏ tới `qa_test_report` sau QA/Tester Agent.
- [x] `quality_reports` có traceability summary.
- [ ] `quality_reports` có QA acceptance summary.
- [ ] `pending_checkpoint.type=code_review`.
- [ ] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.
- [ ] Niko hiển thị PR link, test summary và checkpoint code review ngắn gọn.

## 9. Test Và Acceptance

Mục đích: Phase 4 phải chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve implementation plan checkpoint happy path.
- [x] Unit test missing approved implementation plan.
- [x] Unit test `continue-run` từ `implementation_ready` sang `implementation_written`.
- [x] Unit test Developer.act chặn file ngoài `allowed_paths`.
- [x] Unit test blocked khi target repo dirty trước Developer.act.
- [x] Unit test `continue-run` từ `implementation_written` sang `implementation_tested`.
- [x] Unit test Verification Runner `passed/failed/skipped`.
- [ ] Unit test branch/commit/PR payload formatting.
- [ ] Unit test PR description chứa đủ link/context.
- [x] Unit test test report `passed/failed/skipped`.
- [x] Unit test trace requirement-design-code-test.
- [x] Unit test repair-needed khi trace fail do thiếu test/source/code evidence.
- [x] Unit test repair-needed khi LLM code-trace review phát hiện semantic gap.
- [x] Unit test blocked khi trace fail vì lỗi không repair được ở code/test.
- [x] Unit test `continue-run` từ `implementation_repair_needed` gọi
  Developer.repair, ghi artifact v2 và pass trace khi repair thành công.
- [x] Unit test repair refresh packet từ target repo hiện tại trước khi gọi
  Developer.repair, không dùng stale `current_files`.
- [x] Unit test repair loop tôn trọng `SDLC_IMPLEMENTATION_REPAIR_LIMIT=0`.
- [x] Unit test repair loop chặn output ngoài `allowed_paths`.
- [x] Unit test repair loop block khi Verification Runner fail sau repair.
- [x] Unit test revalidate blocked repair-limit: pass luôn, reopen
  `implementation_repair_needed`, reject blocked state không đúng loại, và CLI
  dispatch `revalidate-implementation`.
- [ ] Unit test QA/Tester report `passed/failed/skipped`.
- [ ] Unit test blocked khi QA fail quá giới hạn repair.
- [ ] Unit test sync mock/local GitHub report.
- [ ] Unit test real GitHub PR create/update nếu dùng GitHub API/CLI thật.
- [ ] Smoke test CLI từ run Phase 3 thật sang `implementation_ready` cho Slice 4.1.
- [x] Live compile/runtime test target repo sau Developer.act pass khi ép JDK 21
  vào PATH tạm: `All tests passed`.
- [x] Ghi nhận và sửa lỗi test script không chịu được đường dẫn Unicode bằng cách
  chuyển sang repo-relative paths trong `SDLC_Target_eTour/scripts/test.ps1`.
- [ ] Smoke test CLI từ run Phase 3 thật sang `code_review_ready`.
- [ ] Smoke test qua Niko sau khi CLI pass.

