# Phase 2 Progress

Tài liệu này ghi lại tiến độ triển khai Phase 2 theo từng lát nhỏ. Không đánh
dấu một mục là xong nếu mới chỉ có tài liệu mà chưa có code/test tương ứng.

## 2026-10-16 - Requirement V2 Dual Read

Đã làm:

- Thêm reader requirement input V1/V2 trước khi Phase 2 mutate state.
- V2 đọc canonical model làm source of truth và xác minh revision, source
  fingerprint, validation cùng packet/formal projections.
- Designer nhận canonical JSON trước review projections.
- Validator dựng graph từ canonical V2 và giữ stable requirement IDs trong trace.
- Artifact Phase 2 ghi mode `traceability_v2`, contract version, revision và refs.
- Run V2 đi tới `waiting_design_approval`. Slice 4B sau đó đã mở Phase 3 bằng
  frozen canonical reader và chuyển boundary sang Phase 4.

Phạm vi chưa mở:

- Phase 4 canonical work packet và stable ID propagation.
- Niko/Telegram thay đổi và live DeepSeek/Jira smoke.

Đã kiểm thử:

- Regression Phase 1-3: `114 passed`.
- Full suite SDLC Harness: `243 passed` trong `291.65s`.
- Sau đó bổ sung test repair loop V2: file dual-reader/V2 `11 passed`; nhóm
  Requirement V2 + Phase 2 `56 passed`.
- `git diff --check`: pass.

## 2026-10-13 - V0 Approve Requirement Sang Design Checkpoint

Đã làm:

- Thêm CLI `approve-checkpoint --run-id ... [--checkpoint-id ...]`.
- Thêm workflow Phase 2 V0 để chỉ chạy khi run đang ở
  `waiting_requirement_approval`.
- Đọc `latest_artifacts.requirement_packet` làm approved requirement input.
- Ghi `approved_artifacts.requirement_packet` vào state.
- Sinh bộ artifact design V0 bằng fake/deterministic Designer/Validator:
  `designer_analysis_v1.md`, `design_artifact_v1.md`,
  `class_diagram_graph_v1.json`, `sequence_diagram_graph_v1.json`,
  `requirement_design_trace_v1.json`, `missing_design_links_v1.json`,
  `design_quality_report_v1.md`.
- Tạo mock sync Phase 2: `design-jira-comment.md`,
  `design-confluence-page.md`, `tool_sync_report_v2.json`.
- Chuyển state sang `current_phase=design_review`,
  `status=waiting_design_approval`, `pending_checkpoint.type=design_approval`.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test*.py" -v`: 34 test pass.
- Smoke CLI với runtime tạm:
  `start-or-get-run --llm-mode fake` rồi
  `approve-checkpoint --llm-mode fake` trả
  `approve_status=waiting_design_approval`, `sync_status=mock_synced`.

Chưa làm trong lát này:

- Chưa gọi LLM Designer thật.
- Chưa parse PlantUML thành graph thật.
- Chưa chạy Validator traceability thật theo TraceDev.
- Chưa có repair loop Designer.update_design.
- Chưa real sync Jira/Confluence cho design checkpoint.
- Chưa nối Niko route duyệt checkpoint sang CLI `approve-checkpoint`.

## 2026-10-13 - V1 Designer Agent Và PlantUML Parser

Đã làm:

- Thêm `DesignerAgent` cho Phase 2 với hai bước TraceDev-style:
  `analysis(requirement/formal_use_case)` và `act(requirement/formal_use_case,
  architecture_recommendation)`.
- Dùng lại prompt Designer/Recommendation đã copy từ TraceDev; fake mode mô phỏng
  output của hai bước này để test không tốn API.
- Phase 2 đọc thêm `formal_use_case_v1.txt` nếu artifact này có trong
  `latest_artifacts`, rồi ghi vào `approved_artifacts`.
- Thêm parser PlantUML cho class diagram và sequence diagram:
  `class_diagram_graph_v1.json`, `sequence_diagram_graph_v1.json`.
- Sửa workflow để có state trung gian `design_validating`.
- Nếu Designer/LLM/parser lỗi, state chuyển `failed` và ghi `last_error`.
- Artifact trace/missing/report đổi sang mode `traceability_pending_v1` hoặc
  `parser_only_v1`, không còn ghi giả `missing links=0`.
- Phát hiện và sửa lỗi parser: alias `Boundary` từng bị nhận nhầm là keyword
  `boundary`, khiến message bắt đầu từ `Boundary -> ...` bị bỏ qua.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test*.py" -v`: 37 test pass.
- Smoke CLI runtime tạm:
  `start-or-get-run --llm-mode fake` rồi
  `approve-checkpoint --llm-mode fake` trả
  `status=waiting_design_approval`, `current_phase=design_review`,
  `quality_mode=parser_only_v1`, `sync_status=mock_synced`.

Chưa làm trong lát này:

- Chưa chạy Validator traceability LLM để nối requirement -> class/method và
  requirement -> lifeline/message.
- Chưa có repair loop `Designer.update_design`.
- Chưa real sync idempotent cho design checkpoint.
- Chưa nối Niko route duyệt checkpoint sang CLI `approve-checkpoint`.

## 2026-10-13 - V2 Validator Traceability

Đã làm:

- Thêm `ValidatorAgent` cho Phase 2.
- Parse `requirement_packet_v1.md` thành `requirement_graph_v1.json` với ID ổn
  định như `REQ-MAIN-001`, `REQ-AC-001`, `REQ-NFR-001`.
- Gọi prompt `traceability_class.txt` để sinh trace requirement -> class/method.
- Gọi prompt `traceability_sequence.txt` để sinh trace requirement ->
  lifeline/message.
- Validate trace output là JSON array và không cho LLM invent target ngoài
  class/sequence graph đã parse.
- Ghi `requirement_design_trace_v1.json` ở mode `traceability_v1`, gồm
  `class_trace` và `sequence_trace`.
- Ghi `missing_design_links_v1.json` với `missing_count` thật.
- `quality_reports.design_traceability` chuyển từ `parser_only_v1` sang
  `traceability_v1`; `ok=false` nếu còn missing links.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test*.py" -v`: 42 test pass.
- Unit test cho requirement graph, traceability fake pass, JSON lỗi, target bịa
  và missing links rỗng.

Chưa làm trong lát này:

- Chưa có repair loop `Designer.update_design`.
- Chưa chuyển state `design_repairing`.
- Chưa real sync idempotent cho design checkpoint.
- Chưa nối Niko route duyệt checkpoint sang CLI `approve-checkpoint`.

## 2026-10-13 - V3 Designer Repair Loop

Đã làm:

- Nối `Designer.update_design(...)` vào prompt `update_designer.txt` đã có.
- Khi Validator báo missing link, workflow chuyển state sang
  `design_repairing` và đưa `design_quality_report_v{n}.md` ngược lại cho
  Designer.
- Mỗi vòng validate/repair ghi artifact version riêng:
  `design_artifact_v{n}.md`, `class_diagram_graph_v{n}.json`,
  `sequence_diagram_graph_v{n}.json`, `requirement_design_trace_v{n}.json`,
  `missing_design_links_v{n}.json`, `design_quality_report_v{n}.md`.
- Giới hạn repair mặc định là 2 vòng.
- Nếu traceability pass sau repair, workflow mở `design_approval` checkpoint ở
  version mới nhất.
- Nếu vẫn còn missing sau giới hạn, workflow chuyển `status=blocked`, không mở
  pending checkpoint design và giữ latest artifacts ở version cuối.
- Mock sync design checkpoint đã dùng artifact ref theo version mới nhất thay vì
  hard-code `v1`.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test*.py" -v`: 45 test pass.
- Unit test Designer `update_design` fake mode trả design parse được.
- Unit test repair loop pass sau một vòng và latest artifacts trỏ `v2`.
- Unit test repair loop blocked sau giới hạn và latest artifacts trỏ `v3`.

Chưa làm trong lát này:

- Chưa real sync idempotent cho design checkpoint.
- Chưa nối Niko route duyệt checkpoint sang CLI `approve-checkpoint`.
- Chưa xử lý nhánh requirement thiếu/mơ hồ để quay về requirement checkpoint.

## 2026-10-13 - Live DeepSeek Sequence Target Fix

Phát hiện khi anh chạy live DeepSeek:

```text
Validator sequence diagram trace invented target:
Repository --> Controller: List<CulturalHeritage>
```

Nguyên nhân:

- Parser sequence diagram đã lưu arrow thật của message, gồm cả return arrow
  `-->`.
- Validator target whitelist chỉ có dạng text thuần và một số dạng `->`, nên
  trace target dùng đúng return arrow của diagram bị hiểu nhầm là target bịa.

Đã sửa:

- `_sequence_targets(...)` bổ sung target theo `arrow` thật của message:
  `Source-->Target: message` và `Source --> Target: message`.
- Thêm unit test `test_validator_accepts_sequence_return_arrow_target`.

Đã kiểm thử:

- `python -m unittest discover -s tests -p "test_phase2.py" -v`: 18 test pass.

Ghi chú vận hành:

- Run live bị lỗi đã vào `status=failed`; cần tạo run mới bằng
  `start-or-get-run` rồi approve lại, không approve lại trực tiếp run cũ.

## 2026-10-13 - V4 Configurable Repair Limit Và Real Design Sync

Đã làm:

- Thêm config `SDLC_DESIGN_REPAIR_LIMIT`, mặc định `2`, cho phép đặt `0` để chỉ
  validate draft đầu tiên mà không gọi `Designer.update_design`.
- Thêm CLI override `approve-checkpoint --design-repair-limit <n>` để smoke test
  nhanh các giới hạn khác nhau mà không sửa `.env`.
- Phase 2 dùng `settings.design_repair_limit` thay cho hằng số hard-code.
- `sync_design_checkpoint(...)` đã hỗ trợ `SDLC_SYNC_MODE=mock|real|both`.
- Thêm `real_sync_design_checkpoint(...)` cho Jira/Confluence:
  - Jira tạo comment mới hoặc update comment cũ nếu report v2 có `external_id`;
  - Confluence tạo page mới hoặc update page cũ nếu report v2 có `external_id`;
  - ghi `tool_sync_report_v2.json` với `phase=design_checkpoint`,
    `operation=created|updated`, `artifact_refs`, `url`, `external_id`.
- `sync-run` đọc `pending_checkpoint.type` để tự chọn re-sync requirement hoặc
  design checkpoint hiện tại.

Đã kiểm thử:

- `rtk python -m pytest`: 52 test pass.
- Unit test repair loop tôn trọng `SDLC_DESIGN_REPAIR_LIMIT=0`.
- Unit test real sync design tạo Jira comment/Confluence page mới.
- Unit test real sync design update lại target Phase 1 đã có `external_id`.
- Unit test partial failure: Jira pass, Confluence lỗi thì report
  `partial_failed` và artifact local vẫn giữ nguyên.

Chưa làm trong lát này:

- Chưa chạy smoke real sync Phase 2 bằng Atlassian thật.
- Chưa nối Niko route duyệt checkpoint design/Phase 2 UI conversation.
- Chưa xử lý nhánh requirement thiếu/mơ hồ để quay lại requirement checkpoint.

## 2026-10-13 - V4.1 Tách Diagram Trong Sync Output

Phát hiện khi live test:

- Jira/Confluence đã có comment/page design, nhưng phần design artifact bị đưa
  lên gần như text workflow/context.
- Class diagram và sequence diagram không nổi bật, reviewer khó thấy đây là
  checkpoint thiết kế.

Đã sửa:

- Tách PlantUML class/sequence từ `design_artifact_vN.md` bằng parser hiện có.
- Jira comment thêm section `Diagrams` và `codeBlock` PlantUML riêng.
- Confluence page đổi từ `Design Artifact` nguyên khối sang `Design Diagrams`
  với từng code macro PlantUML cho Class Diagram và Sequence Diagram.
- Unit test real sync design kiểm tra Jira có `codeBlock` và Confluence có
  `Design Diagrams`, `Class Diagram`, `Sequence Diagram`, `ac:name="code"`.

Đã kiểm thử:

- `rtk python -m pytest`: 52 test pass.

Chưa làm:

- Chưa render diagram thành ảnh SVG/PNG và upload attachment lên Confluence.
  Đây là bước riêng nếu cần reviewer thấy hình thật thay vì PlantUML source.

## 2026-10-13 - V4.2 Class Attribute Trace Target Và Failed Run Retry

Phát hiện khi anh live test Phase 2:

```text
Validator class diagram trace invented target:
DeleteCulturalHeritageController.deleteInProgress
```

Nguyên nhân:

- Class trace validator chỉ whitelist `ClassName` và `ClassName.methodName`.
- DeepSeek có thể link requirement quality/state tới attribute tồn tại trong
  class diagram, ví dụ flag `deleteInProgress`.
- Nếu attribute này thật sự có trong `class_diagram_graph`, không nên coi đó là
  target bịa.

Đã sửa:

- `_class_targets(...)` chấp nhận thêm `ClassName.attributeName` khi attribute
  có trong class graph.
- Prompt `traceability_class.txt` nói rõ target hợp lệ có thể là class, method
  hoặc attribute, nhưng không được invent ngoài JSON graph.
- `RuntimeStore` coi `status=failed` là terminal để lần `start-from-jira` sau tự
  tạo run mới, tránh kẹt active run lỗi.

Đã kiểm thử:

- `rtk python -m pytest`: 54 test pass.
- Unit test class trace chấp nhận target attribute thật.
- Unit test start lại source sau run failed tạo run mới.

## 2026-10-13 - V4.3 Ghi Rõ Policy Sync Và Giới Hạn Diagram Render

Phản hồi khi review live Jira/Confluence:

- Cơ chế sync hiện tại tạo cảm giác như "thay thế" comment/page thay vì thêm mới
  mỗi lần sync.
- Diagram trên Confluence đã được tách ra nhưng vẫn là PlantUML source trong
  code macro, chưa phải hình vẽ render thật.

Đã chốt lại trong docs:

- Sync hiện tại là idempotent theo artifact do `SDLC_Harness` sở hữu: có
  `external_id` thì update Jira comment/Confluence page cũ, chưa có thì tạo mới.
- Harness không ghi đè nội dung người dùng viết tay; chỉ cập nhật comment/page
  mà chính nó tạo và lưu id trong `tool_sync_report_v2.json`.
- Jira/Confluence là bề mặt review checkpoint mới nhất; lịch sử chi tiết nằm ở
  `.runtime/runs` theo version artifact, Confluence có thêm page history.
- Nếu cần audit timeline, hướng sau là policy lai: một summary comment/page được
  update in-place, cộng thêm event comment cho mốc lớn.
- Confluence Cloud code macro không tự render PlantUML thành hình; muốn có hình
  thật thì cần app/macro diagram hoặc để harness render SVG/PNG rồi upload
  attachment và embed vào page.

Đã cập nhật:

- `sync-design-checkpoint.md`
- `README.md`
- `phase-2-checklist.md`
- `docs/implementation-checklist.md`

## 2026-10-13 - V4.4 Render SVG Diagram Và Embed Confluence

Mục tiêu:

- Thử hướng số 2: không phụ thuộc Confluence marketplace app, để
  `SDLC_Harness` tự tạo hình và upload lên Confluence.

Đã làm:

- Thêm `sdlc_harness/artifacts/diagram_svg.py` để render SVG tối giản từ
  `class_diagram_graph_vN.json` và `sequence_diagram_graph_vN.json`.
- Real Confluence design sync tạo SVG trong `sync/diagrams/`.
- Nếu sync dùng API thật, adapter upload SVG lên Confluence page bằng attachment
  endpoint.
- Sau khi upload thành công, Confluence page nhúng ảnh bằng `<ac:image>` và vẫn
  giữ PlantUML code macro dưới ảnh làm fallback/evidence.
- Với page mới, adapter tạo page trước, upload attachment, rồi update page lần
  hai để nhúng ảnh.
- Với page cũ, adapter upload attachment vào page cũ trước rồi update body.
- Nếu uploader không có hoặc upload lỗi, page vẫn có code macro PlantUML để đọc
  được.

Giới hạn hiện tại:

- SVG renderer là layout tối giản từ graph đã parse, chưa phải PlantUML renderer
  đầy đủ.
- Chưa live smoke với Atlassian thật sau khi thêm attachment upload.
- Jira comment vẫn chỉ có PlantUML code block; ảnh SVG ưu tiên cho Confluence.

Đã kiểm thử:

- `rtk python -m pytest tests/test_phase2.py`: 26 test pass.
- `rtk python -m pytest`: 55 test pass.
- Unit test kiểm tra uploader nhận file `.svg`, content bắt đầu bằng `<svg`, page
  final có `<ac:image>` và report ghi `diagram_images.status=synced`.
- Live re-sync Atlassian cho `run_20261013_041915_886e9158` pass:
  `last_sync.status=synced`, Confluence target `external_id=98645`,
  `diagram_images.status=synced`, upload 2 SVG attachment
  `class-diagram-v1.svg` và `sequence-diagram-1-v1.svg`.

## 2026-10-13 - V4.5 PlantUML App Macro Trên Confluence

Mục tiêu:

- Thử app `PlantUML Diagrams` đã cài trên Confluence thay vì chỉ dùng SVG tự vẽ.

Đã làm:

- Đọc storage format từ page test của anh và rút ra phần quan trọng:
  `ac:adf-extension`, extension key `plantuml-confluence`, và
  `guest-params.diagram-code`.
- Thêm Forge macro PlantUML vào đầu mỗi Class/Sequence diagram section.
- Giữ SVG attachment và PlantUML code block phía sau làm fallback/evidence.
- Không hard-code `account-id`, `cloud-id`, page id test hoặc context id từ page
  test; macro sinh `local-id` mới cho mỗi lần sync.
- Sửa upload SVG idempotent hơn: nếu Confluence báo attachment cùng tên đã tồn
  tại, adapter reuse attachment cũ thay vì ghi lỗi.

Đã kiểm thử:

- `rtk python -m pytest`: 56 test pass.
- Live re-sync Atlassian cho `run_20261013_041915_886e9158` pass.
- Đọc storage page `98645` qua API xác nhận:
  `has_plantuml_macro=True`, `adf_extensions=2`, `diagram_code_params=4`,
  `has_svg_image=True`.
- `tool_sync_report_v2.json` ghi `diagram_images.status=synced`, 2 SVG asset đều
  `operation=reused_existing_attachment`, không còn lỗi duplicate attachment.

Chưa chốt:

- Cần anh nhìn trực tiếp Confluence page để xác nhận macro app render đẹp hơn SVG
  fallback không. Nếu đẹp, có thể đổi policy sau này thành app macro primary và
  ẩn/bỏ SVG fallback khỏi phần hiển thị.

## 2026-10-13 - V4.6 Chốt PlantUML App Macro Và Bỏ SVG Fallback

Quyết định sau khi anh xem Confluence:

- PlantUML app macro render ổn hơn nhánh SVG tự vẽ, nên checkpoint design không
  cần nhúng ảnh SVG nữa.
- Confluence page nên gọn: mỗi diagram có macro app để xem hình và một code macro
  PlantUML làm fallback/evidence.
- SVG attachment/image fallback chỉ giữ như hướng đã thử trong lịch sử V4.4,
  không còn là behavior hiện tại.

Đã sửa:

- Bỏ nhánh render/upload/embed SVG khỏi real Confluence design sync.
- `tool_sync_report_v2.json` không còn target phụ `diagram_images`.
- Xóa module render SVG thử nghiệm khỏi source.
- Unit test real sync design kiểm tra body có PlantUML app macro và không còn
  `<ac:image>`.
- Cập nhật README/checklist/sync docs để policy hiện tại là PlantUML macro
  primary + PlantUML code fallback.

Đã kiểm thử:

- `rtk python -m pytest`: 54 test pass.
- Live re-sync Atlassian cho `run_20261013_041915_886e9158` pass.
- Đọc storage page Confluence `98645` sau re-sync xác nhận:
  `has_plantuml_macro=True`, `adf_extensions=2`, `diagram_code_params=4`,
  `fallback_svg_count=0`, `has_svg_image=False`.

Ghi chú hướng Mermaid:

- Có thể chuyển sang Mermaid, nhưng trước hết cần chọn/cài Mermaid app trên
  Confluence và lấy storage format macro giống cách anh đã lấy storage format
  của PlantUML app.
- Với dữ liệu hiện tại, nên sinh Mermaid từ graph đã parse thay vì yêu cầu LLM
  đổi prompt ngay từ đầu; như vậy Validator vẫn giữ được PlantUML/graph pipeline
  đã ổn định trong Phase 2.

## 2026-10-13 - Phase 2 Lock Test Pass

Mục tiêu:

- Khóa Phase 2 chặt trước khi mở Phase 3, đặc biệt ở phần test và live smoke.
- Phân loại rõ các mục còn hở trong checklist: blocker thật, đã làm bên Niko,
  hoặc backlog không chặn Phase 3.

Đã làm:

- Thêm regression test cho requirement không traceable: nếu requirement packet
  không tạo được requirement graph, Phase 2 chuyển `status=blocked`, không sync
  design checkpoint và ghi `last_error.error_code=requirement_traceability_blocked`.
- Bổ sung formatter/test bên Niko để đọc `last_error`, báo rõ trạng thái
  `blocked`, và phân biệt `last_sync.status=partial_failed` thay vì trình bày
  như sync thành công hoàn toàn.
- Cập nhật checklist Phase 2: Niko route `approve-checkpoint`, summary ngắn,
  đọc `pending_checkpoint/allowed_next_actions/last_sync`, báo blocked và smoke
  qua Niko đã được đánh dấu xong.
- Chốt Mermaid là backlog không chặn Phase 3; Phase 2 hiện khóa theo PlantUML
  app macro + PlantUML source fallback.

Đã kiểm thử:

- `rtk proxy python -m pytest tests/test_phase2.py`: 26 test pass.
- `rtk proxy python -m pytest tests/test_sdlc_client.py tests/test_niko_app.py`
  bên `Niko_Agent`: 29 test pass.
- Live smoke trước đó qua Niko: `start-from-jira` -> `approve-checkpoint` ->
  `waiting_design_approval`, có `design_artifact_v2`, `repair iterations: 1`,
  sync real Jira/Confluence `synced`.
- Live smoke khóa Phase 2 sau khi xóa runtime: `run_20261013_085819_173b2d75`
  đi từ Jira issue `SDLC-1` -> requirement checkpoint -> design checkpoint.
  Kết quả cuối là `design_review / waiting_design_approval`, pending checkpoint
  `design_artifact_v1`, Jira comment `10043`, Confluence page `131426`, sync
  real Jira/Confluence `synced`, `last_error=null`, `missing design links=0`,
  `repair iterations=0`.

Kết luận:

- Phase 2 đủ điều kiện khóa để chuẩn bị Phase 3 theo lát nhỏ.
- Các việc chưa mở như `request_change`, dynamic context, Mermaid, approve
  design để sang code vẫn thuộc Phase sau, không phải blocker của Phase 2.
## 2026-10-13 - Patch: sequence note trace target và checkpoint fail-state

Khi live test từ Jira thật, DeepSeek có thể trace requirement NFR vào PlantUML
note dạng `note right of Controller: ...`. Parser sequence đã lưu note trong
`controlFlows`, nhưng Validator chưa coi note là target hợp lệ nên workflow fail
cứng với `validator_traceability_failed`.

Đã sửa:

- `ValidatorAgent` cho phép sequence trace target trỏ tới note đã có trong
  `controlFlows`.
- Nếu LLM trả note nhiều dòng, normalize về dòng note đã parse thay vì coi là
  invented target.
- Khi Phase 2 bắt đầu chạy sau khi requirement được duyệt, checkpoint requirement
  cũ được clear ngay để nếu design fail thì không còn pending checkpoint lỗi thời.
- `sync-run` chỉ sync checkpoint hiện tại theo `pending_checkpoint.type`, tránh
  failed/blocked run rơi ngược về requirement sync và tạo comment lặp.

Test:

- `rtk python -m pytest tests/test_phase2.py -q` -> 28 passed.
- `rtk python -m pytest -q` -> 78 passed.

## 2026-10-13 - Patch: sequence control-flow target và sync guard

Live test `run_20261013_140739_d59c2be5` fail ở bước approve requirement sang
design với lỗi:

```text
Validator sequence diagram trace invented target: Duplicate submission detected
```

Nguyên nhân:

- DeepSeek trace requirement vào condition của nhánh sequence `alt/else/opt`.
- Parser PlantUML đã lưu condition này trong `sequence_graph.controlFlows`,
  nhưng Validator mới chỉ coi message/lifeline/note là sequence target hợp lệ.
- Run lỗi không tạo `tool_sync_report_v2`, nên bản thân run này chưa sync design
  checkpoint. Các comment Design review thấy trên Jira là comment còn lại từ
  những lần live test/re-sync trước trên cùng issue `SDLC-1`.

Đã sửa:

- `_sequence_targets()` cho phép mọi `controlFlows[].condition` làm target hợp lệ,
  không chỉ riêng note.
- Với note, thêm cả phần text sau dấu `:` làm alias để LLM có thể trace vào nội
  dung note thay vì nguyên dòng PlantUML.
- `_coerce_sequence_target()` hỗ trợ normalize mềm các target control-flow ngắn
  khi LLM trả một phần text thật có trong graph.
- Regression test xác nhận target `Duplicate submission detected` được chấp nhận
  khi nó là condition của control flow.
- Regression test xác nhận run fail không còn pending checkpoint và `sync-run`
  trên failed run trả `sync_not_ready`, không tạo report/comment design v2.

Test:

- `rtk python -m pytest tests/test_phase2.py -q` -> 29 passed.
- `rtk python -m pytest -q` -> 79 passed.


