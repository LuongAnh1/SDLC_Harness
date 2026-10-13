# Phase 0 Checklist

Checklist này dùng để review trước khi chuyển sang Phase 1. Mỗi nhóm có mục đích riêng để tránh viết code khi contract còn mơ hồ.

## 1. Ranh Giới Kiến Trúc

Mục đích: tránh nhập nhằng giữa chatbot Niko và workflow SDLC.

- [x] Niko được xác định là conversational control harness.
- [x] SDLC Harness được xác định là workflow execution harness.
- [x] Niko không lưu workflow state chính.
- [x] SDLC Harness là nguồn sự thật cho state, artifact, checkpoint và sync record.
- [x] Luồng `User -> Niko -> SDLC Harness -> Tool/Artifact -> Niko -> User` đã được mô tả rõ.

## 2. Contract Niko <-> Harness

Mục đích: Phase 1 có thể triển khai mock API/CLI mà không phải đoán hành vi.

- [x] `start_run(input_ref)` được mô tả rõ.
- [x] `get_status(run_id)` được mô tả rõ.
- [x] `approve_checkpoint(run_id)` được mô tả rõ.
- [x] `request_change(run_id, message)` được mô tả rõ ở mức Phase 1.
- [x] `resume_run(run_id)` và `cancel_run(run_id)` được giữ trong contract.
- [x] `allowed_next_actions` được xác định là dữ liệu bắt buộc trong status.
- [x] Các lỗi hành vi tối thiểu đã được liệt kê.

## 3. State Machine

Mục đích: workflow có trạng thái rõ để Niko đọc và phản hồi.

- [x] State `created` được định nghĩa.
- [x] State `requirement_drafting` được định nghĩa.
- [x] State `waiting_requirement_approval` được định nghĩa.
- [x] State `design_drafting` được định nghĩa.
- [x] State `waiting_design_approval` được định nghĩa.
- [x] State `coding` được định nghĩa.
- [x] State `review_ready` được định nghĩa ở mức tổng quát; Phase 3 cụ thể hóa thành `code_review_ready`.
- [x] State `done`, `blocked`, `cancelled` được định nghĩa.

## 4. Artifact Và Versioning

Mục đích: mỗi phase tạo artifact có thể lưu, diff, sync và trace.

- [x] `use_case_packet` được định nghĩa.
- [x] `requirement_packet` được định nghĩa.
- [x] `design_artifact` được định nghĩa.
- [x] `trace_graph` được định nghĩa.
- [x] `implementation_artifact` được định nghĩa.
- [x] `test_report` được định nghĩa.
- [x] `tool_sync_report` được định nghĩa.
- [x] Quy tắc version `artifact_v1`, `artifact_v2` đã được chốt.
- [x] Quy tắc artifact stale khi sửa requirement/design/code đã được chốt ở mức khái niệm.

## 5. Dataset Phase 1

Mục đích: chọn đầu vào hẹp để Phase 1 triển khai được ngay.

- [x] eTour được chọn làm dataset mặc định.
- [x] UC1 được chọn làm use case đầu tiên.
- [x] Java được chọn làm target language mặc định.
- [x] Chưa mở SMOS/iTrust trong Phase 1.
- [x] Chưa mở đa ngôn ngữ trong Phase 1.

## 6. Tiêu Chí Sang Phase 1

Mục đích: biết khi nào Phase 0 thật sự xong.

- [x] Người đọc docs hiểu Phase 1 cần tạo flow nào.
- [x] Contract đủ để tạo mock command/API.
- [x] State đủ để trả status cho Niko.
- [x] Artifact đủ để sinh `requirement_packet_v1`.
- [x] Dataset đầu tiên đủ rõ để viết loader.
- [x] Các docs tổng đã trỏ về folder `docs/phase-0/`.

