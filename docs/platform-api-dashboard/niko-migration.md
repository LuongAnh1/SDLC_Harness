# Chuyển Niko Từ CLI Sang HTTP API

Trạng thái: **Slice 8 đang triển khai — smoke Niko/API fake/mock đã pass, CLI vẫn là mặc định cho tới khi live cutover có đủ evidence.**

## Nguyên Tắc

Niko giữ `SdlcResult`, intent detection, background reply và formatter hiện tại.
Chỉ transport thay đổi. API payload phải tương thích với các field Niko đang đọc.

Không chạy cùng một write action qua cả API và CLI để “so sánh”. Contract test dùng
runtime/fixture tách biệt. Live dual period nghĩa là có thể chọn transport bằng
config, không phải fan-out một request tới hai nơi.

## Cấu Hình Dự Kiến

- `NIKO_SDLC_TRANSPORT=cli|api`
- `NIKO_SDLC_API_BASE_URL=http://127.0.0.1:7788`
- `NIKO_SDLC_API_TOKEN=`
- `NIKO_SDLC_API_REQUEST_TIMEOUT_SECONDS=10`
- `NIKO_SDLC_API_OPERATION_TIMEOUT_SECONDS=1800`
- `NIKO_SDLC_API_POLL_INTERVAL_SECONDS=2`

Request timeout chỉ áp dụng cho HTTP round trip. Operation timeout là thời gian
Niko theo dõi job; hết thời gian này Niko báo chưa có kết quả và giữ operation ID,
không kết luận workflow thất bại.

## Các Giai Đoạn Cutover

### 1. API chưa nối Niko

- CLI là production transport.
- Chạy API contract test bằng fake LLM/runtime tạm.
- So sánh payload CLI/API cho read và các workflow fixture độc lập.

### 2. Dual transport có feature flag

- Niko có `CliSdlcTransport` và `HttpSdlcTransport` dưới cùng client facade.
- Mặc định vẫn là CLI.
- Smoke API từ Niko bằng fixture/mock sync.
- Không tự fallback write action. Read fallback chỉ dùng khi được cấu hình rõ và
  phải ghi runtime log rằng transport đã đổi.

### 3. API primary

- Chuyển local config sang `NIKO_SDLC_TRANSPORT=api`.
- Chạy ít nhất một E2E thật từ Jira qua requirement, design, plan, code, QA và
  Code Review checkpoint.
- Theo dõi operation timeout, duplicate action, restart và sync partial.
- CLI vẫn dùng cho operator recovery có chủ đích.

API process nhận ba override phục vụ smoke cô lập mà không sửa runtime thật:

```bash
python -m sdlc_harness.interfaces.http --host 127.0.0.1 --port 7788 --runtime-root .runtime-smoke
```

Smoke tự động nằm ở Niko và phải gọi đúng process trên, không thay bằng HTTP stub:

```bash
python -m pytest tests/test_sdlc_api_process_integration.py -q
```

### 4. Hoàn tất cutover

- Bỏ subprocess CLI khỏi runtime path của Niko sau khi regression và E2E pass.
- Giữ CLI SDLC Harness làm công cụ debug/automation tương thích.
- Cập nhật docs Niko và SDLC để API là integration boundary hiện hành.

## Error Mapping

- HTTP không kết nối được -> `sdlc_api_unavailable`.
- Request timeout trước khi nhận `202` -> trạng thái chưa rõ; truy vấn bằng
  idempotency key/operation lookup, không gọi CLI.
- Operation failed -> giữ nguyên `error_code`, `message`, `run_id` từ Harness.
- Poll timeout -> `sdlc_operation_pending`, kèm operation ID để tiếp tục theo dõi.
- `409 invalid_run_action` -> Niko lấy lại run status rồi format action hợp lệ.
- `last_sync.status=partial_failed|failed` -> Niko yêu cầu `sync_run`, không chạy
  lại approval hoặc workflow phase.

## Bằng Chứng Cutover

- Contract parity cho mọi action Niko đang hỗ trợ.
- Unit test HTTP client: auth, 202/poll, error JSON, invalid JSON, timeout, restart.
- Integration test API với fake LLM cho checkpoint/state transition.
- Test idempotency khi gửi lại cùng request.
- Test API process restart trong operation.
- Một controlled live E2E qua Niko/API; ghi run ID và lỗi/cách xử lý vào progress.
