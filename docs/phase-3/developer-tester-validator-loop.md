# Developer / Tester / Validator Loop

Phase 3 học từ TraceDev ở tầng execution, nhưng thêm state, checkpoint và tool
sync để phù hợp với Agent Harness.

## Ánh Xạ Từ TraceDev

TraceDev đi theo mạch:

```text
formal use case
  -> Designer
  -> Validator
  -> Developer
  -> Tester
  -> code/test artifacts
```

Trong SDLC Harness, Phase 1/2 đã tạo requirement/design đã duyệt. Phase 3 chỉ
bắt đầu khi design checkpoint pass:

```text
approved requirement + approved design + trace graph
  -> Developer
  -> Tester
  -> Validator traceability
  -> GitHub Draft PR
  -> code review checkpoint
```

## Developer Loop

Developer có hai bước:

1. `plan`: tạo `implementation_plan_v1.md`.
2. `act`: thao tác target repo và tạo `code_change_report_v1.md`.

Slice 1 mới triển khai bước `plan` và dừng ở checkpoint
`waiting_implementation_plan_review`. Bước `act`, Tester và Validator code-test
sẽ được triển khai sau khi kế hoạch implement đã có thể review được.

Implementation plan cần nói rõ:

- target repo/branch;
- module/file dự kiến sửa;
- mapping từ design element sang code element;
- test dự kiến chạy;
- rủi ro hoặc giả định.

Code change report cần nói rõ:

- branch, commit hash, PR URL nếu có;
- file đã tạo/sửa;
- lý do từng nhóm thay đổi;
- phần requirement/design nào được cover.

## Tester Loop

Tester tạo `test_report_v1.md`. Report phải phân biệt rõ:

```text
passed
failed
skipped
```

Nếu test bị skipped vì target repo chưa có build/test thật, report vẫn được ghi
nhưng Phase 3 checklist không được tick mục "test thật pass". Mục tiêu là không
đánh tráo "đã tạo report" với "đã kiểm chứng code".

## Validator Loop

Validator tạo `requirement_design_code_trace_v1.json`, tối thiểu nối:

```text
requirement id
  -> design class/method/message
  -> source file/class/function
  -> test file/case nếu có
```

Nếu missing link:

- nếu thiếu do code chưa cover, Developer có thể repair trong giới hạn cấu hình;
- nếu thiếu do requirement/design mơ hồ, workflow `blocked` để quay lại checkpoint
  phù hợp ở Phase 4/5;
- nếu thiếu do test chưa có, report phải ghi rõ test coverage missing.

## Điều Kiện Hoàn Thành Phase 3

Phase 3 được xem là điểm hoàn thành execution graph cơ bản từ requirement tới
code. Vì vậy Phase 3 phải có đủ vai trò tương đương TraceDev trong phạm vi một
use case:

- Requirement Refiner đã chạy ở Phase 1;
- Designer + Validator đã chạy ở Phase 2;
- Developer + Tester + Validator phải chạy ở Phase 3;
- approved design đi vào target repo và tạo code change thật;
- có branch/commit/Draft PR;
- có report code/test/trace dễ review;
- Niko đọc được trạng thái và link PR;
- lỗi được biểu diễn bằng `blocked`/`last_error`, không chỉ nằm trong log.

Những phần chưa thuộc Phase 3 là cải tiến harness sau khi đã có end-to-end:
request change tại checkpoint, context động từ tool, nhiều use case và impact
analysis.

