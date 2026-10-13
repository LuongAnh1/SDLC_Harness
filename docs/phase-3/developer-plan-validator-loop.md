# Developer Plan / Validator Review Loop

Phase 3 học TraceDev ở tầng execution, nhưng không copy luồng chạy thẳng của
TraceDev. TraceDev đưa `final_design` vào Developer để sinh code luôn; SDLC
Harness thêm một checkpoint ở giữa để human có thể xem kế hoạch trước khi repo
code bị thay đổi.

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

Trong SDLC Harness, Phase 3 chỉ làm đoạn handoff:

```text
approved requirement + approved design + requirement-design trace graph
  -> Developer.plan
  -> implementation_plan_v1
  -> Validator.review_plan
  -> waiting_implementation_plan_review
```

`Developer.act`, Tester và code trace thuộc Phase 4.

## Developer.plan

Developer tạo implementation plan, chưa sửa target repo. Plan cần đủ cấu trúc để
Validator bắt thiếu được, không chỉ là prose chung chung.

Nội dung tối thiểu:

- target repo, base branch và planned branch;
- file/class/method dự kiến tạo hoặc sửa;
- bảng mapping từ design element sang planned code target;
- test intent hoặc test command dự kiến;
- assumptions/open questions;
- out-of-scope.

Ví dụ mapping nên có dạng:

```markdown
| Design element | Planned code target | Reason | Test intent |
| --- | --- | --- | --- |
| DeleteCulturalHeritageService.deleteHeritage | src/main/java/.../DeleteCulturalHeritageService.java | main deletion behavior | delete success test |
```

## Validator.review_plan

Validator kiểm implementation plan bằng cách đối chiếu approved graph và plan.
Mục tiêu không phải chấm văn hay, mà là phát hiện kế hoạch thiếu hoặc bịa scope.

Nhóm kiểm tra:

- Coverage: class/method/message quan trọng trong design đã có planned code
  target chưa?
- Traceability: requirement-design trace có được phản ánh vào plan không?
- Executability: repo, branch, path và test intent có đủ rõ để Phase 4 chạy
  không?
- Scope control: plan có thêm chức năng ngoài requirement/design không?

Nếu fail, workflow có thể tạo feedback để Developer.plan rerun trong giới hạn
cấu hình hoặc chuyển `blocked`. Nếu vấn đề nằm ở design/requirement gốc, Phase 3
không tự sửa graph mà phải quay về checkpoint phù hợp.

## Điều Kiện Hoàn Thành Phase 3

Phase 3 hoàn thành khi có:

- `implementation_plan_v1.md`;
- plan review report hoặc quality report;
- sync plan lên Confluence và Jira comment;
- state `waiting_implementation_plan_review`;
- Niko đọc được summary và checkpoint để hỏi human duyệt.

Phase 3 không cần có branch, commit, test report, code trace hoặc Draft PR. Các
phần đó là điều kiện của Phase 4.

