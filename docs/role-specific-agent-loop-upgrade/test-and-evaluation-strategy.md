# Test And Evaluation Strategy

## Mục Tiêu

Kiểm chứng ba nhóm thuộc tính độc lập:

1. **Correctness**: role tạo đúng artifact/mutation và route đúng owner.
2. **Safety/durability**: tool/path/policy/idempotency/pause-resume không bị vượt qua.
3. **Effectiveness**: loop thực sự cải thiện hội tụ/chất lượng so với one-shot,
   không chỉ tăng số LLM/tool call.

## Test Pyramid

### Contract Và Unit

- parse/validate mọi schema session, policy, call/result, completion/failure;
- parse/validate scenario/surface, repo-landscape, design-graph và trace-graph schema;
- stable node/edge ID, graph delta merge, partition và dangling/orphan detection;
- tool allowlist, typed arguments, bounded output và secret/path sanitization;
- hash precondition, stable call ID, duplicate mutation và ambiguous result;
- completion gate theo role;
- deterministic failure classification trước semantic classification;
- historical artifact/read compatibility.

### Role-loop Component

Dùng scripted fake LLM trả chuỗi action có chủ ý:

- read -> inspect -> mutation -> verification -> complete;
- invalid tool/tool args/path denial;
- malformed output rồi recovery trong budget;
- budget exhausted;
- transport fail trước/sau tool submit;
- stale hash giữa observation và mutation;
- handoff đúng và handoff không đủ evidence.

### Workflow Regression

- Phase 1 clarification/checkpoint giữ status và sync contract;
- Phase 2 validation/repair/stalled/oscillation giữ semantics;
- Phase 3 implementation plan approval không tạo code sớm;
- Phase 4 act/verification/code trace/QA/publication giữ thứ tự gate;
- HTTP API/CLI parity và durable operation không thay đổi;
- run lịch sử và active session cũ không bị auto-migrate.

## Scenario Bắt Buộc Theo Role

### Requirement

- use case được tách thành main/alternative/error scenarios có stable ID;
- UI/API/rule/data thiếu trạng thái bị reject; `not_applicable` thiếu reason bị reject;
- tìm thấy evidence và không hỏi clarification thừa;
- evidence thiếu tạo câu hỏi có affected IDs;
- hai nguồn mâu thuẫn tạo human decision;
- source outage pause, không tạo requirement giả;
- delta cố sửa ID ngoài scope bị Python chặn.

### Designer

- cùng frozen repo snapshot tạo cùng AS-IS anchors;
- profile TypeScript/React/Fastify/Prisma đã khóa không bị Discovery tự đổi khi
  gặp package hoặc cấu trúc ngoài kỳ vọng; mismatch tạo finding/human decision;
- brownfield lookup tìm đúng component/API hiện hữu;
- existing component được đánh dấu `reuse|modify`, không bị phát minh thành component mới;
- backend-only scenario cho phép UI `not_applicable`; full-stack scenario bị
  chặn nếu thiếu UI/API/data design bắt buộc;
- typed design graph render projection class/sequence/API/data nhất quán;
- delta sửa đúng blocker và giữ mapping đã pass;
- design dẫn chiếu symbol không tồn tại bị preflight chặn;
- repeated fingerprint và oscillation dừng đúng;
- requirement gap route upstream thay vì workaround trong design.

### Planner/Developer

- feature nhiều file có ordered units và verification riêng;
- file liên quan được đọc nhưng không tự thêm vào write scope;
- verification fail -> diagnose -> repair -> pass trong cùng session;
- `complete` sớm bị từ chối;
- restart tiếp tục đúng cursor/hash, không duplicate write;
- upstream signature/design conflict tạo handoff thay vì sửa lệch contract.

### QA

- generated test sai syntax/import được QA tự sửa;
- assertion trái approved requirement bị nhận diện là QA defect;
- production defect giữ test cố định và route Developer;
- Docker/browser/service unavailable pause không tăng QA repair iteration;
- QA không ghi test ngoài owned paths và Developer không sửa QA-owned test;
- selected/verified scenarios khớp machine-readable report.

### Validator

- write call luôn bị policy từ chối;
- deterministic global scan phát hiện orphan, dangling edge, missing required
  surface và stale evidence;
- phase-aware gate chấp nhận planned work ở Design/Plan nhưng yêu cầu code anchor
  ở Implementation và execution evidence ở QA/final;
- semantic review chỉ nhận đúng bounded scenario/finding subgraph;
- trace graph liên kết CodeGraph bằng anchor mà không sao chép call graph;
- deterministic finding không bị semantic reviewer hạ cấp;
- semantic minority thành disputed observation;
- thiếu quorum thành inconclusive;
- finding thiếu canonical subject/evidence bị reject;
- cùng snapshot tạo cùng deterministic ledger fingerprint.

## Adversarial Và Security

- prompt/source yêu cầu đọc secret hoặc mở rộng path;
- model chèn raw command vào field text;
- symlink/path traversal/case normalization;
- oversized tool result/log và malicious file content;
- stale artifact ref, swapped profile snapshot hoặc forged stage ID;
- retry mutation sau timeout khi kết quả đầu tiên chưa rõ;
- Validator cố gọi write tool hoặc QA cố sửa production file.

## Evaluation So Với Baseline

Mỗi role cutover phải chạy cùng một fixture set ở baseline và candidate:

- tỷ lệ pass lần đầu và sau repair;
- số blocker đúng/sai và route đúng owner;
- số vòng repair, LLM calls, tool calls, tokens và wall time;
- tỷ lệ session resume thành công;
- số policy denial và mutation duplicate;
- artifact diff stability giữa các lần chạy.

Không cutover chỉ vì candidate gọi được nhiều tool hơn. Candidate phải giữ hoặc
tăng correctness, giảm false repair/routing và nằm trong budget đã duyệt.

## Controlled Live Acceptance

Run cuối phải dùng target TypeScript riêng và một vertical feature có UI, API,
database, unit/integration/acceptance evidence. Luồng tối thiểu:

```text
Jira/source
-> Requirement scenario/surface loop + human checkpoint
-> frozen workspace + profile-bounded repo discovery
-> Designer AS-IS/GAP/TO-BE loop + vertical design graph checkpoint
-> vertical trace validation
-> component-aware Planner execution units + plan checkpoint
-> Developer inspect/edit/verify loop
-> Code Trace read-only Validator
-> QA investigate/run/classify loop
-> vertical-slice quality report
-> Draft PR + Jira/Confluence sync
```

Vertical feature bắt buộc có ít nhất một success branch và một alternative/error
branch. Acceptance phải chứng minh đường trace từ scenario qua các surface
`required` tới code và test execution evidence; không chỉ chứng minh file/class
tồn tại.

Merge vẫn do human. Live run phải ghi run ID, commit/PR references, stage evidence,
metrics, failure/recovery nếu có và xác nhận không lộ secret/private path.

## Test Performance

- Unit/contract tests chạy song song khi không chia sẻ runtime/worktree.
- Component tests dùng temporary repo/runtime và fake LLM có transcript xác định.
- Docker/browser tests tách marker; chỉ chạy khi dependency preflight pass.
- Full regression không bị chạy lặp ở mỗi inner slice; dùng targeted suite trước,
  full suite tại gate/cutover.
- Mọi tối ưu phải giữ coverage cho state transition, failure route và policy.
