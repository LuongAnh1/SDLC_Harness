# Phase 3 Checklist

Checklist này dùng khi bắt đầu triển khai code Phase 3. Không tick chỉ vì tài
liệu đã viết xong; chỉ tick khi code/test hoặc live smoke tương ứng đã hoàn
thành.

## 1. CLI/API Approval Contract

Mục đích: human/Niko có thể duyệt design checkpoint và kích hoạt Phase 3 bằng
contract JSON ổn định.

- [ ] `approve-checkpoint` chỉ mở Phase 3 khi run đang `waiting_design_approval`.
- [ ] Nếu checkpoint không phải `design_approval`, trả `checkpoint_not_current`.
- [ ] Success trả JSON stdout, không trộn log tự do.
- [ ] Error trả JSON `ok=false`.
- [ ] `get-status` đọc được state Phase 3 mới.

## 2. Approved Design Input

Mục đích: Phase 3 dùng requirement/design đã duyệt, không tự refine hoặc design
lại.

- [ ] Đọc approved `requirement_packet_v1`.
- [ ] Đọc approved `design_artifact_v{n}`.
- [ ] Đọc `requirement_design_trace_v{n}` và graph liên quan.
- [ ] Ghi `approved_artifacts.design_artifact`.
- [ ] Nếu thiếu design đã duyệt, trả lỗi rõ và không chạy Developer.

## 3. Target Repo Và GitHub Policy

Mục đích: code sinh ra có nơi lưu trữ/review thật, tách khỏi repo harness.

- [x] Có target repo path/remote ban đầu: `D:\Dự án cá nhân\Do_an_II\SDLC_Target_eTour` / `LuongAnh1/SDLC_Target_eTour`.
- [ ] Có config target repo path/remote trong SDLC Harness runtime/config.
- [ ] Có guard không thao tác nhầm repo `SDLC_Harness`.
- [ ] Tạo branch theo issue/run.
- [ ] Commit có issue key.
- [ ] Không tạo GitHub Issue.
- [ ] Có Draft PR thật; mock/local report chỉ được đánh dấu là chưa khóa Phase 3.

## 4. Developer Agent

Mục đích: tạo implementation plan và code change có thể review, không sinh code
mù.

- [ ] Tạo prompt/module Developer riêng cho Phase 3.
- [ ] Sinh `implementation_plan_v1.md`.
- [ ] Developer đọc requirement/design/trace đã duyệt.
- [ ] Developer tạo/sửa code trong target repo.
- [ ] Sinh `code_change_report_v1.md`.
- [ ] Report liệt kê file đổi, branch, commit và reasoning chính.
- [ ] Code change đủ cụ thể để reviewer kiểm tra trong Draft PR, không chỉ là artifact mô tả.

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

## 7. Tool Sync Phase 3

Mục đích: context implementation được đẩy sang GitHub và sync ngược về Jira/
Confluence bởi SDLC Harness.

- [ ] Tạo hoặc cập nhật Draft PR trên GitHub.
- [ ] PR description có Jira link, Confluence link, `run_id`, summary, tests và trace.
- [ ] Sync Jira comment với PR link/status.
- [ ] Sync Confluence code review checkpoint.
- [ ] Ghi `tool_sync_report_v3.json` với `phase=code_review`.
- [ ] Re-sync không tạo PR/comment/page trùng nếu đã có external id.
- [ ] Partial failure không xóa artifact local.

## 8. Workflow State Và Niko Reporting

Mục đích: Niko báo được Phase 3 đang ở đâu mà không hiểu nội bộ Developer/Tester.

- [ ] Ghi state `implementation_planning`.
- [ ] Ghi state `implementation_writing`.
- [ ] Ghi state `implementation_testing`.
- [ ] Ghi state cuối `code_review_ready` khi pass.
- [ ] `latest_artifacts` trỏ tới plan/report/trace mới nhất.
- [ ] `pending_checkpoint.type=code_review`.
- [ ] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.
- [ ] Niko hiển thị PR link, test summary và checkpoint code review ngắn gọn.

## 9. Test Và Acceptance

Mục đích: Phase 3 phải chạy được từ terminal trước khi live qua Niko.

- [ ] Unit test approve design checkpoint happy path.
- [ ] Unit test missing approved design.
- [ ] Unit test target repo guard.
- [ ] Unit test branch/commit/PR payload formatting.
- [ ] Unit test PR description chứa đủ link/context.
- [ ] Unit test test report `passed/failed/skipped`.
- [ ] Unit test trace requirement-design-code-test.
- [ ] Unit test blocked khi trace fail.
- [ ] Unit test sync mock/local GitHub report.
- [ ] Unit test real GitHub PR create/update nếu dùng GitHub API/CLI thật.
- [x] Tạo private target repo GitHub `LuongAnh1/SDLC_Target_eTour` với Java skeleton tối thiểu.
- [ ] Smoke test CLI từ run Phase 2 thật sang `code_review_ready`.
- [ ] Smoke test qua Niko sau khi CLI pass.
- [ ] Live run chứng minh đủ chuỗi Requirement Refiner -> Designer/Validator -> Developer/Tester/Validator trong phạm vi một use case.

