# Implementation Checklist: API Và Ops Dashboard

Trạng thái hiện tại: **Slice 1-3 đã hoàn thành; Slice 4 chưa triển khai**.

## Slice 1 - Application Service Chung

- [x] Tạo command/request model độc lập với argparse và HTTP.
- [x] Di chuyển dispatch workflow khỏi CLI vào application service.
- [x] CLI giữ nguyên command, exit code và JSON stdout.
- [x] Contract regression CLI hiện tại pass.

## Slice 2 - Read API

- [x] Thêm stdlib HTTP server, health endpoint và shutdown an toàn.
- [x] Thêm list/get run, artifact whitelist và dashboard snapshot.
- [x] Chặn absolute path, traversal và secret leakage.
- [x] Test 200/400/401/404/405/413/500.

## Slice 3 - Operation Queue Và Write API

- [x] Persist operation trước khi trả `202`.
- [x] Thêm bounded queue, một worker cố định và terminal result/error.
- [x] Ánh xạ toàn bộ action run lifecycle mà Niko dùng sang `/runs/{id}/actions`.
- [x] Giữ `prepare-target-repo` và `experiment-design-consensus` ở CLI-only V1.
- [x] Dùng source/run lock chung cho mutation qua CLI và API.
- [x] Thêm idempotency key bền vững và conflict detection.
- [x] Không auto replay operation bị gián đoạn khi restart.

## Slice 4 - Events Và Quan Sát

- [ ] Ghi event JSONL có sequence/cursor.
- [ ] Instrument operation, state, checkpoint, repair, test, validator, QA và sync.
- [ ] Lọc prompt/output/secret khỏi event mặc định.
- [ ] Test event ordering và incremental read.

## Slice 5 - Dashboard

- [ ] Runs và run detail.
- [ ] Workflow graph có current node/checkpoint/repair count.
- [ ] Artifact/quality preview.
- [ ] Operations/log timeline.
- [ ] Action button dựa trên `allowed_next_actions` và operation state.
- [ ] Test HTML routes, API calls và render state chính.

## Slice 6 - Runtime Config

- [ ] Thêm schema và `.runtime/config.json` atomic.
- [ ] Áp dụng precedence OS > runtime config > `.env` > defaults.
- [ ] Mask secret, khóa OS env và validate type/range.
- [ ] Giữ `sync_mode=mock` khi request không chọn real.
- [ ] Test reset section, masked secret và restart-required fields.

## Slice 7 - Niko Dual Transport

- [ ] Thêm HTTP transport dưới facade hiện tại.
- [ ] Thêm config base URL/token/request timeout/operation polling.
- [ ] Giữ CLI mặc định trong giai đoạn đầu.
- [ ] Không tự fallback write action từ API sang CLI.
- [ ] Contract parity và integration fixture pass.

## Slice 8 - Cutover

- [ ] Smoke Niko/API với fake LLM và mock sync.
- [ ] Controlled live E2E với real Jira/Confluence/GitHub.
- [ ] Test retry/idempotency/restart/sync partial.
- [ ] Chuyển API thành transport mặc định sau khi evidence pass.
- [ ] Cập nhật integration boundary trong docs hai repo.
- [ ] Giữ CLI cho debug/operator, bỏ subprocess khỏi runtime path của Niko.
