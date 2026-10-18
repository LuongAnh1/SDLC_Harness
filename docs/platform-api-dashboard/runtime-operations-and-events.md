# Runtime Operations Và Events

## Mục Đích

HTTP request không được mở suốt một lượt LLM/workflow dài. Operation là record vận
hành của một lần gọi application service; nó không thay thế workflow state.

## Layout Hiện Hành

```text
.runtime/
  config.json
  operations/
    op_<timestamp>_<suffix>.json
  idempotency/
    <hash>.json
  runs/<run_id>/
    state.json
    events.jsonl
    artifacts/
    sync/
```

`.runtime/README.md` là bản đồ vận hành đặt ngay cạnh dữ liệu. README được sinh
từ source được track trong repo để runtime mới hoặc runtime vừa reset vẫn mô tả
đúng layout, lifecycle, retention và ranh giới bảo mật hiện hành.

Operation JSON tối thiểu:

```json
{
  "operation_id": "op_...",
  "status": "queued",
  "command": "continue_run",
  "run_id": "run_...",
  "request_hash": "sha256:...",
  "idempotency_key_hash": "sha256:...",
  "created_at": "...",
  "started_at": null,
  "finished_at": null,
  "result": null,
  "error": null
}
```

Không lưu bearer token, API key hoặc plaintext idempotency key.

## Lifecycle

```text
queued -> running -> succeeded
                  -> failed
                  -> interrupted
```

- `queued`: đã persist trước khi trả `202`.
- `running`: worker đã nhận và ghi thời điểm bắt đầu.
- `succeeded`: lưu workflow payload cuối.
- `failed`: lưu error payload ổn định.
- `interrupted`: service restart khi operation đang chạy; không tự replay action ghi.

Khi restart, cả operation `queued` và `running` được đánh dấu `interrupted`; không
tự replay. Caller
đọc workflow status trước, sau đó chỉ gửi action mới khi `allowed_next_actions`
cho phép. Cơ chế này tránh chạy lại sync/approval/code generation âm thầm.

## Queue Và Worker

- Queue bounded; đầy queue trả `503 operation_queue_full`.
- V1 cố định đúng một worker để giữ thứ tự và kiểm soát chi phí LLM.
- `SDLC_API_QUEUE_LIMIT=32` là giới hạn mặc định cho operation đang chờ.
- Worker gọi `ApplicationService`, không gọi CLI subprocess.
- Operation terminal luôn được persist kể cả workflow trả `HarnessError`.
- Shutdown dừng nhận request mới, chờ operation hiện tại trong timeout cấu hình,
  rồi đánh dấu interrupted nếu chưa hoàn thành.
- Mỗi run chỉ có một operation chưa terminal; submit key mới khi run đang bận trả
  `409 run_operation_in_progress`.

## Event Contract

Mỗi dòng `events.jsonl` là một JSON object:

```json
{
  "version": "event_v1",
  "event_id": "evt_...",
  "sequence": 42,
  "timestamp": "...",
  "run_id": "run_...",
  "operation_id": "op_...",
  "category": "workflow",
  "type": "validator_completed",
  "phase": "implementation_testing",
  "status": "implementation_repair_needed",
  "message": "Validator requested implementation repair.",
  "data": {"iteration": 2, "blocking_count": 1}
}
```

Event dùng cho timeline/flow visualization và debug. Event không được trở thành
nguồn quyết định state; dashboard luôn lấy state hiện tại từ `state.json` qua API.

Các event tối thiểu: operation queued/started/completed/failed/interrupted,
workflow state changed, checkpoint opened/approved, LLM call started/completed,
repair iteration, verification result, validator result, QA result và sync result.
Prompt/output đầy đủ chỉ nằm trong artifact debug đã kiểm soát; event chỉ chứa
metadata gọn và đã lọc secret.

## Retry Và Duplicate

- Retry HTTP read là an toàn.
- Retry write dùng cùng idempotency key.
- Không tự tạo key mới sau timeout nếu chưa kiểm tra operation/status.
- CLI và API cùng dùng run lock nhưng idempotency API không bảo vệ một CLI action
  được gọi thủ công; operator phải tránh chạy CLI ghi đồng thời với API.

## Lưu Trữ, Cursor Và Khôi Phục

- `sequence` bắt đầu từ 1 và tăng đơn điệu riêng trong từng run; cursor API chính là
  sequence cuối caller đã nhận.
- Append dùng event lock riêng theo run để CLI và API không cấp trùng sequence.
- Mỗi dòng được flush xuống disk sau khi ghi. Nếu process chết giữa một dòng, lần
  append sau cắt phần tail chưa hoàn tất rồi tiếp tục sequence kế tiếp.
- JSON hỏng ở một dòng hoàn chỉnh hoặc sequence không liên tục trả
  `event_log_corrupted`; hệ thống không âm thầm bỏ qua lỗi giữa file.
- Event được giữ tới khi xóa cả run. Slice 4 chưa rotation hoặc tự xóa event.

## Ranh Giới Dữ Liệu

Event data dùng allowlist theo event type và giới hạn 4 KiB. LLM event chỉ giữ mode,
model, JSON mode, finish reason, duration và token counts. Verification chỉ giữ status,
exit code, timeout, duration và số test phát hiện/thực thi. Sync chỉ giữ phase, mode,
status và trạng thái target. Prompt, output, clarification answer, stdout/stderr, shell
command, raw exception, credential URL và local path không được persist vào event.

Lỗi ghi event được ghi vào operational logger nhưng không rollback workflow state hoặc
đổi kết quả operation. `state.json` tiếp tục là nguồn quyết định hiện tại.
