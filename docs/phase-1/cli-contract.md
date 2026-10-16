# CLI Contract Phase 1

CLI in đúng một JSON object ra stdout. Requirement Contract V2 là contract duy nhất;
không còn flag `--requirement-contract-version`.

## Commands

```bash
python -m sdlc_harness.cli start-or-get-run --dataset eTour --use-case UC1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli start-from-jira --issue-key SDLC-1 --requested-by manual --llm-mode fake
python -m sdlc_harness.cli answer-clarification --run-id <run_id> --question-id Q-1 --answer "<answer>" --answered-by manual
python -m sdlc_harness.cli get-status --run-id <run_id>
python -m sdlc_harness.cli sync-run --run-id <run_id> --sync-mode real
```

## Start Response

```json
{
  "ok": true,
  "created_new_run": true,
  "source_ref": "dataset_use_case:eTour:UC1",
  "run_id": "run_...",
  "requirement_contract_version": "v2",
  "current_phase": "requirement_review",
  "status": "waiting_requirement_approval",
  "latest_artifacts": {
    "requirement_model": ".runtime/runs/<run_id>/artifacts/requirement_model_v2_r1.json",
    "requirement_packet": ".runtime/runs/<run_id>/artifacts/requirement_packet_v2_r1.md",
    "formal_use_case": ".runtime/runs/<run_id>/artifacts/formal_use_case_v2_r1.txt",
    "requirement_decisions": ".runtime/runs/<run_id>/artifacts/requirement_decisions_v2_r1.json"
  },
  "pending_checkpoint": {
    "type": "requirement_approval",
    "artifact_ref": "requirement_model_v2_r1"
  },
  "allowed_next_actions": ["approve_checkpoint", "request_change", "cancel_run", "get_status"],
  "last_error": null
}
```

Khi có blocking question, status là `waiting_requirement_clarification`, checkpoint
chứa `current_question` và action hợp lệ gồm `answer_clarification` thay cho approval.

## Idempotency Và Sync

Start idempotent theo `source_ref`. `sync-run` chỉ đồng bộ checkpoint hiện tại và
không refine lại requirement. Các lệnh có side effect mặc định mock; dùng
`--sync-mode real` khi chủ đích cập nhật Jira/Confluence.

## Errors

Lỗi vẫn dùng JSON với `ok=false`, `error_code`, `message` và `run_id` khi có.
State contract khác V2 trả `requirement_contract_unsupported`.
