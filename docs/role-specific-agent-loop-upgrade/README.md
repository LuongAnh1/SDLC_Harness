# Role-specific Agent Loop Upgrade

Trạng thái: **Gate 0 đã duyệt — roadmap đã điều chỉnh theo hướng upstream-first, chưa triển khai runtime**
Ngày mở tài liệu: **2026-10-22**

## Mục Tiêu

Bộ tài liệu này định nghĩa hướng nâng cấp các vai trò Requirement, Designer,
Planner/Developer, QA và Validator từ các lượt LLM bị động thành các vòng làm
việc có tool, evidence và điều kiện dừng phù hợp với đúng trách nhiệm của từng
vai trò.

Đây là roadmap độc lập với
[`typescript-fullstack-upgrade`](../typescript-fullstack-upgrade/README.md).
TypeScript full-stack là target kiểm chứng hiện tại, nhưng kiến trúc loop không
được phụ thuộc cứng vào TypeScript, React, Fastify, Prisma hay PostgreSQL.

## Vấn Đề Cần Giải Quyết

Trong baseline hiện tại, từ agent đang bao phủ nhiều cơ chế khác nhau:

- LLM sinh một artifact trong một lượt;
- LLM được gọi lại một lần để sửa JSON sai schema;
- workflow gọi producer và Validator qua nhiều vòng repair;
- Developer phát ra từng JSON action để Harness thực thi;
- Validator chạy rule, AST/graph evidence và semantic consensus.

Chỉ cơ chế thứ tư gần với model-tool loop, nhưng vẫn chưa có đầy đủ khả năng tự
chia việc, chạy verification theo lúc cần và chứng minh completion. Bộ tài liệu
này chuẩn hóa thuật ngữ, quyền tool, failure routing và lộ trình chuyển đổi mà
không biến mọi vai trò thành một coding agent tổng quát.

## Nguyên Tắc Đã Chốt

- Harness sở hữu state machine, checkpoint, artifact, policy, budget và quyền
  chuyển phase.
- Mỗi vai trò có capability/tool allowlist riêng; không có registry mở dùng chung
  cho tất cả agent.
- Requirement, Designer và Validator không được ghi production code.
- QA chỉ được ghi test do run sở hữu trong vùng policy cho phép.
- Validator luôn read-only và độc lập với producer mà nó đánh giá.
- Verification Runner, CodeGraph, AST extractor và source adapters là service/tool
  do Harness kiểm soát, không phải agent độc lập.
- Repo Discovery cũng là Harness-owned read-only service/artifact step, không tạo
  thêm một agent có quyền tự quyết workflow.
- Lỗi transport/môi trường phải pause/resume và không tiêu hao repair iteration.
- Contract mới được version theo hướng additive; run lịch sử vẫn đọc/audit được.
- DeepSeek/OpenAI-compatible client hiện tại tiếp tục là runtime LLM; roadmap này
  không mở một generic provider abstraction mới.
- Nâng cấp mới đi từ Requirement -> repo discovery -> Design -> Validator ->
  Plan/Developer -> QA; Developer tool-loop hiện tại được giữ làm baseline chứ
  không còn là điểm bắt đầu của roadmap.
- Traceability đích là vertical-slice graph nối scenario qua UI, API, rule, data,
  code và test. UI/API/DB là các lớp trong design/code, không phải phase mới.

## Tài Liệu

- [Current Flow And Gap Audit](current-flow-and-gap-audit.md): mô tả cơ chế
  hiện tại của Requirement, Designer, Planner/Developer, Verification Runner,
  QA và Validator; chỉ ra điểm mạnh cùng khoảng trống cần tool-loop giải
  quyết.
- [Target Architecture](target-architecture.md): định nghĩa kiến trúc role-loop
  dùng chung, ranh giới sở hữu giữa Agent và Harness, canonical loop, contract
  nội bộ, quy tắc an toàn và failure routing.
- [Vertical-slice Trace Graph](vertical-slice-trace-graph.md): giải thích bằng
  ngôn ngữ đơn giản cách đồ thị mới nối Requirement -> UI -> API -> Rule -> DB ->
  response -> test, cách biểu diễn nhánh scenario và cách tránh nạp cả graph vào
  một prompt.
- [Role Capability Contracts](role-capability-contracts.md): liệt kê input, nhóm
  tool được phép, write boundary, completion gate và handoff của từng role;
  đây là tài liệu chính để tra Agent nào được làm gì.
- [Workflow State And Artifacts](workflow-state-and-artifacts.md): mô tả vòng đời
  session, manifest/journal, idempotency, pause/resume, budget, handoff state và
  cách lưu evidence mà không lưu chain-of-thought.
- [Implementation Plan](implementation-plan.md): roadmap Gate 0 và L1-L7, thứ tự
  triển khai, dependency với TypeScript full-stack, cùng ranh giới cutover/
  rollback của từng role.
- [Test And Evaluation Strategy](test-and-evaluation-strategy.md): quy định test
  pyramid, scenario bắt buộc theo role, security/adversarial tests, metrics so sánh
  baseline và controlled live acceptance.
- [Gate 0 Review](gate-0-review.md): lưu checklist review tài liệu, finding đã
  điều chỉnh, evidence đối chiếu và phạm vi phê duyệt Gate 0; approval này
  không tự xếp lịch L1.1.
- [Progress And Execution Log](progress.md): nhật ký các quyết định, lần
  review, mốc hoàn thành và trạng thái thực tế của roadmap qua từng lần
  triển khai.

## Ranh Giới Với Roadmap TypeScript Full-stack

Roadmap này không thay thế công việc Chặng A/B:

- Java runtime cleanup vẫn thuộc Chặng A.
- Designer loop cung cấp nền cho B1 System Design Bundle.
- Planner/Developer loop cung cấp nền cho B2 Component-aware Plan.
- L5/L6 verification tools sử dụng stage registry của B3 Multi-stage Verification.
- QA loop là điều kiện trước B4 Browser QA And Repair.
- Validator evidence upgrade cung cấp nền cho B5 Cross-layer Trace.

Hai roadmap có thể được triển khai theo dependency, nhưng checklist và progress
không được tích thay cho nhau.

**A4 Java runtime-only cleanup** và Chặng A của roadmap TypeScript full-stack đã
hoàn tất. Sau khi rà lại hướng full-stack/brownfield, roadmap role-loop được đổi
sang thứ tự upstream-first. Việc cập nhật tài liệu này không có nghĩa runtime
L1-L7 đã được triển khai.

## Docs-first Gate

Không triển khai runtime slice từ tài liệu này trước khi Gate 0 xác nhận:

1. audit phản ánh đúng code và tests hiện hành;
2. mỗi role có quyền, input/output và completion gate rõ ràng;
3. failure routing không cho một role sửa sai ownership;
4. state/artifact contract đủ pause/resume, audit và rollback;
5. dependency với TypeScript full-stack không trùng ownership.
