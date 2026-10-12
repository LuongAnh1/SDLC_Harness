# Phase 1 Progress

Ngày cập nhật: 2026-10-12

## Đã Làm

- Tạo package Python `sdlc_harness`.
- Tạo CLI JSON `start-or-get-run` và `get-status` để Niko có thể gọi bằng subprocess.
- Tạo fixture loader do SDLC Harness sở hữu cho `fixtures/etour/UC1.txt`.
- Tạo workflow state trong `.runtime/runs/<run_id>/state.json` và active-run index trong `.runtime/index/active_runs.json`.
- Tạo Requirement Refiner adapter theo 2 tầng:
  - formal use case prompt/repair prompt copy/adapt từ TraceDev;
  - requirement packet prompt cho checkpoint người duyệt;
  - `fake` để test/smoke không tốn API;
  - `deepseek`/`openai_compatible` để gọi Chat Completions thật khi có `.env`.
- Tạo Formal Use Case Validator tối thiểu để kiểm tra grammar TraceDev và Requirement Validator để kiểm tra packet có đủ section/main flow.
- Tạo artifact Phase 1:
  - `use_case_packet_v1.md`;
  - `formal_use_case_v1.txt`;
  - `formal_check_report_v1.json`;
  - `requirement_packet_v1.md`;
  - `trace_graph_v1.json`.
- Tạo mock sync Jira/Confluence và `tool_sync_report_v1.json`.
- Tạo unit tests cho loader, fake refiner/validator, workflow state, idempotency, get-status và JSON error.

## Đã Kiểm Tra

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

Kết quả mới nhất sau formal use case/TraceDev prompt update: 14 test pass.

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
```

Kết quả: CLI trả JSON hợp lệ, state cuối là `waiting_requirement_approval`, có pending checkpoint, artifacts và mock sync report.

## Formal use case update 2026-10-12

- Đã copy/adapt prompt `req_refine_with_grammar` và `update_req_with_grammar` từ TraceDev vào `agents/requirements/prompts/` dưới dạng `.txt`, vì đây là formal grammar cho máy đọc.
- Đã copy scaffold prompt cho 4 vai trò TraceDev còn lại: Designer, Validator, Developer, Tester.
- Phase 1 hiện sinh `formal_use_case_v1.txt`, `formal_check_report_v1.json`, rồi mới sinh `requirement_packet_v1.md`.
- Trace graph và status JSON đã có thêm formal artifact và `quality_reports`.
- Smoke CLI bằng fake LLM pass trên runtime tạm, không đụng `.runtime` thật.

## Chưa Làm / Cần Khóa Trước Phase 2

- Đã chạy smoke test với DeepSeek thật ngày 2026-10-12: run `run_20261012_122724_235baa90`, `llm.mode=deepseek`, `llm.model=deepseek-chat`, state cuối `waiting_requirement_approval`, Validator pass.
- Chưa nối `Niko_Agent` gọi CLI này; hiện contract đã sẵn sàng để Niko đọc `summary_for_user`, `pending_checkpoint`, `allowed_next_actions` và `last_sync`.
- Chưa có sync API thật lên Jira/Confluence. Đây là điều kiện cần trước khi chuyển sang Phase 2 theo quyết định Phase 0.



