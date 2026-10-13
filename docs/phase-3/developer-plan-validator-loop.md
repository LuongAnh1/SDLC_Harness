# Developer Plan / Validator Review Loop

## Smart Developer V1 Và CodeGraph

Từ Slice 3.3, Developer.plan không còn chỉ dựa vào rule mapping từ
class/sequence graph. Workflow yêu cầu target repo có `.codegraph/`, đọc repo
context bằng CodeGraph, rồi đưa evidence đó vào Developer LLM để sinh plan có:

- repo evidence;
- implementation steps;
- proposed files;
- design-to-code mapping đã được làm giàu bằng context repo;
- test intents cụ thể hơn.

`approve-checkpoint` không tự chạy `codegraph init`. Người dùng phải chạy bước
setup riêng `prepare-target-repo --init-codegraph`. Đây là ranh giới để Phase 3
không âm thầm mutate target repo.

Lưu ý bảo mật V1: context CodeGraph/source snippets đang được phép gửi sang
DeepSeek cho demo. Về sau cần lọc/redact hoặc chuyển sang local model nếu target
repo có dữ liệu nhạy cảm.

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
  -> Validator.review_plan hybrid
  -> nếu fail và còn lượt repair: Developer.repair_plan -> implementation_plan_v2/v3
  -> waiting_implementation_plan_review
```

`Developer.act`, Tester và code trace thuộc Phase 4.

## Developer.plan

Developer tạo implementation plan, chưa sửa target repo. Plan cần đủ cấu trúc để
Validator bắt thiếu được, không chỉ là prose chung chung.

Implementation plan có hai artifact song song:

- `implementation_plan_v1.md`: bản human đọc/review.
- `implementation_plan_structured_v1.json`: sidecar máy đọc được, chứa mapping
  và test intent để Validator/Niko không phải parse Markdown.

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

Trong V1 hiện tại, `Validator.review_plan` là hybrid:

1. `review_rules` chạy trước như hard gate. Nó kiểm mapping/graph bằng code và
   không cho LLM hợp thức hóa plan thiếu coverage hoặc bịa design element.
2. `review_llm` chỉ chạy khi rule đã pass. Nó đọc requirement packet, design
   artifact, requirement-design trace, class/sequence graph và structured plan để
   bắt semantic gap/chất lượng test intent.
3. Nếu review fail và còn lượt theo `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`,
   `Developer.repair_plan` tạo version kế tiếp. Nếu hết lượt, workflow chuyển
   `blocked` và giữ report mới nhất.

Nếu vấn đề nằm ở design/requirement gốc, Phase 3 không tự sửa graph mà phải quay
về checkpoint phù hợp.

## Điều Kiện Hoàn Thành Phase 3

Phase 3 hoàn thành khi có:

- `implementation_plan_v1.md`;
- plan review report hoặc quality report;
- sync plan lên Confluence và Jira comment;
- state `waiting_implementation_plan_review`;
- Niko đọc được summary và checkpoint để hỏi human duyệt.

Phase 3 không cần có branch, commit, test report, code trace hoặc Draft PR. Các
phần đó là điều kiện của Phase 4.

