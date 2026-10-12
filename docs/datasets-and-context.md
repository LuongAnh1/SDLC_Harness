# Dataset Và Context Động

## Vai Trò Dataset

Các dataset eTour, SMOS và iTrust hữu ích cho tầng requirement-to-code vì chúng có use case/requirement và trace links tới code Java.

Nhưng chúng chủ yếu là dataset tĩnh:

```text
use case / requirement -> source code artifacts
```

Chúng không có đầy đủ:

- comment/thảo luận theo thời gian;
- trạng thái workflow;
- change request giữa chừng;
- human approval checkpoint;
- commit/diff theo từng thay đổi;
- quan hệ nghiệp vụ rõ giữa các use case;
- lịch sử quyết định thiết kế.

## Cách Dùng Cho MVP

MVP nên dùng một use case độc lập trước:

```text
UC1
  -> Jira issue hoặc Jira mock
  -> requirement_packet_v1
  -> design_artifact_v1
  -> implementation_artifact_v1
  -> test_report_v1
  -> trace_graph_v1
```

Mục tiêu không phải chứng minh nhiều use case liên kết ngay, mà chứng minh một use case có thể đi qua workflow có checkpoint và sync tool.

## Vì Sao Cần Context Storage

Vì dataset tĩnh thiếu ngữ cảnh sống, SDLC Harness cần một lớp context storage riêng để lưu:

```text
UC1 -> Jira issue
UC1 comments -> bổ sung/nghi vấn/thay đổi
UC1 linked to UC5 -> quan hệ phụ thuộc hoặc chung component
UC1 touches ClassA -> liên kết code
UC1 design decision -> artifact đã duyệt
UC1 status history -> đang ở phase nào
UC1 trace graph -> requirement-design-code-test
```

Ban đầu context storage có thể rất đơn giản:

- SQLite cho workflow state và metadata;
- Markdown/JSON cho artifact version;
- local files cho mock sync records.

Sau khi workflow ổn, mới xem xét lakehouse/KG để phân tích dài hạn.

## Ngôn Ngữ Lập Trình

Java là target language ban đầu vì:

- TraceDev đang thiên về Java;
- eTour/SMOS/iTrust đều có trace links tới Java artifacts;
- tester/validator/build flow dễ cố định hơn trong MVP.

Đa ngôn ngữ là hướng mở rộng sau. Nếu mở rộng, cần abstraction kiểu:

```text
LanguageProfile
  source_layout
  test_layout
  developer_prompt
  tester_prompt
  symbol_extractor
  build_command
  test_command
  error_parser
```

Nhưng không nên đưa abstraction này vào phase đầu nếu chưa cần.
