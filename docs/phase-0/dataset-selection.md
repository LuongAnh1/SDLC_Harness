# Dataset Selection Cho Phase 0-1

## Quyết Định Mặc Định

Phase đầu dùng:

```text
Dataset: eTour
Use case: UC1
Fixture nội bộ: `fixtures/etour/UC1.txt`
Target language: Java
```

Lý do:

- eTour là dataset TraceDev đã dùng, phù hợp requirement-to-code.
- UC1 đã từng được chạy thử trong TraceDev local nên rủi ro thấp hơn.
- Java khớp với TraceDev prompt, tester, validator và gold trace links.
- Một use case độc lập đủ để chứng minh vertical slice đầu.

## Vì Sao Chưa Dùng Nhiều Dataset Ngay

SMOS và iTrust có giá trị, nhưng đưa nhiều dataset vào ngay sẽ làm Phase 1 bị rộng:

- format use case khác nhau;
- gold links khác nhau;
- repo/code artifacts khác nhau;
- test/validator có thể phải điều chỉnh.

Phase 1 chỉ cần chứng minh:

```text
load UC1 -> requirement_packet_v1 -> checkpoint -> sync mock -> status cho Niko đọc
```

Sau khi flow ổn mới thêm SMOS/iTrust.

## Giới Hạn Của eTour/SMOS/iTrust

Các dataset này chủ yếu là dataset tĩnh:

```text
use case / requirement -> Java code artifacts
```

Chúng không có đầy đủ:

- Jira comments theo thời gian;
- change request giữa chừng;
- human checkpoint;
- trạng thái workflow;
- commit/diff thực tế;
- quan hệ nghiệp vụ rõ giữa nhiều use case.

Vì vậy SDLC Harness cần context storage riêng để bù phần động này.

## Cách Biến Use Case Thành Context Work Item

Với Phase 1, UC1 có thể được ánh xạ thành work item mock:

```text
work_item_id: ETOUR-UC1
source_dataset: eTour
source_use_case: UC1
summary: lấy từ use case name
description: mô tả + flow chính
acceptance_criteria: exit condition / expected result
comments: rỗng hoặc mock comment tối thiểu
status: intake
```

Jira thật chưa bắt buộc. Mock work item đủ để kiểm tra contract và artifact.

## Khi Nào Mở Rộng Dataset

Sau Phase 1-2, có thể thêm:

- SMOS: kiểm tra format use case khác.
- iTrust: kiểm tra domain healthcare và trace links khác.
- nhiều eTour UC: chuẩn bị cho impact analysis.

Không mở đa ngôn ngữ cho tới khi Java flow đã ổn.

