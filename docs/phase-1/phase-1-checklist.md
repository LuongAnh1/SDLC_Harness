# Phase 1 Checklist

Checklist này dùng khi bắt đầu triển khai code Phase 1. Không tick các mục dưới đây chỉ vì docs đã viết xong; chỉ tick khi code/test tương ứng đã hoàn thành.

## 1. CLI Contract

Mục đích: Niko có thể gọi SDLC Harness bằng subprocess và đọc JSON ổn định.

- [ ] Có CLI `start-or-get-run`.
- [ ] Có CLI `get-status`.
- [ ] CLI success trả JSON stdout, không trộn log text.
- [ ] CLI error trả JSON `ok=false`.
- [ ] `start-or-get-run` nhận `--dataset`, `--use-case`, `--requested-by`.
- [ ] `get-status` nhận `--run-id`.

## 2. Source Ownership Và Fixture Loader

Mục đích: Phase 1 có input độc lập, và SDLC Harness là nơi kéo/load source.

- [ ] Niko không đọc fixture/Jira/dataset trực tiếp; SDLC Harness sở hữu việc load source.
- [ ] Đọc được `fixtures/etour/UC1.txt`.
- [ ] Trả `source_not_found` nếu fixture không tồn tại.
- [ ] Trả `source_not_supported` nếu dataset/use case chưa hỗ trợ.

## 3. Workflow State

Mục đích: mỗi run có state để Niko truy vấn.

- [ ] Tạo `source_ref=dataset_use_case:eTour:UC1`.
- [ ] Nếu active run đã tồn tại thì trả run hiện tại, không refine lại.
- [ ] Tạo `run_id` duy nhất khi chưa có active run.
- [ ] Tạo `.runtime/runs/<run_id>/state.json`.
- [ ] Tạo/cập nhật `.runtime/index/active_runs.json`.
- [ ] Ghi state trung gian `requirement_drafting`.
- [ ] Ghi state cuối `waiting_requirement_approval`.
- [ ] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.

## 4. Requirement Refiner Và Artifacts

Mục đích: use case được chuẩn hóa thành requirement packet có thể duyệt.

- [ ] Gọi Requirement Refiner LLM.
- [ ] Requirement Refiner tạo nội dung requirement packet từ UC1.
- [ ] Requirement Validator tối thiểu chạy trước khi checkpoint.
- [ ] Tạo `use_case_packet_v1.md`.
- [ ] Tạo `requirement_packet_v1.md`.
- [ ] Tạo `trace_graph_v1.json`.
- [ ] `requirement_packet_v1.md` có Goal, Actors, Main Flow, Preconditions, Postconditions/Acceptance Criteria, Open Questions, Source Evidence.
- [ ] Không bịa thông tin không có trong UC1; phần thiếu đưa vào Open Questions hoặc Notes.

## 5. Mock Sync

Mục đích: chứng minh sync thuộc SDLC Harness.

- [ ] Tạo mock Jira comment file.
- [ ] Tạo mock Confluence page file.
- [ ] Tạo `tool_sync_report_v1.json`.
- [ ] `last_sync` trong status trỏ tới sync report.
- [ ] Status thể hiện `mock_synced`.

## 6. Status Cho Niko

Mục đích: Niko đọc được trạng thái và phản hồi người dùng.

- [ ] `get-status` trả `current_phase`.
- [ ] `get-status` trả `pending_checkpoint`.
- [ ] `get-status` trả `latest_artifacts`.
- [ ] `get-status` trả `allowed_next_actions`.
- [ ] `get-status` trả `summary_for_user`.
- [ ] Run không tồn tại trả `run_not_found`.

## 7. Test Và Acceptance

Mục đích: Phase 1 thật sự chạy được từ terminal.

- [ ] Unit test loader.
- [ ] Unit test Requirement Refiner adapter hoặc fake refiner.
- [ ] Unit test start-or-get-run tạo runtime files.
- [ ] Unit test start-or-get-run trả active run nếu đã tồn tại.
- [ ] Unit test get-status đọc state.
- [ ] Unit test run_not_found.
- [ ] Manual smoke test `start-or-get-run` pass.
- [ ] Manual smoke test `get-status` pass.
- [ ] Cập nhật `docs/implementation-checklist.md` sau khi code/test pass.
