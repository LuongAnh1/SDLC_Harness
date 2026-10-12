# Implementation Checklist

Checklist này dùng để tick tiến độ khi bắt đầu triển khai code. Mỗi nhóm công việc có mục đích riêng để tránh làm rộng mà không chứng minh được flow.

## Phase 0 - Nền Tài Liệu Và Contract

Mục đích: repo có định hướng rõ trước khi viết code.

Checklist chi tiết nằm ở `docs/phase-0/phase-0-checklist.md`. Phase 0 đã được review theo các quyết định: CLI JSON trước, eTour UC1 fixture nội bộ, runtime output trong `.runtime/`, Phase 1 mock sync nhưng trước Phase 2 cần sync API/tool thật tối thiểu.

- [x] Chốt README và AGENTS.md.
- [x] Chốt kiến trúc Niko <-> SDLC Harness.
- [x] Chốt API tối thiểu để Niko gọi.
- [x] Chốt artifact tối thiểu.
- [x] Chốt state machine ban đầu.
- [x] Chốt dataset/use case đầu tiên để thử.

## Phase 1 - Use Case Đến Requirement Packet

Mục đích: chứng minh một use case có thể thành artifact có trạng thái và checkpoint.

Checklist chi tiết nằm ở `docs/phase-1/phase-1-checklist.md`. Phase 1 hiện đã pass unit/smoke với fake LLM và đã có một lượt chạy thật DeepSeek; trước Phase 2 vẫn cần chốt sync API/tool thật tối thiểu.

- [x] Tạo `start-or-get-run` và loader do SDLC Harness sở hữu.
- [x] Tạo workflow run, `source_ref` và `run_id`.
- [x] Gọi Requirement Refiner adapter và sinh `requirement_packet_v1`.
- [x] Sinh `formal_use_case_v1` và `formal_check_report_v1` theo grammar TraceDev trước requirement packet.
- [x] Copy/adapt prompt scaffold cho Designer/Validator/Developer/Tester để chuẩn bị Phase 2/3.
- [x] Lưu state `waiting_requirement_approval`.
- [x] Tạo sync report mock.
- [x] Có lệnh/API `get-status(run_id)`.
- [x] Niko có thể đọc status qua CLI JSON; tích hợp trực tiếp trong `Niko_Agent` để sau.

## Phase 2 - Requirement Packet Đến Design

Mục đích: sau khi duyệt requirement, tạo thiết kế có trace và checkpoint.

- [ ] Có `approve_checkpoint(run_id)`.
- [ ] Đọc approved `requirement_packet_v1`, không refine requirement lại trong Phase 2.
- [ ] Sinh `design_artifact_v1`.
- [ ] Sinh trace requirement-design.
- [ ] Validator phát hiện thiếu/mâu thuẫn cơ bản.
- [ ] Sync Confluence/Jira mock.
- [ ] State chuyển sang `waiting_design_approval`.

## Phase 3 - Design Đến Code/Test Report

Mục đích: chạy được phần execution graph tới output có thể review.

- [ ] Sinh implementation artifact hoặc code change plan.
- [ ] Sinh test/validation report.
- [ ] Sinh trace requirement-design-code-test.
- [ ] Sync GitHub/GitLab mock hoặc local repo report.
- [ ] State chuyển sang `review_ready`.

## Phase 4 - Sửa Tại Checkpoint

Mục đích: người dùng có thể yêu cầu sửa và hệ thống rerun đúng đoạn.

- [ ] Có `request_change(run_id, message)`.
- [ ] Phân loại change target: requirement/design/code.
- [ ] Tạo artifact version mới.
- [ ] Đánh dấu artifact phía sau là stale nếu cần.
- [ ] Chạy lại Validator.
- [ ] Sync lại tool/mock tool.

## Phase 5 - Context Động

Mục đích: context ngoài checkpoint có thể làm workflow đổi trạng thái.

- [ ] Import Jira comment/update mới.
- [ ] Import Confluence update mới.
- [ ] Import PR review/update mới.
- [ ] Phân loại thay đổi.
- [ ] Invalidate phase phù hợp.
- [ ] Niko báo người dùng biết cần duyệt/chạy lại.

## Phase 6 - Nhiều Use Case Và Impact Analysis

Mục đích: mở rộng từ một use case độc lập sang quan hệ chéo.

- [ ] Lưu use case -> component/class links.
- [ ] Phát hiện shared code artifacts.
- [ ] Tạo impact graph tối thiểu.
- [ ] Khi một use case đổi, liệt kê use case có thể bị ảnh hưởng.
- [ ] Chạy regression/trace check cho nhóm liên quan.







