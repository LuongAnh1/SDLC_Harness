# State Và Artifact Phase 2

Phase 2 mở rộng state machine hiện có từ checkpoint requirement sang checkpoint
design. Mọi state và artifact phải đủ để Niko đọc lại mà không cần hiểu nội bộ
Designer/Validator.

## State Machine Phase 2

```text
waiting_requirement_approval
  -> design_drafting
  -> design_validating
  -> design_repairing
  -> design_validating
  -> waiting_design_approval
```

Các nhánh lỗi:

```text
design_validating -> blocked
design_repairing -> blocked
design_drafting -> failed
```

## Ý Nghĩa State

| State | Ý nghĩa | Ai kích hoạt bước tiếp theo |
| --- | --- | --- |
| `design_drafting` | Designer đang tạo analysis/design từ requirement đã duyệt. | Harness |
| `design_validating` | Parser/Validator đang đọc graph, tạo trace links và missing-link report. | Harness |
| `design_repairing` | Designer đang sửa design theo quality report của Validator. | Harness |
| `waiting_design_approval` | Design đã pass Validator và chờ người dùng duyệt. | Niko + human |
| `blocked` | Không thể tự sửa, thường do requirement thiếu/mâu thuẫn hoặc design vẫn fail sau repair limit. | Niko + human |
| `failed` | Lỗi runtime/API/parse không phục hồi được trong lượt chạy. | Operator |

## Artifact Chính

| Artifact | Mục đích |
| --- | --- |
| `designer_analysis_v{n}.md` | Lý do chọn architecture/pattern, decision ban đầu cho design. |
| `design_artifact_v{n}.md` | Class diagram + sequence diagram, ưu tiên PlantUML để parse thành graph. |
| `class_diagram_graph_v{n}.json` | Graph máy đọc từ class diagram. |
| `sequence_diagram_graph_v{n}.json` | Graph máy đọc từ sequence diagram. |
| `requirement_design_trace_v{n}.json` | Trace links requirement -> class/method/lifeline/message. |
| `missing_design_links_v{n}.json` | Requirement chưa được link đủ ở class hoặc sequence diagram. |
| `design_quality_report_v{n}.md` | Report của Validator cho Designer/human đọc. |
| `tool_sync_report_v2.json` | Kết quả sync checkpoint design lên Jira/Confluence, gồm phase, target, external id, artifact refs và trạng thái partial/full sync. |

## Versioning

Mỗi vòng Validator -> Designer update tạo một version mới:

```text
design_artifact_v1
requirement_design_trace_v1
missing_design_links_v1
design_quality_report_v1

design_artifact_v2
requirement_design_trace_v2
missing_design_links_v2
design_quality_report_v2
```

Quy tắc:

- Version mới không ghi đè version cũ.
- `latest_artifacts` trỏ tới version mới nhất.
- `quality_reports.design_traceability` lưu report mới nhất và danh sách version đã chạy.
- Nếu design pass, `pending_checkpoint.artifact_path` trỏ tới `design_artifact_v{latest}`.
- Repair limit đọc từ `SDLC_DESIGN_REPAIR_LIMIT`, mặc định `2`; đặt `0` để chỉ
  validate draft đầu tiên.
- Nếu hết repair limit mà vẫn còn missing link, `status=blocked`, không mở
  pending checkpoint design và `latest_artifacts` vẫn trỏ tới version cuối để
  human/operator đọc lại.

## State JSON Bổ Sung

Khi Phase 2 bắt đầu:

```json
{
  "current_phase": "design_drafting",
  "status": "design_drafting",
  "approved_artifacts": {
    "requirement_packet": "artifacts/requirement_packet_v1.md",
    "formal_use_case": "artifacts/formal_use_case_v1.txt"
  },
  "summary_for_user": "Đang tạo design từ requirement đã duyệt."
}
```

Khi chờ duyệt design:

```json
{
  "current_phase": "design_review",
  "status": "waiting_design_approval",
  "latest_artifacts": {
    "design_artifact": "artifacts/design_artifact_v2.md",
    "requirement_design_trace": "artifacts/requirement_design_trace_v2.json",
    "missing_design_links": "artifacts/missing_design_links_v2.json",
    "design_quality_report": "artifacts/design_quality_report_v2.md"
  },
  "pending_checkpoint": {
    "checkpoint_id": "checkpoint_design_<run_id>",
    "type": "design_approval",
    "artifact_ref": "design_artifact_v2",
    "artifact_path": "artifacts/design_artifact_v2.md"
  },
  "last_sync": {
    "phase": "design_checkpoint",
    "status": "synced",
    "report_path": "sync/tool_sync_report_v2.json"
  },
  "allowed_next_actions": [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status"
  ]
}
```

## Trace Graph Phase 2

Trace graph Phase 2 mở rộng từ Phase 1:

```text
source_use_case
  -> formal_use_case_v1
  -> requirement_packet_v1
  -> designer_analysis_v1
  -> design_artifact_v1/v2
  -> class_diagram_graph_vN
  -> sequence_diagram_graph_vN
  -> requirement_design_trace_vN
  -> design_quality_report_vN
```

Trong JSON, trace graph nên phân biệt:

- `artifact` nodes: file thật trong runtime.
- `agent_step` nodes: Designer/Validator.
- `validation_step` nodes: graph parse, trace link generation, missing-link check.
- `sync_target` nodes: Jira comment, Confluence page.

## Sync State

Khi Phase 2 pass Validator, workflow chạy sync design checkpoint trước khi trả
state cuối cho Niko. `last_sync` trong `state.json` là bản tóm tắt ngắn; chi tiết
đầy đủ nằm ở `sync/tool_sync_report_v2.json`.

Quy tắc:

- `last_sync.phase=design_checkpoint` khi checkpoint hiện tại là duyệt design.
- `last_sync.status=synced` khi Jira và Confluence đều ghi thành công.
- `last_sync.status=partial_failed` khi một target lỗi nhưng artifact local đã có.
- `last_sync.status=failed` khi không target nào ghi được.
- `last_sync.status=mock_synced` khi đang chạy mock/local.
- Nếu sync lỗi, `pending_checkpoint` vẫn được giữ để người dùng có thể review
  artifact local hoặc operator chạy lại `sync-run`.

## Checkpoint Design

Checkpoint design phải trả đủ thông tin để Niko nói ngắn gọn:

```text
Design đã tạo xong.
Validator đã đọc được class/sequence graph và chạy traceability V1.
Missing links: 0 hoặc danh sách rút gọn.
Artifact: design_artifact_vN.
Đang chờ anh duyệt trước khi sang code.
```

Nếu còn missing links nhưng đã hết repair vòng:

```text
status=blocked
summary_for_user nêu số missing link; report cuối liệt kê requirement cụ thể
allowed_next_actions=[cancel_run, get_status]
```

