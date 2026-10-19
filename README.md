# SDLC Harness

Repo này là nơi thiết kế và triển khai lớp **SDLC Harness** cho hướng Agent Harness hỗ trợ chuyển giao ngữ cảnh trong quy trình phát triển phần mềm.

Repo này tách khỏi `Niko_Agent` để giữ ranh giới rõ:

```text
Niko_Agent
  = conversational control harness
  = nhận yêu cầu, đọc trạng thái, hỏi người dùng, gọi workflow, trả kết quả

SDLC_Harness
  = workflow execution harness
  = chạy graph requirement -> design -> code -> test
  = lưu workflow state, artifact version, checkpoint, tool sync, traceability
```

Giai đoạn hiện tại đã hoàn thiện workflow Requirement → Design → Implementation Plan → Code/Verification/Validator → QA → Code Review, cùng worktree-per-run, Jira/Confluence/GitHub sync và checkpoint do người dùng điều khiển. Mốc API + Ops Dashboard Slice 1-8 đã hoàn thành: Niko gọi Harness bằng HTTP API-only với operation bền vững, idempotency và polling; CLI của Harness vẫn là interface operator/debug độc lập. Live E2E `run_20261019_034156_05cfbb93` đã đi trọn luồng tới `code_review_approved`, PR #7 Ready for review. Phase 5 `request_change` được giữ nguyên và triển khai sau mốc này.


## Cấu Trúc Package

- `sdlc_harness/application/`: command model, settings construction và workflow dispatch độc lập transport.
- `sdlc_harness/interfaces/`: CLI adapter và HTTP API công khai. Cả hai dùng chung application service; `sdlc_harness/cli.py` vẫn là wrapper compatibility cho `python -m sdlc_harness.cli`.
- `sdlc_harness/operations/`: operation JSON bền vững, idempotency index và queue một worker cho HTTP write action dài.
- `sdlc_harness/workflows/`: workflow graph Phase 1-4, từ requirement/design/planning tới code, verification, QA, publication và Code Review approval.
- `sdlc_harness/agents/requirements/`: Requirement Refiner, formal use case Validator, requirement packet Validator và prompt templates copy/adapt từ TraceDev.
- `sdlc_harness/sources/`: loader cho fixture/dataset/Jira source sau này.
- `sdlc_harness/storage/`: runtime state store và active-run index.
- `sdlc_harness/artifacts/`: writer cho các artifact sinh ra trong run.
- `sdlc_harness/workspaces/`: workspace identity, branch/path policy và lock theo từng `run_id`.
- `sdlc_harness/sync/`: adapter sync tool, gồm real Atlassian sync và mock sync cho test/fallback.
- `sdlc_harness/core/` và `sdlc_harness/llm/`: config/errors dùng chung và LLM provider adapter.
## Tài Liệu Chính

- `docs/architecture.md`: kiến trúc tổng quan và ranh giới với Niko.
- `docs/roadmap.md`: các phase triển khai.
- `docs/incremental-delivery-flow.md`: quy trình triển khai từng lát cắt và đồng bộ ngay sau mỗi đoạn.
- `docs/tracedev-reference.md`: cách học TraceDev như một execution graph, không copy nguyên xi.
- `docs/datasets-and-context.md`: cách dùng eTour/SMOS/iTrust và vai trò lớp context động.
- `docs/implementation-checklist.md`: checklist theo phase để tick khi triển khai.
- `docs/phase-1/README.md`: tài liệu triển khai chi tiết cho lát cắt use case -> requirement packet.
- `docs/phase-2/README.md`: tài liệu triển khai dự kiến cho lát cắt requirement packet -> design artifact.
- `docs/phase-2/sync-design-checkpoint.md`: contract sync checkpoint thiết kế lên Jira/Confluence và cách Niko đọc `last_sync`.
- `docs/phase-3/README.md`: implementation planning checkpoint sau khi design được duyệt.
- `docs/phase-4/README.md`: code/test/Draft PR sau khi implementation plan được duyệt.
- `docs/phase-4/workspace-isolation/`: contract worktree-per-run, migration và
  checklist prerequisite trước Code Review approval/Phase 5.
- `docs/typescript-fullstack-upgrade/README.md`: khảo sát và proposal docs-first
  cho target TypeScript cùng vertical web feature frontend/backend/database;
  hướng này chưa được triển khai trong runtime.

## Nguyên Tắc Hiện Tại

1. Bắt đầu từ một use case độc lập trước.
2. Java là target đã dùng để chứng minh baseline eTour/TraceDev. Hướng tiếp theo
   đã chốt ở mức tài liệu là target TypeScript full-stack; xem
   `docs/typescript-fullstack-upgrade/`. Cho đến khi roadmap đó được triển khai,
   runtime Phase 3-4 hiện hành vẫn là Java.
3. Không xây full hệ thống âm thầm; mỗi slice phải chạy được và quan sát được.
4. Niko không lưu workflow state chính; Niko đọc state từ SDLC Harness để phản hồi người dùng.
5. Tool thật có thể đi sau; ban đầu được phép dùng fixture/mock adapter cho Jira, Confluence và GitHub/GitLab.

## Quyết Định Phase 0

- Phase 1 ban đầu dùng CLI JSON để chứng minh contract; quyết định này đã được
  thay thế bởi HTTP API-only sau mốc platform Slice 8.
- eTour UC1 được copy vào `fixtures/etour/UC1.txt` để repo chạy độc lập hơn.
- Runtime artifact và mock sync output nằm trong `.runtime/runs/<run_id>/`, không nằm trong `docs/`.
- Thử nghiệm đầu của Phase 1 được phép mock sync, nhưng trước Phase 2 cần có API/tool sync thật tối thiểu.
- Sync thuộc SDLC Harness; Niko đọc status/sync result để phản hồi người dùng.


## Ranh Giới Source Và Status

Khi người dùng bảo Niko thực hiện một use case, Niko không tự kéo dữ liệu từ fixture/Jira/dataset. Niko gọi SDLC Harness bằng `start-or-get-run`; SDLC Harness load/refine/validate/sync nếu cần và trả status cho Niko.

## Chạy Nhanh Phase 1

Chạy smoke test không tốn API bằng fake LLM:

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC2 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
```

Fixture UC2 là **Insert Cultural Heritage**, lấy từ bộ eTour của TraceDev để chạy
một source mới độc lập với run UC1/SDLC-1 trước đó.

Khi muốn gọi DeepSeek thật, copy `.env.example` thành `.env`, điền `SDLC_LLM_API_KEY`, đặt `SDLC_LLM_MODE=deepseek`, rồi bỏ `--llm-mode fake` hoặc truyền `--llm-mode deepseek`.

Chạy test:

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

Chạy Read API local:

```bash
python -m sdlc_harness.interfaces.http
curl http://127.0.0.1:7788/api/v1/health
```

Khi cần smoke cô lập, có thể override process mà không sửa `.env` hoặc runtime thật:

```bash
python -m sdlc_harness.interfaces.http --host 127.0.0.1 --port 7789 --runtime-root .runtime-smoke
```

Ops Dashboard được phục vụ cùng server tại `http://127.0.0.1:7788/`. Khi
`SDLC_API_TOKEN` được bật, nhập token trong dashboard; trình duyệt chỉ giữ token
trong `sessionStorage` của tab hiện tại.

Các endpoint ngoài health yêu cầu bearer token khi `SDLC_API_TOKEN` được cấu
hình. Write API trả `202 Accepted`; dùng URL operation trong response để poll
đến `succeeded`, `failed` hoặc `interrupted`. Mỗi workflow POST cần `Idempotency-Key`; config save/reset chạy đồng bộ và không tạo operation.


<!-- SDLC_NIKO_SYNC_START -->
## Runtime Snapshot

SDLC_Harness hiện là repo sở hữu pipeline Phase 1-4 cho hướng Agent Harness SDLC:
đọc use case từ fixture hoặc Jira, chuẩn hóa thành formal use case và
Requirement Packet, validate/repair, ghi artifact runtime và sync checkpoint lên
Jira/Confluence/GitHub. Niko_Agent gọi HTTP API rồi diễn giải kết quả cho người
dùng trên Telegram.

Ranh giới hiện tại:

- Phase 1 đã có real Jira source và real Jira/Confluence sync khi cấu hình
  Atlassian đầy đủ.
- Mock sync vẫn tồn tại cho unit/smoke test hoặc khi chưa cấu hình tool thật.
- CLI giữ stdout chỉ có JSON để phục vụ operator/automation độc lập; Niko không
  còn gọi CLI bằng subprocess.
- Phase 2 hiện có contract `approve-checkpoint` đã khóa cho static approve-only: Designer tạo PlantUML,
  parser sinh class/sequence graph thật, Validator traceability tạo
  requirement graph/class trace/sequence trace/missing links ở mode
  `traceability_v1`, Designer repair design theo missing-link report,
  `SDLC_DESIGN_REPAIR_LIMIT` cấu hình số vòng repair, và mock/real design sync
  khi design pass. Phase 3 sinh implementation plan theo version, Validator
  hybrid rule + LLM, repair theo `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`, sync
  Jira/Confluence sau khi pass và chờ `waiting_implementation_plan_review`;
  Phase 4 tạo code/test, chạy Validator và QA/Tester trước khi mở Draft PR. Hiện
  workflow đã được live-test qua Niko tới `code_review_approved`: commit/push và
  Draft PR idempotent đã chạy thật, PR được chuyển sang Ready for review, sau đó
  Jira/Confluence nhận Code Review checkpoint với PR/test/trace/QA evidence.
  Harness không merge PR. Niko đọc JSON status để báo checkpoint hiện tại,
  `blocked` và `last_sync`.

<!-- SDLC_NIKO_SYNC_END -->


## Platform Upgrade Đã Hoàn Thành

Sau Phase 4 và trước Phase 5 `request_change`, repo đã chuyển integration Niko từ
CLI subprocess sang HTTP API có operation bền vững và Ops Dashboard. Thiết kế,
API contract, runtime event, dashboard/config, cutover Niko và checklist nằm tại
[`docs/platform-api-dashboard/`](docs/platform-api-dashboard/README.md).

Slice 1-8 đã hoàn thành. Niko đã xóa dual transport và cấu hình CLI riêng sau
live E2E ngày 2026-10-19; CLI JSON của Harness tiếp tục tồn tại cho operator và
automation, không còn là integration contract của Niko.
