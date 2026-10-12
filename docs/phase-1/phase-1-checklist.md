# Phase 1 Checklist

Checklist này dùng khi bắt đầu triển khai code Phase 1. Không tick các mục dưới đây chỉ vì docs đã viết xong; chỉ tick khi code/test tương ứng đã hoàn thành.

## 1. CLI Contract

Mục đích: Niko có thể gọi SDLC Harness bằng subprocess và đọc JSON ổn định.

- [x] Có CLI `start-or-get-run`.
- [x] Có CLI `get-status`.
- [x] CLI success trả JSON stdout, không trộn log text.
- [x] CLI error trả JSON `ok=false`.
- [x] `start-or-get-run` nhận `--dataset`, `--use-case`, `--requested-by`.
- [x] `get-status` nhận `--run-id`.

## 2. Source Ownership Và Fixture Loader

Mục đích: Phase 1 có input độc lập, và SDLC Harness là nơi kéo/load source.

- [x] Niko không đọc fixture/Jira/dataset trực tiếp; SDLC Harness sở hữu việc load source.
- [x] Đọc được `fixtures/etour/UC1.txt`.
- [x] Trả `source_not_found` nếu fixture không tồn tại.
- [x] Trả `source_not_supported` nếu dataset/use case chưa hỗ trợ.

## 3. Workflow State

Mục đích: mỗi run có state để Niko truy vấn.

- [x] Tạo `source_ref=dataset_use_case:eTour:UC1`.
- [x] Nếu active run đã tồn tại thì trả run hiện tại, không refine lại.
- [x] Tạo `run_id` duy nhất khi chưa có active run.
- [x] Tạo `.runtime/runs/<run_id>/state.json`.
- [x] Tạo/cập nhật `.runtime/index/active_runs.json`.
- [x] Ghi state trung gian `requirement_drafting`.
- [x] Ghi state cuối `waiting_requirement_approval`.
- [x] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.

## 4. Requirement Refiner Và Artifacts

Mục đích: use case được chuẩn hóa thành requirement packet có thể duyệt.

- [x] Gọi Requirement Refiner LLM.
- [x] Requirement Refiner tạo nội dung requirement packet từ UC1.
- [x] Requirement Validator tối thiểu chạy trước khi checkpoint.
- [x] Requirement Refiner tạo `formal_use_case_v1.txt` theo grammar TraceDev trước khi sinh requirement packet.
- [x] Formal Use Case Validator kiểm tra grammar tối thiểu và lưu `formal_check_report_v1.json`.
- [x] Có repair prompt một lượt khi formal use case chưa đạt kiểm tra tối thiểu.
- [x] Tạo `use_case_packet_v1.md`.
- [x] Tạo `requirement_packet_v1.md`.
- [x] Tạo `trace_graph_v1.json`.
- [x] `requirement_packet_v1.md` có Goal, Actors, Main Flow, Preconditions, Alternative/Exception Flows, Postconditions, Acceptance Criteria, Quality/Non-functional Requirements, Open Questions, Source Evidence.
- [x] Không bịa thông tin không có trong UC1; phần thiếu đưa vào Open Questions hoặc Notes.
- [x] Requirement Validator chặn lỗi thô khi exception bị nhét vào Postconditions/Acceptance Criteria.
- [x] Requirement Validator chặn lỗi thô khi quality/non-functional requirement bị nhét vào Postconditions/Acceptance Criteria.
- [x] Requirement Validator chặn `Source Evidence` dùng văn bản formal use case do model sinh ra thay vì nguồn gốc ban đầu.
- [x] Requirement Refiner repair một vòng khi `requirement_packet_v1` fail validator.
- [x] Nếu repair vẫn fail, run ghi `status=failed` và `last_error` để Niko/operator tra lại.

## 5. Mock Sync

Mục đích: chứng minh sync thuộc SDLC Harness.

- [x] Tạo mock Jira comment file.
- [x] Tạo mock Confluence page file.
- [x] Tạo `tool_sync_report_v1.json`.
- [x] `last_sync` trong status trỏ tới sync report.
- [x] Status thể hiện `mock_synced`.

## 6. Status Cho Niko

Mục đích: Niko đọc được trạng thái và phản hồi người dùng.

- [x] `get-status` trả `current_phase`.
- [x] `get-status` trả `pending_checkpoint`.
- [x] `get-status` trả `latest_artifacts`.
- [x] `get-status` trả `allowed_next_actions`.
- [x] `get-status` trả `summary_for_user`.
- [x] Run không tồn tại trả `run_not_found`.

## 7. Test Và Acceptance

Mục đích: Phase 1 thật sự chạy được từ terminal.

- [x] Unit test loader.
- [x] Unit test Requirement Refiner adapter hoặc fake refiner.
- [x] Unit test Formal Use Case Validator pass/fail.
- [x] Unit test prompt scaffold TraceDev cho Designer/Validator/Developer/Tester tồn tại.
- [x] Unit test start-or-get-run tạo runtime files.
- [x] Unit test start-or-get-run trả active run nếu đã tồn tại.
- [x] Unit test get-status đọc state.
- [x] Unit test run_not_found.
- [x] Manual smoke test `start-or-get-run` pass.
- [x] Manual smoke test `get-status` pass.
- [x] Cập nhật `docs/implementation-checklist.md` sau khi code/test pass.

## Ghi chú triển khai 2026-10-12

- Đã triển khai package `sdlc_harness` với CLI JSON `start-or-get-run` và `get-status`.
- Đã có fake LLM để test không tốn API, đồng thời có adapter `deepseek`/`openai_compatible` dùng Chat Completions cho bước gọi thật sau khi điền `.env`.
- Smoke test fake LLM pass; DeepSeek thật cũng đã chạy ngày 2026-10-12 với run `run_20261012_122724_235baa90`, state cuối `waiting_requirement_approval`, Validator pass.
- Sync Phase 1 vẫn là mock file theo đúng quyết định Phase 0; sync API/tool thật tối thiểu là điều kiện trước Phase 2.

## Ghi chú chỉnh Requirement Packet 2026-10-13

- Live run từ Jira đã cho thấy schema cũ gộp `Postconditions / Acceptance Criteria` làm model dễ đưa nhánh hủy/kết nối lỗi/quality vào sai vùng.
- Đã tách requirement packet thành các vùng riêng: exception, postcondition, acceptance và quality.
- Đã thêm guardrail validator để bắt các lỗi phân loại thô trước khi sync checkpoint.
- Live test đầu tiên sau schema mới bị validator chặn đúng lỗi `connection interruption/cancel` nằm trong Acceptance Criteria.
- Đã thêm repair loop cho requirement packet để model có cơ hội tự sửa theo `ValidationReport` trước khi trả lỗi ra bot.
- Cần rerun live `start-from-jira` hoặc qua Niko để anh review artifact thật sau repair loop.
