# TypeScript Full-stack Upgrade

Thư mục này là nguồn định hướng cho bước nâng cấp tiếp theo của SDLC Harness sau
mốc HTTP API-only và live E2E ngày 2026-10-19. Audit/Gate 0 đã hoàn tất và A1.1
đã thêm contract/registry `typescript_fullstack_v1` thuần. A1.2a-A1.2d đã thêm
selector config, snapshot profile theo run, downstream settings binding, target
manifest preflight, workspace binding fail-closed và retirement guard cho run
Java cũ. Profile rỗng không còn được mở run mới; target TypeScript chưa được tạo.

## Mục Tiêu

Hướng nâng cấp gồm hai chặng liên tiếp:

1. bỏ các giả định Java khỏi Phase 3-4 bằng language/project profile;
2. mở rộng target TypeScript từ một codebase đơn khối thành một vertical web
   feature có frontend, backend và database.

Một run V1 vẫn tương ứng với một Jira issue/use case và vẫn đi qua các human
checkpoint hiện có. Niko tiếp tục là conversational/control harness; mọi design,
planning, code, verification, QA, workspace và sync vẫn thuộc SDLC Harness.

## Quyết Định Đã Chốt

- Triển khai tương lai theo hai chặng, không đổi ngôn ngữ và full-stack workflow
  trong một lát cắt lớn.
- Target mới là repo riêng `SDLC_Target_TypeScript`; không chuyển đổi tại chỗ
  repo Java `SDLC_Target_eTour`.
- Stack tham chiếu V1: npm workspaces, React/Vite, Fastify,
  PostgreSQL/Prisma, Vitest và Playwright.
- Target repo cung cấp infra skeleton; agent triển khai nghiệp vụ, không dựng
  lại toàn bộ framework trong mỗi run.
- Một run triển khai một vertical feature xuyên frontend, backend và database.
- Không duy trì Java execution profile cho execution mới. Run Java cũ vẫn
  đọc/audit/sync-existing/cleanup được, còn resume/revalidate sinh artifact đã
  bị chặn bởi retirement contract tại application boundary.

Các quyết định trên là hướng thiết kế đã thống nhất, chưa mô tả hành vi đang tồn
tại trong code.

## Tài Liệu

- [Current Flow And Gap Audit](current-flow-and-gap-audit.md): luồng hiện hành,
  Java coupling và khoảng trống full-stack có dẫn chứng.
- [Target Direction](target-direction.md): kiến trúc profile/topology và artifact
  đích ở mức proposal.
- [Implementation Plan](implementation-plan.md): roadmap triển khai; Gate 0,
  toàn bộ A1, A2.1a/A2.1b, A2.2a và A2.3 đã hoàn tất, các checklist còn lại giữ theo
  trạng thái thực tế.
- [Prompt Usage Map](prompt-usage-map.md): call site prompt thật, file
  reference-only và owner của từng lát migration.
- [Gate 0 Review](gate-0-review.md): phát hiện, quyết định contract và acceptance
  evidence đã khóa trước khi code.
- [Progress](progress.md): execution log của công việc khảo sát, quyết định và
  triển khai.
- [Test Performance Optimization](test-performance-optimization.md): baseline,
  ngân sách và guardrail cho việc giảm thời gian regression suite.

## Docs-first Gate

Trước khi triển khai code cần review đủ bốn câu hỏi:

1. Audit có phản ánh đúng code và tests tại baseline hay không?
2. Ranh giới language profile, project topology và workflow có rõ owner không?
3. Artifact Phase 2-4 có đủ để trace một vertical feature xuyên các tầng không?
4. Acceptance criteria có chứng minh web chạy thật thay vì chỉ sinh file không?

Gate này đã được review và đóng ở mức tài liệu ngày 2026-10-19. A1 hiện đã khóa
profile contract, selector, immutable run snapshot, target manifest/workspace
binding và legacy retirement. Run mới bắt buộc chọn profile; target repo mới
chưa được tạo và chưa chạy migration hay live workflow cho hướng TypeScript.
Phase 3 profiled hiện phát hành `implementation_plan_structured_v2`, bind review
với SHA-256 của artifact và chỉ kích hoạt smoke suite `typecheck -> unit`.
Verification Runner đã fail-closed với packet stage-only nhưng chưa ánh xạ hoặc
thực thi các stage này cho tới lát A3.
