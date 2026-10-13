# Quy Trình Triển Khai Từng Lát Cắt

Mục tiêu của repo không phải xây xong toàn bộ graph rồi mới nối với Niko. Mỗi đoạn triển khai phải chạy được, đồng bộ được và Niko đọc được trạng thái ngay.

## Nguyên Tắc Vertical Slice

Mỗi slice chỉ được xem là hoàn thành khi có đủ:

```text
state + artifact + checkpoint + sync/mock sync + status cho Niko đọc
```

Nếu một phase chỉ sinh file nhưng không có state, không có checkpoint hoặc Niko không truy vấn được thì chưa đạt tinh thần harness.

## Slice 1 - Use Case Đến Requirement Packet

```text
Input: source_ref = dataset_use_case:eTour:UC1
Processing:
  start-or-get-run
  SDLC Harness load/refine source nếu cần
  Requirement Refiner LLM
  Requirement Validator tối thiểu

Output:
  workflow_run
  requirement_packet_v1
  sync_report_v1
  status=waiting_requirement_approval
```

Niko cần đọc được:

```text
run_id
current_phase
artifact_summary
pending_checkpoint
allowed_next_actions
```

## Slice 2 - Requirement Packet Đến Design

```text
Input: approve requirement checkpoint
Output:
  design_artifact_v1
  trace_graph_v1(requirement-design)
  tool_sync_report_v2
  status=waiting_design_approval
```

Niko cần trả lời được:

- Thiết kế đã tạo chưa?
- Đã sync lên tool/mock tool nào?
- Có thiếu trace hoặc câu hỏi mở không?
- Có thể chạy tiếp chưa?

Sync của slice này đi theo `docs/phase-2/sync-design-checkpoint.md`: Jira nhận
summary/comment checkpoint, Confluence nhận bản review đầy đủ hơn, còn Niko chỉ
đọc `last_sync` và `pending_checkpoint` từ state.

## Slice 3 - Design Đến Implementation Plan

```text
Input: approve design checkpoint
Output:
  implementation_plan_v{n}
  implementation_plan_review_v{n}
  tool_sync_report_v3
  status=waiting_implementation_plan_review
```

Slice này là checkpoint riêng của harness. Developer.plan chỉ ánh xạ approved
requirement/design/trace graph sang kế hoạch triển khai dự kiến. Validator.review_plan
chạy hybrid: rule hard gate kiểm coverage/traceability/executability/scope, LLM
review kiểm semantic gap giữa requirement/design/trace và plan. Nếu fail và còn
lượt theo `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`, Developer.repair_plan tạo
version kế tiếp. GitHub target repo chỉ được kiểm tra, chưa tạo branch/commit/PR.

## Slice 4 - Implementation Plan Đến Code/Test Report

```text
Input: approve implementation plan checkpoint
Output:
  code_change_report_v1
  test_report_v1
  requirement_design_code_trace_v1
  GitHub Draft PR
  status=code_review_ready
```

Slice này dùng target test repo GitHub tách khỏi `SDLC_Harness`. Jira vẫn là
issue nguồn nên không tạo GitHub Issue mới. GitHub dùng cho branch, commit,
Draft PR, code review và test/CI signal nếu có.

Lát mở đường có thể ghi rõ compile/test skipped để không đánh tráo trạng thái.
Nhưng điều kiện khóa Phase 4 là phải có code change thật, Draft PR thật và tối
thiểu một lớp validation có thể chạy được trong target repo hoặc được ghi rõ là
blocked nếu chưa đủ điều kiện kiểm chứng.

## Slice 5 - Sửa Tại Checkpoint

```text
Input: request_change(run_id, message)
Output:
  artifact_v2
  validator_report_v2
  sync_report_v2
  same or updated checkpoint
```

Quy tắc:

- Sửa requirement thì design/code/test phía sau có thể stale.
- Sửa design thì giữ requirement, code/test phía sau có thể stale.
- Sửa code review thì giữ requirement/design, chạy lại code/test.

## Cách Đồng Bộ Tool

Ban đầu đồng bộ tool có thể là mock/local:

```text
Jira mock        -> .runtime/runs/<run_id>/sync/<phase>-jira-comment.md
Confluence mock  -> .runtime/runs/<run_id>/sync/<phase>-confluence-page.md
Git mock         -> .runtime/runs/<run_id>/sync/<phase>-git-report.md
```

Với Phase 4, mock/local git report chỉ dùng để mở đường cho test tự động. Điều
kiện khóa Phase 4 là có target repo GitHub và Draft PR thật hoặc một quyết định
rõ ràng thay GitHub bằng GitLab.

## Không Làm Trong Giai Đoạn Đầu

- Không xử lý nhiều use case cùng lúc.
- Không làm lakehouse/KG ngay.
- Không mở đa ngôn ngữ lập trình.
- Không bắt buộc Jira/Confluence/GitHub thật ở slice đầu.
- Không để Niko lưu workflow state chính.

## Trước Khi Sang Code Phase 2

Mock sync chỉ dùng để mở đường cho thử nghiệm đầu. Trước khi triển khai code
Phase 2, cần có một adapter sync thật tối thiểu, Niko phải gọi được SDLC Harness
qua CLI, và tài liệu Phase 2 phải chốt rõ vòng Designer/Validator.

Điều kiện tối thiểu:

- SDLC Harness có CLI trả JSON ổn định.
- Niko có adapter gọi CLI và đọc status.
- Sync thuộc SDLC Harness; Niko chỉ đọc kết quả sync để báo người dùng.
- Có tool thật hoặc API thật được chứng minh. Theo lựa chọn hiện tại để khóa Phase 1, triển khai cả Jira comment và Confluence page.
- Runtime/provider cho workflow mới được cấu hình rõ, ưu tiên DeepSeek thay vì phụ thuộc FCC.
- `docs/phase-2/` mô tả rõ state/artifact/checkpoint và sync design checkpoint
  cho design loop trước khi viết code.

