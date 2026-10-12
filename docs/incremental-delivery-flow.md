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
  sync_report_v2
  status=waiting_design_approval
```

Niko cần trả lời được:

- Thiết kế đã tạo chưa?
- Đã sync lên tool/mock tool nào?
- Có thiếu trace hoặc câu hỏi mở không?
- Có thể chạy tiếp chưa?

## Slice 3 - Design Đến Code/Test Report

```text
Input: approve design checkpoint
Output:
  implementation_artifact_v1
  test_report_v1
  trace_graph_v2(requirement-design-code-test)
  status=review_ready
```

Ban đầu có thể chưa cần code compile thật. Nhưng report phải nói rõ mức độ đã làm: generated code, compile skipped, test skipped hoặc test passed.

## Slice 4 - Sửa Tại Checkpoint

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
Jira mock        -> .runtime/runs/<run_id>/jira-comment.md
Confluence mock  -> .runtime/runs/<run_id>/confluence-page.md
Git mock         -> .runtime/runs/<run_id>/git-report.md
```

Khi flow ổn mới thay bằng API thật.

## Không Làm Trong Giai Đoạn Đầu

- Không xử lý nhiều use case cùng lúc.
- Không làm lakehouse/KG ngay.
- Không mở đa ngôn ngữ lập trình.
- Không bắt buộc Jira/Confluence/GitHub thật ở slice đầu.
- Không để Niko lưu workflow state chính.

## Trước Khi Sang Phase 2

Mock sync chỉ dùng để mở đường cho thử nghiệm đầu. Trước khi chuyển sang Phase 2, cần có một adapter sync thật tối thiểu và Niko phải gọi được SDLC Harness qua CLI.

Điều kiện tối thiểu:

- SDLC Harness có CLI trả JSON ổn định.
- Niko có adapter gọi CLI và đọc status.
- Sync thuộc SDLC Harness; Niko chỉ đọc kết quả sync để báo người dùng.
- Có tool thật hoặc API thật được chứng minh. Theo lựa chọn hiện tại để khóa Phase 1, triển khai cả Jira comment và Confluence page.
- Runtime/provider cho workflow mới được cấu hình rõ, ưu tiên DeepSeek thay vì phụ thuộc FCC.

