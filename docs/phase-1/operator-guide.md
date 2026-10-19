# Operator Guide Phase 1

Tài liệu này là sổ tay chạy Phase 1 khi demo hoặc kiểm thử tay. Nếu cần đọc
schema JSON chi tiết cho Niko, xem `cli-contract.md`. Nếu cần hiểu kiến trúc
luồng, xem `flow.md`.

## Khi Nào Dùng File Này

Dùng file này khi anh cần:

- tạo hoặc lấy lại run cho `eTour UC1` từ fixture local;
- tạo hoặc lấy lại run từ Jira issue thật;
- xem run đang ở trạng thái nào;
- đồng bộ lại checkpoint lên Jira/Confluence;
- kiểm tra Niko đã đọc được trạng thái SDLC Harness chưa.

Phase 1 hiện có bốn lệnh CLI chính:

| Lệnh | Mục đích | Có ghi dữ liệu thật không |
| --- | --- | --- |
| `start-or-get-run` | Tạo run mới nếu chưa có active run, hoặc trả run đang chờ duyệt | Có thể gọi LLM; tool sync mặc định mock |
| `start-from-jira` | Tạo run mới từ Jira issue thật, hoặc trả run đang chờ duyệt của issue đó | Có thể gọi LLM và gọi Jira đọc dữ liệu; tool sync mặc định mock |
| `get-status` | Đọc trạng thái run đã có | Không |
| `sync-run` | Re-sync checkpoint của run đã có lên Jira/Confluence/mock file | Có, chỉ khi truyền `--sync-mode real` hoặc `both` |

## Chuẩn Bị

Chạy từ repo `SDLC_Harness`:

```powershell
cd "D:\Dự án cá nhân\Do_an_II\SDLC_Harness"
```

Với smoke test an toàn, dùng fake LLM và mock sync:

```powershell
rtk python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
```

Với chạy thật, `.env` cục bộ cần có tối thiểu:

```text
SDLC_LLM_MODE=deepseek
SDLC_LLM_API_KEY=<secret>
SDLC_LLM_BASE_URL=https://api.deepseek.com
SDLC_LLM_MODEL=deepseek-chat

SDLC_SYNC_MODE=real  # Giá trị nền; CLI vẫn cần --sync-mode real để ghi tool thật.
ATLASSIAN_BASE_URL=https://<site>.atlassian.net
ATLASSIAN_EMAIL=<email Atlassian>
ATLASSIAN_API_TOKEN=<api token>
JIRA_SYNC_ENABLED=1
JIRA_SYNC_ISSUE_KEY=SDLC-1
CONFLUENCE_SYNC_ENABLED=1
CONFLUENCE_SYNC_SPACE_ID=<space id>
CONFLUENCE_SYNC_PARENT_PAGE_ID=<parent page id>
```

Không commit `.env`, `.runtime`, API token hoặc output có dữ liệu riêng tư.

## Workflow Chạy Tay

### 1. Tạo Hoặc Lấy Run

Từ fixture local:

```powershell
rtk python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --sync-mode real
```

Từ Jira issue thật:

```powershell
rtk python -m sdlc_harness.cli start-from-jira --issue-key SDLC-1 --requested-by manual --sync-mode real
```

Kết quả cần chú ý:

- `ok=true`;
- `run_id=run_...`;
- `status=waiting_requirement_approval`;
- `latest_artifacts` có `use_case_packet`, `requirement_packet`, `trace_graph`;
- `last_sync` cho biết đã sync mock hay real.

`start-or-get-run` idempotent theo `source_ref`. Nếu `eTour UC1` đã có active
run chưa done/cancelled, lệnh này trả lại run cũ và không refine lại mù quáng.
`start-from-jira` cũng idempotent nhưng dùng `source_ref=jira_issue:<KEY>`, nên
không trộn với active run của fixture local.

### Khi Prompt/Schema Đã Đổi Nhưng Active Run Còn Cũ

`start-or-get-run` và `start-from-jira` ưu tiên trả lại active run hiện tại để
tránh refine lại mù quáng. Vì vậy, sau khi sửa prompt/schema Requirement Packet,
chạy lại cùng `SDLC-1` có thể chỉ trả về artifact cũ.

Trong Phase 1 chưa có CLI `cancel_run`/`request_change` thật. Khi cần kiểm thử
schema mới trong môi trường local, dùng một trong các cách an toàn sau:

- tạo issue test mới để có `source_ref` mới;
- hoặc dọn runtime local có chủ đích trước khi chạy lại, sau khi đã ghi lại
  `run_id`/URL cần giữ.

Không dùng thao tác dọn runtime thủ công như một phần demo chính thức; đây chỉ
là cách reset môi trường dev cho tới khi Phase sau có approval/change command.

### 2. Xem Trạng Thái Run

```powershell
rtk python -m sdlc_harness.cli get-status --run-id <run_id>
```

Lệnh này chỉ đọc state hiện tại, không gọi LLM và không ghi lên Jira/Confluence.
Niko dùng lệnh này để trả lời các câu như “run này đến đâu rồi?”.

### 3. Re-sync Checkpoint

```powershell
rtk python -m sdlc_harness.cli sync-run --run-id <run_id> --sync-mode real
```

Lệnh này dùng khi:

- run được tạo lúc còn mock sync, sau đó cần đẩy thật lên Jira/Confluence;
- formatter Jira/Confluence thay đổi và cần cập nhật lại artifact đã sync;
- sync trước đó `failed` hoặc `partial_failed`.

Hành vi hiện tại:

- không gọi Requirement Refiner;
- không tạo requirement artifact mới;
- đọc `latest_artifacts` trong `state.json`;
- cập nhật `last_sync` và `tool_sync_report_v1.json`;
- nếu report đã có Jira comment `external_id`, re-sync sẽ update comment cũ;
- nếu report đã có Confluence page `external_id`, re-sync sẽ update page cũ.

Vì vậy, với một run đã sync thật thành công, `sync-run` không nên tạo thêm Jira
comment hoặc Confluence page mới.

## Kiểm Tra Sau Khi Sync Thật

Sau `sync-run`, chạy:

```powershell
rtk python -m sdlc_harness.cli get-status --run-id <run_id>
```

Kỳ vọng trong JSON:

```text
last_sync.mode=real
last_sync.status=synced
last_sync.targets[].tool=jira
last_sync.targets[].tool=confluence
last_sync.targets[].url=<URL thật>
```

Trên Jira, comment nên là checkpoint review có summary, status, next action và
artifact list. Trên Confluence, page nên có bảng checkpoint, review summary,
open questions, artifact table và requirement packet được render theo heading/list.

## Kiểm Tra Qua Niko

Sau khi terminal pass, hỏi Niko trên Telegram:

```text
run_<...> đến đâu rồi em?
```

Hoặc yêu cầu Niko bắt đầu từ Jira issue:

```text
Niko, thực hiện issue SDLC-1 giúp anh
```

Kỳ vọng:

- Niko route sang `sdlc_get_status`;
- hoặc route sang `sdlc_start_from_jira` nếu prompt yêu cầu thực hiện Jira issue;
- reply nói run đang `waiting_requirement_approval`;
- reply hoặc trace đọc được `last_sync.status=synced`;
- Niko không tự gọi Jira/Confluence API.

## Lỗi Thường Gặp

| Dấu hiệu | Nguyên nhân thường gặp | Cách xử lý |
| --- | --- | --- |
| `run_not_found` | Sai `run_id` hoặc runtime root khác | Kiểm tra `.runtime/runs/<run_id>` |
| `last_sync.status=failed` | Thiếu Atlassian config hoặc token/quyền sai | Kiểm tra `.env`, Jira permission, Confluence permission |
| `partial_failed` | Một trong hai target Jira/Confluence lỗi | Mở `tool_sync_report_v1.json` để xem target lỗi |
| Tạo nhiều comment/page | Report cũ chưa có `external_id`, hoặc run khác nhau | Xem `tool_sync_report_v1.json` và `run_id` |
> Ghi chú đồng bộ với Niko: Niko gọi HTTP API và format kết quả cho Telegram.
> CLI trong tài liệu này dành cho operator chạy tay. Mọi source intake, artifact,
> state, Jira comment và Confluence page đều được tạo/sync bởi SDLC_Harness.

