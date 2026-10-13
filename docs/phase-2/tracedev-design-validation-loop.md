# TraceDev Design Validation Loop

Tài liệu này ghi lại phần cần học từ TraceDev cho Phase 2 của SDLC_Harness.
Trọng tâm là tương tác giữa Requirement Refiner, Designer và Validator, đặc biệt
là cách Validator dựng traceability graph, phát hiện missing links và đẩy
feedback ngược lại cho Designer.

## Flow Trong TraceDev

Trong `agent/main.py`, sau khi Requirement Refiner tạo `updated_requirements`,
TraceDev chuyển requirement sang JSON rồi chạy:

```text
use_case_json = from_req_to_json(updated_requirements)
designer = Designer()

designer_report = designer.analysis(use_case_json)
design_model = designer.act(use_case_json, designer_report)

validator = Validator()
for i in check_rounds:
  class_diagram, sequence_diagram = extract_class_sequence_diagram(design_model)
  missing_links, trace_class, trace_sequence, class_graph, sequence_graph =
    validator.construct_tracibility(use_case_json, class_diagram, sequence_diagram)
  report = validator.act(missing_links, use_case_json, design_model)
  design_model = designer.update_design(report)
  if no missing links:
    break
```

TraceDev lưu các output vào nhiều folder: initial design, updated design,
trace/class, trace/sequence, missing link redesign, graph JSON và quality report.

## Designer Trong TraceDev

`Designer.analysis(requirements)`:

- đọc prompt `designer/recommendation.txt`;
- đề xuất architectural style/pattern;
- lưu `analysis_report` trong memory.

`Designer.act(requirements, architect_report)`:

- đọc prompt `designer/designer.txt`;
- sinh class diagram và sequence diagram bằng PlantUML;
- lưu `initial_design` và `updated_design`.

`Designer.update_design(feedback)`:

- đọc prompt `designer/update_designer.txt`;
- nhận diagram hiện tại và UML audit report;
- sửa class/sequence diagram, giữ phần đúng, chỉ thêm/sửa phần cần thiết.

Điểm cần học: Designer không chỉ tạo diagram một lần. Nó phải có state nội bộ
hoặc artifact version để cập nhật theo report của Validator.

## Validator Trong TraceDev

`Validator.construct_tracibility(requirements, class_diagram, sequence_diagram)`
làm các việc sau:

1. Tạo `requirement_graph` từ requirements.
2. Parse class diagram thành `class_diagram_graph_json`.
3. Parse sequence diagram thành `sequence_diagram_graph_json`.
4. Gọi LLM với prompt `tracibility_class.txt` để link requirement sang class/method.
5. Gọi LLM với prompt `tracibility_sequence.txt` để link requirement sang lifeline/message.
6. Với từng requirement id:
   - nếu không có link class, thêm requirement vào `missing_reqs.class_diagram`;
   - nếu không có link sequence, thêm requirement vào `missing_reqs.sequence_diagram`.
7. Trả về missing links, trace class, trace sequence, class graph và sequence graph.

`Validator.act(missing_links, requirement, diagram)`:

- đọc prompt `validator/validator.txt`;
- nhận requirements, design model và missing links;
- tạo quality report gồm lỗi UML, lỗi consistency sequence/class và khuyến nghị sửa.

Điểm cần học: Validator có hai tầng:

- tầng graph/link có output JSON máy đọc;
- tầng quality report có output markdown/text cho Designer và human đọc.

## Cơ Chế Link Graph

TraceDev dùng LLM để tạo traceability JSON theo rule:

- Requirement actor -> class/lifeline tương ứng.
- Requirement action verb -> method/message tương ứng.
- Responsibility delegation -> link thêm các class/message thấp hơn như manager,
  repository, server.
- UI/system reactions -> link tới UI class, confirmation message, notification,
  cancellation/error flow.

Output class trace là JSON array:

```json
[
  {
    "requirement": "REQ-001",
    "content": "original text",
    "links": [
      {
        "target": "ClassName.methodName",
        "reason": "why they match"
      }
    ]
  }
]
```

Output sequence trace tương tự nhưng `target` là lifeline hoặc message.

## Missing Links Và Update Loop

TraceDev coi `links=[]` là dấu hiệu requirement chưa được design thể hiện. Khi
missing link xuất hiện:

```text
missing_links
-> Validator quality report
-> Designer.update_design(report)
-> parse diagram mới
-> construct_tracibility lại
```

SDLC_Harness cần lưu mỗi vòng, thay vì chỉ giữ memory trong object như TraceDev:

```text
design_artifact_v1
requirement_design_trace_v1
missing_design_links_v1
design_quality_report_v1

design_artifact_v2
requirement_design_trace_v2
missing_design_links_v2
design_quality_report_v2
```

## Điều Chỉnh Cho SDLC_Harness

TraceDev chạy batch thẳng từ use case đến code. Phase 2 của SDLC_Harness phải
thêm các điểm kiểm soát:

- đọc requirement đã duyệt từ state/artifact, không tự lấy source mới;
- ghi state trước/sau mỗi bước lớn;
- lưu mọi artifact vào `.runtime/runs/<run_id>/artifacts/`;
- sync checkpoint lên Jira/Confluence sau khi design pass;
- trả JSON status để Niko nói chuyện với người dùng;
- không tiếp tục sang code cho tới khi human approve design checkpoint.

## Quy Tắc Khi Validator Nghi Ngờ Requirement

Nếu missing link đến từ design thiếu, Designer được sửa design. Nếu missing link
đến từ requirement thiếu thông tin, mơ hồ hoặc self-conflicting, Phase 2 không
tự gọi Requirement Refiner để sửa. Workflow phải:

```text
status=waiting_requirement_approval hoặc blocked
last_error / quality_reports nêu rõ requirement nào thiếu
allowed_next_actions=[request_change, cancel_run, get_status]
```

Lý do: sửa requirement là thay đổi đầu vào đã được human duyệt, nên phải quay lại
checkpoint requirement.

