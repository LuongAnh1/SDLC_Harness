# Phase 2 - Requirement Đến Design

Phase 2 bắt đầu sau khi canonical Requirement Model V2 được duyệt. Designer và
Validator luôn đọc `CanonicalRequirementInput`; packet/formal V2 chỉ là projection
review và context.

## Luồng Phase 2

```text
approve_checkpoint(run_id)
-> verify approved requirement_model_v2 revision và source fingerprint
-> state=design_drafting
-> Designer.analysis/act với canonical requirement
-> parse class/sequence graph
-> Validator tạo requirement graph bằng stable IDs và trace links
-> Designer repair trong giới hạn nếu còn missing links
-> pass: sync design checkpoint và waiting_design_approval
-> không hội tụ: blocked
```

Phase 2 không còn dual-read Requirement V1/V2. State khác V2 bị reader từ chối
trước khi workflow mutate state.

## Ba Vai Trò AI Trong Phase 2

### Requirement Refiner

Requirement Refiner không chạy lại trong Phase 2. Artifact của nó
canonical Requirement Model V2 là input đã được duyệt từ Phase 1. Nếu Validator phát hiện thiếu thông tin ở requirement, Phase 2 không tự sửa
requirement; workflow phải yêu cầu người dùng quay lại checkpoint requirement.

### Designer

Designer nhận requirement đã duyệt và tạo thiết kế. Theo TraceDev, Designer có
hai bước:

- `analysis`: đề xuất kiến trúc/pattern/cách tổ chức thiết kế.
- `act`: sinh class diagram và sequence diagram, hiện ưu tiên PlantUML để dễ
  parse thành graph.

Khi Validator tạo quality report, Designer dùng `update_design` để sửa diagram,
không sinh lại từ đầu mù quáng.

### Validator

Validator là trung tâm Phase 2. Nó không chỉ chấm điểm cuối cùng, mà tạo graph
và missing-link report để quyết định workflow:

- requirement graph: các requirement id/content từ packet đã duyệt;
- class diagram graph: class, attributes, methods, relationships;
- sequence diagram graph: lifelines, messages, control flow;
- traceability links: requirement -> class/method và requirement -> lifeline/message;
- missing links: requirement chưa có link ở class diagram hoặc sequence diagram;
- quality report: hướng dẫn Designer sửa đúng phần thiếu/mâu thuẫn.

## Đầu Ra Bắt Buộc

Sau khi Phase 2 pass, một run phải có thêm. Nếu repair xảy ra, các file này sẽ
dùng version mới nhất, ví dụ `v2` hoặc `v3`, và bản `v1` vẫn được giữ lại để
truy vết:

```text
.runtime/runs/<run_id>/artifacts/designer_analysis_v1.md
.runtime/runs/<run_id>/artifacts/design_artifact_v1.md
.runtime/runs/<run_id>/artifacts/class_diagram_graph_v1.json
.runtime/runs/<run_id>/artifacts/sequence_diagram_graph_v1.json
.runtime/runs/<run_id>/artifacts/requirement_design_trace_v1.json
.runtime/runs/<run_id>/artifacts/missing_design_links_v1.json
.runtime/runs/<run_id>/artifacts/design_quality_report_v1.md
.runtime/runs/<run_id>/sync/tool_sync_report_v2.json
```

Và `state.json` phải có:

```text
current_phase=design_review
status=waiting_design_approval
latest_artifacts.design_artifact
latest_artifacts.requirement_design_trace
quality_reports.design_traceability
pending_checkpoint.type=design_approval
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

## Sync Checkpoint Design

Phase 2 phải đồng bộ checkpoint thiết kế từ `SDLC_Harness`, không đẩy trách
nhiệm này sang Niko. Jira chỉ nên nhận comment/tóm tắt dễ đọc; Confluence là nơi
lưu bản review đầy đủ hơn gồm designer analysis, diagram, traceability summary
và quality report. Chi tiết contract nằm ở `sync-design-checkpoint.md`.

Sync hiện tại là sync artifact do harness sở hữu theo từng checkpoint phase:
Phase 2 chỉ update Jira comment/Confluence page đã ghi trong
`tool_sync_report_v2.json`; không reuse target của requirement checkpoint Phase
1. Nếu chưa có `external_id` trong report v2 thì tạo target mới. Đây là cách giữ
mỗi checkpoint có bề mặt review riêng, trong khi re-sync cùng phase vẫn không
tạo trùng; không phải cơ chế ghi đè nội dung người dùng viết tay.

Diagram trên Confluence hiện có hai lớp: PlantUML app macro nếu site đã cài
`PlantUML Diagrams`, và PlantUML source trong code macro làm fallback/evidence.
Harness không còn upload/nhúng SVG attachment trong checkpoint design vì macro app
đã đủ tốt cho luồng review hiện tại. Nếu muốn chuyển sang Mermaid, cần chọn app
Mermaid trên Confluence và lấy storage format macro tương tự cách đã làm với
PlantUML.

## Tiến Độ Hiện Tại

Cập nhật 2026-10-13: đã triển khai lát Phase 2 V4 cho `approve-checkpoint`.
Lát này đọc requirement packet đã duyệt, đọc thêm formal use case nếu có, gọi
Designer Agent fake/LLM-ready để sinh PlantUML, parse class/sequence graph thật,
chạy Validator traceability V1 để tạo requirement graph, class trace, sequence
trace và missing-link report. Nếu còn missing link, workflow gọi
`Designer.update_design` theo `SDLC_DESIGN_REPAIR_LIMIT`, ghi artifact version
mới và chỉ tạo checkpoint khi traceability pass. Khi pass, Phase 2 có thể mock
sync hoặc real sync Jira/Confluence theo `SDLC_SYNC_MODE`, trong đó real sync
update lại bot-owned comment/page Phase 2 khi report v2 có `external_id`.
Confluence real sync nhúng PlantUML app macro và giữ PlantUML source fallback,
không còn nhúng ảnh SVG.
Nếu hết vòng repair mà vẫn thiếu link thì state chuyển `blocked`. Chưa nối
Niko route duyệt checkpoint.

## Không Làm Trong Phase 2

- Không sinh code.
- Không chạy Developer/Tester.
- Không sửa requirement tự động nếu requirement thiếu nghiêm trọng.
- Không cho Niko đọc Jira/Confluence trực tiếp để dựng design.
- Không coi traceability link LLM là sự thật tuyệt đối; Validator phải lưu cả
  graph, missing links và report để người dùng/agent kiểm lại.

## Tài Liệu Trong Folder Này

- `tracedev-design-validation-loop.md`: khảo sát cách TraceDev triển khai
  Designer/Validator, graph traceability và missing-link feedback.
- `state-and-artifacts.md`: state machine, artifact, versioning và checkpoint
  của Phase 2.
- `sync-design-checkpoint.md`: policy sync checkpoint design lên Jira/Confluence,
  schema `tool_sync_report_v2.json`, re-sync và cách Niko đọc `last_sync`.
- `phase-2-checklist.md`: checklist triển khai code/test/live smoke cho Phase 2.
- `progress.md`: tiến độ triển khai và kiểm thử Phase 2 theo từng lát nhỏ.

