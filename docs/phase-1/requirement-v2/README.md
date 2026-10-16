# Requirement Model V2 Và Clarification Workflow

Tài liệu trong thư mục này mô tả Requirement Contract V2 đang chạy. Sau E2E UC2, V2 đã trở thành contract runtime duy nhất và code V1 đã được gỡ ngày 2026-10-16.

## Vấn Đề Cần Giải Quyết

Contract V1 sinh `formal_use_case_v1.txt` trước rồi dùng artifact đó hỗ trợ tạo
`requirement_packet_v1.md`. Hai prompt và hai validator có mục tiêu khác nhau:

- formal grammar TraceDev buộc mọi block có nội dung nhưng không có block riêng
  cho alternative/exception flow;
- prompt vừa cấm thêm hành vi vừa cho phép suy diễn field còn thiếu;
- Requirement Packet tách happy path, exception, quality và Open Questions,
  nhưng Formal Use Case có thể đã đổi nghĩa source trước đó;
- Formal Validator chỉ kiểm cú pháp, chưa kiểm grounding với source;
- Phase 2 đưa cả Requirement Packet và Formal Use Case vào Designer mà chưa có
  quy tắc ưu tiên khi hai artifact mâu thuẫn;
- Open Questions hiện là Markdown tự do, không có ID, blocking policy, trạng
  thái trả lời hoặc decision provenance.

UC2 thể hiện trực tiếp hai lỗi:

- nhánh dữ liệu không hợp lệ bị đưa thành một bước bắt buộc trong Main Flow của
  Formal Use Case;
- rule không chấp nhận duplicate đã có trong source nhưng tiêu chí nhận diện
  duplicate chưa rõ, nên phase sau tự chọn policy mà không có quyết định human.

## Quyết Định Kiến Trúc

Requirement V2 dùng một nguồn sự thật duy nhất:

```text
source context
  -> Requirement Refiner structured output
  -> requirement_model_v2_r<n>.json       # canonical
  -> Python validators + blocking policy
  -> clarification loop nếu cần
  -> deterministic projections
       requirement_packet_v2_r<n>.md
       formal_use_case_v2_r<n>.txt
  -> requirement approval
  -> Designer/Validator đọc canonical JSON
```

Các quyết định đã chốt:

- `requirement_model_v2` dạng JSON là canonical artifact.
- Requirement Packet và Formal Use Case V2 là projection, không phải nguồn sự
  thật độc lập và không được gọi LLM riêng để viết lại semantics.
- LLM đề xuất category/impact/blocking cho câu hỏi; Python áp policy cuối cùng.
- Niko hỏi một blocking question mỗi lượt theo thứ tự dependency và priority.
- Câu trả lời human tạo revision mới và decision provenance.
- Run mới luôn dùng V2; run V1 lịch sử chỉ còn là dữ liệu audit và không thể tiếp tục workflow.
- Full redesign này xử lý requirement semantics và clarification. Hardening
  output-token/model capability vẫn là backlog LLM client riêng.

## Trách Nhiệm

```text
Niko_Agent
  hiển thị current_question
  chuyển câu trả lời người dùng sang action answer-clarification
  format status/summary cho Telegram

SDLC_Harness
  load source
  sinh/validate requirement_model_v2
  quyết định effective_blocking bằng Python policy
  lưu question/decision/revision
  render packet/formal projection
  quản lý state/checkpoint/sync/downstream input
```

Niko không tự phân loại question, sửa requirement artifact hoặc quyết định câu
trả lời đã đủ để đóng question hay chưa.

## Thứ Tự Đọc

1. `requirement-model-v2.md`: canonical schema, grounding và projections.
2. `clarification-workflow.md`: state machine và CLI/status contract.
3. `migration-and-rollout.md`: trạng thái retirement V1 và contract V2-only.
4. `checklist.md`: thứ tự triển khai, test và controlled smoke.
5. `progress.md`: nhật ký code, kiểm tra, lỗi phát sinh và việc tiếp theo.

## Điều Kiện Khóa Thiết Kế

- Không có requirement confirmed nào thiếu evidence hoặc human decision.
- Không có blocking question mở khi approve requirement.
- Main/alternative/exception flow có cấu trúc riêng.
- JSON, Requirement Packet và Formal Use Case cùng revision và cùng semantics.
- Phase 2 V2 ưu tiên canonical JSON; projection không thể ghi đè model.
- Contract không phải V2 bị từ chối rõ ràng, không fallback sang legacy text.

