# Phase 2 - Requirement Packet Đến Design

Phase 2 bắt đầu sau khi người dùng duyệt checkpoint requirement của Phase 1.
Mục tiêu của phase này là biến `requirement_packet_v1` đã duyệt thành
`design_artifact_v1`, kiểm tra traceability bằng Validator, đồng bộ checkpoint
design lên tool và trả state để Niko báo lại cho người dùng.

Điểm khác biệt quan trọng so với TraceDev: TraceDev chạy thẳng trong một script,
còn SDLC_Harness phải lưu state, artifact version, trace report và checkpoint
sau từng vòng để Niko/human có thể dừng, đọc, duyệt hoặc yêu cầu sửa.

## Luồng Phase 2

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_requirement_approval
-> đọc requirement_packet_v1 đã duyệt
-> đọc formal_use_case_v1 nếu có
-> state=design_drafting
-> Designer.analysis(requirement_packet_v1)
-> Designer.act(requirement_packet_v1, designer_analysis)
-> sinh design_artifact_v1
-> state=design_validating
-> Parser tạo class diagram graph, sequence diagram graph từ PlantUML
-> Validator tạo requirement graph và requirement-design trace links
-> Validator sinh requirement-design trace links
-> Validator phát hiện missing links và tạo design_quality_report_v1
-> nếu còn missing link: Designer.update_design(report), tạo design_artifact_v2
-> Validator kiểm lại tối đa số vòng cho phép
-> nếu pass: sync design checkpoint theo policy Jira/Confluence
-> state=waiting_design_approval
-> nếu fail vì design chưa đủ: state=blocked
-> nếu fail vì requirement thiếu/nhập nhằng: quay về waiting_requirement_approval
```

## Ba Vai Trò AI Trong Phase 2

### Requirement Refiner

Requirement Refiner không chạy lại trong Phase 2. Artifact của nó
(`requirement_packet_v1` và `formal_use_case_v1`) là input đã được duyệt từ Phase
1. Nếu Validator phát hiện thiếu thông tin ở requirement, Phase 2 không tự sửa
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

Sau khi Phase 2 pass, một run phải có thêm:

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

## Tiến Độ Hiện Tại

Cập nhật 2026-10-13: đã triển khai lát Phase 2 V1 cho `approve-checkpoint`.
Lát này đọc requirement packet đã duyệt, đọc thêm formal use case nếu có, gọi
Designer Agent fake/LLM-ready để sinh PlantUML, parse class/sequence graph thật,
tạo `tool_sync_report_v2.json` bằng mock sync và chuyển state sang
`waiting_design_approval`. Traceability LLM hiện ghi `traceability_pending_v1`;
chưa có repair loop, chưa real sync Jira/Confluence cho Phase 2 và chưa nối Niko
route duyệt checkpoint.

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

