# Vertical-slice Trace Graph

## Nói Ngắn Gọn

`Vertical-slice trace graph` là đồ thị trả lời câu hỏi:

> Một yêu cầu của người dùng đã đi qua những phần nào của hệ thống, được hiện
> thực ở đâu và được kiểm chứng bằng test nào?

Tên ngắn “vertical graph” trong bộ tài liệu này đều chỉ đồ thị trên. Nó không
phải một phase mới và cũng không thay thế Requirement, Design, Code hay Test.
Nó nối các artifact của bốn tầng đó thành các lát chức năng có thể kiểm tra.

## Từ Đồ Thị Hiện Tại Sang Vertical Slice

Cách nhìn hiện tại gần với ba nhóm nút:

```text
Requirement A
  -> Design B (class/sequence)
      -> Code C (class/symbol/file)
```

Cách này hữu ích nhưng dễ bỏ sót một đường đi full-stack. Ví dụ class backend
có thể đúng, nhưng UI không gọi endpoint, endpoint không dùng đúng rule hoặc
Prisma không ghi đúng dữ liệu.

Vertical slice giữ nguyên liên kết A -> B -> C, nhưng làm rõ B và C thuộc lớp
nào của hành vi:

```text
Requirement / Scenario
  -> UI interaction
  -> API contract
  -> business rule
  -> data operation
  -> response / UI state
  -> test and execution evidence
```

Có thể hình dung bằng một bảng hai chiều:

| Hàng theo vòng đời | Cột theo lớp chức năng |
| --- | --- |
| Requirement | Scenario, acceptance criterion |
| Design | UI, API, rule, data, interaction |
| Code | React component, Fastify route, service/function, Prisma model/query |
| Verification | Unit, integration, acceptance/browser evidence |

UI, API và DB là **cột**. Requirement, Design, Code và Verification là **hàng**.
Vì vậy vertical graph không bỏ class/sequence; class và sequence chỉ trở thành
hai loại projection của design graph, được dùng khi use case thực sự cần.

## Ví Dụ Nhỏ Từ eTour

Use case: quản trị viên xóa một di sản văn hóa.

Luồng thành công có thể là:

```text
UC-DELETE-HERITAGE
  -> SCN-SUCCESS
  -> AC-CONFIRM-DELETE
  -> UI DeleteButton + ConfirmationDialog
  -> API DELETE /api/heritages/:id
  -> RULE chỉ Admin được xóa, bản ghi phải tồn tại
  -> SERVICE DeleteHeritage
  -> DATA Prisma Heritage.delete
  -> DB heritage row removed
  -> API 204
  -> UI remove row + show success message
  -> TEST browser delete flow
  -> TEST API integration
  -> TEST service authorization/not-found
```

Đây không phải một đường thẳng duy nhất. Cùng use case có thể rẽ nhánh:

```text
ConfirmationDialog
  |-> confirm -> API -> rule -> DB -> success UI
  |-> cancel  -> close dialog -> không gọi API -> DB không đổi

API / rule
  |-> forbidden -> 403 -> UI báo không đủ quyền
  |-> not found -> 404 -> UI refresh hoặc báo bản ghi không còn tồn tại
  |-> reference conflict -> 409 -> UI giải thích vì sao chưa thể xóa
```

Mỗi scenario là một subgraph có hướng. Các scenario được phép dùng chung nút
ổn định như màn hình, endpoint hoặc service, nhưng giữ acceptance và test riêng
để Validator biết nhánh nào đã được chứng minh.

## Use Case Không Phải Lúc Nào Cũng Có Đủ UI, API Và DB

Requirement phải chỉ ra mức kỳ vọng của từng surface, nhưng không đoán file cụ
thể. Mỗi surface có một trong ba trạng thái:

- `required`: hành vi phải xuất hiện ở lớp này;
- `not_applicable`: lớp này không liên quan và phải có lý do;
- `clarification_required`: chưa đủ thông tin để quyết định.

Ví dụ một use case chỉ sửa validation backend có thể đánh dấu UI là
`not_applicable`. Ngược lại, “người dùng xóa di sản trên màn hình quản trị” làm
UI, API, rule và data đều là `required`. Tuy nhiên `required` không đồng nghĩa
chắc chắn phải tạo file mới: Designer phải khảo sát repo để kết luận
`reuse|modify|add|no_change`.

## Designer Phải Làm AS-IS Trước TO-BE

Sau khi Requirement được duyệt, Harness tạo hoặc khóa workspace read-only cho
run và phát hành `repo_landscape` từ manifest, cấu trúc repo, route, schema,
test và CodeGraph nếu có.

Repo Discovery ở đây **không tự đoán kiến trúc dự án**. Profile hiện hành đã khóa
TypeScript monolith với React, Fastify, Prisma/PostgreSQL, npm và các root cần
quét. Discovery chỉ tìm trong boundary đó xem UI/API/rule/data/test nào đã có,
cái nào khớp requirement và repo có mâu thuẫn với profile hay không.

“Read-only” ở đây là quyền của Requirement/Designer, không nhất thiết là khóa
filesystem vĩnh viễn. Harness đóng băng **base snapshot identity** để design có
bằng chứng ổn định; tới Developer, workflow mới cấp write scope trong worktree.
Sau mutation, graph/evidence phải bind phiên bản và hash mới thay vì giả vờ base
snapshot chưa đổi.

Designer đi theo ba bước:

```text
AS-IS: hệ thống hiện có gì?
-> GAP: scenario còn thiếu hoặc mâu thuẫn ở đâu?
-> TO-BE: cần reuse, modify hay add phần nào?
```

Như vậy Requirement mô tả hành vi cần có, còn Designer mới quyết định ảnh hưởng
kỹ thuật dựa trên bằng chứng code hiện hành. Designer không được sửa production
code; nó chỉ đề xuất design delta và trace edges.

## Không Nạp Cả Đồ Thị Vào Một Prompt

Đồ thị sẽ lớn hơn cách A -> B -> C cũ, nhưng không có nghĩa mỗi lượt LLM phải
đọc toàn bộ graph.

- Python lưu canonical graph, kiểm schema, ID, cạnh treo, coverage và tính nhất
  quán toàn cục.
- Mỗi role chỉ nhận subgraph theo use case, scenario, finding hoặc component đang
  xử lý.
- Tool query trả bounded neighborhood và evidence reference, không trả toàn repo.
- Các delta nhỏ được validate rồi merge vào canonical graph.
- Sau khi merge, deterministic global checks bảo đảm không bỏ sót scenario hoặc
  làm gãy liên kết đã pass.

CodeGraph và trace graph cũng không trùng nhau:

- CodeGraph trả lời “code đang gọi và phụ thuộc nhau thế nào?”.
- Trace graph trả lời “vì sao code này tồn tại và nó chứng minh requirement nào?”.

Trace graph chỉ lưu symbol/file anchors và evidence cần thiết; nó không sao chép
toàn bộ call graph hay từng dòng mã nguồn.

## Loại Nút Và Cạnh Tối Thiểu

Các tên chính thức sẽ được version khi triển khai, nhưng mô hình tối thiểu gồm:

- Requirement: use case, scenario, acceptance criterion, constraint.
- Design: UI interaction/state, API contract, business rule/service, data entity/
  operation, interaction step.
- Implementation: file, symbol, route, schema/model, migration.
- Verification: test scenario, verification stage, execution evidence.

Cạnh tối thiểu gồm:

- `contains`: use case chứa scenario hoặc scenario chứa acceptance criterion;
- `realized_by`: requirement/design được hiện thực bởi design/code;
- `calls`: UI gọi API, API gọi service, service gọi repository/data operation;
- `reads|writes`: rule/service đọc hoặc ghi data;
- `returns`: response quay lại API/UI state;
- `verified_by`: claim hoặc nhánh được test/evidence chứng minh;
- `depends_on`: dependency kỹ thuật hoặc thứ tự thực hiện.

## Projection Design Theo Use Case

Canonical source là typed design graph. Các tài liệu quen thuộc là projection:

- component view khi cần thấy FE/BE/DB boundary;
- API/data contract view cho request, response, error và persistence;
- sequence view cho luồng tương tác có thứ tự;
- class/module view khi cấu trúc type/class/module quan trọng;
- state view khi hành vi phụ thuộc trạng thái;
- data view khi có entity, relation hoặc migration.

Không bắt buộc mọi use case phải có mọi view. Validator chỉ chấp nhận view vắng
mặt khi Requirement/Designer ghi `not_applicable` kèm lý do hợp lệ.

## Điều Kiện Hoàn Thành Một Vertical Slice

Một slice chỉ được coi là trace đủ khi:

1. mọi scenario bắt buộc có acceptance criterion;
2. mọi surface `required` có design node hoặc quyết định reuse/no-change có evidence;
3. design node cần triển khai có code anchor hoặc implementation work item;
4. mỗi nhánh thành công/lỗi quan trọng có test intent và execution evidence tương ứng;
5. không có cạnh treo, node mồ côi hoặc evidence đã hết hiệu lực theo current hash;
6. phần `not_applicable` và unresolved assumption đều được giải thích rõ.

Đích cuối cùng không phải “graph càng nhiều nút càng tốt”, mà là đủ để nhìn một
scenario và lần theo được từ nhu cầu người dùng đến hành vi chạy thật.

Các điều kiện trên được áp dụng theo phase, không ép Phase 2 phải có code/test
chưa được tạo:

- Requirement gate kiểm scenario, acceptance và trạng thái surface.
- Design gate kiểm Requirement -> Design; phần chưa code trỏ tới planned work.
- Implementation gate thay planned work bằng code anchors và verification intent.
- QA/final gate mới yêu cầu execution evidence cho các nhánh bắt buộc.

Nhờ vậy graph lớn dần theo workflow nhưng không “fail sớm” chỉ vì artifact của
phase sau chưa tồn tại.
