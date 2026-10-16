# Requirement V2 Implementation Checklist

Checklist này theo dõi code/test sau khi thiết kế được duyệt. Không tick mục
triển khai chỉ vì tài liệu đã được viết.

## 1. Thiết Kế Và Contract

- [x] Chốt structured JSON V2 là canonical requirement artifact.
- [x] Chốt packet/formal là deterministic projections.
- [x] Chốt LLM đề xuất và Python quyết định effective blocking.
- [x] Chốt clarification hỏi từng câu.
- [x] Chốt V2 chỉ áp dụng cho run mới, không migrate lịch sử.
- [x] Chốt error codes, state, CLI/status shape và rollout boundary.

## 2. Canonical Model Và Validator

- [x] Tạo type/schema cho `requirement_model_v2` và revision naming.
- [x] Tạo stable ID/reference validator.
- [x] Kiểm evidence quote thuộc source snapshot.
- [x] Cấm confirmed statement thiếu evidence/decision.
- [x] Áp blocking policy bằng Python và phát hiện dependency cycle.
- [x] Phân biệt main, alternative và exception flow.
- [x] Chặn assumption bị dùng như confirmed constraint.

## 3. Refiner Và Projections

- [x] Requirement Refiner sinh structured model, không sinh hai artifact semantic
  độc lập.
- [x] Bỏ chỉ dẫn infer từ domain knowledge cho confirmed requirement.
- [x] Bounded repair chỉ sửa schema/grounding/reference violation.
- [x] Render `requirement_packet_v2_r<n>.md` deterministically.
- [ ] Render/parse `formal_use_case_v2_r<n>.txt` với alternative/exception và
  `UNKNOWN_FROM_SOURCE [Q-id]`.
- [x] Cross-projection validator so ID/content với canonical model.

## 4. Clarification Workflow

- [ ] Thêm state `waiting_requirement_clarification`.
- [ ] Thêm deterministic current-question selection.
- [ ] Thêm CLI/action `answer-clarification`.
- [ ] Lưu accepted/insufficient decision provenance và revision mới.
- [ ] Khóa mutation ngoài affected IDs.
- [ ] Cùng question/answer phải idempotent.
- [ ] Chặn `approve_checkpoint` khi còn blocking question.
- [ ] Hết blocking question mới mở `waiting_requirement_approval`.
- [ ] Sync clarification/approval cùng remote identity; `sync-run` không áp lại answer.

## 5. Downstream Và Niko Contract

- [ ] Ghi `requirement_contract_version` vào state.
- [ ] Tạo V1/V2 requirement input reader cho Phase 2-4.
- [ ] Designer/Validator V2 ưu tiên canonical JSON.
- [ ] Stable requirement IDs đi xuyên trace graph/work packets.
- [ ] Status trả current question và remaining count, không lộ source/path dài.
- [ ] Niko route/formatter hỏi đúng một câu và gửi đúng question ID.
- [ ] Giữ run V1 hoạt động không đổi.

## 6. Test Và Acceptance

- [ ] Source thiếu business value/quality tạo question, không tạo nội dung đoán.
- [x] UC2 giữ invalid data, cancel và connection failure ngoài Main Flow.
- [x] Duplicate rule và duplicate criterion được biểu diễn tách biệt.
- [x] LLM gắn non-blocking nhưng category hard-blocking bị Python nâng thành blocking.
- [ ] Non-blocking question không chặn approval và được render như assumption.
- [ ] Answer đủ/thiếu, answer lặp, answer khác, wrong question và wrong state có
  kết quả đúng contract.
- [x] Projection mismatch và invalid evidence fail closed.
- [ ] V1/V2 dual-read regression pass.
- [ ] Mock sync idempotent; real sync controlled smoke update đúng target.
- [ ] E2E qua Niko: start -> hỏi từng câu -> requirement approval -> Design.

## 7. Rollout

- [ ] Thêm `SDLC_REQUIREMENT_CONTRACT_VERSION`, default ban đầu `v1`.
- [ ] Chạy fixture smoke V2.
- [ ] Chạy controlled Jira/Telegram smoke UC2.
- [ ] Review artifact/trace/sync với project owner.
- [ ] Chuyển default sang `v2` sau khi mọi gate pass.
- [ ] Cập nhật docs trạng thái implemented và đóng backlog clarification.

