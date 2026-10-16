# Phase 1 - Use Case Đến Requirement Packet

Phase 1 là lát cắt triển khai đầu tiên của SDLC Harness. Mục tiêu là chứng minh một use case có thể được SDLC Harness kéo về từ fixture local hoặc Jira issue, chuẩn hóa thành **formal use case** theo tinh thần TraceDev, sinh **requirement packet** cho người duyệt, kiểm tra bằng Validator, đồng bộ mock hoặc real tool, rồi trả trạng thái cho Niko đọc.

Luồng bên dưới là contract V1 đang chạy. Requirement Model V2 và clarification
checkpoint đã được chốt ở mức thiết kế trong `requirement-v2/`, nhưng chưa thay
contract runtime cho tới khi checklist V2 hoàn tất.

Luồng Phase 1:

```text
start-or-get-run(eTour UC1) hoặc start-from-jira(SDLC-1)
-> SDLC Harness kiểm tra active run theo source_ref
-> nếu chưa có active run: load fixture hoặc fetch Jira issue
-> tạo workflow run
-> gọi Requirement Refiner LLM để sinh formal use case
-> Formal Use Case Validator kiểm tra grammar tối thiểu
-> tạo use_case_packet_v1
-> tạo formal_use_case_v1 + formal_check_report_v1
-> sinh requirement_packet_v1 cho checkpoint người duyệt
-> tạo trace_graph_v1
-> sync Jira/Confluence theo cấu hình mock/real
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
- Sync report được tạo bởi SDLC Harness, không phải bởi Niko.
- `get-status` trả đủ thông tin để Niko báo lại người dùng.

## Không Làm Trong Phase 1

- Chưa gọi Designer/Developer/Tester.
- Chưa tạo design/code/test artifact.
- Chưa gọi GitHub API thật.
- Chưa để Niko tự điều khiển approval/change request; Niko hiện chỉ gọi CLI/status ở mức tối thiểu, không tự kéo use case hay tự sync tool.
- Chưa xử lý nhiều use case hoặc impact analysis.
- Chưa mở đa ngôn ngữ.

## Tài Liệu Trong Folder Này

- `flow.md`: luồng xử lý `start-or-get-run` và `get-status`.
- `cli-contract.md`: contract CLI JSON cho Niko gọi.
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

Sau khi Phase 1 code hoàn thành, một smoke test `start-or-get-run` phải tạo được:

```text
.runtime/runs/<run_id>/state.json
.runtime/runs/<run_id>/artifacts/use_case_packet_v1.md
.runtime/runs/<run_id>/artifacts/formal_use_case_v1.txt
.runtime/runs/<run_id>/artifacts/formal_check_report_v1.json
.runtime/runs/<run_id>/artifacts/requirement_packet_v1.md
.runtime/runs/<run_id>/artifacts/trace_graph_v1.json
.runtime/runs/<run_id>/sync/jira-comment.md              # mock mode
.runtime/runs/<run_id>/sync/confluence-page.md           # mock mode
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
- Requirement Validator chạy trước checkpoint và chặn các lỗi phân loại thô như
  nhét exception/quality vào postcondition hoặc acceptance criteria.
- Requirement checkpoint có `allowed_next_actions` rõ.
- Sync report thể hiện Jira/Confluence đã được cập nhật mock hoặc real theo cấu hình.
- Checklist Phase 1 được tick sau khi test pass.

## Trạng Thái Triển Khai

Cập nhật 2026-10-13: Phase 1 đã có package/CLI chạy được với fake LLM, formal use case artifact, unit test pass, real sync Jira/Confluence, re-sync idempotent, Jira source intake và smoke test qua Niko. Sau live run thật, schema requirement packet đã được tách rõ exception/postcondition/acceptance/quality để checkpoint dễ review hơn. Chi tiết nằm ở `progress.md`.

Trước khi chuyển sang Phase 2, không chỉ cần code Phase 1 chạy pass. Phase 1 chỉ được khóa khi artifact thật trên Jira/Confluence đã đủ rõ để người duyệt dùng như checkpoint, sau đó mới mở approval/design flow.

## Requirement Packet Và Formal Use Case

Artifact hiện tại của Phase 1 gồm `formal_use_case_v1.txt` và `requirement_packet_v1.md`. `formal_use_case_v1.txt` là bản máy đọc theo grammar TraceDev để tầng Designer/Validator dùng sau. `requirement_packet_v1.md` là bản dễ đọc để Niko/người dùng duyệt, có Open Questions và Source Evidence.

Requirement packet không được copy mù từ formal use case. Nó phải tách riêng
happy path, alternative/exception flows, postconditions, acceptance criteria và
quality/non-functional requirements. `Source Evidence` chỉ dùng nguồn gốc ban
đầu như Jira issue/comment hoặc fixture, không dùng dòng formal use case do
model sinh ra làm bằng chứng.

Quy ước đuôi file: prompt/artifact formal use case dùng `.txt` vì là grammar máy đọc; prompt/artifact requirement packet dùng `.md` vì hướng tới checkpoint người duyệt.

## Requirement V2 Đã Chốt Ở Mức Thiết Kế

V2 đổi thứ tự và quyền ưu tiên artifact:

```text
source
-> requirement_model_v2_r<n>.json (canonical)
-> Python grounding/blocking validation
-> clarification từng câu nếu còn blocking question
-> requirement_packet_v2_r<n>.md + formal_use_case_v2_r<n>.txt (projection)
-> requirement approval
```

Formal V2 có block riêng cho main/alternative/exception flow và không được dùng
để ghi đè canonical JSON. Run V1 giữ nguyên; V2 chỉ áp dụng cho run mới sau khi
rollout gate pass. Xem `requirement-v2/README.md` và `requirement-v2/checklist.md`.

