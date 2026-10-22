# Role-specific Agent Loop Upgrade

Trạng thái: **Gate 0 đã duyệt — roadmap đã note, chưa xếp lịch triển khai**
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
- Lỗi transport/môi trường phải pause/resume và không tiêu hao repair iteration.
- Contract mới được version theo hướng additive; run lịch sử vẫn đọc/audit được.
- DeepSeek/OpenAI-compatible client hiện tại tiếp tục là runtime LLM; roadmap này
  không mở một generic provider abstraction mới.

## Tài Liệu

- [Current Flow And Gap Audit](current-flow-and-gap-audit.md)
- [Target Architecture](target-architecture.md)
- [Role Capability Contracts](role-capability-contracts.md)
- [Workflow State And Artifacts](workflow-state-and-artifacts.md)
- [Implementation Plan](implementation-plan.md)
- [Test And Evaluation Strategy](test-and-evaluation-strategy.md)
- [Gate 0 Review](gate-0-review.md)
- [Progress And Execution Log](progress.md)

## Ranh Giới Với Roadmap TypeScript Full-stack

Roadmap này không thay thế công việc Chặng A/B:

- Java runtime cleanup vẫn thuộc Chặng A.
- Designer loop cung cấp nền cho B1 System Design Bundle.
- Planner/Developer loop cung cấp nền cho B2 Component-aware Plan.
- Shared tool contract sử dụng stage registry của B3 Multi-stage Verification.
- QA loop là điều kiện trước B4 Browser QA And Repair.
- Validator evidence upgrade cung cấp nền cho B5 Cross-layer Trace.

Hai roadmap có thể được triển khai theo dependency, nhưng checklist và progress
không được tích thay cho nhau.

**A4 Java runtime-only cleanup** và Chặng A của roadmap TypeScript full-stack đã
hoàn tất. Gate 0 của roadmap này chỉ chốt hướng tài liệu; roadmap vẫn
park và chưa có kế hoạch triển khai chi tiết cho L1.1.

## Docs-first Gate

Không triển khai runtime slice từ tài liệu này trước khi Gate 0 xác nhận:

1. audit phản ánh đúng code và tests hiện hành;
2. mỗi role có quyền, input/output và completion gate rõ ràng;
3. failure routing không cho một role sửa sai ownership;
4. state/artifact contract đủ pause/resume, audit và rollback;
5. dependency với TypeScript full-stack không trùng ownership.
