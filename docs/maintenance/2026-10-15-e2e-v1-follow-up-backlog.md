# Backlog Sau E2E V1: Requirement Clarification Và Structured Output Budget

Tài liệu này ghi lại hai vấn đề còn mở sau lần chạy E2E V1 ngày 2026-10-15. Đây
là backlog của SDLC Harness; các bản sửa runtime đã giúp run hiện tại hoàn tất
nhưng chưa giải quyết đầy đủ hai policy ở cấp workflow.

## 1. Open Question Phải Tạo Checkpoint Làm Rõ

### Vấn đề quan sát được

Requirement Packet của UC2 giữ tiêu chí nhận diện bản ghi trùng trong phần
`Open Questions`. Human vẫn duyệt checkpoint và các phase sau tự chọn hoặc suy
diễn policy khác nhau. Đến Code Trace Validator, câu hỏi chưa được quyết định bị
diễn giải thành constraint blocking, làm vòng Developer/Validator sửa đi sửa lại
và thiếu nguồn quyết định rõ ràng.

### Hành vi mong muốn

- Requirement Refiner phải phân loại từng open question là `blocking` hoặc
  `non_blocking` trước checkpoint duyệt requirement.
- Open question ảnh hưởng acceptance criteria, business rule, dữ liệu, API hoặc
  hành vi quan sát được phải là `blocking`.
- Khi còn câu hỏi blocking, run dừng ở checkpoint làm rõ requirement. Niko đọc
  câu hỏi từ JSON status, hỏi người dùng và gửi câu trả lời lại SDLC Harness.
- Câu trả lời của human phải tạo artifact/version mới và lưu decision provenance:
  câu hỏi, câu trả lời, người/thời điểm quyết định và requirement nào được cập
  nhật.
- Chỉ khi mọi câu hỏi blocking đã được giải quyết thì workflow mới cho phép
  approve requirement và chuyển sang Design.
- Designer, Developer và Validator chỉ dùng policy đã có trong artifact được
  duyệt; không tự biến open question thành constraint.

### Contract cần bổ sung

Trạng thái/checkpoint dự kiến cần biểu diễn tối thiểu:

```text
status=waiting_requirement_clarification
pending_checkpoint.type=requirement_clarification
pending_checkpoint.blocking_questions=[...]
allowed_next_actions=[answer_clarification]
```

Sau khi nhận câu trả lời:

```text
requirement_decisions_v{n}.json
requirement_packet_v{n+1}.md
formal_use_case_v{n+1}.txt      # khi quyết định làm thay đổi formal use case
```

### Điều kiện hoàn thành

- Use case có open question blocking không thể đi thẳng sang Design.
- Use case chỉ có câu hỏi non-blocking vẫn có thể được duyệt, với assumption rõ.
- Câu trả lời qua Niko được lưu và trace tới requirement cập nhật.
- Phase sau không tạo thêm policy trái với quyết định đã duyệt.

Trạng thái: **Planned - thiết kế đã chốt tại
`docs/phase-1/requirement-v2/`; code/test chưa triển khai**.

## 2. Output Token Của Model Có Thể Cắt Structured Output

### Vấn đề quan sát được

Developer/Validator từng nhận phản hồi DeepSeek bị cắt hoặc không parse được
JSON. Khi output dừng giữa payload, workflow chỉ thấy lỗi parser dù nguyên nhân
thực tế có thể là ngân sách output token hoặc tổng context đã đầy. Việc đặt cố
định `SDLC_LLM_STRUCTURED_MAX_TOKENS=8192` đã giảm lỗi cho run hiện tại nhưng
không phản ánh giới hạn của model/provider và kích thước request cụ thể.

### Biện pháp hiện có

- Structured request dùng JSON mode khi provider hỗ trợ.
- Raw response, attempt metadata, usage và `finish_reason` được lưu trước khi
  parse.
- Có một bounded retry khi payload bị cắt hoặc JSON không hợp lệ.
- `SDLC_LLM_STRUCTURED_MAX_TOKENS` là fallback/ngân sách cấu hình hiện tại.

### Hành vi mong muốn

- LLM client có một boundary nội bộ trả về capability đã chuẩn hóa:
  `context_window`, `max_output_tokens`, `supports_json_mode`.
- Với DeepSeek, capability có thể lấy từ model metadata và cache. Nếu discovery
  lỗi hoặc provider không công bố metadata, dùng cấu hình/model registry nội bộ.
- Không để workflow đọc trực tiếp schema `/models` riêng của DeepSeek.
- Ngân sách hiệu lực cho mỗi request phải bị chặn bởi ngân sách agent, giới hạn
  output của model và phần context còn lại có safety margin.
- Khi `finish_reason=length`, retry có thể tăng ngân sách trong giới hạn model.
  Nếu input chiếm hết context, workflow phải compact/chia packet thay vì chỉ tăng
  output token.
- Nếu đổi provider, chỉ adapter capability/structured-output thay đổi; workflow
  Requirement, Designer, Validator, Developer và QA giữ contract chung.

Thứ tự fallback dự kiến:

```text
runtime override
-> provider capability discovery
-> internal model metadata
-> safe configured default
```

### Điều kiện hoàn thành

- Capability discovery failure không làm hỏng run và có fallback quan sát được.
- Không gửi `max_tokens` vượt model ceiling hoặc phần context còn lại.
- Artifact debug phân biệt được output-limit truncation, context exhaustion và
  malformed JSON.
- Retry tăng budget có giới hạn; không lặp vô hạn.
- Có thể thay provider mà không sửa logic chọn budget trong từng agent.

Trạng thái: **Open - hardening LLM client trước khi mở rộng model/provider**.
