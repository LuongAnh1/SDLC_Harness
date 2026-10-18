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

## 2026-10-18 - Hoàn thành Slice 3 Operation Queue và Write API

### Thay đổi

- Thêm operation/idempotency JSON store atomic và queue bounded dùng cố định một
  worker; operation được persist trước khi API trả `202`.
- Mở create-run, run-action và operation-status API; ánh xạ các action Phase 1-4,
  adoption và cleanup qua `ApplicationService`, không gọi CLI subprocess.
- Bắt buộc `Idempotency-Key`, replay cùng request, chặn payload conflict và chặn
  hai operation chưa terminal trên cùng run.
- Chuyển source/run mutation lock lên application service chung để CLI/API cùng
  serialize; workspace lock tiếp tục nằm trong workflow hiện hành.
- Restart/shutdown không replay: mọi operation `queued` hoặc `running` còn dở
  được kết thúc ở `interrupted`.
- Health và dashboard snapshot đã phản ánh queue/operation; terminal result dùng
  public projection để che secret/local path và công bố artifact metadata.

### Test

- Focused operation, HTTP API và Slice 1-2 regression: `47 passed`.
- Bao phủ persist-before-ack, idempotency replay/conflict, queue full, same-run
  conflict, worker error, restart/shutdown interrupted, schema/action mapping,
  auth và public result projection.
- Full regression suite: `370 passed, 0 failed` trong `417.99s`.

### Lỗi phát hiện và cách xử lý

- Lượt full regression đầu tiên phát hiện `PermissionError: [WinError 5]` khi
  worker thay file operation bằng atomic replace đúng lúc HTTP polling đọc cùng
  file trên Windows.
- `OperationStore` đã serialize đọc/ghi trong cùng process bằng `RLock` và retry
  ngắn, có giới hạn cho atomic replace khi Windows tạm giữ file.
- Bổ sung stress test với 200 lượt ghi và 400 lượt đọc đồng thời; test này cùng
  toàn bộ regression suite đã pass sau bản sửa.

## 2026-10-18 - Dọn runtime trước Slice 4

- Xóa sáu run E2E đã kết thúc ở `code_review_approved`, hai run SDLC-2 lỗi/dở,
  một run design consensus thử nghiệm và các thư mục `live-tests`,
  `e2e-v2-prompt-retry` không còn được code tham chiếu.
- Giữ `run_20261017_131015_4fa470b4` của SDLC-4 đang
  `waiting_design_approval`; viết lại `active_runs.json` chỉ còn source này.
- Xóa run/workspace lock của các run đã xóa; giữ lock của SDLC-4 và
  `worktree-registry.lock`.
- Bổ sung `code_review_approved` vào `TERMINAL_STATUSES` để lần chạy sau cùng
  source không nhận lại run đã hoàn tất vòng đời SDLC Harness.
- Tạo `.runtime/README.md` làm bản đồ state/operation/event/artifact/lock; Slice 4
  sẽ chuyển nội dung này thành README được sinh từ source có version trong repo.

## 2026-10-18 - Hoàn thành Slice 4 Runtime Events và Observability

### Thay đổi

- Xóa run SDLC-4 còn lại, xóa run lock tương ứng và đưa `active_runs.json` về `{}`;
  runtime bắt đầu Slice 4 không có run lịch sử để backfill.
- Thêm `EventStore` JSONL theo run với sequence/cursor, event lock, append flush, phục
  hồi partial tail và báo lỗi corruption ở dòng hoàn chỉnh.
- Thêm execution context bằng `ContextVar`; create-run buffer event queued/started cho
  tới khi workflow xác định `run_id`, sau đó flush đúng thứ tự và gắn `operation_id`.
- Instrument operation, workflow state, checkpoint, LLM, repair, verification,
  validator, QA và sync bằng metadata allowlist; lỗi observability không làm đổi kết
  quả nghiệp vụ.
- Mở `GET /api/v1/runs/{run_id}/events` với `after`, `limit`, `next_cursor` và
  `has_more`; bật `capabilities.events=true`.
- Chuyển `.runtime/README.md` thành nội dung `runtime_layout_v2` sinh từ source được
  track; RuntimeStore, OperationStore và EventStore cùng đảm bảo README hiện hành.
- Giữ `code_review_approved` trong terminal status và thêm regression test để source
  đã hoàn tất không tái sử dụng run cũ.

### Test

- Focused events/runtime/operations/HTTP/Application Service: `59 passed`.
- Bao phủ sequence, pagination, restart, concurrent append, tail recovery, corruption,
  traversal, README idempotent, context create-run, fake LLM, verification, redaction,
  auth, cursor/limit và read không mutation.
- Full regression lần đầu: `381 passed, 1 failed` trong `565.38s`; failure nằm ở concurrent README update trên Windows và đang được sửa trước lần chạy cuối.

### Lỗi phát hiện và xử lý

- Lần compile đầu phát hiện escape newline trong source sinh `events.jsonl` tạo
  `SyntaxError`; đã sửa literal `\n` và compile package sạch trước khi chạy test.
- Lệnh cleanup PowerShell đầu tiên bị shell ngoài nội suy biến `$runtime`/`$run`; đã
  chuyển sang script PowerShell có kiểm tra resolved path nằm trong `.runtime` trước
  khi xóa.

- Full suite đầu tiên phát hiện `PermissionError: [WinError 5]` khi nhiều EventStore
  đồng thời atomic-replace `.runtime/README.md`. README writer đã được serialize bằng
  `RLock`, đồng thời retry ngắn có giới hạn và chấp nhận kết quả nếu process khác đã
  ghi đúng nội dung. Event log/sequence không bị corruption trong lỗi này.

### Kết quả cuối

- Focused suite cuối sau hardening message/sync: `45 passed` trong `18.57s`.
- Full regression cuối trên đúng bản bàn giao: `384 passed, 0 failed` trong `519.38s`.
- Slice 4 hoàn thành; mốc tiếp theo là Slice 5 Ops Dashboard.

- Rà bảo mật cuối phát hiện event `message` còn nhận chuỗi caller; đã khóa message theo
  event type để raw prompt/exception không thể lọt qua field này. Đồng thời bỏ type
  assert thừa ở sync wrapper để giữ khả năng dùng test double. Hai test operation
  failed/interrupted và mock sync metadata được bổ sung trước full regression cuối.

- Full regression dùng một fixture còn trỏ default runtime root nên tái tạo file lock của
  run cũ dù không tạo lại run/state. Sau khi test kết thúc đã xác minh thư mục `runs/`
  rỗng, `active_runs.json` là `{}` và xóa lock sót; runtime bàn giao chỉ giữ
  `worktree-registry.lock`.

## 2026-10-18 - Hoàn thành Slice 5 Ops Dashboard

### Thay đổi

- Thêm dashboard cùng origin bằng HTML/CSS/JavaScript thuần, asset allowlist và CSP;
  API token chỉ được giữ theo phiên trong `sessionStorage`.
- Thêm dashboard schema dùng chung action metadata với HTTP validation, gồm source
  form, action argument/confirmation, workflow topology và repair limit mặc định.
- Thêm operation summary theo run; run detail ghép state, operation và event cursor
  để hiển thị graph, checkpoint, repair count, artifact, quality và timeline.
- Mở tạo run từ Jira/dataset và toàn bộ action API hợp lệ; UI lấy giao giữa
  `allowed_next_actions` và action catalog, dùng idempotency key, poll operation tới
  terminal và khóa action khi run đang bận.
- `sync_mode` giữ mặc định `mock`; real/both, cleanup và các thao tác nhạy cảm có
  bước xác nhận cụ thể. Artifact/API content luôn được escape hoặc đưa qua `textContent`.

### Test

- Focused API/operation/frontend: `35 passed` trong `18.95s`.
- Frontend view-model dùng Node built-in runner, không thêm npm dependency; bao phủ
  phase-to-node, action visibility, repair limits và timeline ordering.
- Full regression trên bản bàn giao: `387 passed, 0 failed` trong `444.25s`.
- Impeccable detector chạy một lượt. Detector không resolve được stylesheet HTTP
  `/assets/dashboard.css` nên báo false positive type hierarchy; source thực tế dùng
  body 14px và heading 24-38px.

### Lỗi và giới hạn kiểm tra

- Computer-use inventory của phiên không có browser/app khả dụng, nên không tạo được
  screenshot 1440px/390px. HTTP assets, CSP, frontend state helpers và responsive CSS
  vẫn có automated coverage; visual browser smoke cần chạy lại khi browser surface có sẵn.
- Full suite tiếp tục tái tạo lock fixture lịch sử
  `run-run_20261013_160421_f661babf.lock`; file đã được xóa sau test và runtime bàn giao
  chỉ còn README, active-run index cùng `worktree-registry.lock`.

- Browser smoke đầu tiên gặp `state_corrupted` vì `active_runs.json` đã được ghi thủ công
  bằng Windows PowerShell với UTF-8 BOM trong lượt dọn runtime trước đó. File runtime đã
  được chuẩn hóa lại về UTF-8 không BOM; API và dashboard không tạo ra BOM này.
- Browser smoke tiếp theo phát hiện event `repair_iteration_started` không chứa `limit`
  nhưng frontend coi trường thiếu là `0`, khiến Implementation hiển thị `4 / 0`. Frontend
  giờ chỉ dùng event limit khi field thực sự tồn tại và giữ repair default từ dashboard schema.
- Cùng lượt smoke cho thấy graph tuyến tính ghi Verification “Chưa chạy” dù quality report
  đã có kết quả `failed` và Developer đang ở repair loop. Graph giờ giữ evidence riêng của
  Verification, Validator và QA, nên bước đã chạy vẫn thể hiện pass/fail trong lúc producer repair.


## 2026-10-18 - Hoàn thành Slice 6 Runtime Config

### Thay đổi

- Thêm schema backend cho năm nhóm API, LLM, Developer/Validation, target repository
  và sync; dashboard không hard-code danh sách key.
- Thêm `.runtime/config.json` atomic với precedence
  `defaults < .env < runtime < OS < invocation/action override`.
- Thêm `GET /api/v1/config`, `PUT /api/v1/config` và
  `POST /api/v1/config/reset`; config mutation chạy đồng bộ, không tạo operation.
- Secret luôn được mask; secret rỗng/`********` giữ nguyên. Bootstrap field chỉ đọc,
  OS env khóa field tương ứng, type/range/enum được validate trước khi ghi.
- Operation Manager giữ gate idle xuyên suốt config mutation; mọi save/reset bị từ
  chối bằng `config_busy` nếu có operation queued hoặc running.
- Queue/shutdown config có cờ cần restart; cấu hình workflow khác được đọc lại ở
  operation kế tiếp. API action thiếu `sync_mode` vẫn dùng `mock`.
- Thêm trang Cấu hình theo section, changed-only save, reset field/section, source
  indicator, busy lock và restart banner; giữ nguyên visual system của dashboard.
- Nâng tài liệu `.runtime/README.md` lên `runtime_layout_v3` và ghi rõ vai trò,
  precedence, secret, restart cùng cleanup policy của `config.json`.

### Test

- Runtime config + Config API mới: `13 passed`.
- Frontend view-model: `8 passed` bằng Node built-in runner.
- Impeccable detector chạy một lượt; cảnh báo type hierarchy là false positive do detector không resolve được asset URL `/assets/dashboard.css`, giống Slice 5.
- API/operation/dashboard regression tập trung: `40 passed`.
- Full regression trên bản bàn giao: `400 passed, 0 failed` trong `484.78s`.

### Lỗi phát hiện và xử lý

- Lần import đầu phát hiện hai literal escape (`\\` trong path normalization và
  `\n` khi ghi JSON) bị script tạo file làm mất escape, gây `SyntaxError`. Hai
  literal đã được sửa, sau đó import và toàn bộ focused test đều pass.
- Config mutation ban đầu chỉ định kiểm tra queue status trước khi ghi, có khe race
  với HTTP submit. Gate được chuyển vào `OperationManager.run_when_idle`, giữ cùng
  lock từ lúc xác nhận idle đến khi atomic replace hoàn tất.
- Full suite tái tạo lock fixture lịch sử `run-run_20261013_160421_f661babf.lock`; file đã được xóa, còn run smoke `run_20261018_102318_bb4678c1` được giữ nguyên.
