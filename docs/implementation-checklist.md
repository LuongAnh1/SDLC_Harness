# Implementation Checklist

Checklist này dùng để tick tiến độ khi bắt đầu triển khai code. Mỗi nhóm công việc có mục đích riêng để tránh làm rộng mà không chứng minh được flow.

## Phase 0 - Nền Tài Liệu Và Contract

Mục đích: repo có định hướng rõ trước khi viết code.

Checklist chi tiết nằm ở `docs/phase-0/phase-0-checklist.md`. Phase 0 đã được review theo các quyết định: CLI JSON trước, eTour UC1 fixture nội bộ, runtime output trong `.runtime/`, Phase 1 mock sync nhưng trước Phase 2 cần sync API/tool thật tối thiểu.

- [x] Chốt README và AGENTS.md.
- [x] Chốt kiến trúc Niko <-> SDLC Harness.
- [x] Chốt API tối thiểu để Niko gọi.
- [x] Chốt artifact tối thiểu.
- [x] Chốt state machine ban đầu.
- [x] Chốt dataset/use case đầu tiên để thử.

## Phase 1 - Use Case Đến Requirement Packet

Mục đích: chứng minh một use case có thể thành artifact có trạng thái và checkpoint.

Checklist chi tiết nằm ở `docs/phase-1/phase-1-checklist.md`. Phase 1 code slice đã pass unit/smoke với fake LLM và đã có một lượt chạy thật DeepSeek, nhưng Phase 1 chưa khóa để mở Phase 2. Trước Phase 2 cần nối Niko gọi CLI/status và có sync API/tool thật tối thiểu.

- [x] Tạo `start-or-get-run` và loader do SDLC Harness sở hữu.
- [x] Tạo workflow run, `source_ref` và `run_id`.
- [x] Gọi Requirement Refiner adapter và sinh `requirement_packet_v1`.
- [x] Sinh `formal_use_case_v1` và `formal_check_report_v1` theo grammar TraceDev trước requirement packet.
- [x] Copy/adapt prompt scaffold cho Designer/Validator/Developer/Tester để chuẩn bị Phase 2/3.
- [x] Tách Requirement Packet thành exception, postcondition, acceptance criteria và quality/non-functional requirements.
- [x] Thêm guardrail validator để chặn exception/quality bị phân loại sai trước khi sync checkpoint.
- [x] Thêm một vòng repair cho Requirement Packet khi guardrail validator bắt lỗi phân loại.
- [x] Lưu state `waiting_requirement_approval`.
- [x] Tạo sync report mock.
- [x] Có lệnh/API `get-status(run_id)`.
- [x] Niko có thể đọc status qua CLI JSON; đã tích hợp trực tiếp trong `Niko_Agent`.

### Phase 1 Gate - Real Jira/Confluence Sync

Mục đích: khóa điều kiện trước Phase 2 bằng sync thật, không chỉ mock file.

- [x] Viết tài liệu triển khai real sync: `docs/phase-1/real-sync-implementation.md`.
- [x] Anh tạo/chọn Jira project và issue target cho `eTour UC1`.
- [x] Anh tạo/chọn Confluence space và parent page target.
- [x] Anh tạo Atlassian API token và điền `.env` cục bộ.
- [x] Thêm config sync thật trong SDLC Harness.
- [x] Thêm Jira comment adapter.
- [x] Thêm Confluence page adapter.
- [x] Thêm CLI `sync-run` để sync lại run đã có.
- [x] Ghi `tool_sync_report_v1.json` với mode/id/url thật.
- [x] Chạy smoke terminal với sync thật.
- [x] Chạy smoke qua Niko sau sync thật.
- [x] Cải thiện format Jira/Confluence sync thành checkpoint dễ review.
- [x] Re-sync Jira cập nhật comment cũ khi đã có `external_id`, tránh sinh comment lặp.
- [x] Tách hướng dẫn vận hành CLI sang `docs/phase-1/operator-guide.md`.
- [x] Thêm CLI `start-from-jira` và source loader Jira issue ở mức unit test.
- [x] Tích hợp Niko route `sdlc_start_from_jira` ở mức unit test.
- [x] Chạy smoke live `start-from-jira --issue-key SDLC-1` với Jira issue có nội dung use case thật.
- [x] Chạy smoke live qua Niko bằng prompt thực hiện issue Jira.
- [ ] Rerun live sau schema Requirement Packet mới để xác nhận Jira/Confluence checkpoint không còn trộn exception/quality vào acceptance/postcondition.
- [x] Cập nhật `docs/phase-1/progress.md` với run id và URL thật.

## Phase 2 - Requirement Packet Đến Design

Mục đích: sau khi duyệt requirement, tạo thiết kế có trace và checkpoint.

Checklist chi tiết nằm ở `docs/phase-2/phase-2-checklist.md`. Phase 2 đã có CLI
`approve-checkpoint`, Designer Agent fake/LLM-ready, parser PlantUML
class/sequence graph, Validator traceability V1, mock `tool_sync_report_v2` và
repair loop Designer.update_design với artifact versioning. Real sync Phase 2 đã
có unit test cho Jira/Confluence create/update/partial failure; live smoke
Atlassian Phase 2 và Niko route duyệt checkpoint đã chạy được tới
`waiting_design_approval`.

- [x] Có `approve_checkpoint(run_id)`.
- [x] Đọc approved `requirement_packet_v1`, không refine requirement lại trong Phase 2.
- [x] Đọc kèm `formal_use_case_v1` khi có để Designer/Validator dùng input máy đọc.
- [x] Sinh `design_artifact_v1`.
- [x] Parse PlantUML thành `class_diagram_graph_v1` và `sequence_diagram_graph_v1`.
- [x] Sinh trace requirement-design ở mức `traceability_v1`.
- [x] Validator phát hiện missing links cơ bản.
- [x] Validator chấp nhận trace target tới attribute thật trong class graph.
- [x] Validator traceability V1 sinh missing-link report và quality report cho Designer/human.
- [x] Designer repair design theo Validator report với giới hạn vòng lặp rõ.
- [x] `SDLC_DESIGN_REPAIR_LIMIT` cấu hình được số vòng repair.
- [x] Sync Confluence/Jira mock theo `docs/phase-2/sync-design-checkpoint.md`.
- [x] Sync Confluence/Jira real theo `docs/phase-2/sync-design-checkpoint.md` ở mức unit test.
- [x] Tách Class/Sequence PlantUML thành section diagram riêng khi sync Phase 2.
- [x] Re-sync design checkpoint không tạo Jira comment/Confluence page trùng khi có `external_id`.
- [x] Chú thích rõ sync hiện tại là update artifact do harness sở hữu, không ghi đè nội dung người dùng.
- [x] Nhúng PlantUML app macro trên Confluence cho Class/Sequence diagram.
- [x] Bỏ SVG attachment/image fallback khỏi Phase 2 real sync sau khi PlantUML app macro chạy ổn.
- [x] Chốt Mermaid là backlog không chặn Phase 3; Phase 2 dùng PlantUML app macro.
- [x] Niko đọc `last_sync` để báo trạng thái sync Phase 2, gồm cả partial/full failure.
- [x] State chuyển sang `waiting_design_approval`.

## Phase 3 - Design Đến Implementation Plan

Mục đích: sau khi duyệt design, tạo implementation plan như một checkpoint riêng
trước khi sửa code.

Checklist chi tiết nằm ở `docs/phase-3/phase-3-checklist.md`. Phase 3 không tạo
branch/code/commit/Draft PR. Nó chỉ ánh xạ approved requirement/design/trace
graph sang kế hoạch triển khai dự kiến để human/Niko duyệt.

- [x] Viết tài liệu triển khai Phase 3 trong `docs/phase-3/`.
- [x] Duyệt design checkpoint để mở Phase 3.
- [x] Đọc approved requirement/design/trace graph.
- [x] Kiểm tra target repo path/remote/dirty worktree.
- [x] Sinh `implementation_plan_v1.md`.
- [ ] Developer.plan sinh plan có bảng mapping design -> code target.
- [ ] Validator.review_plan kiểm coverage/traceability/executability/scope.
- [ ] Sinh plan review report hoặc quality report cho implementation plan.
- [ ] Sync implementation plan lên Confluence và comment trạng thái lên Jira.
- [ ] State dừng ở `waiting_implementation_plan_review`.

## Phase 4 - Implementation Plan Đến Code/Test/Draft PR

Mục đích: sau khi duyệt implementation plan, triển khai code thật và tạo
checkpoint code review.

Checklist chi tiết nằm ở `docs/phase-4/phase-4-checklist.md`.

- [x] Chốt target test repo GitHub: `LuongAnh1/SDLC_Target_eTour`.
- [ ] Duyệt `implementation_plan_review` để mở Phase 4.
- [ ] Tạo branch trong target repo.
- [ ] Developer.act đọc implementation plan + approved design graph.
- [ ] Sinh code change thật trong target repo.
- [ ] Sinh `code_change_report_v1.md`.
- [ ] Tester chạy compile/test hoặc ghi skipped rõ ràng.
- [ ] Sinh `test_report_v1.md`.
- [ ] Validator sinh `requirement_design_code_trace_v1.json`.
- [ ] Tạo Draft PR thật trên GitHub.
- [ ] Sync PR/test/trace summary về Jira và Confluence.
- [ ] State chuyển sang `code_review_ready`.
- [ ] Live run chứng minh chuỗi Requirement Refiner -> Designer/Validator -> Developer/Tester/Validator trong phạm vi một use case.

## Phase 5 - Sửa Tại Checkpoint

Mục đích: người dùng có thể yêu cầu sửa và hệ thống rerun đúng đoạn.

- [ ] Có `request_change(run_id, message)`.
- [ ] Phân loại change target: requirement/design/implementation plan/code.
- [ ] Tạo artifact version mới.
- [ ] Đánh dấu artifact phía sau là stale nếu cần.
- [ ] Chạy lại Validator.
- [ ] Sync lại tool/mock tool.

## Phase 6 - Context Động

Mục đích: context ngoài checkpoint có thể làm workflow đổi trạng thái.

- [ ] Import Jira comment/update mới.
- [ ] Import Confluence update mới.
- [ ] Import PR review/update mới.
- [ ] Phân loại thay đổi.
- [ ] Invalidate phase phù hợp.
- [ ] Niko báo người dùng biết cần duyệt/chạy lại.

## Phase 7 - Nhiều Use Case Và Impact Analysis

Mục đích: mở rộng từ một use case độc lập sang quan hệ chéo.

- [ ] Lưu use case -> component/class links.
- [ ] Phát hiện shared code artifacts.
- [ ] Tạo impact graph tối thiểu.
- [ ] Khi một use case đổi, liệt kê use case có thể bị ảnh hưởng.
- [ ] Chạy regression/trace check cho nhóm liên quan.







