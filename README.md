# SDLC Harness

Repo này là nơi thiết kế và triển khai lớp **SDLC Harness** cho hướng Agent Harness hỗ trợ chuyển giao ngữ cảnh trong quy trình phát triển phần mềm.

Repo này tách khỏi `Niko_Agent` để giữ ranh giới rõ:

```text
Niko_Agent
  = conversational control harness
  = nhận yêu cầu, đọc trạng thái, hỏi người dùng, gọi workflow, trả kết quả

SDLC_Harness
  = workflow execution harness
  = chạy graph requirement -> design -> code -> test
  = lưu workflow state, artifact version, checkpoint, tool sync, traceability
```

Giai đoạn hiện tại repo chỉ chứa tài liệu. Phần triển khai code sẽ được thêm theo từng vertical slice: mỗi đoạn chạy được sẽ có state, artifact, checkpoint, đồng bộ tool/mock tool và Niko có thể đọc status ngay.

## Tài Liệu Chính

- `docs/architecture.md`: kiến trúc tổng quan và ranh giới với Niko.
- `docs/roadmap.md`: các phase triển khai.
- `docs/incremental-delivery-flow.md`: quy trình triển khai từng lát cắt và đồng bộ ngay sau mỗi đoạn.
- `docs/tracedev-reference.md`: cách học TraceDev như một execution graph, không copy nguyên xi.
- `docs/datasets-and-context.md`: cách dùng eTour/SMOS/iTrust và vai trò lớp context động.
- `docs/implementation-checklist.md`: checklist theo phase để tick khi triển khai.
- `docs/phase-1/README.md`: tài liệu triển khai chi tiết cho lát cắt use case -> requirement packet.

## Nguyên Tắc Hiện Tại

1. Bắt đầu từ một use case độc lập trước.
2. Java là ngôn ngữ thử nghiệm chính vì eTour/SMOS/iTrust và TraceDev đều thiên về Java.
3. Không xây full hệ thống âm thầm; mỗi slice phải chạy được và quan sát được.
4. Niko không lưu workflow state chính; Niko đọc state từ SDLC Harness để phản hồi người dùng.
5. Tool thật có thể đi sau; ban đầu được phép dùng fixture/mock adapter cho Jira, Confluence và GitHub/GitLab.

## Quyết Định Phase 0

- Phase 1 dùng CLI trả JSON để Niko gọi SDLC Harness.
- eTour UC1 được copy vào `fixtures/etour/UC1.txt` để repo chạy độc lập hơn.
- Runtime artifact và mock sync output nằm trong `.runtime/runs/<run_id>/`, không nằm trong `docs/`.
- Thử nghiệm đầu của Phase 1 được phép mock sync, nhưng trước Phase 2 cần có API/tool sync thật tối thiểu.
- Sync thuộc SDLC Harness; Niko đọc status/sync result để phản hồi người dùng.


## Ranh Giới Source Và Status

Khi người dùng bảo Niko thực hiện một use case, Niko không tự kéo dữ liệu từ fixture/Jira/dataset. Niko gọi SDLC Harness bằng `start-or-get-run`; SDLC Harness load/refine/validate/sync nếu cần và trả status cho Niko.
