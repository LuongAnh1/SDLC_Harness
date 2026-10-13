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

- [ ] Tạo branch theo issue/run.
- [x] Không thao tác khi target repo dirty.
- [ ] Commit có issue key.
- [ ] Không tạo GitHub Issue.
- [ ] Có Draft PR thật; mock/local report chỉ được đánh dấu là chưa khóa Phase 4.

## 4. Developer.act

Mục đích: tạo code change có thể review, dựa trên plan đã duyệt.

- [ ] Tạo prompt/module Developer.act riêng cho Phase 4.
- [ ] Developer đọc implementation plan + approved design graph.
- [ ] Developer tạo/sửa code trong target repo.
- [ ] Sinh `code_change_report_v1.md`.
- [ ] Report liệt kê file đổi, branch, commit và reasoning chính.
- [ ] Code change đủ cụ thể để reviewer kiểm tra trong Draft PR.

## 5. Tester Và Validation

Mục đích: code output có kiểm chứng rõ ràng, kể cả khi test bị skipped.

- [ ] Tạo prompt/module Tester hoặc test runner adapter.
- [ ] Chạy build/test nếu target repo hỗ trợ.
- [ ] Sinh `test_report_v1.md`.
- [ ] Report phân biệt `passed`, `failed`, `skipped`.
- [ ] Nếu test fail, workflow không báo pass.

## 6. Code Traceability

Mục đích: chứng minh requirement/design đã đi tới code/test.

- [ ] Sinh `requirement_design_code_trace_v1.json`.
- [ ] Trace requirement -> design element -> source file/class/function.
- [ ] Trace requirement -> test file/case nếu có.
- [ ] Missing link được ghi riêng.
- [ ] Nếu trace fail nghiêm trọng, state=`blocked` với `last_error` rõ.

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

Mục đích: Niko báo được Phase 4 đang ở đâu mà không hiểu nội bộ Developer/Tester.

- [ ] Ghi state `implementation_writing`.
- [ ] Ghi state `implementation_testing`.
- [ ] Ghi state cuối `code_review_ready` khi pass.
- [ ] `latest_artifacts` trỏ tới report/trace mới nhất.
- [ ] `pending_checkpoint.type=code_review`.
- [ ] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.
- [ ] Niko hiển thị PR link, test summary và checkpoint code review ngắn gọn.

## 9. Test Và Acceptance

Mục đích: Phase 4 phải chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve implementation plan checkpoint happy path.
- [x] Unit test missing approved implementation plan.
- [ ] Unit test branch/commit/PR payload formatting.
- [ ] Unit test PR description chứa đủ link/context.
- [ ] Unit test test report `passed/failed/skipped`.
- [ ] Unit test trace requirement-design-code-test.
- [ ] Unit test blocked khi trace fail.
- [ ] Unit test sync mock/local GitHub report.
- [ ] Unit test real GitHub PR create/update nếu dùng GitHub API/CLI thật.
- [ ] Smoke test CLI từ run Phase 3 thật sang `implementation_ready` cho Slice 4.1.
- [ ] Smoke test CLI từ run Phase 3 thật sang `code_review_ready`.
- [ ] Smoke test qua Niko sau khi CLI pass.

