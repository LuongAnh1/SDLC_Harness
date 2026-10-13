# Runtime Layout Phase 1

Runtime output không được ghi vào `docs/`. Tất cả output chạy thử nằm trong `.runtime/` và được ignore khỏi git.

## Layout Chính

```text
.runtime/
  index/
    active_runs.json
  runs/
    <run_id>/
      state.json
      artifacts/
        use_case_packet_v1.md
        requirement_packet_v1.md
        trace_graph_v1.json
      sync/
        jira-comment.md
        confluence-page.md
        tool_sync_report_v1.json
      logs/
        run.log
```

`logs/run.log` là optional ở Phase 1. Nếu chưa cần log file riêng thì có thể bỏ, nhưng layout nên dành chỗ trước.

## `state.json`

Nội dung tối thiểu:

```json
{
  "run_id": "run_...",
  "source_ref": "dataset_use_case:eTour:UC1",
  "source": {
    "source_type": "dataset_use_case",
    "dataset": "eTour",
    "use_case": "UC1",
    "fixture_path": "fixtures/etour/UC1.txt"
  },
  "current_phase": "waiting_requirement_approval",
  "status": "waiting_requirement_approval",
  "latest_artifacts": {},
  "pending_checkpoint": {},
  "allowed_next_actions": [],
  "last_sync": {},
  "last_error": null,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

## Active Run Index

Để `start-or-get-run` không tạo run mới mù quáng, Phase 1 cần một index tối thiểu:

```text
.runtime/index/active_runs.json
```

Nội dung khái niệm:

```json
{
  "dataset_use_case:eTour:UC1": "run_..."
}
```

Khi run chuyển sang `done` hoặc `cancelled`, key có thể được gỡ hoặc trỏ sang latest run tùy policy Phase sau. Phase 1 chỉ cần đảm bảo active run chưa done/cancelled được trả lại.

## File Naming

Quy tắc đặt tên:

```text
<artifact_name>_v<number>.<ext>
```

Ví dụ:

```text
requirement_packet_v1.md
trace_graph_v1.json
tool_sync_report_v1.json
```

Phase 1 chỉ tạo version `v1`. Version `v2` bắt đầu quan trọng ở Phase 5 khi có sửa tại checkpoint.

## Git Policy

`.runtime/` không commit. Chỉ commit docs, fixture, source code và tests.
