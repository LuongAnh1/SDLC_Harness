# Workflow State And Artifacts

## Mục Tiêu

Role loop phải bền vững và audit được nhưng không trở thành state machine thứ hai
cạnh tranh với Phase 1-4. Workflow state vẫn là nguồn sự thật; role session là
sub-state bị ràng vào một phase, artifact version và immutable input snapshot.

## Session Lifecycle

```text
created
-> running
-> completed
   |-> handed_off
   |-> clarification_required
   |-> paused
   |-> blocked
```

- `created`: manifest đã persist trước LLM/tool call đầu tiên.
- `running`: có active cursor và budget counters.
- `completed`: completion evidence đã được Harness kiểm tra.
- `handed_off`: role kết luận lỗi thuộc owner khác; workflow xác nhận route.
- `clarification_required`: cần human input, không tự tiêu hao repair loop.
- `paused`: transport/environment retriable; giữ cùng session/call identity.
- `blocked`: policy violation, budget exhaustion không an toàn hoặc conflict
  không thể giải quyết trong role boundary.

Không resume session với role, profile snapshot, input digest hoặc capability
policy khác. Thay đổi một trong các authority input phải mở session version mới.

## Artifact Định Hướng

| Artifact | Nội dung | Owner |
| --- | --- | --- |
| `role_loop_session_v1.json` | identity, authority digests, status, cursor, budgets | Harness |
| `role_loop_manifest_v1.json` | role policy, units, paths/tools/stages được phép | Harness |
| `role_loop_journal_v1.json` | ordered observations, calls, results, mutations, hashes | Harness |
| `role_completion_v1.json` | covered units, evidence, unresolved items, outcome | Role đề xuất, Harness xác nhận |
| `role_failure_classification_v1.json` | failure owner, severity, repairability, route | Role/Validator đề xuất, workflow xác nhận |
| Role delta artifact | requirement/design/test/code mutation đề xuất hoặc đã apply | Domain writer/workflow |
| `requirement_scenario_model_v1.json` | scenario, acceptance criteria, required surfaces và provenance | Requirement workflow |
| `repo_landscape_v1.json` | frozen snapshot, observed stack/topology và code/schema/test anchors | Repo Discovery |
| `vertical_design_graph_v1.json` | typed UI/API/rule/data nodes, interactions và disposition | Designer đề xuất, Harness apply |
| `vertical_trace_graph_v1.json` | Requirement -> Design -> Implementation -> Verification edges | Harness/Validator |

Tên file vật lý phải thêm role, scope và version để không va chạm giữa các phase,
ví dụ `qa_loop_session_v1.json`; bảng trên mô tả schema family chứ không bắt buộc
mọi role dùng cùng một filename.

## Journal Và Transcript

Journal là audit record có cấu trúc, không phải raw chain-of-thought. Mỗi entry có:

- sequence, timestamp, session/call ID;
- observation/tool/action/result type;
- sanitized summary và evidence refs;
- input/output hashes cho mutation;
- duration, budget delta và retryability;
- outcome/error code.

Không persist reasoning bí mật, secret, raw environment hoặc log không giới hạn.
Transcript gửi lại model được dựng từ manifest, completed journal entries và
bounded working context; resume không chỉ dựa vào filesystem hiện tại.

## Graph Partition Và Context

Canonical graph được lưu như artifact của run, nhưng role không nhận toàn bộ graph
trong mỗi lượt. Mỗi node/edge có stable ID, artifact version và partition keys
tối thiểu theo use case, scenario và component.

- Requirement làm việc với requirement/scenario partition.
- Designer nhận scenario cùng bounded AS-IS neighborhood liên quan.
- Planner/Developer nhận execution-unit subgraph.
- QA nhận scenario/test subgraph.
- Validator deterministic được phép quét toàn graph; semantic review nhận bounded
  subgraph quanh finding.

Role chỉ đề xuất graph delta. Harness kiểm schema, stable ID, edge endpoint,
authority và precondition hash trước khi merge. Sau merge, global checks chạy lại
để phát hiện orphan, dangling edge, thiếu required surface và stale evidence.

Coverage policy đi theo phase/version: design graph có thể trỏ tới planned work;
implementation graph mới phải thay planned work bằng code anchors; final/QA graph
mới phải có execution evidence. Không dùng một completion rule cuối luồng để
chặn artifact hợp lệ của phase sớm.

Trace graph không chứa bản sao toàn bộ mã nguồn hoặc CodeGraph. Implementation
node chỉ giữ sanitized repository-relative file/symbol anchor, snapshot hash và
evidence reference.

## Idempotency Và Ambiguous Result

- Persist tool call trước khi thực thi mutation.
- Mutation tool dùng stable call ID và precondition hash.
- Nếu transport đứt sau submit, resume phải tra journal/result trước khi phát lại.
- Read tool có thể retry; write/tool sync không retry mù.
- Verification report bind target/worktree SHA, file hashes và selected stages.
- Completion bind đúng report/evidence snapshot; code đổi sau report làm
  completion evidence hết hiệu lực.

## Budget Và Repair Counter

Tách ba loại counter:

1. **tool/action budget** trong một role session;
2. **role repair iteration** khi role tạo candidate mới sau finding;
3. **workflow repair cycle** khi handoff qua nhiều role.

Transport/environment retry không tăng repair iteration. Invalid model output có
thể tiêu call budget nhưng không tự tạo artifact version. Product mutation thành
công mới tăng repair iteration theo workflow policy hiện hành.

## Handoff State

Role chỉ đề xuất handoff. Workflow kiểm finding/evidence rồi cập nhật phase state:

```text
QA product_defect
-> qa_repair_needed
-> Developer session
-> Verification
-> Code Trace Validator
-> QA resume/new session against new hashes
```

```text
Designer requirement_gap
-> stop design repair
-> requirement checkpoint reopen request
-> human approval before a new design input version
```

```text
Vertical trace missing required surface
-> finding binds scenario + missing layer
-> route Requirement nếu surface còn mơ hồ
-> route Designer nếu thiếu design
-> route Planner/Developer nếu thiếu implementation
-> route QA nếu thiếu test/evidence
```

```text
Validator inconclusive
-> keep candidate unapproved
-> retry evidence review when budget/policy permits
-> otherwise human review; no producer repair feedback
```

Phase 5 vẫn là owner của checkpoint reopening. Trước khi Phase 5 tồn tại, route
upstream defect phải fail closed hoặc dừng ở human decision; không tự mutation
artifact đã duyệt.

## Status Và API

Gate 0/L1 không thay public HTTP action. Status sau này chỉ cần additive summary:

- active role/session status;
- current execution unit;
- tool/call budget used và remaining;
- last sanitized observation/error;
- proposed handoff/failure owner;
- manifest/journal artifact refs đã qua public allowlist.

Không expose prompt, raw model response, local absolute path hoặc private logs.

## Compatibility Và Cleanup

- Run cũ không có role-loop state tiếp tục đọc/audit theo schema hiện hành.
- Không auto-migrate active legacy session qua authority contract mới.
- Mỗi role cutover độc lập và bind executor vào persisted session.
- Rollback config chỉ áp dụng cho run/session mới; không đổi executor giữa chừng.
- Cleanup role artifacts đi cùng run workspace lifecycle và không xóa historical
  evidence được public checkpoint tham chiếu.
