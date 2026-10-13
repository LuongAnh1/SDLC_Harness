# Phase 2 Progress

Tài liệu này ghi lại tiến độ triển khai Phase 2 theo từng lát nhỏ. Không đánh
dấu một mục là xong nếu mới chỉ có tài liệu mà chưa có code/test tương ứng.

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
