# Target Architecture

## Kiến Trúc Tổng Quát

```text
HTTP API / CLI
  -> Application Service
  -> Phase Workflow
      -> Role Loop Executor
          -> Role Capability Policy
          -> Role Tool Registry
          -> DeepSeek LLM Client
          -> Session Manifest + Journal
      -> Deterministic Gate / Validator
      -> Artifact Store + Workflow State
      -> Human Checkpoint / Tool Sync
```

Role Loop Executor chỉ sở hữu cơ chế lặp: transcript, action/result, budget,
pause/resume và completion proposal. Phase workflow vẫn sở hữu thứ tự nghiệp vụ,
artifact version, checkpoint và route sang role khác.

## Luồng Nâng Cấp Upstream-first

Kiến trúc đích không bắt đầu bằng việc bọc lại Developer. Nó đi từ thông tin
thượng nguồn xuống:

~~~text
Requirement làm rõ scenario và surface
-> Harness khóa snapshot/workspace cho run
-> Repo Discovery mô tả AS-IS
-> Designer so sánh AS-IS với requirement để tạo TO-BE
-> Validator kiểm vertical trace và design
-> Planner chia execution unit
-> Developer sửa và verify
-> QA chứng minh từng scenario
~~~

Developer tool-loop hiện tại tiếp tục là baseline trong thời gian L1-L4 được xây.
Shared loop mechanics được trích ra khi role đầu tiên thực sự cần, tránh tạo một
abstraction lớn dựa riêng trên hành vi coding rồi ép Requirement/Designer dùng theo.

## Ranh Giới Sở Hữu

### Harness

- resolve immutable project/language profile;
- chọn role và capability policy;
- thực thi tool sau khi validate input;
- giới hạn path, command, thời gian, call/token budget;
- ghi session state, journal, artifact và event;
- quyết định pass, repair, pause, blocked hoặc human checkpoint;
- không tin `complete` của model nếu completion evidence chưa đủ.

### Role Agent

- đọc task/evidence đã cấp;
- chọn tool tiếp theo trong allowlist;
- diễn giải tool result;
- đề xuất delta/mutation trong đúng scope;
- khai báo complete, escalate hoặc clarification kèm evidence.

### Deterministic Gate Và Validator

- kiểm schema, policy, graph, AST, signature và machine-readable execution;
- không sửa artifact đang đánh giá;
- không chuyển warning thành blocker nếu policy không cho phép;
- không cho semantic judgment phủ quyết hard evidence.

## Canonical Loop

```text
load immutable inputs
-> build role session manifest
-> model observes task + bounded context
-> model requests one typed tool action
-> Harness authorizes and executes
-> append normalized tool result + evidence reference
-> repeat until:
     completion evidence passes
     clarification/human decision is required
     role boundary requires handoff
     budget is exhausted
     retriable transport/environment failure pauses session
     non-retriable policy failure blocks session
```

Không cho model gửi raw shell command. Verification được gọi bằng stage ID từ
profile registry. Source/repo reads phải trả bounded content cùng provenance.

## Mô Hình Vertical-slice Trace Graph

Graph mới không thay bốn tầng Requirement, Design, Code và Verification. Nó làm
rõ các lớp UI/API/rule/data nằm trong Design và Code:

| Tầng | Nội dung điển hình |
| --- | --- |
| Requirement | Use case, scenario, acceptance criterion, required surfaces |
| Design | UI interaction/state, API contract, rule/service, data operation |
| Code | Component/function, route/schema, service/class, model/query/migration |
| Verification | Unit, integration, acceptance/browser test và execution evidence |

Một scenario có thể tạo graph có hướng và rẽ nhánh:

~~~text
scenario
-> UI action
-> API
-> rule
-> data
-> response
-> UI state
-> tests/evidence
~~~

Nhánh cancel, forbidden, not-found hay persistence failure dùng chung các node ổn
định nếu phù hợp nhưng có acceptance/test edge riêng. Xem
[Vertical-slice Trace Graph](vertical-slice-trace-graph.md) để có ví dụ eTour đầy đủ.

### Design Source Và Projection

Canonical design là typed graph. PlantUML class/sequence, component view, API/data
contract và state view là các projection được render từ graph theo use case.
Không bắt buộc mọi use case có mọi projection. Surface/view vắng mặt phải có trạng
thái `not_applicable` và lý do; nếu chưa quyết định được thì chuyển thành
`clarification_required`.

Class vẫn là một design/code symbol hợp lệ. Điểm thay đổi là Validator không chỉ
nối class design tới class code, mà còn có thể nối interaction UI, route API,
business rule, data operation và test evidence.

### Repo-aware Design

Sau Requirement approval, Harness cấp snapshot read-only và artifact
`repo_landscape_v1`. Designer dùng repo/CodeGraph/schema/route/test tools để làm:

~~~text
AS-IS -> GAP -> TO-BE
~~~

Mỗi design element kết luận `reuse|modify|add|no_change` và trỏ tới repo evidence
khi có. Requirement nói hành vi/surface nào cần có; Designer mới quyết định file,
symbol hay contract nào thực sự bị tác động.

Read-only là capability của Requirement/Designer. Base snapshot identity được
đóng băng để evidence ổn định; tới Developer, Harness mới cấp write scope và phát
hành graph/evidence version mới theo current hashes.

### Graph Lớn Nhưng Context Phải Nhỏ

- Canonical graph được Python lưu và kiểm deterministic trên toàn cục.
- Graph được partition theo use case, scenario, component và artifact version.
- Mỗi lượt LLM chỉ nhận bounded subgraph quanh task/finding hiện tại.
- Role đề xuất delta nhỏ; Harness validate rồi merge.
- Sau merge, global checks phát hiện orphan, dangling edge, thiếu required surface
  và evidence hết hiệu lực.

Trace graph chỉ giữ file/symbol anchors và quan hệ requirement coverage. CodeGraph
giữ quan hệ gọi/phụ thuộc của code. Hai graph liên kết qua anchors, không sao chép
lẫn nhau.

Coverage gate phải phase-aware: Phase 1 yêu cầu scenario/surface, Phase 2 yêu cầu
Requirement -> Design, Phase 3 cho phép planned implementation units, Phase 4 yêu
cầu code anchors, và QA/final gate mới yêu cầu execution evidence. Artifact của
phase tương lai chưa tồn tại không được báo lỗi như một blocker hiện tại.

## Shared Internal Contracts

Các tên dưới đây là contract định hướng; schema chi tiết chỉ được phát hành ở
lát L1 và phải versioned:

- `role_loop_session_v1`: identity, role, run/profile snapshot digest, input
  artifact digests, budgets, status và resume cursor.
- `role_capability_policy_v1`: role, allowed tool IDs, read/write roots,
  verification stages, limits và completion requirements.
- `role_tool_call_v1`: call ID, tool ID, typed arguments và observation cursor.
- `role_tool_result_v1`: call ID, status, bounded output, evidence refs, duration
  và retryability.
- `role_completion_v1`: outcome `complete|handoff|clarification|blocked`, covered
  units, evidence refs, unresolved items và requested next owner.
- `role_failure_classification_v1`: owner `role|product|upstream|environment|human`,
  repairability, severity và routing recommendation.

Các artifact domain mới đi cùng contract role-loop:

- `requirement_scenario_model_v1`: scenario, acceptance criteria và trạng thái
  surface `required|not_applicable|clarification_required`.
- `repo_landscape_v1`: snapshot, observed stack/topology, component, route,
  schema/test anchors và uncertainty.
- `vertical_design_graph_v1`: typed design nodes/edges cùng disposition
  `reuse|modify|add|no_change`.
- `vertical_trace_graph_v1`: liên kết Requirement -> Design -> Implementation ->
  Verification và partition metadata.

Các contract này là nội bộ. HTTP API hiện hành không đổi trong Gate 0/L1. Nếu cần
expose status sau này, chỉ thêm field sanitized vào response hiện có.

## Security Và Determinism

- Tool registry được tạo từ code-reviewed policy, không từ prompt hay model output.
- Path được normalize và kiểm tra lại tại thời điểm thực thi.
- Write tool dùng optimistic hash/precondition; conflict không tự overwrite.
- Tool result không mang secret, absolute local path hoặc raw unbounded log.
- Retry transport dùng cùng call/session identity; không phát hành mutation mới
  khi kết quả trước còn mơ hồ.
- Mọi mutation có before/after hash và journal record.
- Validator registry không đăng ký write tool.

## Failure Routing Chuẩn

| Failure owner | Ví dụ | Route |
| --- | --- | --- |
| Role output | JSON/delta/test do role sinh sai | Cùng role sửa trong budget |
| Product | Acceptance đúng nhưng implementation sai | Developer repair |
| Upstream artifact | Requirement/design đã duyệt mâu thuẫn | Dừng và reopen checkpoint |
| Environment | Docker/service/network/LLM transport lỗi | Pause/resume, không tăng repair count |
| Human decision | Thiếu business rule hoặc scope choice | Clarification/checkpoint |
| Policy/security | Tool/path/contract không được phép | Fail closed và block |

## Quan Hệ Với Project Profile

Role capability là language-neutral; project profile cung cấp dữ liệu cụ thể:

- component/source/test roots;
- tool adapter/extractor phù hợp;
- approved verification stage IDs;
- QA test ownership policy;
- topology và runtime-service capabilities.

Vì vậy TypeScript có thể dùng CodeGraph, TypeScript AST, Vitest và Playwright,
trong khi một profile tương lai thay adapter mà không thay workflow semantics.
