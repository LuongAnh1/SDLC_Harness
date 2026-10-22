# Current Flow And Gap Audit

Ngày audit: **2026-10-22**
Baseline: nhánh `main`, sau default TypeScript/tool-loop cutover.

## Thuật Ngữ

- **One-shot generation**: model nhận packet và trả một artifact hoàn chỉnh.
- **Format retry**: gọi lại model vì output không parse/validate được.
- **Workflow repair loop**: Harness gọi producer, Validator và producer repair
  theo state machine.
- **Model-tool loop**: model quan sát kết quả từng tool call rồi tự chọn hành động
  tiếp theo trong quyền hạn và ngân sách.
- **Evidence loop**: role chỉ đọc/query evidence, không có quyền mutation.

Việc một class có tên `Agent` hoặc workflow có vòng `for` không tự động biến nó
thành model-tool loop.

## Mô Hình Trace Hiện Tại

Cách trace hiện tại có thể hiểu đơn giản:

~~~text
Requirement nodes
-> Design nodes, chủ yếu class và sequence
-> Code nodes/symbols
~~~

Mô hình này đã kiểm được requirement có xuất hiện trong design và class/signature
thiết kế có bằng chứng trong code hay không. Tuy nhiên nó chưa mô tả đầy đủ một
vertical feature đi qua UI interaction, API contract, business rule, data
operation, response state và test evidence. Vì vậy “class đã có trong code” chưa
đủ chứng minh người dùng thực hiện được scenario từ đầu đến cuối.

## Evidence Map

| Khu vực | Runtime source chính | Regression evidence chính |
| --- | --- | --- |
| Requirement V2 | [`refiner_v2.py`](../../sdlc_harness/agents/requirements/refiner_v2.py), [`validator_v2.py`](../../sdlc_harness/agents/requirements/validator_v2.py), [`phase1.py`](../../sdlc_harness/workflows/phase1.py) | [`test_requirement_refiner_v2.py`](../../tests/test_requirement_refiner_v2.py), [`test_requirement_model_v2.py`](../../tests/test_requirement_model_v2.py), [`test_requirement_clarification_v2.py`](../../tests/test_requirement_clarification_v2.py) |
| Design | [`designer.py`](../../sdlc_harness/agents/designer/designer.py), [`design_validation.py`](../../sdlc_harness/agents/validator/design_validation.py), [`phase2.py`](../../sdlc_harness/workflows/phase2.py) | [`test_phase2.py`](../../tests/test_phase2.py), [`test_design_validation_consensus.py`](../../tests/test_design_validation_consensus.py) |
| Plan | [`plan_review.py`](../../sdlc_harness/agents/validator/plan_review.py), [`phase3.py`](../../sdlc_harness/workflows/phase3.py) | [`test_phase3.py`](../../tests/test_phase3.py), [`test_implementation_plan_contract.py`](../../tests/test_implementation_plan_contract.py) |
| Developer loop | [`execution.py`](../../sdlc_harness/agents/developer/execution.py), [`phase4.py`](../../sdlc_harness/workflows/phase4.py) | [`test_developer_tool_loop.py`](../../tests/test_developer_tool_loop.py), [`test_phase4.py`](../../tests/test_phase4.py) |
| Verification | [`runner.py`](../../sdlc_harness/verification/runner.py) | [`test_phase4.py`](../../tests/test_phase4.py), [`test_profiled_qa.py`](../../tests/test_profiled_qa.py) |
| QA | [`qa.py`](../../sdlc_harness/agents/tester/qa.py), [`phase4.py`](../../sdlc_harness/workflows/phase4.py) | [`test_profiled_qa.py`](../../tests/test_profiled_qa.py), [`test_phase4.py`](../../tests/test_phase4.py) |
| Code Trace | [`code_trace.py`](../../sdlc_harness/agents/validator/code_trace.py) | [`test_profiled_code_trace.py`](../../tests/test_profiled_code_trace.py), [`test_phase4.py`](../../tests/test_phase4.py) |

## Requirement

Luồng V2 hiện tại:

```text
fixture/Jira source
-> source snapshot
-> RequirementModelV2Refiner.refine
-> parse + normalize + Python validation
-> tối đa một format/validation repair
-> clarification hoặc requirement checkpoint
```

`RequirementModelV2Refiner` là structured generator có retry giới hạn.
`RequirementClarificationUpdater` nhận đúng câu trả lời của câu hỏi hiện hành,
đề xuất delta rồi để Python kiểm tra ID và mutation scope. Canonical validation
kiểm schema, ID, reference, grounding, decision, dependency và semantic policy.

Điểm mạnh:

- source snapshot và stable requirement ID;
- Python sở hữu canonical mutation/validation;
- clarification có human checkpoint;
- fail closed khi output không hợp lệ.

Khoảng trống:

- Refiner không tự tìm evidence bổ sung từ Jira/docs/repo;
- không có tool để kiểm tra một nhận định hoặc mở rộng source context;
- thiếu evidence được chuyển thành câu hỏi dựa trên packet ban đầu, không phải
  kết quả điều tra chủ động;
- chưa có provenance ledger thống nhất cho các nguồn động.

Kết luận: Requirement hiện là bounded structured-output workflow, chưa phải
context/evidence agent loop.

## Designer

`DesignerAgent.draft` gọi LLM để phân tích rồi sinh PlantUML. Phase 2 parse graph,
chạy deterministic preflight, traceability checks và semantic review/consensus.
Nếu có blocker, workflow gọi `DesignerAgent.update_design` và validate lại trong
giới hạn; repeated blocker và oscillation được phát hiện.

Đầu vào hiện tại của Designer chủ yếu là requirement packet, formal use case,
canonical requirement, analysis do chính Designer tạo và component-routing
directive từ project profile. Directive này chỉ hướng dẫn stereotype/root như
web, API, contracts hoặc database; nó không phải bằng chứng rằng component,
route, schema hay symbol đó thực sự tồn tại trong target repo.

Điểm mạnh:

- producer và Validator tách biệt;
- deterministic check chạy trước semantic judgment;
- có bounded repair, stalled/oscillation guard và versioned artifact;
- warning không tự động trở thành blocker.

Khoảng trống:

- Designer chỉ thấy context Harness đưa sẵn;
- không tự tra CodeGraph, repo topology, API/data contract hiện hữu;
- chưa có frozen repo snapshot/repo-landscape để phân biệt AS-IS với TO-BE;
- chưa kết luận rõ mỗi phần tử là `reuse|modify|add|no_change`;
- repair chủ yếu sinh lại artifact hoàn chỉnh thay vì design delta có phạm vi;
- không tự chạy design check giữa các bước suy luận;
- thiết kế greenfield hợp lý hơn brownfield vì evidence repo còn thụ động.

Kết luận: đây là producer-validator workflow repair loop tốt, nhưng không phải
Designer tool loop.

## Planner Và Developer

Phase 3 hiện dùng Developer plan generation, rule review và LLM semantic review;
workflow gọi `repair_plan` khi còn blocker. Phase 4 mặc định dùng
`DeveloperToolLoopExecutor` cho act và repair.

Developer tool-loop hiện cho phép các JSON action `list_files`, `read_file`,
`search_code`, `read_diff`, `write_file`, `apply_patch` và `complete`. Harness
parse action, thi hành trong exact approved paths, ghi manifest/journal và trả
tool result vào transcript.

Điểm mạnh:

- exact allowed paths, hash, rollback và durable journal;
- secret/unsafe path denial;
- bounded action count cùng per-call output/context budget;
- pause/resume khi transport lỗi;
- workflow vẫn sở hữu Verification Runner và downstream gates.

Khoảng trống:

- plan thường bị thu gọn thành một execution unit lớn;
- model không gọi Verification Runner trong loop;
- completion gate chưa chứng minh từng requirement/unit đã hoàn tất;
- chưa có cumulative token/time budget cho toàn session;
- resume giữ filesystem/journal và archive action cũ vào `attempt_history`, nhưng lượt
  gọi model tiếp theo bắt đầu với working transcript mới thay vì replay toàn bộ lịch sử;
- QA repair vẫn do Developer nhận feedback ở mức `code_or_test` dù nguyên nhân
  có thể là QA-generated test sai.

Kết luận: đây là JSON-action loop có guardrail, chưa phải execution loop đầy đủ
kiểu inspect -> edit -> verify -> diagnose -> complete.

## Verification Runner

`VerificationRunner` resolve stage policy từ immutable profile và thực thi fixed
argv/cwd/env/timeout với `shell=False`. Workflow gọi Runner sau Developer hoặc QA;
model không tự gửi command và hiện cũng không thể chủ động gọi một approved stage
giữa lúc đang xử lý execution unit.

Điểm mạnh:

- command registry code-reviewed và stage ID language/project-profile aware;
- kết quả từng stage, selected test files và machine-readable QA evidence;
- fail/timeout dừng theo ordered policy;
- không thực thi raw command do LLM hoặc plan sinh ra.

Khoảng trống:

- Runner mới là outer workflow gate, chưa phải scoped tool cho Developer/QA loop;
- classification environment/product/test còn nằm ở workflow/feedback builders;
- report phải được bind chặt hơn với session unit và current file hashes khi role
  được phép gọi verification bên trong loop.

Kết luận: Verification Runner phải được giữ làm Harness-owned service và expose
qua typed stage tool; không chuyển command execution cho model.

## QA

`QaTesterAgent.generate_tests` nhận compact packet, sinh toàn bộ test files và
scenario JSON trong một lượt, có tối đa một retry khi parse/policy validation
thất bại. Workflow ghi test vào QA-owned paths rồi gọi Verification Runner và
đòi machine-readable evidence chứng minh đúng generated files đã chạy.

Nếu runtime test fail, workflow tạo QA repair feedback, giao Developer sửa, chạy
lại Verification Runner, Code Trace Validator rồi mới sinh/chạy lại QA suite.

Điểm mạnh:

- test path và framework bị khóa bởi immutable profile policy;
- QA không sửa production file;
- không ghi đè test ngoài tập do run sở hữu;
- exit code đơn thuần không đủ để khai pass.

Khoảng trống:

- QA không tự inspect thêm code, chạy test mục tiêu hoặc đọc stack trace;
- không phân loại test defect với product defect trước khi giao Developer;
- không thể tự sửa test dựa trên execution evidence trong cùng session;
- mỗi vòng có thể sinh lại cả suite, làm giảm ổn định giữa các lần repair.

Kết luận: QA hiện là test generator + workflow executor, chưa phải QA tool loop.

## Validator

Các Validator hiện có chủ ý khác nhau:

- Requirement V2: deterministic Python normalization/validation.
- Design: graph/rule checks kết hợp semantic trials/consensus.
- Implementation Plan: rule hard gate trước, LLM semantic review sau.
- Code Trace: path/test discovery, TypeScript AST/signature evidence,
  deterministic findings, verification evidence và adaptive semantic consensus.

Validator không tự sửa artifact. Workflow dùng finding để quyết định producer
repair, checkpoint reopen, inconclusive hoặc blocked.

Điểm mạnh:

- rule evidence không bị LLM phủ quyết;
- semantic findings bị ràng vào canonical claim/requirement;
- có consensus, rejected/disputed finding và ledger;
- inconclusive không tự phát hành Developer repair feedback.

Khoảng trống:

- semantic reviewer chỉ thấy snapshot/snippet đã được chuẩn bị;
- chưa có read-only query tool cho evidence bị thiếu hoặc mơ hồ;
- evidence contract giữa các Validator chưa có một capability vocabulary chung;
- failure classification giữa QA, implementation, design và requirement còn nằm
  rải rác trong workflow.

Kết luận: Validator không cần mutation loop. Hướng đúng là bounded evidence loop
read-only, giữ deterministic checks làm hard gate.

## Gap Xuyên Suốt

1. Không có session/tool/result contract dùng chung nhưng vẫn giữ role policy.
2. Tool availability, budget và completion gate chưa được mô tả thống nhất.
3. Artifact repair thường ở mức toàn bộ tài liệu thay vì delta có phạm vi.
4. Failure routing chưa luôn phân biệt producer defect, reviewer defect,
   environment defect và approved-upstream defect.
5. Observability hiện theo workflow/artifact, chưa thể hiện rõ agent observation,
   tool decision, evidence và lý do complete/escalate.
6. Requirement chưa biểu diễn rõ scenario cùng trạng thái UI/API/rule/data
   `required|not_applicable|clarification_required`.
7. Design source vẫn nghiêng về class/sequence thay vì typed graph có thể render
   projection phù hợp theo từng use case.
8. Trace chưa biểu diễn nhánh end-to-end và chưa có partition để LLM chỉ đọc
   bounded subgraph trong khi Python kiểm coverage toàn cục.

## Hướng Khắc Phục Đã Chốt

Không mở rộng Developer loop trước. Roadmap mới đi theo thứ tự Requirement
scenario/surface -> profile-bounded read-only repo discovery -> repo-aware Designer -> vertical
trace Validator -> Plan/Developer -> QA. Chi tiết mô hình graph nằm tại
[Vertical-slice Trace Graph](vertical-slice-trace-graph.md).

Repo discovery trong roadmap này là profile-bounded: profile/kiến trúc được chọn
trước, discovery chỉ inventory thành phần hiện hữu và kiểm conformance. Dynamic
stack detection/profile selection cho repo bất kỳ chưa nằm trong scope.
