# Phase 1 Flow

Tài liệu này mô tả luồng xử lý bắt buộc của Phase 1. Implementer không tự đổi thứ tự bước nếu không cập nhật lại docs.

## Command `start-or-get-run`

Input mặc định:

```text
source_type=dataset_use_case
dataset=eTour
use_case=UC1
requested_by=<id>
```

Luồng xử lý:

```text
1. Validate input
2. Tạo source_ref từ dataset/use_case: dataset_use_case:eTour:UC1
3. Kiểm tra active run theo source_ref
4. Nếu active run tồn tại và chưa done/cancelled: trả status run hiện tại
5. Nếu chưa có active run: load fixture `fixtures/etour/UC1.txt`
6. Tạo `run_id`
7. Tạo folder `.runtime/runs/<run_id>/`
8. Ghi state ban đầu: `requirement_drafting`
9. Tạo `use_case_packet_v1.md`
10. Gọi Requirement Refiner LLM để tạo `formal_use_case_v1.txt`
11. Chạy Formal Use Case Validator tối thiểu
12. Tạo `formal_check_report_v1.json`
13. Tạo `requirement_packet_v1.md` cho người duyệt
14. Chạy Requirement Validator cấu trúc + guardrail ngữ nghĩa nhẹ
15. Nếu validator fail, gọi Requirement Refiner repair `requirement_packet_v1` một vòng
16. Nếu repair vẫn fail, ghi `state.status=failed`, `last_error`, rồi trả JSON lỗi
17. Tạo `trace_graph_v1.json`
18. Sync Jira/Confluence theo `SDLC_SYNC_MODE`
19. Ghi `tool_sync_report_v1.json`
20. Cập nhật active run index `.runtime/index/active_runs.json`
21. Cập nhật state: `waiting_requirement_approval`
22. Trả JSON status
```

## Command `start-from-jira`

Input:

```text
issue_key=SDLC-1
requested_by=<id>
```

Luồng xử lý:

```text
1. Validate input
2. Tạo source_ref: jira_issue:<KEY>
3. Kiểm tra active run theo source_ref
4. Nếu active run tồn tại và chưa done/cancelled: trả status run hiện tại
5. Nếu chưa có active run: fetch Jira issue bằng Atlassian API
6. Đóng gói summary/description/comment thành source text
7. Chạy chung luồng tạo formal use case, requirement packet, trace graph và sync checkpoint như fixture source
8. Trả JSON status
```

Jira source intake thuộc chiều kéo context xuống. Real sync thuộc chiều đẩy
checkpoint đã xử lý lên Jira/Confluence.

## Command `get-status`

Input:

```text
run_id=<run_id>
```

Luồng xử lý:

```text
1. Tìm `.runtime/runs/<run_id>/state.json`
2. Nếu không có, trả `run_not_found`
3. Đọc latest artifact refs
4. Đọc latest sync report
5. Trả JSON status cho Niko
```

## Command `sync-run`

Input:

```text
run_id=<run_id>
```

Luồng xử lý:

```text
1. Tìm `.runtime/runs/<run_id>/state.json`
2. Nếu không có, trả `run_not_found`
3. Đọc `latest_artifacts`
4. Sync lại requirement checkpoint theo `SDLC_SYNC_MODE`
5. Nếu report đã có Jira/Confluence `external_id`, update target cũ
6. Ghi lại `tool_sync_report_v1.json`
7. Cập nhật `last_sync` trong state
8. Trả JSON status cho Niko/operator
```

`sync-run` không gọi LLM, không refine lại requirement và không tạo artifact
mới. Lệnh này chỉ dùng để đồng bộ lại checkpoint đã có.

## State Cuối Của Phase 1

Sau `start-or-get-run` thành công, state cuối phải là:

```text
waiting_requirement_approval
```

`allowed_next_actions` phải gồm:

```text
approve_checkpoint
request_change
cancel_run
get_status
```

## Summary Cho Niko

`summary_for_user` cần đủ ngắn để Niko có thể báo lại ngay:

```text
Đã chuẩn hóa eTour UC1 thành requirement packet v1 bằng Requirement Refiner và đồng bộ checkpoint. Run đang chờ anh duyệt requirement trước khi chuyển sang design.
```

Niko có thể chỉnh giọng văn sau này, nhưng Phase 1 CLI phải trả nội dung đủ thông tin.

## Guardrail Requirement Packet

Requirement Refiner sinh hai artifact khác vai trò:

- `formal_use_case_v1.txt`: bản máy đọc theo grammar TraceDev.
- `requirement_packet_v1.md`: bản checkpoint cho người duyệt.

Requirement Validator phải chạy trên `requirement_packet_v1.md` trước khi sync.
Ngoài kiểm tra đủ section, validator còn chặn các lỗi thô sau:

- nhánh hủy, lỗi kết nối, gián đoạn hoặc exception bị đưa vào `Postconditions`;
- exception bị viết như `Acceptance Criteria` thành công;
- quality/non-functional requirement bị đưa vào `Postconditions` hoặc
  `Acceptance Criteria`;
- `Source Evidence` trích từ formal use case do model sinh ra thay vì nguồn gốc
  ban đầu.

Nếu validator fail, Phase 1 không nới lỏng luật ngay. SDLC Harness đưa
`ValidationReport` và packet hiện tại quay lại Requirement Refiner để repair một
vòng. Chỉ packet sau repair pass validator mới được ghi artifact và sync lên
tool. Nếu repair vẫn fail, run được ghi `status=failed` để operator có thể tra
lại bằng `get-status`.

## Niko Không Kéo Use Case Trực Tiếp

Khi người dùng bảo Niko thực hiện một use case, Niko không tự đọc fixture/Jira/dataset. Niko gọi SDLC Harness:

```text
Niko -> start-or-get-run(source_ref)
SDLC Harness -> load/refine/validate/sync nếu cần
SDLC Harness -> trả status
Niko -> phản hồi người dùng
```

Nếu use case đã có active run, SDLC Harness trả run hiện tại. Niko không tự quyết định load lại hay refine lại.

## Failure Flow Tối Thiểu

| Trường hợp | Error code |
| --- | --- |
| Dataset không hỗ trợ | `source_not_supported` |
| Use case không tồn tại | `source_not_found` |
| Không ghi được runtime folder | `runtime_write_failed` |
| Requirement Refiner lỗi | `requirement_refiner_failed` |
| Requirement Validator lỗi | `requirement_validator_failed` |
| Run id không tồn tại khi get-status | `run_not_found` |
| State file lỗi JSON | `state_corrupted` |

Khi lỗi, CLI vẫn trả JSON với `ok=false`.

## Flow V2 - Planned

Run được tạo với `requirement_contract_version=v2` sẽ dùng flow đã chốt tại
`requirement-v2/clarification-workflow.md`:

```text
load source
-> Requirement Refiner sinh structured requirement model
-> validate schema/evidence/references và tính effective_blocking bằng Python
-> nếu còn blocking question: waiting_requirement_clarification
-> answer-clarification từng câu, ghi decision và revision mới
-> hết blocking question: render packet/formal projections
-> waiting_requirement_approval
```

Clarification là state hợp lệ, không phải validator failure. Validator repair chỉ
sửa output sai schema/grounding; nó không tự trả lời câu hỏi nghiệp vụ. Approval
bị chặn cho tới khi canonical model không còn blocking question mở.
