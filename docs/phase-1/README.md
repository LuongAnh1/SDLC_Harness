# Phase 1 - Use Case Đến Requirement Packet

Phase 1 là lát cắt triển khai đầu tiên của SDLC Harness. Mục tiêu là chứng minh một use case có thể được SDLC Harness kéo về từ fixture local hoặc Jira issue, chuẩn hóa thành **formal use case** theo tinh thần TraceDev, sinh **requirement packet** cho người duyệt, kiểm tra bằng Validator, đồng bộ mock hoặc real tool, rồi trả trạng thái cho Niko đọc.

Requirement Contract V2 hiện là contract runtime duy nhất. Phase 1 sinh canonical JSON, chạy validation/clarification, rồi render packet và formal use case làm projection.

Luồng Phase 1:

```text
start-or-get-run(eTour UC1) hoặc start-from-jira(SDLC-1)
-> kiểm tra active run theo source_ref
-> load fixture hoặc Jira source
-> Requirement Refiner sinh requirement_model_v2_r1.json
-> Python validator kiểm tra schema, grounding, references và blocking questions
-> nếu còn blocking question: state=waiting_requirement_clarification
-> nếu đã rõ: render requirement_packet_v2_r1.md + formal_use_case_v2_r1.txt
-> sync Jira/Confluence
-> state=waiting_requirement_approval
-> get-status trả JSON cho Niko
```

## Mục Tiêu

Phase 1 cần chứng minh:

- SDLC Harness có application contract ổn định cho HTTP API và CLI operator.
- SDLC Harness sở hữu việc load source; Niko không tự kéo use case từ fixture/Jira/dataset.
- Fixture eTour UC1 được đưa qua Requirement Refiner để tạo requirement artifact đầu tiên.
- Requirement Validator tối thiểu kiểm tra packet trước khi tạo checkpoint.
- Workflow state được lưu trong `.runtime/runs/<run_id>/`.
- Requirement packet có checkpoint chờ người dùng duyệt.
- Sync report được tạo bởi SDLC Harness, không phải bởi Niko.
- `get-status` trả đủ thông tin để Niko báo lại người dùng.

## Không Làm Trong Phase 1

- Chưa gọi Designer/Developer/Tester.
- Chưa tạo design/code/test artifact.
- Chưa gọi GitHub API thật.
- Trong phạm vi Phase 1 ban đầu, Niko chưa điều khiển approval/change request;
  integration hiện hành đã dùng HTTP API nhưng vẫn không tự kéo source hay sync tool.
- Chưa xử lý nhiều use case hoặc impact analysis.
- Chưa mở đa ngôn ngữ.

## Tài Liệu Trong Folder Này

- `flow.md`: luồng xử lý `start-or-get-run` và `get-status`.
- `cli-contract.md`: contract CLI JSON lịch sử và interface operator hiện hành.
- `operator-guide.md`: cách chạy tay Phase 1, bao gồm `start-or-get-run`,
  `get-status`, `sync-run` và các bước kiểm tra Jira/Confluence/Niko.
- `runtime-layout.md`: cấu trúc `.runtime/runs/<run_id>/` và active run index.
- `artifacts.md`: nội dung tối thiểu của các artifact Phase 1.
- `mock-sync.md`: cách mock sync Jira/Confluence và ranh giới với API thật.
- `real-sync-implementation.md`: kế hoạch triển khai sync thật Jira/Confluence
  Cloud trước khi khóa Phase 1.
- `phase-1-checklist.md`: checklist triển khai code Phase 1.
- `requirement-v2/README.md`: thiết kế canonical requirement model, xử lý xung
  đột formal/packet và clarification cho Open Questions.

## Đầu Ra Bắt Buộc

Một run V2 tạo source packet/snapshot, canonical model, deterministic projections, validation/decision artifacts và sync report như mô tả tại `runtime-layout.md`.

## Run Idempotency Theo Source Ref

Phase 1 dùng hành vi `start-or-get-run`, không phải luôn tạo run mới.

```text
Nếu eTour/UC1 chưa có active run:
  -> tạo run mới, gọi Requirement Refiner, sync mock, trả status.

Nếu eTour/UC1 đã có active run chưa done/cancelled:
  -> không load/refine lại mù quáng, trả status của run hiện tại.

Nếu run cũ đã done/cancelled:
  -> Phase 1 có thể tạo run mới khi người dùng yêu cầu thực hiện lại.
```

Niko không cần biết UC đã được kéo/load hay chưa. Niko chỉ gọi SDLC Harness và nhận status/sync result.

## Điều Kiện Hoàn Thành Phase 1

- CLI `start-or-get-run` chạy được với eTour UC1.
- CLI `get-status` trả lại state của run vừa tạo.
- Output runtime đúng layout đã chốt.
- Requirement Refiner được gọi và tạo requirement packet.
- Requirement Validator chạy trước checkpoint và chặn các lỗi phân loại thô như
  nhét exception/quality vào postcondition hoặc acceptance criteria.
- Requirement checkpoint có `allowed_next_actions` rõ.
- Sync report thể hiện Jira/Confluence đã được cập nhật mock hoặc real theo cấu hình.
- Checklist Phase 1 được tick sau khi test pass.

## Trạng Thái Triển Khai

Cập nhật 2026-10-13: Phase 1 đã có package/CLI chạy được với fake LLM, formal use case artifact, unit test pass, real sync Jira/Confluence, re-sync idempotent, Jira source intake và smoke test qua Niko. Sau live run thật, schema requirement packet đã được tách rõ exception/postcondition/acceptance/quality để checkpoint dễ review hơn. Chi tiết nằm ở `progress.md`.

Trước khi chuyển sang Phase 2, không chỉ cần code Phase 1 chạy pass. Phase 1 chỉ được khóa khi artifact thật trên Jira/Confluence đã đủ rõ để người duyệt dùng như checkpoint, sau đó mới mở approval/design flow.

## Requirement Packet Và Formal Use Case

Canonical artifact là `requirement_model_v2_r<n>.json`. `requirement_packet_v2_r<n>.md` và `formal_use_case_v2_r<n>.txt` được render deterministically từ cùng revision để phục vụ review, debug và downstream context.

V1 refiner, V1 validator, V1 prompt và dual reader đã được gỡ ngày 2026-10-16. Run có `requirement_contract_version` khác `v2` bị từ chối bằng `requirement_contract_unsupported`; không có fallback ngầm sang text artifact cũ. Các tên `use_case_packet_v1` và `tool_sync_report_v1` là version riêng của raw-source/sync artifact.
