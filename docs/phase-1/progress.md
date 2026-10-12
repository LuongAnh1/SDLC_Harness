# Phase 1 Progress

Ngày cập nhật: 2026-10-13

## Trạng Thái Phase Gate

Kết luận hiện tại: **chưa được chuyển sang Phase 2**.

Phase 1 đã có code slice chạy được cho luồng:

```text
use case -> formal use case -> requirement packet -> checkpoint
```

Niko_Agent cũng đã gọi được `start-or-get-run` và `get-status` của
SDLC_Harness ở mức tối thiểu. SDLC Harness đã sync thật Jira/Confluence từ
terminal thành công, sau đó Niko đã đọc lại được `last_sync.status=synced`
qua Telegram.

Điều kiện còn thiếu trước khi mở Phase 2:

- Anh rerun live từ Jira issue `SDLC-1` sau schema Requirement Packet mới và
  review artifact thật trên Jira/Confluence.

Các mục đã pass bên dưới chứng minh vertical slice kỹ thuật của Phase 1, chưa
phải tín hiệu bắt đầu Phase 2.

## Đã Làm

- Tạo package Python `sdlc_harness`.
- Tạo CLI JSON `start-or-get-run` và `get-status` để Niko có thể gọi bằng subprocess.
- Tạo fixture loader do SDLC Harness sở hữu cho `fixtures/etour/UC1.txt`.
- Tạo workflow state trong `.runtime/runs/<run_id>/state.json` và active-run index trong `.runtime/index/active_runs.json`.
- Tạo Requirement Refiner adapter theo 2 tầng:
  - formal use case prompt/repair prompt copy/adapt từ TraceDev;
  - requirement packet prompt cho checkpoint người duyệt;
  - fake để test/smoke không tốn API;
  - `deepseek`/`openai_compatible` để gọi Chat Completions thật khi có `.env`.
- Tạo Formal Use Case Validator tối thiểu để kiểm tra grammar TraceDev.
- Tạo Requirement Validator để kiểm tra packet có đủ section/main flow.
- Nâng Requirement Validator từ kiểm tra cấu trúc tối thiểu lên guardrail nhẹ:
  chặn exception/quality bị nhét sai vào postcondition hoặc acceptance criteria,
  và chặn `Source Evidence` trích từ formal use case do model sinh ra.
- Tạo artifact Phase 1:
  - `use_case_packet_v1.md`;
  - `formal_use_case_v1.txt`;
  - `formal_check_report_v1.json`;
  - `requirement_packet_v1.md`;
  - `trace_graph_v1.json`.
- Tạo mock sync Jira/Confluence và `tool_sync_report_v1.json`.
- Tạo unit tests cho loader, fake refiner/validator, workflow state, idempotency, get-status và JSON error.
- Nối Niko_Agent gọi CLI/status ở mức tối thiểu:
  - Telegram prompt `Niko, thực hiện eTour UC1` gọi `start-or-get-run`;
  - Telegram prompt hỏi `run_id` gọi `get-status`;
  - Niko đọc được `summary_for_user`, `pending_checkpoint`, `allowed_next_actions` và `last_sync`.
- Thêm real sync runner và CLI `sync-run`:
  - Jira comment adapter;
  - Confluence page create/update adapter;
  - `tool_sync_report_v1.json` ghi `mode=real`, target URL/id và lỗi từng target nếu có.
- Chạy smoke terminal real sync thành công cho run `run_20261012_122724_235baa90`:
  - Jira comment: `https://tranluonganh2911-1790686783351.atlassian.net/browse/SDLC-1?focusedCommentId=10001`;
  - Confluence page: `https://tranluonganh2911-1790686783351.atlassian.net/wiki/spaces/SDLC/pages/131224/Requirement+Packet+-+eTour+UC1+-+run_20261012_122724_235baa90`;
  - `last_sync.status=synced`, `mode=real`.
- Chạy smoke qua Niko sau sync thật thành công:
  - prompt hỏi `run_20261012_122724_235baa90` được route sang `sdlc_get_status`;
  - trace `2026-10-12T15:54:57Z` ghi `last_sync.mode=real` và `last_sync.status=synced`;
  - Niko trả lời Telegram rằng run đang chờ duyệt requirement trước khi sang design.
- Cải thiện formatter sync thật:
  - Jira comment dùng heading/list và summary thay vì các paragraph rời rạc;
  - Confluence page dùng bảng checkpoint, review summary, open questions, artifact table và render markdown theo heading/list thay vì nhét nguyên packet vào `<pre>`;
  - re-sync Jira cập nhật comment cũ nếu report đã có comment id, tránh sinh comment lặp.
- Tách tài liệu vận hành CLI sang `operator-guide.md` để gom cách dùng
  `start-or-get-run`, `get-status`, `sync-run` và các bước kiểm tra Jira/Confluence/Niko.
- Thêm Phase 1B Jira source intake ở mức code/test:
  - CLI `start-from-jira --issue-key <KEY> --requested-by <id>`;
  - source_ref dạng `jira_issue:<KEY>`;
  - Jira issue summary/description/comment được đóng gói thành source text;
  - fixture flow cũ vẫn giữ để regression.
- Niko_Agent đã nhận diện prompt có ý thực hiện Jira issue, ví dụ
  `Niko, thực hiện issue SDLC-1 giúp anh`, rồi gọi `start-from-jira`.

## Đã Kiểm Tra

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

Kết quả mới nhất sau Requirement Packet schema/validator update: 27 test pass.

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
```

Kết quả: CLI trả JSON hợp lệ, state cuối là `waiting_requirement_approval`, có
pending checkpoint, artifacts và mock sync report.

Đã chạy smoke test với DeepSeek thật ngày 2026-10-12: run
`run_20261012_122724_235baa90`, `llm.mode=deepseek`,
`llm.model=deepseek-chat`, state cuối `waiting_requirement_approval`,
Validator pass.

Đã chạy live test qua Niko_Agent ngày 2026-10-12:

- `start-or-get-run` qua Telegram pass;
- `get-status` qua Telegram pass;
- Niko runtime/trace ghi route `sdlc_start_or_get_run` và `sdlc_get_status`.

## Formal Use Case Update 2026-10-12

- Đã copy/adapt prompt `req_refine_with_grammar` và `update_req_with_grammar` từ TraceDev vào `agents/requirements/prompts/` dưới dạng `.txt`, vì đây là formal grammar cho máy đọc.
- Đã copy scaffold prompt cho 4 vai trò TraceDev còn lại: Designer, Validator, Developer, Tester.
- Phase 1 hiện sinh `formal_use_case_v1.txt`, `formal_check_report_v1.json`, rồi mới sinh `requirement_packet_v1.md`.
- Trace graph và status JSON đã có thêm formal artifact và quality reports.
- Smoke CLI bằng fake LLM pass trên runtime tạm, không đụng `.runtime` thật.

## Requirement Packet Schema Update 2026-10-13

- Live run `run_20261012_170506_b0909cb6` đã chứng minh Niko gọi được
  `start-from-jira` và SDLC Harness sync thật Jira/Confluence, nhưng nội dung
  `requirement_packet_v1.md` còn khó review.
- Lỗi chính: nhánh hủy/kết nối lỗi và quality requirement bị dồn vào vùng
  `Postconditions / Acceptance Criteria`, làm Jira/Confluence nhìn như tiêu chí
  thành công nhưng thực ra là exception hoặc ràng buộc phi chức năng.
- Đã tách prompt requirement packet thành:
  `Alternative / Exception Flows`, `Postconditions`, `Acceptance Criteria`,
  `Quality / Non-functional Requirements`.
- Đã nhắc model chỉ dùng nguồn gốc ban đầu làm `Source Evidence`; formal use
  case chỉ là bản chuẩn hóa trung gian, không phải bằng chứng nguồn.
- Đã cập nhật fake LLM, formatter Jira/Confluence và unit tests theo schema mới.
- Live test sau chỉnh schema tạo run `run_20261012_172511_833ac277` và formal
  use case pass, nhưng Requirement Validator chặn packet vì model vẫn đưa
  `connection interruption/cancel` vào `Acceptance Criteria`.
- Đã thêm một vòng repair cho `requirement_packet_v1`: nếu validator fail, SDLC
  Harness gửi `ValidationReport` và packet hiện tại lại cho Requirement Refiner
  để sửa đúng schema trước khi trả lỗi ra Niko.
- Nếu repair vẫn fail, state của run được ghi `status=failed` và `last_error`
  có mã lỗi rõ để operator tra lại bằng `get-status`.
- Unit test sau repair loop: 28 test pass.

## Chưa Làm / Cần Khóa Trước Phase 2

- Cần rerun live `start-from-jira --issue-key SDLC-1` hoặc prompt tương ứng qua
  Niko sau repair loop, để DeepSeek sinh requirement packet theo schema mới.
- Cần anh review lại Jira comment/Confluence page sau lần live mới để xác nhận
  checkpoint đủ rõ: exception, postcondition, acceptance và quality không còn
  bị trộn.
- Sau khi artifact thật ổn, mới quyết định khóa Phase 1 để mở Phase 2 hoặc bổ
  sung approval/change request nhỏ.
