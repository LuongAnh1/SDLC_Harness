# Phase 1 Flow

Requirement Contract V2 là contract runtime duy nhất.

## `start-or-get-run` Và `start-from-jira`

```text
validate input
-> derive source_ref
-> return active run nếu source đang có run chưa kết thúc
-> load fixture hoặc Jira issue
-> tạo run với requirement_contract_version=v2
-> ghi use_case_packet_v1.md và source_snapshot_v1.json
-> Requirement Refiner sinh requirement_model_v2_r1.json
-> Python validator kiểm tra schema, IDs, references, evidence và semantics
-> nếu output sai kỹ thuật: một bounded repair call
-> nếu còn blocking question: waiting_requirement_clarification
-> nếu không còn blocking question: render packet/formal V2 và waiting_requirement_approval
-> sync Jira/Confluence theo sync mode
-> trả JSON status cho Niko
```

`use_case_packet_v1` và `source_snapshot_v1` là schema của raw source artifact,
không phải Requirement Contract V1.

## `answer-clarification`

Action cần `run_id`, `question_id`, câu trả lời và người trả lời. Harness chỉ nhận
đúng current question, ghi decision provenance, tăng requirement revision, validate
lại toàn bộ model rồi chọn câu blocking tiếp theo. Khi hết câu blocking, run chuyển
sang `waiting_requirement_approval`.

## `get-status` Và `sync-run`

`get-status` chỉ đọc state. `sync-run` sync lại checkpoint hiện tại, không gọi LLM
và không tạo requirement revision mới.

## Approval Gate

Requirement approval chỉ hợp lệ khi canonical model V2 pass validation và không
còn blocking question. Downstream đọc canonical JSON; packet và formal use case là
projection phục vụ review và context.

## V1 Retirement

V1 refiner, validator, prompts và downstream legacy reader đã bị gỡ. State ghi
contract khác `v2` fail closed với `requirement_contract_unsupported`.
