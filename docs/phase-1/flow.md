# Phase 1 Flow

Tài liệu này mô tả luồng xử lý bắt buộc của Phase 1. Implementer không tự đổi thứ tự bước nếu không cập nhật lại docs.

## Command `start-or-get-run`

Input mặc định:

```text
source_type=dataset_use_case
dataset=eTour
use_case=UC1
requested_by=<id>
```

Luồng xử lý:

```text
1. Validate input
2. Tạo source_ref từ dataset/use_case: dataset_use_case:eTour:UC1
3. Kiểm tra active run theo source_ref
4. Nếu active run tồn tại và chưa done/cancelled: trả status run hiện tại
5. Nếu chưa có active run: load fixture `fixtures/etour/UC1.txt`
6. Tạo `run_id`
7. Tạo folder `.runtime/runs/<run_id>/`
8. Ghi state ban đầu: `requirement_drafting`
9. Tạo `use_case_packet_v1.md`
10. Gọi Requirement Refiner LLM để tạo nội dung requirement đã chuẩn hóa
11. Chạy Requirement Validator tối thiểu
12. Tạo `requirement_packet_v1.md`
13. Tạo `trace_graph_v1.json`
14. Mock sync Jira/Confluence
15. Ghi `tool_sync_report_v1.json`
16. Cập nhật active run index `.runtime/index/active_runs.json`
17. Cập nhật state: `waiting_requirement_approval`
18. Trả JSON status
```

## Command `get-status`

Input:

```text
run_id=<run_id>
```

Luồng xử lý:

```text
1. Tìm `.runtime/runs/<run_id>/state.json`
2. Nếu không có, trả `run_not_found`
3. Đọc latest artifact refs
4. Đọc latest sync report
5. Trả JSON status cho Niko
```

## State Cuối Của Phase 1

Sau `start-or-get-run` thành công, state cuối phải là:

```text
waiting_requirement_approval
```

`allowed_next_actions` phải gồm:

```text
approve_checkpoint
request_change
cancel_run
get_status
```

## Summary Cho Niko

`summary_for_user` cần đủ ngắn để Niko có thể báo lại ngay:

```text
Đã chuẩn hóa eTour UC1 thành requirement packet v1 bằng Requirement Refiner và đồng bộ mock Jira/Confluence. Run đang chờ anh duyệt requirement trước khi chuyển sang design.
```

Niko có thể chỉnh giọng văn sau này, nhưng Phase 1 CLI phải trả nội dung đủ thông tin.

## Niko Không Kéo Use Case Trực Tiếp

Khi người dùng bảo Niko thực hiện một use case, Niko không tự đọc fixture/Jira/dataset. Niko gọi SDLC Harness:

```text
Niko -> start-or-get-run(source_ref)
SDLC Harness -> load/refine/validate/sync nếu cần
SDLC Harness -> trả status
Niko -> phản hồi người dùng
```

Nếu use case đã có active run, SDLC Harness trả run hiện tại. Niko không tự quyết định load lại hay refine lại.

## Failure Flow Tối Thiểu

| Trường hợp | Error code |
| --- | --- |
| Dataset không hỗ trợ | `source_not_supported` |
| Use case không tồn tại | `source_not_found` |
| Không ghi được runtime folder | `runtime_write_failed` |
| Requirement Refiner lỗi | `requirement_refiner_failed` |
| Requirement Validator lỗi | `requirement_validator_failed` |
| Run id không tồn tại khi get-status | `run_not_found` |
| State file lỗi JSON | `state_corrupted` |

Khi lỗi, CLI vẫn trả JSON với `ok=false`.
