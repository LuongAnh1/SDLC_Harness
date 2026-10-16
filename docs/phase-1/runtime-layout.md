# Runtime Layout Phase 1

Runtime output nằm trong `.runtime/` và không được commit.

```text
.runtime/
  index/active_runs.json
  runs/<run_id>/
    state.json
    artifacts/
      use_case_packet_v1.md
      source_snapshot_v1.json
      requirement_model_v2_r1.json
      requirement_packet_v2_r1.md
      formal_use_case_v2_r1.txt
      requirement_validation_v2_r1.json
      requirement_decisions_v2_r1.json
      ... revision r2, r3 khi clarification thay đổi model
    sync/
      jira-comment.md
      confluence-page.md
      tool_sync_report_v1.json
```

`state.json` luôn có `requirement_contract_version: "v2"`. `latest_artifacts`
trỏ tới requirement revision hiện tại; revision cũ được giữ bất biến để audit.
`pending_checkpoint.current_question` và `remaining_blocking_count` chỉ xuất hiện
khi workflow đang chờ clarification.

Tên `use_case_packet_v1`, `source_snapshot_v1` và `tool_sync_report_v1` là version
schema riêng của các artifact đó. Chúng không kích hoạt Requirement Contract V1.

Run lịch sử ghi contract khác V2 không được tiếp tục qua downstream workflow.
