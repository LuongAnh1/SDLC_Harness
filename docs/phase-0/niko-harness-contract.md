# Contract Niko <-> SDLC Harness

Tài liệu này mô tả contract tối thiểu để `Niko_Agent` gọi `SDLC_Harness`. Đây chưa phải API kỹ thuật cuối cùng, nhưng là giao ước hành vi để Phase 1 triển khai không bị nhập nhằng.

## Vai Trò Hai Bên

```text
Niko_Agent
  - nhận tin nhắn từ người dùng
  - hiểu intent hội thoại
  - gọi SDLC Harness
  - đọc status
  - hỏi người dùng duyệt/sửa/chạy tiếp
  - trả kết quả ngắn gọn qua chat/dashboard

SDLC_Harness
  - tạo workflow run
  - lưu workflow state
  - sinh artifact theo phase
  - chạy Validator
  - sync tool hoặc mock tool
  - trả status/action tiếp theo cho Niko
```

Niko có thể đọc và điều khiển workflow, nhưng không phải nguồn sự thật của workflow state.

## API Tối Thiểu

### `start_or_get_run(input_ref)`

Tạo workflow run mới nếu chưa có active run cho source, hoặc trả active run hiện tại nếu đã có.

Input khái niệm:

```text
input_ref:
  source_type: dataset_use_case | jira_issue | local_file
  source_id: UC1 | JIRA-123 | path/to/file
  dataset: eTour | SMOS | iTrust | null
  requested_by: user/chat identifier
```

Output khái niệm:

```text
run_id
current_phase
status
artifact_summary
pending_checkpoint
allowed_next_actions
```

Phase 1 mặc định chỉ cần hỗ trợ `source_type=dataset_use_case` với `dataset=eTour`, `source_id=UC1`. Niko không tự load use case; SDLC Harness kiểm tra source_ref, load/refine/sync nếu cần rồi trả status.

### `get_status(run_id)`

Trả trạng thái hiện tại để Niko báo lại người dùng.

Output cần có:

```text
run_id
current_phase
status
latest_artifacts
pending_checkpoint
allowed_next_actions
last_sync
last_error
summary_for_user
```

`summary_for_user` nên là đoạn ngắn để Niko có thể đưa thẳng vào phản hồi hoặc dùng làm context cho câu trả lời.

### `approve_checkpoint(run_id)`

Xác nhận checkpoint hiện tại và cho workflow chạy sang phase tiếp theo.

Quy tắc:

- Nếu không có checkpoint đang chờ, trả lỗi hành vi `no_pending_checkpoint`.
- Nếu checkpoint đã hết hạn hoặc artifact bị stale, trả `checkpoint_not_current`.
- Nếu hợp lệ, chuyển state sang phase tiếp theo.

### `request_change(run_id, message)`

Ghi nhận yêu cầu sửa của người dùng tại checkpoint hoặc trong lúc workflow cần chỉnh artifact.

Phase đầu chưa cần phân loại quá phức tạp. Chỉ cần lưu change request và gắn vào artifact/phase hiện tại.

Về sau có thể phân loại:

```text
requirement_change
design_change
code_review_change
unknown_change
```

### `resume_run(run_id)`

Tiếp tục một run đang bị dừng do lỗi tạm thời hoặc đã được người dùng bổ sung thông tin.

Phase 1 có thể chưa cần triển khai thật, nhưng contract nên giữ sẵn để Niko không phải đổi luồng sau này.

### `cancel_run(run_id)`

Dừng một run. State chuyển sang `cancelled`, không xóa artifact đã sinh.

## Allowed Next Actions

`allowed_next_actions` là phần rất quan trọng để Niko không phải đoán người dùng có thể làm gì.

Ví dụ:

```text
waiting_requirement_approval:
  - approve_checkpoint
  - request_change
  - cancel_run
  - get_status

blocked:
  - request_change
  - resume_run
  - cancel_run
  - get_status
```

## Lỗi Tối Thiểu

Các lỗi hành vi nên ổn định để Niko xử lý được:

| Error code | Ý nghĩa |
| --- | --- |
| `run_not_found` | Không tìm thấy run id. |
| `invalid_action_for_state` | Action không hợp lệ với state hiện tại. |
| `no_pending_checkpoint` | Người dùng approve nhưng không có checkpoint chờ duyệt. |
| `checkpoint_not_current` | Checkpoint không còn hợp lệ vì artifact đã stale hoặc state đã đổi. |
| `source_not_found` | Không tìm thấy use case / issue / file đầu vào. |
| `tool_sync_failed` | Artifact đã sinh nhưng sync tool thất bại. |
| `phase_failed` | Phase đang chạy lỗi. |

## Giao Tiếp Ban Đầu

Phase 1 chốt cách giao tiếp đầu tiên là CLI trả JSON:

```text
CLI: Niko gọi command và đọc JSON output.
```

Lý do chọn CLI trước:

- dễ test tay trong terminal;
- dễ ghi log input/output;
- Niko có thể gọi qua subprocess mà chưa cần package/API server;
- contract JSON ổn định trước khi chọn local package hay HTTP API.

HTTP API hoặc local package để sau khi contract và state ổn định hơn.

## Sync Ownership

Sync tool thuộc trách nhiệm của SDLC Harness, không thuộc Niko.

Luồng đúng:

```text
SDLC Harness tạo artifact
-> SDLC Harness sync Jira/Confluence/GitHub hoặc mock tool
-> SDLC Harness ghi tool_sync_report
-> Niko gọi get_status(run_id)
-> Niko đọc sync result và báo lại người dùng
```

Niko không tự sync artifact SDLC lên tool. Niko chỉ điều phối hội thoại, gọi action và phản hồi dựa trên status/sync result mà Harness trả về.

## Phase 1 Và Điều Kiện Trước Phase 2

Thử nghiệm đầu của Phase 1 được phép dùng mock/local sync. Tuy nhiên trước khi sang Phase 2, cần có ít nhất một sync API thật hoặc adapter thật ở mức tối thiểu, ví dụ Jira comment hoặc Confluence page.

Trước Phase 2 cũng cần cập nhật phía Niko:

- thêm adapter/tool để gọi SDLC Harness CLI;
- đọc `allowed_next_actions` để không đoán hành động tiếp theo;
- hiển thị status/sync result cho người dùng;
- chuẩn bị runtime/provider phù hợp cho SDLC flow, trong đó DeepSeek là hướng ưu tiên thay vì phụ thuộc FCC cho workflow mới.

