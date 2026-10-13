# Roadmap SDLC Harness

Roadmap này đi theo hướng triển khai từng vertical slice. Mỗi phase phải tạo được một đoạn flow có thể chạy, có trạng thái và có nơi để Niko đọc lại.

## Phase 0 - Nền Tài Liệu Và Contract

Mục đích: chốt ranh giới repo, artifact, state và contract Niko gọi.

Tài liệu chi tiết: `docs/phase-0/README.md`. Phase này chỉ được xem là xong khi checklist trong `docs/phase-0/phase-0-checklist.md` được review và tick đủ những quyết định nền.

Việc cần làm:

- Chốt vai trò Niko và SDLC Harness.
- Chốt Java là target language ban đầu.
- Chốt một use case là đơn vị workflow đầu tiên.
- Chốt API tối thiểu: `start_run`, `get_status`, `approve_checkpoint`, `request_change`, `resume_run`, `cancel_run`.
- Chốt artifact tối thiểu: requirement packet, design artifact, trace graph, implementation artifact, test report, tool sync report.

## Phase 1 - Use Case Đến Requirement Packet

Mục đích: chạy được lát cắt đầu từ một use case eTour/SMOS/iTrust tới requirement packet có checkpoint.

Tài liệu chi tiết: `docs/phase-1/README.md`. Phase này chỉ tick hoàn thành khi CLI/runtime/artifact chạy thật, mock sync pass cho test, real Jira/Confluence sync pass khi cấu hình Atlassian, và checklist trong `docs/phase-1/phase-1-checklist.md` pass.

Luồng:

```text
start-or-get-run(UC1) hoặc start-from-jira(SDLC-1)
-> SDLC Harness kiểm tra active run theo source_ref
-> nếu cần thì load fixture hoặc fetch Jira issue
-> Requirement Refiner LLM tạo requirement_packet_v1
-> Requirement Validator kiểm tra tối thiểu
-> sync Jira/Confluence mock
-> status=waiting_requirement_approval
```

Kết quả mong muốn:

- Có `run_id`.
- Có state truy vấn được.
- Có artifact `requirement_packet_v1` do Requirement Refiner tạo.
- Có sync report.
- Niko có thể hỏi status và báo người dùng duyệt.

## Phase 2 - Requirement Packet Đến Design

Mục đích: sau khi người dùng duyệt requirement, tạo design artifact và checkpoint tiếp theo.

Tài liệu chi tiết: `docs/phase-2/README.md`. Phase này chỉ bắt đầu triển khai
code sau khi đã chốt vòng Designer/Validator, state/artifact versioning và
checklist trong `docs/phase-2/phase-2-checklist.md`.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved requirement_packet_v1
-> đọc formal_use_case_v1 nếu có
-> Designer tạo design_artifact_v1
-> Parser tạo class/sequence graph từ PlantUML
-> Validator tạo requirement-design trace từ requirement/class/sequence graph
-> nếu thiếu link: Validator tạo quality report, Designer update design, rồi kiểm lại
-> sync design checkpoint theo `docs/phase-2/sync-design-checkpoint.md`
-> status=waiting_design_approval
```

Nếu Phase 2 phát hiện requirement thiếu nghiêm trọng, không tự refine requirement trong Phase 2. Workflow phải chuyển về `waiting_requirement_approval` hoặc `blocked` để người dùng sửa/duyệt lại.

Kết quả mong muốn:

- Có design artifact dễ đọc.
- Có trace requirement-design.
- Có missing-link report và quality report của Validator.
- Có trạng thái chờ duyệt design.
- Niko đọc được summary để báo lại.

## Phase 3 - Design Đến Implementation Plan

Mục đích: sau khi người dùng duyệt design checkpoint, tạo một bản kế hoạch triển
khai trước khi agent được phép sửa target repo. Phase này là checkpoint riêng của
harness, không phải bước có sẵn trong TraceDev.

Tài liệu chi tiết: `docs/phase-3/README.md`. Phase này chỉ sinh và kiểm
`implementation_plan_v1`, chưa tạo branch/code/commit/Draft PR.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved requirement/design/trace graph
-> Developer.plan tạo implementation_plan_v1
-> Validator.review_plan hybrid: rule hard gate + LLM content review
-> nếu fail và còn lượt repair: Developer.repair_plan tạo implementation_plan_v2/v3
-> sync plan lên Confluence và comment trạng thái lên Jira
-> status=waiting_implementation_plan_review
```

`implementation_plan_v1` chỉ ánh xạ từ graph hiện tại ra kế hoạch code dự kiến:
target repo/branch, file/class/method dự kiến, test intent, assumptions và out of
scope. Nó không tự sửa requirement/design graph. Nếu plan phát hiện design thiếu
hoặc mâu thuẫn, workflow phải `blocked` hoặc quay về checkpoint phù hợp.

Kết quả mong muốn:

- Có `implementation_plan_v1.md`.
- Có plan review report của Validator.
- Có sync report Phase 3 cho Jira/Confluence.
- State dừng ở `waiting_implementation_plan_review`.
- Chưa tạo branch/code/commit/Draft PR.

## Phase 4 - Implementation Plan Đến Code/Test/Draft PR

Mục đích: sau khi implementation plan được duyệt, Developer triển khai code dựa
trên plan và approved design, sau đó Tester/Validator kiểm chứng bằng graph.

Tài liệu chi tiết: `docs/phase-4/README.md`.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved implementation_plan_v1 + approved design graph
-> sinh developer_work_packet_v1.json
-> tạo branch trong target repo
-> Developer.act tạo/sửa code dựa trên plan
-> Tester chạy compile/test hoặc ghi skipped rõ ràng
-> Validator kiểm requirement -> design -> code -> test trace
-> tạo Draft PR trên GitHub
-> sync PR/test/trace summary về Jira và Confluence
-> status=code_review_ready
```

Kết quả mong muốn:

- Có `developer_work_packet_v1.json` và state tạm `implementation_ready` sau Slice 4.1.
- Có branch/commit/Draft PR thật trong target repo.
- Có `code_change_report_v1.md`.
- Có `test_report_v1.md`.
- Có `requirement_design_code_trace_v1.json`.
- Có `tool_sync_report_v4.json`.
- Niko báo được PR link, test summary và checkpoint code review.

## Phase 5 - Sửa Tại Checkpoint

Mục đích: người dùng có thể yêu cầu sửa ngay tại checkpoint và hệ thống rerun đúng phase.

Ví dụ:

```text
request_change(run_id, "đổi actor thành Admin")
-> tạo requirement_packet_v2 hoặc design_artifact_v2
-> chạy Validator lại
-> sync lại tool
-> giữ status ở checkpoint tương ứng
```

## Phase 6 - Context Động Từ Tool

Mục đích: context thay đổi ngoài checkpoint cũng được phát hiện và invalidate đúng phần.

Nguồn thay đổi:

- Jira comment mới.
- Confluence page sửa.
- PR review mới.
- Chat bổ sung acceptance criteria.

## Phase 7 - Nhiều Use Case Và Impact Analysis

Mục đích: xử lý quan hệ chéo giữa use case.

- Suy luận shared component/class từ trace links.
- Tạo impact graph.
- Khi một use case đổi, phát hiện use case khác có thể bị ảnh hưởng.
- Chạy regression/trace check cho nhóm liên quan.




> Cập nhật 2026-10-13: Phase 1 hiện đã có đường chạy thật từ Jira issue,
> Requirement Refiner, validator/repair loop và sync Jira/Confluence. Niko gọi
> SDLC_Harness qua CLI JSON và chỉ format kết quả cho Telegram. Phase 2
> thiết kế/coding vẫn chưa bắt đầu.

> Cập nhật 2026-10-13: đã viết tài liệu triển khai Phase 2 trong
> `docs/phase-2/`, tập trung vào vòng Designer/Validator, traceability graph,
> missing-link feedback và state/artifact versioning. Chưa triển khai code Phase 2.

> Cập nhật 2026-10-13: Phase 2 đã có lát code V0 cho
> `approve-checkpoint`: đọc requirement packet đã duyệt, sinh fake design
> artifact/trace/report, mock sync `tool_sync_report_v2` và chuyển state sang
> `waiting_design_approval`. Designer/Validator thật, repair loop, real sync và
> Niko approve route vẫn chưa triển khai.

> Cập nhật 2026-10-13: Phase 2 đã có lát V1 cho Designer/parser: Designer Agent
> dùng prompt TraceDev-style, fake mode sinh PlantUML ổn định, parser tạo
> class/sequence graph thật và workflow ghi `parser_only_v1` /
> `traceability_pending_v1`. Validator traceability LLM và repair loop vẫn là
> bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V2 cho Validator traceability:
> requirement graph, class trace, sequence trace và missing-link report được ghi
> ở mode `traceability_v1`. Repair loop `Designer.update_design`, real sync
> idempotent và Niko approve route vẫn là bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V3 cho Designer repair loop:
> Validator missing-link report được đưa ngược lại cho Designer, artifact sinh
> theo version `v1/v2/v3`, pass thì mở `design_approval`, hết 2 vòng repair vẫn
> thiếu link thì `status=blocked`. Real sync idempotent và Niko approve route
> vẫn là bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V4 cho cấu hình và real sync:
> `SDLC_DESIGN_REPAIR_LIMIT` điều khiển số vòng repair, `sync-run` tự chọn
> requirement/design checkpoint, real Jira/Confluence sync tạo hoặc update target
> theo `external_id`, và unit test đã khóa create/update/partial failure. Cần
> smoke real Atlassian Phase 2 và nối Niko route duyệt checkpoint.

> Cập nhật 2026-10-13: Phase 2 đã được khóa trước Phase 3 ở mức static
> approve-only. `approve-checkpoint` chạy qua Niko, Design/Validator/repair loop
> dừng ở `waiting_design_approval`, PlantUML app macro là output diagram chính,
> Mermaid để backlog, và regression test đã khóa trường hợp requirement không
> traceable bằng `status=blocked` + `last_error` rõ.

> Cập nhật 2026-10-13: đã viết lại ranh giới Phase 3 thành implementation
> planning checkpoint. Phase 3 sinh và kiểm `implementation_plan_v1`; Phase 4
> mới sở hữu branch/code/test/Draft PR.

> Cập nhật 2026-10-13: đã tạo target test repo private
> `LuongAnh1/SDLC_Target_eTour` cùng cấp với `Niko_Agent` và `SDLC_Harness`.
> Repo này là code collaboration lane ban đầu cho Phase 4.

> Cập nhật 2026-10-13: Phase 3 đã có Slice 1 cho `approve-checkpoint` từ
> `waiting_design_approval`: dispatcher chọn đúng workflow, guard target repo
> kiểm tra path/remote/dirty worktree, sinh `implementation_plan_v1.md`, ghi
> `approved_artifacts` và dừng ở `waiting_implementation_plan_review`. Chưa tạo
> branch/code/commit/test report/Draft PR.

> Cập nhật 2026-10-13: Phase 3 đã có Slice 2/3 cho Developer.plan có cấu trúc,
> Validator.review_plan và sync Jira/Confluence. Run Phase 3 hiện sinh
> `implementation_plan_v1.md`, `implementation_plan_structured_v1.json`,
> `implementation_plan_review_v1.json`, `tool_sync_report_v3.json` và ghi
> `last_sync.phase=implementation_plan_review`.

> Cập nhật 2026-10-13: Phase 3 đã chuyển `Validator.review_plan` sang hybrid
> rule + LLM. Rule gate kiểm graph/schema trước, LLM review kiểm semantic gap
> giữa requirement/design/trace và implementation plan. `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`
> điều khiển số vòng `Developer.repair_plan`; mỗi vòng ghi artifact version
> riêng và chỉ version cuối pass mới sync Jira/Confluence.

> Cập nhật 2026-10-13: Phase 4 được tách ra thành phase mới để khóa phần
> code/test/Draft PR sau khi implementation plan được duyệt.

> Cập nhật 2026-10-13: Phase 3 đã khóa bằng live Telegram/Niko run
> `run_20261013_160421_f661babf`. Niko gọi SDLC_Harness từ Jira issue, duyệt
> requirement, duyệt design, SDLC sinh `implementation_plan_v4`, Validator pass
> sau 3 vòng repair và sync Jira/Confluence ở checkpoint
> `waiting_implementation_plan_review`. Phase 4 code/test/Draft PR vẫn là bước
> kế tiếp, chưa mở tự động từ Niko.
