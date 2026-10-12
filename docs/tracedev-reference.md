# TraceDev Reference

TraceDev là nguồn tham khảo chính cho phần execution graph tầng dưới. Trong hướng này, TraceDev không được xem là Agent Harness đầy đủ, mà là một graph chuyên cho requirement-to-code.

## Luồng TraceDev Tham Chiếu

```text
structured use case
  -> Requirement Refiner
  -> Designer
  -> Validator
  -> Developer
  -> Tester
  -> code/test/traceability artifacts
```

Validator có vai trò kiểm tra yêu cầu có được phản ánh vào design/code hay không. Khi thiếu trace, Validator yêu cầu agent trước đó sửa lại.

## Điểm Cần Học

- Chia role agent theo phase.
- Dùng use case có cấu trúc làm input cho execution graph.
- Có traceability graph để nối requirement, design, code, test.
- Có validate/repair loop thay vì chỉ sinh output một lần.
- Dùng dataset eTour/SMOS để đánh giá requirement-to-code.

## Điểm Cần Khác TraceDev

TraceDev gốc chạy gần như batch:

```text
input use case -> chạy một lượt -> đọc log/output
```

SDLC Harness cần thêm:

- workflow state truy vấn được;
- artifact version;
- checkpoint/human approval;
- tool sync theo phase;
- Niko conversation loop;
- request_change tại checkpoint;
- context động từ Jira/Confluence/GitHub/chat;
- khả năng resume/rerun đúng phase.

## Vai Trò Validator

Trong SDLC Harness, Validator không chỉ là một LLM reviewer. Validator nên được hiểu như traceability/QA lead:

```text
Validator
  = LLM review
  + rule checker
  + trace graph checker
  + compile/test runner
  + tool sync checker
```

Validator nên xuất hiện sau từng phase quan trọng:

- requirement packet đủ thông tin chưa;
- design có cover requirement không;
- code có nối được tới design/requirement không;
- test/report có phản ánh acceptance criteria không;
- artifact đã sync đúng tool chưa.

## Model Theo Vai Trò

Mỗi role có thể dùng model khác nhau về sau:

| Role | Model định hướng |
| --- | --- |
| Requirement Refiner | model rẻ/nhanh nếu input rõ |
| Designer | model mạnh hơn, reasoning tốt |
| Developer | model coding tốt |
| Tester | model coding/test hoặc tool runner |
| Validator | model ổn định, nghiêm, ưu tiên trace/độ đúng |

Giai đoạn đầu chưa cần tối ưu model. Ưu tiên làm rõ state, artifact và checkpoint trước.
