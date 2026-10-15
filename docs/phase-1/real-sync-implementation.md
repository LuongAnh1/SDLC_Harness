# Real Sync Phase 1

Ngày lập: 2026-10-12

Tài liệu này ghi cách Phase 1 sync thật lên Jira Cloud và Confluence Cloud.
Đây là phần vận hành checkpoint requirement, không phải phần bắt đầu design.

## Mục Tiêu

Sau khi SDLC Harness tạo xong `requirement_packet_v1`, hệ thống:

- tạo hoặc cập nhật một Jira comment thật trong issue demo của use case;
- tạo hoặc cập nhật một Confluence page thật chứa checkpoint requirement;
- ghi `tool_sync_report_v1.json` với external id/url thật;
- trả `last_sync` để Niko chỉ đọc status và báo lại người dùng.

Ranh giới vẫn giữ nguyên:

```text
SDLC Harness tạo artifact
-> SDLC Harness sync Jira/Confluence thật
-> SDLC Harness ghi tool_sync_report
-> Niko gọi get-status
-> Niko báo người dùng
```

Niko không tự gọi Jira/Confluence API và không tự ghi workflow state.

## Việc Anh Cần Làm Trên Atlassian Cloud

1. Tạo hoặc chọn một Jira project dùng cho demo.
   - Ví dụ project key: `SDLC`.
   - Project phải cho tài khoản API token quyền Browse Project và Add Comment.

2. Tạo issue đại diện cho use case `eTour UC1`.
   - Ví dụ issue key: `SDLC-1`.
   - Nội dung issue có thể là bản mô tả gốc UC1 hoặc chỉ là placeholder demo.
   - Phase 1 chưa kéo source từ Jira xuống, nên issue này là sync target, không
     phải source owner.

3. Tạo hoặc chọn Confluence space dùng cho demo.
   - Ví dụ space: `SDLC`.
   - Tài khoản API token cần quyền view space và create/update page.

4. Tạo một page cha trong Confluence để chứa artifact sync.
   - Ví dụ title: `SDLC Harness Runs`.
   - Ghi lại page id hoặc space id theo cách adapter yêu cầu.

5. Tạo Atlassian API token.
   - Atlassian Cloud dùng email + API token cho Basic auth khi gọi REST API.
   - Không dùng mật khẩu tài khoản Atlassian trong `.env`.

6. Điền `.env` cục bộ của `SDLC_Harness`.

```text
ATLASSIAN_BASE_URL=https://<site>.atlassian.net
ATLASSIAN_EMAIL=<email Atlassian>
ATLASSIAN_API_TOKEN=<api token>

SDLC_SYNC_MODE=real
JIRA_SYNC_ENABLED=1
JIRA_SYNC_ISSUE_KEY=SDLC-1

CONFLUENCE_SYNC_ENABLED=1
CONFLUENCE_SYNC_SPACE_ID=<space id>
CONFLUENCE_SYNC_PARENT_PAGE_ID=<parent page id>
```

Nếu chưa có `space id`, có thể bắt đầu với Jira comment trước nhưng không được
khóa Phase 1 cho tới khi cả hai target Jira và Confluence theo lựa chọn hiện tại
đã có smoke test pass.

## Adapter Trong SDLC Harness

### Config

Cấu hình đọc từ `.env` trong `sdlc_harness/core/config.py`:

```text
SDLC_SYNC_MODE=mock|real|both
ATLASSIAN_BASE_URL
ATLASSIAN_EMAIL
ATLASSIAN_API_TOKEN
JIRA_SYNC_ENABLED
JIRA_SYNC_ISSUE_KEY
CONFLUENCE_SYNC_ENABLED
CONFLUENCE_SYNC_SPACE_ID
CONFLUENCE_SYNC_PARENT_PAGE_ID
```

Default an toàn:

- `SDLC_SYNC_MODE=mock` để test hiện tại không gọi API thật;
- `JIRA_SYNC_ENABLED=0`;
- `CONFLUENCE_SYNC_ENABLED=0`.

### Jira Comment

- dùng Basic auth với `ATLASSIAN_EMAIL:ATLASSIAN_API_TOKEN`;
- tạo comment bằng `POST /rest/api/3/issue/{issueIdOrKey}/comment` nếu report
  chưa có Jira `external_id`;
- update comment bằng `PUT /rest/api/3/issue/{issueIdOrKey}/comment/{commentId}`
  nếu report đã có Jira `external_id`;
- body dùng Atlassian Document Format với heading, summary, status, next action
  và danh sách artifact chính;
- trả `comment_id` và URL comment/issue.

Jira Cloud REST API v3 có operation add comment cho issue ở
`POST /rest/api/3/issue/{issueIdOrKey}/comment`; operation này cần quyền browse
project và add comment.

### Confluence Page

- tạo một page con dưới parent page đã cấu hình nếu report chưa có Confluence
  `external_id`;
- update page cũ nếu report đã có Confluence `external_id`;
- title nên ổn định theo run id, ví dụ `Requirement Packet - eTour UC1 - <run_id>`;
- body dùng representation `storage`, gồm bảng checkpoint, review summary, open
  questions, artifact table và requirement packet render theo heading/list;
- trả `page_id`, `_links.webui`, `web_url`.

Confluence Cloud REST API v2 có operation create page ở `POST /wiki/api/v2/pages`;
body cần `spaceId`, `title`, `parentId` và `body`.

Khi update page, adapter đọc version hiện tại rồi PUT version mới.

### Tool Sync Report

`tool_sync_report_v1.json` phải giữ schema dễ đọc cho Niko:

```json
{
  "version": 1,
  "mode": "real",
  "status": "synced",
  "created_at": "ISO-8601",
  "targets": [
    {
      "tool": "jira",
      "mode": "real_api",
      "status": "synced",
      "issue_key": "SDLC-1",
      "external_id": "<comment id>",
      "url": "<comment or issue url>",
      "source_ref": "dataset_use_case:eTour:UC1"
    },
    {
      "tool": "confluence",
      "mode": "real_api",
      "status": "synced",
      "external_id": "<page id>",
      "url": "<page url>",
      "source_ref": "dataset_use_case:eTour:UC1"
    }
  ]
}
```

Nếu một target lỗi:

- không chuyển workflow sang Phase 2;
- vẫn giữ `current_phase=waiting_requirement_approval`;
- ghi target lỗi với `status=failed`, `error_code`, `message`;
- `last_sync.status` là `failed` nếu cả hai lỗi hoặc `partial_failed` nếu chỉ một
  target lỗi.

## Luồng Test

### Test Không Gọi API Thật

```bash
python -m unittest discover -s tests -p "test*.py" -v
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli get-status --run-id <run_id> --llm-mode fake
```

Kỳ vọng: mock sync cũ vẫn pass.

### Test Sync Thật Từ Terminal

Trước khi chạy, dùng runtime sạch hoặc run mới để tránh nhầm active run. Nếu đã
có run cũ từ mock sync, dùng `sync-run` để sync thật lại run đó.

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode deepseek --sync-mode real
python -m sdlc_harness.cli sync-run --run-id <run_id> --sync-mode real
python -m sdlc_harness.cli get-status --run-id <run_id>
```

Kỳ vọng:

- Jira issue có comment mới hoặc comment cũ được update nếu report đã có `external_id`;
- Confluence có page mới hoặc page cũ được update nếu report đã có `external_id`;
- `.runtime/runs/<run_id>/sync/tool_sync_report_v1.json` có `mode=real`;
- status cuối vẫn là `waiting_requirement_approval`;
- `last_sync.status=synced`.

### Test Qua Niko

Sau khi terminal pass:

```text
Niko, thực hiện eTour UC1
run_<...> đến đâu rồi em?
```

Kỳ vọng:

- Niko route sang `sdlc_start_or_get_run` hoặc `sdlc_get_status`;
- reply báo được status requirement approval;
- reply hoặc runtime log đọc được sync result thật từ `last_sync`.

## Điều Kiện Khóa Trước Phase 2

Phase 1 chỉ được coi là khóa khi:

- Niko đã gọi được `start-or-get-run` và `get-status`;
- Jira real comment sync pass;
- Confluence real page sync pass;
- `tool_sync_report_v1.json` có URL/id thật;
- smoke test qua Niko sau sync thật pass;
- `progress.md` và `implementation-checklist.md` được cập nhật lại.

## Nguồn Tham Khảo Chính

- Jira Cloud Basic auth: https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
- Jira Cloud add comment: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-post
- Confluence Cloud create page: https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/#api-pages-post
