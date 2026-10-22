# Role Capability Contracts

## Contract Chung

Mỗi role profile phải khai báo:

- immutable inputs và artifact digests;
- tool IDs được phép gọi;
- read scope, write scope và protected paths;
- action/call/token/time budget;
- completion requirements;
- allowed handoff targets;
- failure classes mà role được phép kết luận.

Model không được tự thêm tool, path hay verification command. Tool ID được map
sang adapter code-reviewed; mọi kết quả đều có evidence reference.

## Capability Matrix

| Capability | Requirement | Designer | Planner | Developer | QA | Validator |
| --- | --- | --- | --- | --- | --- | --- |
| Đọc approved artifacts | Có | Có | Có | Có | Có | Có |
| Đọc source Jira/docs | Có | Chỉ evidence đã duyệt | Không | Không | Không | Có, read-only |
| Đọc/search repo | Không mặc định; chỉ technical evidence đã cấp | Có, read-only | Có | Có | Có | Có |
| Query CodeGraph/AST | Không mặc định | Read-only | Read-only | Read-only | Read-only | Read-only |
| Sửa requirement/design | Đề xuất requirement delta | Đề xuất design delta | Không | Không | Không | Không |
| Sửa production code | Không | Không | Không | Exact approved paths | Không | Không |
| Sửa test | Không | Không | Chỉ lập kế hoạch | Approved developer tests | QA-owned tests | Không |
| Chạy verification | Requirement checks | Design checks | Plan checks | Approved stages | Approved QA stages | Evidence query, không mutation |
| Yêu cầu human input | Clarification | Design decision | Plan checkpoint | Scope/upstream conflict | Acceptance ambiguity | Inconclusive/upstream conflict |

## Requirement Context Loop

### Input

- immutable source snapshot và source inventory;
- source adapter policy;
- requirement model/version hiện tại nếu là clarification/revision;
- existing decisions, answers và evidence ledger.

Requirement sở hữu hành vi cần có, không sở hữu quyết định file/symbol sẽ sửa.
Mỗi scenario phải ghi trạng thái của UI, API, business rule và data là
`required|not_applicable|clarification_required`. Trạng thái `required` nói
lớp đó phải được đáp ứng, không có nghĩa chắc chắn phải tạo hoặc sửa code: kết
luận `reuse|modify|add|no_change` thuộc Designer sau repo discovery.

### Tool Nhóm

- `source.list`, `source.search`, `source.read`;
- `technical_evidence.read` khi workflow đã cấp approved evidence; Requirement
  không tự khảo sát repo để chọn kiến trúc/file;
- `evidence.record_reference`;
- `requirement.propose_delta`;
- `requirement.run_validation`;
- `clarification.propose`.

### Write Boundary

Role không ghi artifact trực tiếp. Nó chỉ trả requirement delta và evidence refs;
Python kiểm stable ID, affected refs, grounding, dependency và canonical mutation
trước khi artifact writer phát hành version mới.

### Completion Gate

- mọi canonical requirement có evidence hoặc assumption được gắn nhãn;
- không còn validation issue;
- blocker chưa đủ evidence phải thành clarification có `affects` hợp lệ;
- không tự suy diễn business rule để né clarification.

### Handoff

- complete -> requirement checkpoint;
- clarification -> human/Niko;
- source unavailable -> pause hoặc blocked theo retryability;
- conflict với approved decision -> human decision, không tự ghi đè.

## Designer Architecture Loop

### Input

- approved Requirement Model và evidence refs;
- frozen workspace snapshot và `repo_landscape_v1`;
- immutable project profile/topology contract;
- current design graph/artifact khi repair;
- bounded repo/CodeGraph access;
- Validator findings của đúng design version.

### Tool Nhóm

- `artifact.read_approved`;
- `repo.list_readonly`, `repo.read_readonly`, `repo.search_readonly`;
- `codegraph.query_readonly`;
- `design.propose_delta`;
- `design.render_candidate`;
- `design.run_preflight`.

Designer phải dùng các tool read-only để thực hiện `AS-IS -> GAP -> TO-BE`.
Mỗi design element đề xuất disposition `reuse|modify|add|no_change` và repo
evidence khi có. Canonical output là typed design graph; class, sequence,
component, API, data và state view là projection theo use case.

### Write Boundary

Designer chỉ đề xuất structured design delta. Harness apply delta vào candidate,
render PlantUML/projections và version artifact sau validation. Designer không
ghi target repo hoặc sửa requirement.

### Completion Gate

- canonical requirement coverage đủ theo policy;
- mọi surface `required` có design node hoặc quyết định reuse/no-change có evidence;
- component, UI, API, rule, data và interaction references không dangling;
- deterministic preflight pass;
- unresolved tradeoff được ghi assumption hoặc human decision;
- repair delta nêu rõ finding nào được giải quyết và evidence nào giữ nguyên.

### Handoff

- deterministic pass -> Validator semantic evidence loop;
- requirement gap -> reopen requirement checkpoint;
- architecture choice ngoài approved policy -> human design decision;
- repeated fingerprint/oscillation -> blocked, không tiếp tục rewrite toàn bộ.

## Planner Và Developer Execution Loop

### Planner

Planner chỉ đọc approved requirement/vertical design graph/repo evidence và đề xuất structured
implementation plan. Nó không sửa repo và chỉ tham chiếu verification stage ID.
Plan completion yêu cầu work item có dependency, component, exact path policy,
scenario/requirement/design-node mapping, signature contract và test intent.

### Developer

Developer nhận approved plan/work packet đã bind digest. Tool nhóm:

- `repo.list`, `repo.read`, `repo.search`, `repo.read_diff`;
- `codegraph.query_readonly`;
- `repo.write_file`, `repo.apply_patch` trong exact approved paths;
- `verification.run_stage` với stage ID từ immutable profile;
- `execution.complete_unit`, `execution.complete_session`.

Không có raw shell tool. `verification.run_stage` map sang fixed argv/cwd/env/
timeout. Một implementation plan phải được tách thành execution units theo
dependency; không dùng một `TASK-001` bao trùm toàn bộ feature.

Developer chỉ được complete một unit khi:

- tất cả path của unit có trạng thái rõ;
- requirement/design mapping của unit có evidence;
- trace delta của unit nêu design node nào sẽ được hiện thực và test intent nào
  phải được chứng minh;
- required verification stage cho unit đã pass trên current hashes;
- diff không chứa out-of-scope path;
- unresolved item được handoff, không bị ẩn trong summary.

Session chỉ complete khi mọi required unit complete và Harness chạy completion
gate tổng. Transport resume phải tiếp tục đúng unit/tool cursor và filesystem
hash; conflict yêu cầu re-observe trước mutation tiếp theo.

## QA Investigation Loop

### Input

- approved acceptance criteria và requirement IDs;
- approved scenario/vertical trace partitions;
- current implementation/code-trace evidence;
- QA policy và tập QA-owned paths của run;
- verification stage catalog;
- prior QA execution report khi repair.

### Tool Nhóm

- `repo.read_readonly`, `repo.search_readonly`, `codegraph.query_readonly`;
- `qa.create_test`, `qa.update_owned_test`, `qa.delete_owned_test`;
- `verification.run_qa_stage`;
- `verification.read_report`;
- `qa.classify_failure`;
- `qa.complete_suite` hoặc `qa.handoff_defect`.

### Write Boundary

- chỉ path khớp `QaTestPolicy` và được run sở hữu;
- không ghi đè test có trước run;
- không sửa production code, manifest, lockfile, config hoặc script;
- test mutation có before/after hash và scenario/requirement mapping.

### Failure Classification

- `qa_test_defect`: syntax/import/setup/assertion trái approved contract -> QA sửa;
- `product_defect`: test đúng contract nhưng behavior sai -> Developer repair;
- `upstream_ambiguity`: acceptance/design không đủ quyết định -> human checkpoint;
- `environment_failure`: service/Docker/browser/resource lỗi -> pause/resume;
- `inconclusive`: evidence chưa đủ -> QA đọc/chạy thêm trong budget.

QA không được làm yếu assertion chỉ để test pass. Developer không được sửa
QA-owned acceptance test trong product repair.

### Completion Gate

- scenario union phủ canonical acceptance requirements;
- machine-readable report chứng minh đúng QA-owned files/scenarios đã chạy;
- current production/test hashes khớp report;
- không còn unclassified failure;
- risks và non-blocking observations được lưu riêng.

## Validator Evidence Loop

### Input

- immutable candidate và approved upstream artifacts;
- deterministic findings;
- profile policy;
- evidence ledger và verification reports.

### Tool Nhóm

- `artifact.read_version`;
- `repo.read_readonly`, `repo.search_readonly`;
- `codegraph.query_readonly`, `ast.query_readonly`;
- `verification.read_report`;
- `validator.record_finding`, `validator.complete_review`.

Registry của Validator không chứa write/apply/execute-arbitrary tools. Query chỉ
được mở rộng trong snapshot hiện hành; không fetch context mới rồi âm thầm thay
đổi approved requirement.

Validator đọc canonical graph theo partition. Deterministic code có thể kiểm toàn
graph; semantic reviewer chỉ nhận bounded subgraph theo use case, scenario hoặc
finding. CodeGraph cung cấp call/dependency evidence, còn trace graph chỉ giữ
anchor và quan hệ coverage; Validator không sao chép toàn bộ CodeGraph.

### Completion Gate

- mỗi blocker trỏ tới canonical claim, evidence và owner sửa;
- deterministic blocker luôn được giữ;
- semantic finding không đủ quorum trở thành disputed observation;
- thiếu valid trials/budget tạo `inconclusive`, không tạo repair feedback;
- kết quả nêu rõ `pass|fail|inconclusive` và next owner.
- mọi required scenario/surface có đường trace tới artifact mà phase hiện hành
  yêu cầu, hoặc có finding chỉ rõ owner chịu trách nhiệm; không đòi code/test
  trước phase tạo chúng;
- view/surface vắng mặt chỉ hợp lệ khi có `not_applicable` và reason được duyệt.
