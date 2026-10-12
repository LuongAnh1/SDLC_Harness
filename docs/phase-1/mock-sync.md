# Mock Sync Phase 1

Phase 1 dùng mock sync để chứng minh Harness là nơi đồng bộ artifact lên tool. Mock sync không thay thế API thật; nó chỉ giúp khóa flow trước khi tích hợp Jira/Confluence/GitHub.

## Sync Ownership

Sync thuộc SDLC Harness:

```text
SDLC Harness tạo artifact
-> SDLC Harness sync mock/tool thật
-> SDLC Harness ghi tool_sync_report
-> Niko đọc status/sync result
-> Niko báo người dùng
```

Niko không tự sync requirement/design/code artifact lên tool. Niko chỉ nhận `last_sync` và `tool_sync_report` từ SDLC Harness để báo cho người dùng.

## Mock Targets

Phase 1 tạo hai mock target:

```text
.runtime/runs/<run_id>/sync/jira-comment.md
.runtime/runs/<run_id>/sync/confluence-page.md
```

### Jira Mock

Mô phỏng comment Jira:

```markdown
# Jira Mock Comment

Run: <run_id>
Status: waiting_requirement_approval

Requirement packet v1 đã được tạo từ eTour UC1 bằng Requirement Refiner.
Đang chờ human approval trước khi sang design.
```

### Confluence Mock

Mô phỏng page Confluence:

```markdown
# Requirement Packet - eTour UC1

<summary requirement packet>

## Trace

source:etour:UC1 -> requirement_refiner -> requirement_packet_v1
```

## Tool Sync Report

Sau khi tạo mock files, Harness ghi:

```text
.runtime/runs/<run_id>/sync/tool_sync_report_v1.json
```

Status Phase 1:

```text
mock_synced
```

## Điều Kiện Trước Phase 2

Mock sync chỉ dùng cho thử nghiệm đầu. Trước khi sang Phase 2, cần có ít nhất một adapter thật tối thiểu, ưu tiên:

1. Jira comment API, hoặc
2. Confluence page API.

Khi adapter thật có mặt, `tool_sync_report` phải ghi rõ:

```text
mode: real
external_id hoặc url
sync_status
error nếu thất bại
```
