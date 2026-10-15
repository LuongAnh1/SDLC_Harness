# CLI Contract Phase 1

Phase 1 dùng CLI trả JSON làm giao tiếp đầu tiên giữa Niko và SDLC Harness. CLI cần dễ test tay và dễ gọi bằng subprocess.

File này mô tả contract kỹ thuật. Nếu cần hướng dẫn chạy theo workflow, xem
`operator-guide.md`.

## Entry Point Hiện Tại

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli start-from-jira --issue-key SDLC-1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
python -m sdlc_harness.cli sync-run --run-id <run_id> --sync-mode real
```

Tên module hiện tại là `sdlc_harness.cli`. Behavior và JSON contract không nên thay đổi nếu không cập nhật docs.

## `start-or-get-run`

Input flags:

| Flag | Bắt buộc | Giá trị Phase 1 |
| --- | --- | --- |
| `--dataset` | có | `eTour` |
| `--use-case` | có | `UC1` |
| `--requested-by` | có | id người gọi, ví dụ `manual` hoặc `telegram:<id>` |
| `--llm-mode` | không | `fake`, `deepseek`, `openai_compatible`; CLI override `.env` để test an toàn |
| `--runtime-root` | không | override thư mục runtime, chủ yếu dùng cho test |

Success response:

```json
{
  "ok": true,
  "created_new_run": true,
  "source_ref": "dataset_use_case:eTour:UC1",
  "run_id": "run_...",
  "current_phase": "waiting_requirement_approval",
  "status": "waiting_requirement_approval",
  "latest_artifacts": {
    "use_case_packet": ".runtime/runs/<run_id>/artifacts/use_case_packet_v1.md",
    "formal_use_case": ".runtime/runs/<run_id>/artifacts/formal_use_case_v1.txt",
    "formal_check_report": ".runtime/runs/<run_id>/artifacts/formal_check_report_v1.json",
    "requirement_packet": ".runtime/runs/<run_id>/artifacts/requirement_packet_v1.md",
    "trace_graph": ".runtime/runs/<run_id>/artifacts/trace_graph_v1.json"
  },
  "pending_checkpoint": {
    "checkpoint_id": "checkpoint_...",
    "type": "requirement_approval",
    "artifact_ref": "requirement_packet_v1"
  },
  "allowed_next_actions": ["approve_checkpoint", "request_change", "cancel_run", "get_status"],
  "last_sync": {
    "status": "mock_synced",
    "report": ".runtime/runs/<run_id>/sync/tool_sync_report_v1.json"
  },
  "last_error": null,
  "quality_reports": {
    "formal_use_case": {"ok": true},
    "requirement_packet": {"ok": true}
  },
  "summary_for_user": "Đã chuẩn hóa eTour UC1 thành requirement packet v1 và đang chờ duyệt."
}
```

## Idempotency Theo Source Ref

`start-or-get-run` không luôn tạo run mới. Lệnh này dùng `source_ref=dataset_use_case:eTour:UC1` để tìm active run.

```text
active run tồn tại, status chưa done/cancelled
  -> trả status run hiện tại, không gọi Requirement Refiner lại.

không có active run
  -> tạo run mới, load fixture, gọi Requirement Refiner, sync theo cấu hình.

run cũ done/cancelled
  -> tạo run mới khi nhận yêu cầu thực hiện lại.
```

## `start-from-jira`

Input flags:

| Flag | Bắt buộc | Giá trị Phase 1B |
| --- | --- | --- |
| `--issue-key` | có | Jira issue key, ví dụ `SDLC-1` |
| `--requested-by` | có | id người gọi, ví dụ `manual` hoặc `telegram:<id>` |
| `--llm-mode` | không | `fake`, `deepseek`, `openai_compatible`; CLI override `.env` để test an toàn |
| `--runtime-root` | không | override thư mục runtime, chủ yếu dùng cho test |

Lệnh này fetch Jira issue, đóng gói summary/description/comment thành source
text, rồi dùng cùng schema response với `start-or-get-run`.

Idempotency dùng `source_ref=jira_issue:<KEY>`. Nếu issue đã có active run chưa
done/cancelled, command trả lại status run hiện tại và không fetch/refine lại.

Error codes chính:

```text
jira_source_config_missing
jira_source_not_found
jira_source_empty
jira_source_fetch_failed
```

## `get-status`

Input flags:

| Flag | Bắt buộc | Ý nghĩa |
| --- | --- | --- |
| `--run-id` | có | run cần đọc trạng thái |
| `--llm-mode` | không | giữ schema chung với lệnh start, không gọi LLM trong get-status |
| `--runtime-root` | không | override thư mục runtime, chủ yếu dùng cho test |

Success response dùng cùng schema với `start-or-get-run` để Niko không cần xử lý hai dạng dữ liệu khác nhau.

## `sync-run`

Input flags:

| Flag | Bắt buộc | Ý nghĩa |
| --- | --- | --- |
| `--run-id` | có | run đã có artifact requirement cần sync lại |
| `--sync-mode` | không | `mock` (mặc định), `real` hoặc `both`; phải ghi rõ `real` khi muốn cập nhật tool thật |
| `--runtime-root` | không | override thư mục runtime, chủ yếu dùng cho test |

`sync-run` không tạo artifact mới và không gọi Requirement Refiner. Lệnh này đọc
`latest_artifacts` trong state hiện tại, sync lại requirement checkpoint theo
`--sync-mode`, cập nhật `last_sync`, ghi sync report đúng phase, rồi trả
cùng schema status như `get-status`.

Lý do có lệnh riêng: `start-or-get-run` cố ý idempotent theo `source_ref`. Nếu
run cũ đang tồn tại, lệnh start không refine hoặc sync lại mù quáng. Khi chuyển
từ mock sync sang real sync, dùng `sync-run` để thao tác rõ ràng.

Với real sync, `sync-run` idempotent theo target đã lưu trong report hoặc marker
remote ổn định `source_ref + phase`: Jira comment/Confluence page cũ được update.
Nếu nhiều legacy target cùng khớp, lệnh fail closed thay vì tạo hoặc xóa mù.

## Error Response

Mọi lỗi đều trả JSON:

```json
{
  "ok": false,
  "error_code": "run_not_found",
  "message": "Run not found: <run_id>",
  "run_id": "<run_id>"
}
```

## Output Rules

- JSON in ra stdout.
- Log debug nếu có thì ghi stderr hoặc file runtime, không trộn vào stdout.
- Không in text tự do trước/sau JSON.
- Đường dẫn trong JSON nên dùng relative path từ repo root để dễ hiển thị và tránh lộ đường dẫn máy.
- Encoding UTF-8.

## Contract Với Niko

Niko chỉ cần đọc các field sau ở Phase 1. Niko không tự kéo use case từ fixture/Jira/dataset và không tự sync tool:

```text
ok
created_new_run
source_ref
run_id
current_phase
pending_checkpoint
allowed_next_actions
last_sync
summary_for_user
error_code
message
```

Những field khác là metadata để debug và dashboard dùng sau.

## Cấu hình LLM

Phase 1 hỗ trợ fake LLM để smoke test không tốn token. Khi chạy thật, copy `.env.example` thành `.env` và điền:

```text
SDLC_LLM_MODE=deepseek
SDLC_LLM_API_KEY=<secret>
SDLC_LLM_BASE_URL=https://api.deepseek.com
SDLC_LLM_MODEL=deepseek-chat
SDLC_SYNC_MODE=mock
ATLASSIAN_BASE_URL=https://<site>.atlassian.net
ATLASSIAN_EMAIL=<email>
ATLASSIAN_API_TOKEN=<token>
JIRA_SYNC_ENABLED=1
JIRA_SYNC_ISSUE_KEY=SDLC-1
CONFLUENCE_SYNC_ENABLED=1
CONFLUENCE_SYNC_SPACE_ID=<space id>
CONFLUENCE_SYNC_PARENT_PAGE_ID=<page id>
```

CLI flag `--llm-mode` có ưu tiên cao hơn `.env`, nên có thể ép `--llm-mode fake`
trong test mà không sửa file cấu hình thật. Các lệnh có side effect luôn mặc định
`--sync-mode mock`, kể cả `.env` ghi `SDLC_SYNC_MODE=real`; muốn sync thật phải
truyền `--sync-mode real` rõ ràng.

