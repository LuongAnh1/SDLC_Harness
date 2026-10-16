# Requirement Clarification Workflow

## Mục Tiêu

Clarification là checkpoint riêng nằm trước requirement approval. Nó dùng để
thu thập quyết định nghiệp vụ còn thiếu; không phải validator repair và không
phải Phase 5 request-change.

```text
validator repair
  sửa output sai schema/grounding mà không cần human quyết định

clarification
  hỏi human khi source không đủ để chốt hành vi

request-change
  sửa artifact đã được approve hoặc phase phía sau
```

## State Machine V2

```text
requirement_drafting
  -> requirement_model_v2_r1 valid, không có blocking question
       -> waiting_requirement_approval
  -> requirement_model_v2_r1 valid, còn blocking question
       -> waiting_requirement_clarification
            -> answer chưa đủ
                 -> waiting_requirement_clarification, giữ/mở follow-up
            -> answer đủ, còn blocking question khác
                 -> requirement_model_v2_r<n+1>
                 -> waiting_requirement_clarification
            -> answer đủ, hết blocking question
                 -> requirement_model_v2_r<n+1>
                 -> render packet/formal
                 -> waiting_requirement_approval
```

`approve_checkpoint` không được phép chạy khi status là
`waiting_requirement_clarification` hoặc canonical model còn question
`effective_blocking=true,status=open`.

## Chọn Câu Hỏi Hiện Tại

Niko hỏi đúng một câu mỗi lượt. SDLC Harness chọn `current_question` bằng rule
xác định, không để Niko tự sắp xếp:

1. Chỉ lấy question `open`, `effective_blocking=true` và mọi `depends_on` đã
   resolved.
2. Sắp category theo thứ tự `business_rule`, `data_identity`, `api_contract`,
   `observable_behavior`, `acceptance_criteria`, rồi các category còn lại.
3. Trong cùng priority, sắp theo stable question ID.

Nếu còn blocking question nhưng không có question đủ điều kiện do dependency
cycle/missing reference, workflow chuyển `failed` với
`requirement_question_dependency_invalid`; không tự chọn ngẫu nhiên.

## Pending Checkpoint Contract

Khi chờ clarification, `get-status` trả:

```json
{
  "current_phase": "requirement_clarification",
  "status": "waiting_requirement_clarification",
  "requirement_contract_version": "v2",
  "pending_checkpoint": {
    "checkpoint_id": "checkpoint_requirement_clarification_<run_id>",
    "type": "requirement_clarification",
    "artifact_ref": "requirement_model_v2_r1",
    "current_question": {
      "id": "Q-3",
      "text": "Tiêu chí nào xác định hai cultural heritage record là duplicate?",
      "category": "data_identity",
      "affects": ["BR-1"],
      "answer_guidance": "Mô tả trường hoặc tổ hợp trường dùng để so trùng."
    },
    "remaining_blocking_count": 3
  },
  "allowed_next_actions": [
    "answer_clarification",
    "get_status",
    "sync_run",
    "cancel_run"
  ]
}
```

Status không trả toàn bộ source, evidence quote dài hoặc local path. Artifact
refs vẫn dùng relative path như contract hiện tại.

## Action `answer-clarification`

CLI dự kiến:

```bash
python -m sdlc_harness.cli answer-clarification \
  --run-id <run_id> \
  --question-id Q-3 \
  --answer "Duplicate khi trùng mã định danh cultural heritage." \
  --answered-by telegram:<user-id> \
  --sync-mode mock
```

Input bắt buộc:

| Field | Ý nghĩa |
| --- | --- |
| `run_id` | Run đang chờ clarification |
| `question_id` | Phải đúng `pending_checkpoint.current_question.id` |
| `answer` | Câu trả lời nguyên văn, sau trim không được rỗng |
| `answered_by` | Caller identity tối thiểu để audit |
| `sync_mode` | `mock` mặc định; caller phải ghi `real` rõ ràng |

Luồng xử lý:

```text
1. Lock run và đọc state/model revision hiện tại.
2. Guard status, contract version và current question identity.
3. Ghi nhận answer submission với hash để hỗ trợ idempotency.
4. Gọi Requirement Refiner structured update với source snapshot, canonical
   model hiện tại, current question và answer.
5. Chỉ cho phép sửa affected IDs, question/decision liên quan và artifact được
   sinh do thay đổi đó; thay đổi ngoài phạm vi bị validator chặn.
6. Validate schema, references, evidence, blocking policy và answer resolution.
7. Nếu answer chưa đủ: lưu decision status `insufficient`, giữ question mở và
   trả `follow_up_text` làm current question tiếp theo.
8. Nếu đủ: lưu decision status `accepted`, đóng/supersede question, tăng revision.
9. Recompute question queue, render packet/formal cùng revision và sync checkpoint.
10. Còn blocking question thì tiếp tục clarification; hết thì mở requirement approval.
```

## Decision Và Idempotency

- Cùng `question_id` và cùng answer sau normalization trả lại kết quả hiện tại,
  không gọi LLM và không tăng revision lần nữa.
- Cùng question nhưng answer khác chỉ được chấp nhận khi requirement chưa approve;
  decision mới ghi `supersedes_decision_id` và tạo revision mới.
- Question không phải current question bị từ chối để giữ hội thoại tuần tự.
- Sau requirement approval, mọi sửa đổi đi qua Phase 5 `request_change`.
- Answer submission chưa đủ vẫn được audit nhưng không được dùng làm confirmed
  requirement.

## Approval Contract

Khi hết blocking question:

```json
{
  "current_phase": "waiting_requirement_approval",
  "status": "waiting_requirement_approval",
  "requirement_contract_version": "v2",
  "latest_artifacts": {
    "requirement_model": ".runtime/runs/<run_id>/artifacts/requirement_model_v2_r4.json",
    "requirement_packet": ".runtime/runs/<run_id>/artifacts/requirement_packet_v2_r4.md",
    "formal_use_case": ".runtime/runs/<run_id>/artifacts/formal_use_case_v2_r4.txt",
    "requirement_decisions": ".runtime/runs/<run_id>/artifacts/requirement_decisions_v2_r4.json"
  },
  "pending_checkpoint": {
    "type": "requirement_approval",
    "artifact_ref": "requirement_model_v2_r4",
    "review_projection_ref": "requirement_packet_v2_r4"
  },
  "allowed_next_actions": [
    "approve_checkpoint",
    "request_change",
    "get_status",
    "sync_run",
    "cancel_run"
  ]
}
```

Non-blocking question còn mở được hiển thị trong Requirement Packet dưới
`Assumptions / Non-blocking Open Questions`; approval không biến chúng thành
confirmed requirement.

## Sync Jira/Confluence

- Clarification dùng cùng remote identity `source_ref + requirement phase` để
  update checkpoint cũ, không tạo comment/page cho từng câu hỏi.
- Mỗi revision sync packet mới nhất, current question, số blocking question còn
  lại và decision summary; không sync raw model đầy đủ nếu không cần review.
- Sync failure giữ state nghiệp vụ và ghi `last_sync` partial/failed. Retry dùng
  `sync-run`, không áp lại answer và không gọi LLM.
- Khi chuyển sang requirement approval, cùng remote target được update thành bản
  review cuối thay vì tạo target mới.

## Error Codes

| Error code | Điều kiện |
| --- | --- |
| `requirement_clarification_not_ready` | Run không ở state clarification |
| `requirement_question_not_current` | Question ID không phải câu hiện tại |
| `requirement_answer_empty` | Answer rỗng sau trim |
| `requirement_question_dependency_invalid` | Dependency cycle/missing ID |
| `requirement_answer_update_failed` | Structured update không hợp lệ sau bounded repair |
| `requirement_projection_mismatch` | Packet/formal khác canonical model |
| `requirement_approval_blocked` | Approve khi còn blocking question |

