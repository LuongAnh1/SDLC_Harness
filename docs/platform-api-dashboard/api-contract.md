# HTTP API Contract V1

Base path: `/api/v1`  
Content type: `application/json; charset=utf-8`

## Trạng Thái Triển Khai

Slice 2 hiện chỉ mở các endpoint đọc sau:

- `GET /api/v1/health`
- `GET /api/v1/runs?limit=<1..200>`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_key}`
- `GET /api/v1/dashboard/snapshot?limit=<1..200>`

Các endpoint tạo run, action, operation, event và config trong tài liệu này là
contract cho các slice tiếp theo. Mọi method ghi hiện trả `405
method_not_allowed`; health không cần auth, còn endpoint đọc khác dùng bearer
token khi `SDLC_API_TOKEN` được cấu hình. Service mặc định bind
`127.0.0.1:7788`; bind non-loopback bắt buộc có token.

## Response Chung

Read thành công trả workflow/status payload hiện tại và bổ sung metadata transport
nếu cần. Không đổi tên các field Niko đang dùng: `ok`, `run_id`, `current_phase`,
`status`, `summary_for_user`, `pending_checkpoint`, `allowed_next_actions`,
`latest_artifacts`, `quality_reports`, `last_sync`, `last_error`, `workspace`.

Read API không trả filesystem reference trong `latest_artifacts`. Mỗi artifact
được công bố thành metadata:

```json
{
  "latest_artifacts": {
    "requirement_model": {
      "url": "/api/v1/runs/run_.../artifacts/requirement_model",
      "media_type": "application/json",
      "size_bytes": 1234
    }
  }
}
```

Endpoint artifact trả envelope ổn định và parse JSON khi media type là JSON:

```json
{
  "ok": true,
  "run_id": "run_...",
  "artifact_key": "requirement_model",
  "media_type": "application/json",
  "size_bytes": 1234,
  "content": {}
}
```

Artifact phải đồng thời có key trong allowlist, được state hiện tại công bố và
nằm trong thư mục đúng run. Preview chỉ nhận JSON/Markdown/text UTF-8, tối đa 1
MiB. API che token/secret và local path trước khi serialize response.

Lỗi giữ contract máy đọc được:

```json
{
  "ok": false,
  "error_code": "invalid_run_action",
  "message": "Action is not allowed in the current state.",
  "run_id": "run_...",
  "operation_id": "op_..."
}
```

## Endpoint

### Health và snapshot

- `GET /api/v1/health`: process health, version và queue availability; không gọi LLM.
- `GET /api/v1/dashboard/snapshot`: danh sách run gọn, operation gần nhất, config
  đã mask và số liệu phục vụ render ban đầu.

### Runs

- `POST /api/v1/runs`: thay `start-or-get-run` và `start-from-jira`.
- `GET /api/v1/runs?limit=<n>`: danh sách run gọn cho dashboard.
- `GET /api/v1/runs/{run_id}`: thay `get-status`.
- `GET /api/v1/runs/{run_id}/events?after=<cursor>&limit=<n>`: timeline tăng dần.
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_key}`: chỉ đọc artifact key đã
  công bố trong state; không nhận filesystem path và chặn path traversal.

Body tạo run từ fixture:

```json
{
  "source": {"type": "dataset", "dataset": "etour", "use_case": "UC1"},
  "requested_by": "telegram:<user-id>",
  "sync_mode": "mock"
}
```

Body tạo run từ Jira:

```json
{
  "source": {"type": "jira", "issue_key": "SDLC-1"},
  "requested_by": "telegram:<user-id>",
  "sync_mode": "real"
}
```

### Actions

`POST /api/v1/runs/{run_id}/actions`

```json
{
  "action": "continue_run",
  "requested_by": "telegram:<user-id>",
  "sync_mode": "real",
  "arguments": {}
}
```

Action V1 ánh xạ các đường chạy hiện có:

| API action | CLI hiện tại |
| --- | --- |
| `answer_clarification` | `answer-clarification` |
| `approve_checkpoint` | `approve-checkpoint` |
| `continue_run` | `continue-run` |
| `sync_run` | `sync-run` |
| `revalidate_design` | `revalidate-design` |
| `revalidate_implementation_plan` | `revalidate-implementation-plan` |
| `revalidate_implementation` | `revalidate-implementation` |
| `adopt_run_workspace` | `adopt-run-workspace` |
| `cleanup_run_workspace` | `cleanup-run-workspace` |

`answer_clarification` bắt buộc có `question_id`, `answer`, `answered_by` trong
`arguments`. Approval có thể nhận `checkpoint_id` khi state công bố ID. Limit
override chỉ được nhận cho đúng field CLI hiện có và phải qua cùng validation.

Action chưa có workflow/CLI thật như `request_change`, `resume_run` hoặc
`cancel_run` không được quảng bá trong API contract hoặc dashboard.

Hai command vận hành `prepare-target-repo` và `experiment-design-consensus`
tiếp tục là CLI-only trong V1. Chúng không thuộc run lifecycle mà Niko điều
khiển; chỉ mở API admin riêng trong một kế hoạch sau nếu có nhu cầu thực tế.

### Operations

- `GET /api/v1/operations/{operation_id}`: trạng thái job và result/error cuối.
- Write endpoint trả `202 Accepted`:

```json
{
  "ok": true,
  "operation_id": "op_20261018_...",
  "operation_status": "queued",
  "run_id": "run_...",
  "status_url": "/api/v1/operations/op_20261018_..."
}
```

`run_id` có thể rỗng ở operation tạo run cho tới khi workflow xác định active run
hoặc tạo run mới. Result cuối chứa nguyên workflow payload.

### Config

- `GET /api/v1/config`: schema, effective value đã mask và source của từng key.
- `PUT /api/v1/config`: cập nhật các key được schema cho phép.
- `POST /api/v1/config/reset`: reset key/section trong runtime config.

## Idempotency

- Mọi `POST`/`PUT` gây side effect từ Niko hoặc dashboard phải gửi
  `Idempotency-Key`.
- Cùng key và cùng canonical payload trả lại operation cũ.
- Cùng key nhưng payload khác trả `409 idempotency_conflict`.
- Key được lưu bền vững cùng operation, không chỉ giữ trong process memory.
- API mất kết nối sau `202` không cho phép Niko tự động gọi CLI fallback.

## HTTP Status

| Status | Ý nghĩa |
| --- | --- |
| `200` | Read hoặc config operation đồng bộ thành công |
| `202` | Write action đã được queue/đã tồn tại theo idempotency |
| `400` | JSON/schema/argument không hợp lệ |
| `401` | Thiếu hoặc sai token khi auth được bật |
| `404` | Run, operation hoặc artifact không tồn tại |
| `405` | Method ghi/chưa được triển khai trong slice hiện tại |
| `409` | Action không hợp lệ theo state, duplicate conflict hoặc resource busy |
| `413` | Request body vượt giới hạn |
| `500` | Lỗi nội bộ đã được chuyển thành error payload |
| `503` | Queue/service chưa sẵn sàng |

## Sync Guard

- Side-effect action mặc định dùng `sync_mode=mock`.
- Ghi Jira/Confluence/GitHub chỉ xảy ra khi request chọn `real` và action cho phép.
- API không lấy `real` ngầm từ dashboard config cho request thiếu `sync_mode`.
- Response tiếp tục trả `last_sync` để Niko phân biệt workflow pass với sync partial.
