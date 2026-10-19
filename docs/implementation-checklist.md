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

Checklist chi tiết nằm ở `docs/phase-1/phase-1-checklist.md`. Phase 1 đã hoàn
thành và được mở rộng qua Requirement V2; Niko hiện đọc status qua HTTP API,
trong khi CLI được giữ cho operator/debug.

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
- [x] Niko có thể đọc status qua HTTP API; CLI JSON vẫn có regression độc lập.
- [ ] Chặn approval khi Requirement Packet còn open question ảnh hưởng business
  rule/acceptance; mở checkpoint clarification và lưu decision provenance trước
  khi sang Design. Chi tiết:
  `docs/phase-1/requirement-v2/README.md` và
  `docs/phase-1/requirement-v2/checklist.md`.

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
- [x] Developer.plan sinh plan có bảng mapping design -> code target.
- [x] Validator.review_plan kiểm coverage/traceability/executability/scope.
- [x] Validator.review_plan chạy hybrid: rule hard gate + LLM semantic/content review.
- [x] `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT` cấu hình số vòng repair implementation plan.
- [x] Developer.repair_plan tạo artifact version mới khi Validator yêu cầu sửa.
- [x] Sinh plan review report hoặc quality report cho implementation plan.
- [x] Sync implementation plan lên Confluence và comment trạng thái lên Jira.
- [x] State dừng ở `waiting_implementation_plan_review`.

## Phase 4 - Implementation Plan Đến Code/Test/Draft PR

Mục đích: sau khi duyệt implementation plan, triển khai code thật và tạo
checkpoint code review.

Checklist chi tiết nằm ở `docs/phase-4/phase-4-checklist.md`.

- [x] Chốt target test repo GitHub: `LuongAnh1/SDLC_Target_eTour`.
- [x] Duyệt `implementation_plan_review` để mở Phase 4 Slice 4.1.
- [x] Sinh `developer_work_packet_v1.json` từ implementation plan đã duyệt.
- [x] State chuyển sang `implementation_ready` trước khi Developer.act sửa code.
- [x] Tạo branch trong target repo.
- [x] Developer.act đọc work packet từ implementation plan + approved design graph.
- [x] Sinh code change thật trong target repo.
- [x] Sinh `code_change_report_v1.md`.
- [x] Verification Runner chạy compile/unit test hoặc ghi skipped rõ ràng.
- [x] Sinh `test_report_v1.md`.
- [x] Validator sinh `requirement_design_code_trace_v1.json` từ approved requirement/design sang code/unit test.
- [x] Code trace bắt buộc chạy LLM/DeepSeek graph review sau static evidence; nếu
  LLM phát hiện semantic gap do code/test thì dừng ở `implementation_repair_needed`
  dù rule-based pass.
- [x] Nếu trace fail vì code/test thiếu, ghi feedback máy đọc được cho Developer
  trước; repair loop thật dùng feedback này ở Slice 4.6.
- [x] Nếu trace repair feedback đã có, Developer repair trong `allowed_paths` theo
  giới hạn cấu hình; sau repair phải chạy lại Verification Runner -> Validator
  trước khi QA/Tester được chạy.
- [x] Trước mỗi vòng Developer repair, refresh `developer_work_packet_v{n}` từ target
  repo hiện tại để Developer không dùng stale `current_files`.
- [ ] Nếu compile/unit fail trước khi có trace feedback, feedback quay về Developer
  thay vì block ngay.
- [ ] Nếu approved design mâu thuẫn/không triển khai được, block hoặc reopen design checkpoint có human approval; không tự sửa design trong Phase 4.
- [x] QA/Tester Agent kiểm acceptance/system behavior từ use case sau khi unit/trace
  baseline đã pass ổn định sau repair loop nếu có.
- [x] Sinh `qa_test_report_v1.md` trước khi mở checkpoint code review.
- [x] Tạo Draft PR thật trên GitHub.
- [x] Triển khai, unit test và live-test sync PR/test/trace/QA summary về Jira +
  Confluence; re-sync giữ nguyên external ID.
- [x] State chỉ chuyển sang `code_review_ready` sau khi unit/trace/QA pass, Draft
  PR tồn tại và sync Phase 4 thành công.
- [ ] Tự phát hiện capability/giới hạn output của model, tính structured-output
  budget theo context còn lại và giữ config fallback khi discovery không khả
  dụng. Chi tiết:
  `docs/maintenance/2026-10-15-e2e-v1-follow-up-backlog.md`.
- [x] Live run chứng minh chuỗi Requirement Refiner -> Designer/Validator ->
  Developer/Verification Runner/Validator/QA Tester -> Draft PR trong phạm vi một
  use case; live sync Code Review cũng đã đạt `code_review_ready`.

### Phase 4 Gate - Run Workspace Isolation Và Code Review Approval

Mục đích: cô lập code lineage theo run trước khi cho phép request-change. Chi tiết
nằm tại `docs/phase-4/workspace-isolation/`.

- [x] Viết kiến trúc, runtime contract, migration và checklist worktree-per-run.
- [x] Tạo `run_workspace_v1.json` và workspace manager.
- [x] Mọi action Phase 4 resolve đúng worktree theo `run_id`.
- [x] Branch mới chứa run suffix và bắt đầu từ `origin/main` vừa fetch.
- [x] Adopt run hiện tại/PR #1 mà không đổi HEAD.
- [x] Thêm explicit cleanup cho merged/cancelled run; không xóa remote branch.
- [x] Approve Code Review chỉ chuyển Draft PR thành Ready for review.
- [x] Có guard/test chứng minh Harness không merge hoặc bật auto-merge.

## Platform Upgrade - API Và Ops Dashboard

Checklist chi tiết nằm tại `docs/platform-api-dashboard/implementation-checklist.md`.
Đây là mốc nền tảng ưu tiên sau Phase 4 và trước Phase 5 `request_change`.

- [x] Chốt architecture, API, operation/event, dashboard/config và Niko cutover docs.
- [x] Tách application service dùng chung cho CLI/API.
- [x] Thêm Read API cho health, run, artifact whitelist và dashboard snapshot.
- [x] Thêm write API với operation queue và idempotency.
- [x] Thêm event timeline và Ops Dashboard.
- [x] Thêm runtime config có schema, mask secret và precedence rõ.
- [x] Nối Niko dual transport, chạy contract parity và controlled live E2E.
- [x] Chuyển Niko sang API-only; giữ CLI Harness cho debug/operator.

## Phase 5 - Sửa Tại Checkpoint

Mục đích: người dùng có thể yêu cầu sửa và hệ thống rerun đúng đoạn.

- [ ] Có `request_change(run_id, message)`.
- [ ] Phân loại change target: requirement/design/implementation plan/code.
- [ ] Request change tại Code Review tiếp tục trên cùng worktree/branch/PR.
- [ ] Nếu PR đã Ready, chuyển về Draft trước khi chạy vòng sửa.
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

## Nhật Ký Mốc Lịch Sử

> Các cập nhật bên dưới giữ nguyên trạng thái tại thời điểm ghi. Mốc hiện hành là
> bản cập nhật cuối cùng ngày 2026-10-15.

> Cập nhật 2026-10-13: Phase 3 đã pass live qua Telegram/Niko với run
> `run_20261013_160421_f661babf`. Run dừng ở
> `waiting_implementation_plan_review`, artifact cuối `implementation_plan_v4`,
> sync Jira/Confluence `synced`.
>
> Cập nhật 2026-10-14: Phase 4 hiện đã chạy tới `qa_tested`: duyệt
> implementation plan thành DeveloperWorkPacket, tạo branch target repo,
> Developer.act sinh/apply code, Verification Runner pass compile/unit test,
> Validator code trace hybrid rule + LLM, Developer.repair loop, refresh packet
> trước mỗi repair và `revalidate-implementation` cho run từng bị block vì repair
> limit. QA/Tester đã sinh và execute acceptance test thật; live DeepSeek đã xác
> nhận vòng QA fail -> Developer repair -> Runner -> Validator -> QA pass. Các mục
> còn lại trước khi khóa Phase 4 là compile/unit feedback quay về Developer khi
> fail sớm, nhánh block/reopen design khi design nền sai, QA `skipped` hardening,
> commit/Draft PR thật, sync Phase 4 và `code_review_ready`.
>
> Cập nhật 2026-10-15: Slice 4.9A-D đã hoàn tất ở mức code và test. Approval
> kiểm PR/workspace identity, chuyển Draft sang Ready theo hướng idempotent, ghi
> `github_code_review_approval_v1.json`, sync Jira/Confluence và retry sync mà
> không chạy lại GitHub operation. Full regression: `188 passed`. Live approval
> PR #1 chưa chạy; merge vẫn do human trực tiếp trên GitHub.
>
> Cập nhật hiện hành 2026-10-15: controlled live smoke đã chạy qua Niko cho run
> `run_20261013_160421_f661babf`. Run đạt `code_review_approved`; PR #1 đã chuyển
> sang Ready for review, vẫn OPEN/chưa merge; Jira comment `10104` và Confluence
> page `2129921` đã nhận trạng thái sau approval. Regression gần nhất: SDLC
> Harness `192 passed`, Niko Agent `299 passed`.
>
> Cập nhật E2E-V1-005 ngày 2026-10-15: Validator code trace đã được sửa để
> phân loại rõ blocking finding và observation, đối chiếu evidence cục bộ với
> requirement/design/plan đã duyệt, không gửi thêm toàn bộ artifact ra LLM, và
> không cho phép feedback repair trở thành requirement mới. Các run bị block bởi
> `validator_evidence_or_mapping_issue` hoặc lỗi structured output có thể chạy
> `revalidate-implementation` sau khi logic Validator được sửa.
