# Phase 1 - Use Case Đến Requirement Packet

Phase 1 là lát cắt triển khai đầu tiên của SDLC Harness. Mục tiêu là chứng minh một use case có thể được SDLC Harness kéo về, chuẩn hóa thành **formal use case** theo tinh thần TraceDev, sinh **requirement packet** cho người duyệt, kiểm tra bằng Validator, đồng bộ mock tool và trả trạng thái cho Niko đọc.

Luồng Phase 1:

```text
start-or-get-run(eTour UC1)
-> SDLC Harness kiểm tra active run theo source_ref
-> nếu chưa có active run: load fixture
-> tạo workflow run
-> gọi Requirement Refiner LLM để sinh formal use case
-> Formal Use Case Validator kiểm tra grammar tối thiểu
-> tạo use_case_packet_v1
-> tạo formal_use_case_v1 + formal_check_report_v1
-> sinh requirement_packet_v1 cho checkpoint người duyệt
-> tạo trace_graph_v1
-> mock sync Jira/Confluence
-> state=waiting_requirement_approval
-> get-status trả JSON cho Niko
```

## Mục Tiêu

Phase 1 cần chứng minh:

- SDLC Harness có CLI JSON đủ ổn định để Niko gọi sau này.
- SDLC Harness sở hữu việc load source; Niko không tự kéo use case từ fixture/Jira/dataset.
- Fixture eTour UC1 được đưa qua Requirement Refiner để tạo requirement artifact đầu tiên.
- Requirement Validator tối thiểu kiểm tra packet trước khi tạo checkpoint.
- Workflow state được lưu trong `.runtime/runs/<run_id>/`.
- Requirement packet có checkpoint chờ người dùng duyệt.
- Mock sync report được tạo bởi SDLC Harness, không phải bởi Niko.
- `get-status` trả đủ thông tin để Niko báo lại người dùng.

## Không Làm Trong Phase 1

- Chưa gọi Designer/Developer/Tester.
- Chưa tạo design/code/test artifact.
- Chưa gọi Jira/Confluence/GitHub API thật.
- Chưa tích hợp Niko thật; Niko sau này chỉ gọi CLI/status, không tự kéo use case hay tự sync tool.
- Chưa xử lý nhiều use case hoặc impact analysis.
- Chưa mở đa ngôn ngữ.

## Tài Liệu Trong Folder Này

- `flow.md`: luồng xử lý `start-or-get-run` và `get-status`.
- `cli-contract.md`: contract CLI JSON cho Niko gọi.
- `runtime-layout.md`: cấu trúc `.runtime/runs/<run_id>/` và active run index.
- `artifacts.md`: nội dung tối thiểu của các artifact Phase 1.
- `mock-sync.md`: cách mock sync Jira/Confluence và ranh giới với API thật.
- `phase-1-checklist.md`: checklist triển khai code Phase 1.

## Đầu Ra Bắt Buộc

Sau khi Phase 1 code hoàn thành, một smoke test `start-or-get-run` phải tạo được:

```text
.runtime/runs/<run_id>/state.json
.runtime/runs/<run_id>/artifacts/use_case_packet_v1.md
.runtime/runs/<run_id>/artifacts/formal_use_case_v1.txt
.runtime/runs/<run_id>/artifacts/formal_check_report_v1.json
.runtime/runs/<run_id>/artifacts/requirement_packet_v1.md
.runtime/runs/<run_id>/artifacts/trace_graph_v1.json
.runtime/runs/<run_id>/sync/jira-comment.md
.runtime/runs/<run_id>/sync/confluence-page.md
.runtime/runs/<run_id>/sync/tool_sync_report_v1.json
.runtime/index/active_runs.json
```

Và CLI phải trả JSON có:

```text
ok
created_new_run
source_ref
run_id
current_phase
status
latest_artifacts
pending_checkpoint
allowed_next_actions
last_sync
last_error
summary_for_user
```

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
- Requirement Validator tối thiểu chạy trước checkpoint.
- Requirement checkpoint có `allowed_next_actions` rõ.
- Mock sync report thể hiện Jira/Confluence đã được cập nhật giả lập.
- Checklist Phase 1 được tick sau khi test pass.

## Trạng Thái Triển Khai

Cập nhật 2026-10-12: Phase 1 đã có package/CLI chạy được với fake LLM, formal use case artifact, unit test pass và smoke test tạo runtime artifact thành công. Chi tiết nằm ở `progress.md`.

Trước khi chuyển sang Phase 2, không chỉ cần code Phase 1 chạy pass. Phase 1 chỉ được khóa khi đã nối Niko gọi CLI/status ở mức tối thiểu, có sync API/tool thật tối thiểu cho Jira hoặc Confluence, và smoke test lại sau integration.

## Requirement Packet Và Formal Use Case

Artifact hiện tại của Phase 1 gồm `formal_use_case_v1.txt` và `requirement_packet_v1.md`. `formal_use_case_v1.txt` là bản máy đọc theo grammar TraceDev để tầng Designer/Validator dùng sau. `requirement_packet_v1.md` là bản dễ đọc để Niko/người dùng duyệt, có Open Questions và Source Evidence.

Quy ước đuôi file: prompt/artifact formal use case dùng `.txt` vì là grammar máy đọc; prompt/artifact requirement packet dùng `.md` vì hướng tới checkpoint người duyệt.

