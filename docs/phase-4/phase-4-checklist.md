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

- [ ] Sinh `requirement_design_code_trace_v1.json`.
- [ ] Trace requirement -> design element -> source file/class/function.
- [ ] Trace requirement -> test file/case nếu có.
- [ ] Missing link được ghi riêng.
- [ ] Nếu trace fail vì code/unit test thiếu so với approved design, feedback về
  Developer trong giới hạn repair.
- [ ] Nếu trace fail vì approved design mâu thuẫn/không triển khai được,
  state=`blocked` hoặc reopen design checkpoint có human approval; không tự sửa
  design trong Phase 4.

## 6.1 QA/Tester Agent Loop

Mục đích: kiểm acceptance/system behavior từ use case sau khi compile/unit và
trace baseline đã ổn. Đây mới là vai Tester/QA gần thực tế doanh nghiệp.

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
- [ ] Ghi state cuối `code_review_ready` khi pass.
- [x] `latest_artifacts` trỏ tới `test_report` mới nhất sau Verification Runner.
- [ ] `latest_artifacts` trỏ tới report/trace mới nhất sau Validator code trace.
- [ ] `latest_artifacts` trỏ tới `qa_test_report` sau QA/Tester Agent.
- [ ] `quality_reports` có traceability và QA acceptance summary.
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
- [ ] Unit test trace requirement-design-code-test.
- [ ] Unit test blocked khi trace fail.
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

