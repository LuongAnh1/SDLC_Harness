# HTTP API Contract V1

Base path: `/api/v1`  
Content type: `application/json; charset=utf-8`

## Trạng Thái Triển Khai

Slice 6 hiện mở các endpoint sau:

- `GET /api/v1/health`
- `GET /api/v1/runs?limit=<1..200>`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/events?after=<0..>&limit=<1..200>`
- `GET /api/v1/runs/{run_id}/operations?limit=<1..200>`
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_key}`
- `GET /api/v1/dashboard/snapshot?limit=<1..200>`
- `GET /api/v1/dashboard/schema`
- `POST /api/v1/runs`
- `POST /api/v1/runs/{run_id}/actions`
- `GET /api/v1/operations/{operation_id}`
- `GET /api/v1/config`
- `PUT /api/v1/config`
- `POST /api/v1/config/reset`

Config mutation chạy đồng bộ, không tạo operation và bị chặn khi operation đang
queued/running. Các method khác trả `405 method_not_allowed`; health không cần
auth, còn endpoint khác dùng bearer token khi `SDLC_API_TOKEN` được cấu hình.
Service mặc định bind
`127.0.0.1:7788`; bind non-loopback bắt buộc có token.

Dashboard cùng origin được phục vụ tại `GET /` cùng hai asset allowlist
`/assets/dashboard.css` và `/assets/dashboard.js`. Ba route tĩnh không nhúng token
và không yêu cầu auth; mọi API ngoài health giữ nguyên bearer-token boundary.

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
- `GET /api/v1/dashboard/schema`: source form, action argument/confirmation,
  workflow topology, phase/status mapping và repair limit mặc định cho UI.

### Runs

- `POST /api/v1/runs`: thay `start-or-get-run` và `start-from-jira`.
- `GET /api/v1/runs?limit=<n>`: danh sách run gọn cho dashboard.
- `GET /api/v1/runs/{run_id}`: thay `get-status`.
- `GET /api/v1/runs/{run_id}/events?after=<cursor>&limit=<n>`: timeline tăng dần; mặc định `after=0`, `limit=100`, giới hạn tối đa 200.
- `GET /api/v1/runs/{run_id}/operations?limit=<n>`: operation summary của đúng
  run; không trả terminal result đầy đủ và không lộ request/idempotency hash.
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_key}`: chỉ đọc artifact key đã
  công bố trong state; không nhận filesystem path và chặn path traversal.

Response event page:

```json
{
  "ok": true,
  "run_id": "run_...",
  "events": [],
  "count": 0,
  "after": 0,
  "next_cursor": 0,
  "has_more": false
}
```

Event được trả theo `sequence` tăng dần. `next_cursor` là sequence cuối đã trả, hoặc
giữ nguyên `after` khi page rỗng. Endpoint read không tạo hoặc sửa state/event; run
không tồn tại trả `404 run_not_found`, cursor/limit sai trả `400 invalid_query`.

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
Mọi body reject field lạ; `requested_by` bắt buộc, `arguments` mặc định `{}` và
`sync_mode` mặc định `mock` dù `.env` đang đặt `real`.

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
  "status_url": "/api/v1/operations/op_20261018_...",
  "idempotency_replayed": false
}
```

`run_id` có thể rỗng ở operation tạo run cho tới khi workflow xác định active run
hoặc tạo run mới. Result cuối chứa nguyên workflow payload.
Operation GET trả payload đã che secret/local path và chuyển `latest_artifacts`
thành metadata URL giống run status. Dashboard snapshot chỉ lấy operation summary,
không nhúng toàn bộ result.

### Config

- `GET /api/v1/config`: schema, effective value đã mask và source của từng key.
- `PUT /api/v1/config`: cập nhật các key được schema cho phép.
- `POST /api/v1/config/reset`: reset `keys`, `section` hoặc cả hai trong runtime config.

`PUT` dùng body `{"values": {...}}`. Secret rỗng hoặc `********` có nghĩa là giữ
nguyên. Reset chỉ xóa runtime override để giá trị ưu tiên thấp hơn có hiệu lực.
Bootstrap field là read-only; field đến từ OS trả `409 config_locked`; save/reset
trong lúc operation queued/running trả `409 config_busy`; config JSON hỏng trả
`500 config_corrupted`.

## Idempotency

- Mọi workflow `POST` gây side effect từ Niko hoặc dashboard phải gửi
  `Idempotency-Key`.
- Config save/reset là mutation đồng bộ, atomic và không tạo operation nên không
  dùng `Idempotency-Key`; gửi lại cùng payload cho cùng kết quả hiệu lực.
- Key phải có nội dung và dài tối đa 256 ký tự; runtime chỉ lưu SHA-256 của key.
- Cùng key và cùng canonical payload trả lại operation cũ.
- Cùng key nhưng payload khác trả `409 idempotency_conflict`.
- Key được lưu bền vững cùng operation, không chỉ giữ trong process memory.
- API mất kết nối sau `202` không cho phép Niko tự động gọi CLI fallback.
- Cùng run chỉ có một operation chưa terminal; key mới trong lúc run bận trả
  `409 run_operation_in_progress`.
- Restart chuyển cả `queued` và `running` thành `interrupted`; retry sau khi đọc
  workflow state phải dùng idempotency key mới.

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
