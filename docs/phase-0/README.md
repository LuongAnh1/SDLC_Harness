# Phase 0 - Nền Tài Liệu Và Contract

Phase 0 là bước khóa nền trước khi viết code cho SDLC Harness. Mục tiêu không phải tạo graph chạy thật ngay, mà làm rõ những quyết định tối thiểu để Phase 1 có thể triển khai một vertical slice mà không phải đoán lại kiến trúc.

## Mục Tiêu

Phase 0 cần chốt các điểm sau:

- Ranh giới giữa `Niko_Agent` và `SDLC_Harness`.
- Contract tối thiểu để Niko gọi Harness.
- State machine ban đầu cho một workflow run.
- Artifact tối thiểu mà mỗi phase tạo ra.
- Dataset/use case đầu tiên để thử.
- Tiêu chí để biết Phase 0 đã đủ điều kiện chuyển sang Phase 1.

## Kết Quả Bắt Buộc

Sau Phase 0, người triển khai Phase 1 phải biết rõ:

```text
Niko gọi gì?
Harness lưu gì?
Run đi qua các trạng thái nào?
Mỗi trạng thái tạo artifact nào?
Dataset đầu tiên lấy từ đâu?
Khi nào một slice được xem là hoàn thành?
```

## Tài Liệu Trong Folder Này

- `niko-harness-contract.md`: contract hội thoại/điều phối giữa Niko và SDLC Harness.
- `state-and-artifacts.md`: state machine ban đầu, artifact tối thiểu và quan hệ phase -> artifact.
- `dataset-selection.md`: chọn dataset/use case đầu tiên, lý do chọn Java/eTour UC1.
- `phase-0-checklist.md`: checklist nghiệm thu Phase 0.
- `niko-integration-before-phase-2.md`: các thay đổi cần chuẩn bị ở Niko và tool/API thật trước khi sang Phase 2.

## Quyết Định Chốt Cho Phase 0

- `Niko_Agent` là conversational control harness: nhận tin, hiểu intent, đọc status, hỏi người dùng, gọi action.
- `SDLC_Harness` là workflow execution harness: giữ state, artifact version, checkpoint, tool sync và traceability.
- Giao tiếp ban đầu dùng CLI trả JSON để dễ test tay và dễ cho Niko gọi bằng subprocess; local package/HTTP API để sau khi flow ổn.
- Dataset mặc định cho lát cắt đầu là eTour UC1, đã copy tối thiểu vào `fixtures/etour/UC1.txt`.
- Target language mặc định là Java.
- Tool thật chưa bắt buộc ở thử nghiệm đầu của Phase 1; được phép dùng mock/local sync report. Tuy nhiên trước khi sang Phase 2 cần có sync API thật tối thiểu và Niko adapter tương ứng.

## Điều Kiện Chuyển Sang Phase 1

Phase 0 chỉ được xem là sẵn sàng khi:

- Contract Niko <-> Harness đủ rõ để mock call qua CLI JSON.
- State machine có trạng thái chờ duyệt requirement.
- Artifact `requirement_packet` được định nghĩa đủ để sinh bản v1.
- Dataset/use case đầu tiên đã được chọn và có fixture nội bộ.
- Checklist Phase 0 được review và tick những phần đã thống nhất.

