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

Giai đoạn hiện tại đã có vertical slice Phase 1 bằng Python package/CLI: load eTour UC1 hoặc Jira issue thật, gọi Requirement Refiner theo formal grammar kiểu TraceDev, validate/repair formal use case và requirement packet, ghi runtime artifact, sync Jira/Confluence thật khi cấu hình Atlassian đầy đủ, và trả JSON status cho Niko đọc. Phase 2 đã có `approve-checkpoint`, Designer Agent fake/LLM-ready, parser PlantUML class/sequence graph và mock design sync để khóa contract design checkpoint. Validator traceability LLM, repair loop và real sync Phase 2 vẫn là bước tiếp theo. Mock sync vẫn tồn tại cho test/fallback. Các phase sau tiếp tục mở rộng theo từng lát cắt chạy được.


## Cấu Trúc Package

- `sdlc_harness/interfaces/`: CLI/entrypoint công khai để Niko gọi. `sdlc_harness/cli.py` chỉ là wrapper giữ compatibility cho `python -m sdlc_harness.cli`.
- `sdlc_harness/workflows/`: workflow graph theo phase, hiện có Phase 1 `use case -> requirement checkpoint`.
- `sdlc_harness/agents/requirements/`: Requirement Refiner, formal use case Validator, requirement packet Validator và prompt templates copy/adapt từ TraceDev.
- `sdlc_harness/sources/`: loader cho fixture/dataset/Jira source sau này.
- `sdlc_harness/storage/`: runtime state store và active-run index.
- `sdlc_harness/artifacts/`: writer cho các artifact sinh ra trong run.
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

## Nguyên Tắc Hiện Tại

1. Bắt đầu từ một use case độc lập trước.
2. Java là ngôn ngữ thử nghiệm chính vì eTour/SMOS/iTrust và TraceDev đều thiên về Java.
3. Không xây full hệ thống âm thầm; mỗi slice phải chạy được và quan sát được.
4. Niko không lưu workflow state chính; Niko đọc state từ SDLC Harness để phản hồi người dùng.
5. Tool thật có thể đi sau; ban đầu được phép dùng fixture/mock adapter cho Jira, Confluence và GitHub/GitLab.

## Quyết Định Phase 0

- Phase 1 dùng CLI trả JSON để Niko gọi SDLC Harness.
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
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
```

Khi muốn gọi DeepSeek thật, copy `.env.example` thành `.env`, điền `SDLC_LLM_API_KEY`, đặt `SDLC_LLM_MODE=deepseek`, rồi bỏ `--llm-mode fake` hoặc truyền `--llm-mode deepseek`.

Chạy test:

```bash
python -m unittest discover -s tests -p "test*.py" -v
```


<!-- SDLC_NIKO_SYNC_START -->
## Phase 1 Runtime Snapshot

SDLC_Harness hiện là repo sở hữu pipeline Phase 1 cho hướng Agent Harness SDLC:
đọc use case từ fixture hoặc Jira, chuẩn hóa thành formal use case và
Requirement Packet, validate/repair, ghi artifact runtime và sync checkpoint lên
Jira/Confluence. Niko_Agent chỉ gọi repo này qua CLI JSON rồi diễn giải kết quả
cho người dùng trên Telegram.

Ranh giới hiện tại:

- Phase 1 đã có real Jira source và real Jira/Confluence sync khi cấu hình
  Atlassian đầy đủ.
- Mock sync vẫn tồn tại cho unit/smoke test hoặc khi chưa cấu hình tool thật.
- CLI phải giữ stdout chỉ có JSON vì Niko parse trực tiếp response đó.
- Phase 2 hiện có contract `approve-checkpoint` V1: Designer tạo PlantUML,
  parser sinh class/sequence graph thật, traceability đang ở
  `traceability_pending_v1`, và mock design sync. Validator traceability thật
  theo TraceDev vẫn là bước tiếp theo.

<!-- SDLC_NIKO_SYNC_END -->

