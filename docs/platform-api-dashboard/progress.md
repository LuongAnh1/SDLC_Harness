# Progress: API Và Ops Dashboard

## 2026-10-18 - Chốt kế hoạch tài liệu

Đã khảo sát CLI dispatch, Settings, RuntimeStore, workflow ownership, Niko
`SdlcClient` và Niko Ops Dashboard. Kết luận: workflow/state/lock hiện tại đủ ổn
để thêm transport API mà không viết lại execution graph.

### Quyết định

- Tạo mốc nền tảng riêng trước Phase 5; Phase 5 vẫn là `request_change`.
- Dùng `ThreadingHTTPServer` và frontend thuần giống Niko.
- Tạo application service dùng chung cho CLI/API.
- Dùng operation queue bền vững cho tác vụ dài; mặc định một worker.
- Dashboard chỉ gọi API.
- Chuyển Niko theo feature flag và E2E; không auto fallback action ghi.
- Chưa đổi Java sang TypeScript trong mốc này.

### Trạng thái

- [x] Viết architecture và ownership boundary.
- [x] Viết HTTP API contract V1.
- [x] Viết operation/event contract.
- [x] Viết dashboard/config contract.
- [x] Viết Niko migration/cutover plan.
- [x] Viết implementation checklist.
- [x] Hoàn thành Slice 1 application service.

Mọi lỗi, thay đổi quyết định, test và controlled live run của quá trình triển khai
sẽ được nối tiếp theo ngày trong file này.


## 2026-10-18 - Hoàn thành Slice 1 Application Service

### Thay đổi

- Thêm `ApplicationCommand` bất biến và `ApplicationService` độc lập transport.
- Chuyển settings construction cùng toàn bộ dispatch Phase 1-4/operator command
  ra khỏi `interfaces/cli.py`.
- CLI giữ nguyên parser, JSON stdout, exit code và `sync_mode=mock` mặc định.
- Cập nhật các CLI mock target sang owner mới và thêm coverage trực tiếp cho
  settings mapping, special sync routing, experiment và mọi nhóm workflow.

### Test

- Focused application/CLI selection: `26 passed, 51 deselected`.
- Toàn bộ test file bị thay đổi: `61 passed`.
- Full suite hiện tại: `289 passed, 45 failed`. Các failure nằm trong progressive
  Design Validator/Phase 4 fixture và không nằm trong file workflow bị sửa ở Slice 1.
- Đối chiếu bằng worktree sạch tại commit nền `a0ffac7`: cùng nhóm CLI cho kết quả
  giống hệt `12 passed, 4 failed`; bốn failure Phase 4 đã tồn tại trước refactor.

### Kết luận

Các contract adapter trực tiếp của Slice 1 đã pass và application service sẵn sàng làm
seam cho Slice 2 Read API. Bộ failure workflow cũ cần được xử lý như một maintenance
task riêng, không mở rộng Slice 1 sang sửa Validator/Phase 4.

## 2026-10-18 - Dọn sạch regression suite sau Slice 1

### Nguyên nhân

- Progressive Design Validator đã thêm `attempt_recorder` và một lần retry JSON,
  nhưng test doubles cùng các test lỗi đầu ra vẫn dùng contract cũ.
- Developer đổi output chính sang `changed_file_blocks_v1`, còn một số assertion
  vẫn kiểm `changed_files_json_v1` hoặc yêu cầu LLM trả traceability metadata mà
  Python hiện tự suy ra từ work packet đã duyệt.
- Fake LLM router chưa nhận marker `Developer Batch Act v2` và
  `Developer Batch Repair v2`, nên trả nhầm Requirement JSON cho Developer.
- Fake repair trả lại đúng nội dung fake act, khiến atomic apply chặn hợp lệ vì
  không có thay đổi file thực tế và kéo theo nhiều test Phase 4 bị block.

### Sửa đổi

- Đồng bộ fake LLM router với prompt Developer v2.
- Cho fake repair tạo một thay đổi Java hợp lệ, xác định theo prompt hiện tại;
  guard production vẫn tiếp tục từ chối batch không tạo thay đổi thực tế.
- Cập nhật test doubles theo callback mới, kiểm đủ hai lần retry JSON và đổi
  expectation từ exception/`failed` sang response `blocked` của workflow hiện tại.
- Cập nhật assertion Developer output contract theo file-block format và ownership
  traceability hiện tại.

### Test

- Toàn bộ nhóm Phase 2, Requirement V2, Phase 3 và Phase 4: `143 passed`.
- Full regression suite: `337 passed, 0 failed` trong `321.36s`.

## 2026-10-18 - Hoàn thành Slice 2 Read API

### Thay đổi

- Thêm `ApplicationReadService` để list run, đọc status qua application service,
  dựng dashboard snapshot và phục vụ artifact công khai.
- Thêm stdlib `ThreadingHTTPServer` với health, list/get run, artifact preview và
  dashboard snapshot; hỗ trợ graceful shutdown qua `SIGINT`/`SIGTERM`.
- Thêm cấu hình host/port/token/shutdown timeout. Loopback có thể chạy không token;
  bind non-loopback bị từ chối nếu chưa cấu hình bearer token.
- Dùng artifact allowlist đóng, xác minh artifact nằm trong đúng run directory,
  chặn traversal, giới hạn preview 1 MiB và loại local path/secret khỏi response.
- Giữ toàn bộ write method ở trạng thái `405`; operation queue, write API, event
  và config CRUD tiếp tục thuộc các slice sau.

### Test

- Focused Read API và Application Service: `32 passed`.
- Bao phủ response `200/400/401/404/405/413/500`, auth, traversal, artifact
  boundary, redaction, read không mutation và graceful shutdown.
- Full regression suite: `355 passed, 0 failed` trong `489.53s`.
