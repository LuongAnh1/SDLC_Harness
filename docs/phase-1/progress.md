# Phase 1 Progress

Ngày cập nhật: 2026-10-12

## Trạng Thái Phase Gate

Kết luận hiện tại: **chưa được chuyển sang Phase 2**.

Phase 1 đã có code slice chạy được cho luồng use case -> formal use case -> requirement packet -> checkpoint, nhưng Phase 1 vẫn chưa khóa theo thỏa thuận ban đầu. Điều kiện còn thiếu trước khi mở Phase 2 là:

- Nối Niko_Agent gọi CLI/status của SDLC_Harness ở mức tối thiểu.
- Có sync API/tool thật tối thiểu lên Jira hoặc Confluence, không chỉ mock file.
- Chạy lại smoke sau khi có integration/sync thật và ghi kết quả vào tài liệu.

Vì vậy, các mục đã pass bên dưới chỉ chứng minh vertical slice kỹ thuật của Phase 1, không phải tín hiệu bắt đầu Phase 2.

## Đã Làm

- Tạo package Python sdlc_harness.
- Tạo CLI JSON start-or-get-run và get-status để Niko có thể gọi bằng subprocess.
- Tạo fixture loader do SDLC Harness sở hữu cho ixtures/etour/UC1.txt.
- Tạo workflow state trong .runtime/runs/<run_id>/state.json và active-run index trong .runtime/index/active_runs.json.
- Tạo Requirement Refiner adapter theo 2 tầng:
  - formal use case prompt/repair prompt copy/adapt từ TraceDev;
  - requirement packet prompt cho checkpoint người duyệt;
  - ake để test/smoke không tốn API;
  - deepseek/openai_compatible để gọi Chat Completions thật khi có .env.
- Tạo Formal Use Case Validator tối thiểu để kiểm tra grammar TraceDev và Requirement Validator để kiểm tra packet có đủ section/main flow.
- Tạo artifact Phase 1:
  - use_case_packet_v1.md;
  - ormal_use_case_v1.txt;
  - ormal_check_report_v1.json;
  - equirement_packet_v1.md;
  - 	race_graph_v1.json.
- Tạo mock sync Jira/Confluence và 	ool_sync_report_v1.json.
- Tạo unit tests cho loader, fake refiner/validator, workflow state, idempotency, get-status và JSON error.

## Đã Kiểm Tra

`ash
python -m unittest discover -s tests -p  test*.py -v
`

Kết quả mới nhất sau formal use case/TraceDev prompt update: 14 test pass.

`ash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
`

Kết quả: CLI trả JSON hợp lệ, state cuối là waiting_requirement_approval, có pending checkpoint, artifacts và mock sync report.

Đã chạy smoke test với DeepSeek thật ngày 2026-10-12: run un_20261012_122724_235baa90, llm.mode=deepseek, llm.model=deepseek-chat, state cuối waiting_requirement_approval, Validator pass.

## Formal Use Case Update 2026-10-12

- Đã copy/adapt prompt eq_refine_with_grammar và update_req_with_grammar từ TraceDev vào gents/requirements/prompts/ dưới dạng .txt, vì đây là formal grammar cho máy đọc.
- Đã copy scaffold prompt cho 4 vai trò TraceDev còn lại: Designer, Validator, Developer, Tester.
- Phase 1 hiện sinh ormal_use_case_v1.txt, ormal_check_report_v1.json, rồi mới sinh equirement_packet_v1.md.
- Trace graph và status JSON đã có thêm formal artifact và quality_reports.
- Smoke CLI bằng fake LLM pass trên runtime tạm, không đụng .runtime thật.

## Chưa Làm / Cần Khóa Trước Phase 2

- Chưa nối Niko_Agent gọi CLI này; hiện contract đã sẵn sàng để Niko đọc summary_for_user, pending_checkpoint, llowed_next_actions và last_sync.
- Chưa có sync API/tool thật lên Jira hoặc Confluence. Đây là điều kiện cần trước khi chuyển sang Phase 2 theo quyết định Phase 0.
- Chưa có smoke test sau khi nối Niko + sync thật tối thiểu.