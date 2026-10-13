# Phase 3 Checklist

Checklist này dùng khi triển khai Phase 3: design approved -> implementation
plan. Không tick chỉ vì tài liệu đã viết xong; chỉ tick khi code/test hoặc smoke
tương ứng đã hoàn thành.

## 1. CLI/API Approval Contract

Mục đích: human/Niko có thể duyệt design checkpoint và kích hoạt Phase 3 bằng
contract JSON ổn định.

- [x] `approve-checkpoint` chỉ mở Phase 3 khi run đang `waiting_design_approval`.
- [x] Nếu checkpoint không phải `design_approval`, trả `checkpoint_not_current`.
- [x] Success trả JSON stdout, không trộn log tự do.
- [ ] Error trả JSON `ok=false`.
- [ ] `get-status` đọc được state Phase 3 mới.

## 2. Approved Design Input

Mục đích: Phase 3 dùng requirement/design đã duyệt, không tự refine hoặc design
lại.

- [x] Đọc approved `requirement_packet_v1`.
- [x] Đọc approved `design_artifact_v{n}`.
- [x] Đọc `requirement_design_trace_v{n}` và graph liên quan.
- [x] Ghi `approved_artifacts.design_artifact`.
- [x] Nếu thiếu design đã duyệt, trả lỗi rõ và không chạy Developer.

## 3. Target Repo Guard

Mục đích: Phase 4 có nơi code an toàn; Phase 3 chỉ kiểm tra và ghi target vào
plan, chưa tạo branch/commit/PR.

- [x] Có target repo path/remote ban đầu: `D:\Dự án cá nhân\Do_an_II\SDLC_Target_eTour` / `LuongAnh1/SDLC_Target_eTour`.
- [x] Có config target repo path/remote trong SDLC Harness runtime/config.
- [x] Có guard không thao tác nhầm repo `SDLC_Harness`.
- [x] Có guard worktree dirty trước khi mở plan checkpoint.
- [ ] Plan ghi rõ target repo/base branch/planned branch.

## 4. Developer.plan

Mục đích: tạo implementation plan có cấu trúc từ graph đã duyệt, không sinh code
mù và không tự sửa graph.

- [ ] Tạo prompt/module Developer riêng cho Phase 3.
- [x] Sinh `implementation_plan_v1.md`.
- [ ] Developer đọc requirement/design/trace đã duyệt.
- [ ] Plan có bảng mapping design element -> planned code target.
- [ ] Plan có test intent hoặc test command dự kiến.
- [ ] Plan có assumptions/open questions và out-of-scope.
- [ ] Plan không tạo branch, commit hoặc sửa file trong target repo.

## 5. Validator.review_plan

Mục đích: kiểm plan đủ thông tin trước khi human duyệt và trước khi Phase 4 sửa
code.

- [ ] Validator kiểm coverage: design class/method/message quan trọng có planned code target.
- [ ] Validator kiểm traceability: requirement-design trace được phản ánh vào plan.
- [ ] Validator kiểm executability: repo/branch/path/test intent đủ rõ.
- [ ] Validator kiểm scope: plan không bịa thêm ngoài requirement/design.
- [ ] Sinh `implementation_plan_review_v1.json` hoặc quality report tương đương.
- [ ] Nếu plan fail, workflow repair plan hoặc `blocked`, không mở checkpoint duyệt.

## 6. Tool Sync Phase 3

Mục đích: plan được đẩy sang đúng tool review, còn GitHub để Phase 4.

- [ ] Tạo/cập nhật Confluence page cho implementation plan.
- [ ] Jira comment ngắn có link Confluence, run id và trạng thái chờ duyệt.
- [ ] Ghi `tool_sync_report_v3.json` với `phase=implementation_plan_review`.
- [ ] Re-sync không tạo comment/page trùng nếu đã có external id.
- [ ] Partial failure không xóa artifact local.
- [ ] Không tạo GitHub branch/commit/PR trong Phase 3.

## 7. Workflow State Và Niko Reporting

Mục đích: Niko báo được Phase 3 đang ở đâu mà không hiểu nội bộ Developer.plan/
Validator.review_plan.

- [x] Ghi state `implementation_planning`.
- [x] `pending_checkpoint.type=implementation_plan_review` cho Slice 1 trước khi sửa code.
- [x] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.
- [ ] `latest_artifacts.implementation_plan` trỏ tới plan mới nhất.
- [ ] `quality_reports.implementation_plan_review` trỏ tới review mới nhất.
- [ ] Niko hiển thị link plan và checkpoint approve plan ngắn gọn.

## 8. Test Và Acceptance

Mục đích: Phase 3 phải chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve design checkpoint happy path.
- [x] Unit test missing approved design.
- [x] Unit test target repo guard.
- [ ] Unit test Developer.plan mapping format.
- [ ] Unit test Validator.review_plan missing design element.
- [ ] Unit test Validator.review_plan scope violation.
- [ ] Unit test sync implementation plan Jira/Confluence.
- [ ] Unit test blocked khi plan review fail quá giới hạn repair.
- [x] Tạo private target repo GitHub `LuongAnh1/SDLC_Target_eTour` với Java skeleton tối thiểu.
- [ ] Smoke test CLI từ run Phase 2 thật sang `waiting_implementation_plan_review`.
- [ ] Smoke test qua Niko sau khi CLI pass.

