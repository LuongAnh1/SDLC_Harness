# Phase 1 Artifacts

Phase 1 tạo artifact tối thiểu để chứng minh use case đã được Requirement Refiner chuẩn hóa thành requirement packet có thể duyệt.

## `use_case_packet_v1.md`

Mục đích: giữ bản chuẩn hóa nhẹ của input gốc.

Nội dung tối thiểu:

```markdown
# Use Case Packet v1

- Run ID:
- Source Ref: dataset_use_case:eTour:UC1
- Dataset: eTour
- Use Case: UC1
- Source fixture: fixtures/etour/UC1.txt

## Raw Use Case

<copy nội dung UC1 hoặc trích nguyên văn có cấu trúc>
```

## `requirement_packet_v1.md`

Mục đích: artifact chính để human duyệt trước khi sang design.

Nội dung tối thiểu:

```markdown
# Requirement Packet v1

- Run ID:
- Source: eTour UC1
- Produced by: Requirement Refiner
- Status: waiting_requirement_approval

## Goal

## Actors

## Main Flow

## Preconditions

## Postconditions / Acceptance Criteria

## Open Questions

## Source Evidence

## Notes For Validator
```

Phase 1 dùng LLM ở role Requirement Refiner để sinh requirement packet. Nếu trường nào không có đủ bằng chứng từ UC1, Requirement Refiner phải ghi vào `Open Questions` hoặc `Notes For Validator`, không bịa.

## Requirement Validator Phase 1

Validator tối thiểu kiểm tra packet có đủ các vùng chính:

```text
Goal
Actors
Main Flow
Preconditions
Postconditions / Acceptance Criteria
Open Questions
Source Evidence
```

Validator chưa cần chứng minh semantic correctness sâu. Mục tiêu là ngăn packet thiếu cấu trúc hoặc bịa thông tin không có trong UC1.

## `trace_graph_v1.json`

Mục đích: trace tối thiểu từ source use case sang requirement packet.

Schema khái niệm:

```json
{
  "version": 1,
  "nodes": [
    {"id": "source:etour:UC1", "type": "source_use_case"},
    {"id": "agent:requirement_refiner", "type": "agent_role"},
    {"id": "artifact:requirement_packet_v1", "type": "requirement_packet"}
  ],
  "edges": [
    {"from": "source:etour:UC1", "to": "agent:requirement_refiner", "type": "read_by"},
    {"from": "agent:requirement_refiner", "to": "artifact:requirement_packet_v1", "type": "produced"},
    {"from": "source:etour:UC1", "to": "artifact:requirement_packet_v1", "type": "normalized_into"}
  ]
}
```

## `tool_sync_report_v1.json`

Mục đích: ghi nhận sync mock do SDLC Harness thực hiện.

Nội dung tối thiểu:

```json
{
  "version": 1,
  "mode": "mock",
  "status": "mock_synced",
  "targets": [
    {"tool": "jira", "artifact": "requirement_packet_v1", "path": ".runtime/runs/<run_id>/sync/jira-comment.md"},
    {"tool": "confluence", "artifact": "requirement_packet_v1", "path": ".runtime/runs/<run_id>/sync/confluence-page.md"}
  ]
}
```

## Artifact Ownership

SDLC Harness sở hữu tất cả artifact Phase 1. Niko chỉ đọc summary/status và không tự sửa artifact runtime.

## Formal Use Case v1

`formal_use_case_v1.txt` là artifact máy đọc theo grammar TraceDev. Nó dùng các keyword `UsecaseName`, `Description`, `Actors`, `Entry Conditions`, `Flow of Events`, `Exit Conditions`, `Quality Requirement` để Designer/Validator có input ổn định.

`formal_check_report_v1.json` lưu kết quả kiểm tra grammar tối thiểu và cờ `repaired` để biết Requirement Refiner có phải gọi repair prompt hay không.
