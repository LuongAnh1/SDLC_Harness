# Phase 2 Checklist

Checklist này dùng khi bắt đầu triển khai code Phase 2. Không tick chỉ vì tài
liệu đã viết xong; chỉ tick khi code/test hoặc live smoke tương ứng đã hoàn
thành.

## 1. CLI/API Approval Contract

Mục đích: Niko/human có thể duyệt checkpoint requirement và kích hoạt Phase 2
bằng contract JSON ổn định.

- [x] Có CLI `approve-checkpoint`.
- [x] `approve-checkpoint` nhận `--run-id`, `--checkpoint-id` hoặc dùng pending checkpoint hiện tại.
- [x] CLI success trả JSON stdout, không trộn log text.
- [x] CLI error trả JSON `ok=false`.
- [x] Trả `run_not_found` nếu run không tồn tại.
- [x] Trả `no_pending_checkpoint` nếu run không có checkpoint đang chờ.
- [x] Trả `checkpoint_not_current` nếu checkpoint không khớp state hiện tại.
- [x] Chỉ cho Phase 2 chạy khi status hiện tại là `waiting_requirement_approval`.

## 2. Approved Requirement Input

Mục đích: Phase 2 dùng requirement đã duyệt, không tự kéo source hoặc refine lại.

- [x] Đọc `requirement_packet_v1.md` từ `latest_artifacts`.
- [x] Đọc `formal_use_case_v1.txt` nếu cần input máy đọc cho Designer/Validator.
- [x] Ghi `approved_artifacts.requirement_packet`.
- [x] Không gọi Requirement Refiner trong Phase 2.
- [x] Nếu artifact requirement thiếu, trả lỗi `approved_requirement_not_found`.

## 3. Designer Agent

Mục đích: sinh design có thể review và parse thành graph.

- [x] Tạo module/prompt Designer riêng cho Phase 2.
- [x] Có bước `analysis` tạo `designer_analysis_v1.md`.
- [x] Có bước `act` tạo `design_artifact_v1.md`.
- [x] Design artifact chứa class diagram và sequence diagram.
- [x] Ưu tiên PlantUML để dễ parse class/sequence graph.
- [x] Nếu LLM lỗi hoặc output không có diagram, state ghi `failed` với `last_error`.

## 4. Diagram Graph Parser

Mục đích: Validator có dữ liệu máy đọc để kiểm trace, không chỉ đọc text diagram.

- [x] Parse class diagram thành `class_diagram_graph_v1.json`.
- [x] Parse sequence diagram thành `sequence_diagram_graph_v1.json`.
- [x] Parser trả lỗi rõ khi thiếu class diagram.
- [x] Parser trả lỗi rõ khi thiếu sequence diagram.
- [x] Lưu graph JSON vào runtime artifacts ở mức parser graph V1.

## 5. Validator Traceability

Mục đích: kiểm tra requirement đã được design thể hiện đủ ở cả class và sequence.

- [x] Tạo requirement graph từ requirement packet đã duyệt.
- [x] Sinh trace requirement -> class/method.
- [x] Sinh trace requirement -> lifeline/message.
- [x] Lưu `requirement_design_trace_v1.json` ở mức `traceability_v1`.
- [x] Lưu `missing_design_links_v1.json`.
- [x] Missing link class và sequence được tách riêng.
- [x] Trace placeholder không invent target không có trong graph.
- [x] Class trace chấp nhận `ClassName.attributeName` nếu attribute tồn tại trong class graph.
- [x] Sequence trace chấp nhận return message target dùng arrow `-->`.

## 6. Validator Quality Report Và Repair Loop

Mục đích: nếu design thiếu link, Designer sửa đúng phần thiếu thay vì sinh lại mù quáng.

- [x] Validator traceability V1 tạo `design_quality_report_v1.md`.
- [x] Designer có bước `update_design(report)`.
- [x] Mỗi vòng repair tạo version mới `design_artifact_v{n}`.
- [x] Mỗi vòng repair tạo trace/missing/report version tương ứng.
- [x] Có giới hạn vòng repair rõ, mặc định 2 vòng.
- [x] Giới hạn vòng repair đọc từ config `SDLC_DESIGN_REPAIR_LIMIT` và có CLI override cho `approve-checkpoint`.
- [x] Nếu pass trước giới hạn, dừng loop.
- [x] Nếu fail sau giới hạn do design thiếu, state=`blocked`.
- [ ] Nếu fail do requirement thiếu/mơ hồ, quay về `waiting_requirement_approval` hoặc state=`blocked` với lý do rõ.

## 7. Workflow State Và Artifact Store

Mục đích: Niko có thể đọc tiến độ, checkpoint và artifact mới nhất mà không hiểu nội bộ agent.

- [x] Ghi state `design_drafting`.
- [x] Ghi state `design_validating`.
- [x] Ghi state `design_repairing` khi chạy repair.
- [x] Ghi state cuối `waiting_design_approval` khi pass.
- [x] `latest_artifacts` trỏ tới design/trace/missing/report mới nhất.
- [x] `quality_reports.design_traceability` có summary và danh sách versions.
- [x] `pending_checkpoint.type=design_approval`.
- [x] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.

## 8. Tool Sync

Mục đích: checkpoint design được đẩy lên tool bởi SDLC_Harness, không phải Niko.
Chi tiết policy nằm ở `docs/phase-2/sync-design-checkpoint.md`.

- [x] Mock sync design checkpoint tạo file local cho test.
- [x] Mock sync tạo `design-jira-comment.md`.
- [x] Mock sync tạo `design-confluence-page.md`.
- [x] Real sync cập nhật Jira comment cũ nếu có `external_id`.
- [x] Real sync tạo Jira comment mới nếu chưa có `external_id`.
- [x] Real sync cập nhật Confluence page của run nếu có `external_id`.
- [x] Real sync tạo Confluence page mới dưới parent page nếu chưa có `external_id`.
- [x] Real sync tách Class/Sequence PlantUML thành section diagram riêng trên Jira/Confluence.
- [x] Ghi rõ policy sync hiện tại: update bot-owned comment/page khi có `external_id`, không ghi đè nội dung người dùng.
- [ ] Render PlantUML thành SVG/PNG thật, upload attachment và embed vào Confluence nếu cần reviewer xem hình thay vì source code.
- [x] `tool_sync_report_v2.json` ghi `phase=design_checkpoint`.
- [x] `tool_sync_report_v2.json` ghi `mode`, `status`, `targets`, `artifact_refs`.
- [x] `state.json` ghi `last_sync.phase=design_checkpoint`.
- [x] `state.json` ghi `last_sync.status`.
- [x] `sync-run` tự nhận biết checkpoint hiện tại là requirement hay design.
- [x] Re-sync không tạo comment/page trùng nếu đã có external id.
- [x] Partial failure không xóa artifact local.
- [ ] Niko đọc `last_sync` để báo rõ sync thành công, lỗi một phần hoặc lỗi toàn bộ.

## 9. Niko Integration

Mục đích: Niko có thể duyệt requirement và báo checkpoint design bằng JSON status.

- [ ] Niko có route/prompt để gọi `approve-checkpoint`.
- [ ] Niko hiển thị summary Phase 2 ngắn, không dump toàn bộ diagram.
- [ ] Niko đọc `pending_checkpoint`, `allowed_next_actions`, `last_sync`.
- [ ] Niko báo rõ khi workflow bị `blocked` vì requirement thiếu hoặc design fail.

## 10. Test Và Acceptance

Mục đích: Phase 2 thật sự chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve checkpoint happy path.
- [x] Unit test checkpoint mismatch/no pending checkpoint.
- [x] Unit test Designer fake output tạo design artifact.
- [x] Unit test parser class/sequence diagram.
- [x] Unit test Validator phát hiện missing links.
- [x] Unit test Validator chấp nhận class attribute target.
- [x] Unit test Validator chấp nhận sequence return arrow target.
- [x] Unit test repair loop pass sau một vòng.
- [x] Unit test repair loop blocked sau giới hạn.
- [x] Unit test repair loop tôn trọng `SDLC_DESIGN_REPAIR_LIMIT=0`.
- [x] Unit test sync mock design checkpoint.
- [x] Unit test real sync design create/update Jira và Confluence.
- [x] Unit test real sync design format diagram thành code block/Confluence code macro.
- [x] Unit test partial failure real sync design.
- [x] Smoke test CLI từ run Phase 1 thật sang `waiting_design_approval`.
- [ ] Smoke test qua Niko sau khi CLI pass.

