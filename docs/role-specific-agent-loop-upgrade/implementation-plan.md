# Implementation Plan

> Đây là roadmap triển khai tương lai. Việc tạo bộ tài liệu không đồng nghĩa các
> runtime slice bên dưới đã hoàn tất.

## Gate 0 — Docs-first Review

- [x] Ghi current-flow audit cho Requirement, Design, Planning/Development, QA và Validator.
- [x] Định nghĩa kiến trúc đích và capability boundary theo role.
- [x] Định nghĩa state/artifact, failure routing và test strategy ở mức proposal.
- [x] Đối chiếu lần cuối từng nhận định với code/tests hiện hành.
- [x] Review không trùng ownership với TypeScript full-stack Chặng B và Phase 5.
- [x] Chủ dự án duyệt Gate 0 trước runtime implementation.

Kết quả review và chỉnh sửa bắt buộc được ghi trong
[Gate 0 Review](gate-0-review.md). Gate 0 chỉ đóng ambiguity; không tự cho phép
cutover bất kỳ role nào.

## L1 — Shared Role-loop Foundation

Mục tiêu: chuẩn hóa mechanics dùng chung quanh Developer loop hiện có mà chưa đổi
behavior Phase 1-4.

- [ ] Phát hành schema/version cho session, capability policy, tool call/result,
  completion và failure classification.
- [ ] Tách mechanics transcript/budget/journal/pause-resume khỏi Developer-specific
  action handling thành core dùng lại được.
- [ ] Tạo role-scoped registry; registry construction chỉ từ code-reviewed profile
  và workflow context.
- [ ] Chuẩn hóa tool result có evidence refs, retryability, duration và bounded output.
- [ ] Thêm optimistic hash, stable call ID và ambiguous-result recovery dùng chung.
- [ ] Ghi sanitized role/tool events nhưng không persist chain-of-thought.
- [ ] Bọc Developer hiện hành qua contract mới với behavior parity trước khi mở role khác.

Acceptance:

- Developer baseline vẫn chạy cùng HTTP actions/state transitions.
- Restart giữa một read hoặc write action không thực thi mutation hai lần.
- Policy không thể được mở rộng bởi prompt/model output.
- Run cũ không có session contract mới vẫn đọc/audit được.

## L2 — Planner/Developer Execution Loop

Mục tiêu: chuyển JSON-action executor hiện tại thành vòng inspect -> implement ->
verify -> diagnose -> complete có execution units thật.

- [ ] Phase 3 phát hành ordered execution units theo dependency/component thay vì
  một task tổng quát; bind unit với requirement/design/path/signature/test intent.
- [ ] Thêm read-only CodeGraph query tool và giữ plain search làm fallback có nhãn.
- [ ] Thêm `verification.run_stage(stage_id)`; chỉ stage trong immutable profile
  và unit policy được gọi.
- [ ] Persist current unit, completed units, evidence và current hashes để resume.
- [ ] Completion gate từ chối `complete` nếu unit chưa đủ mapping hoặc verification.
- [ ] Phân biệt code/test production defect, upstream plan/design conflict và
  environment failure trước khi tạo repair feedback.
- [ ] Cutover theo feature flag/session binding; xóa legacy executor chỉ sau parity
  regression và controlled live smoke.

Acceptance:

- Một feature nhiều file được chia thành ít nhất hai unit có dependency rõ.
- Developer đọc file liên quan ngoài direct error path nhưng chỉ ghi exact allowed paths.
- Verification fail được quan sát và sửa trong cùng session khi thuộc Developer.
- Upstream conflict không bị che bằng code workaround.

## L3 — QA Investigation Loop

Mục tiêu: QA tự tạo, chạy, chẩn đoán và sửa QA-owned test trước khi giao lỗi sản
phẩm cho Developer.

- [ ] Tạo QA capability policy từ immutable `QaTestPolicy` và project profile.
- [ ] Cho QA inspect bounded code/trace và quản lý chỉ test path do run sở hữu.
- [ ] Mở approved QA verification stage qua registry, không nhận raw command.
- [ ] Thêm deterministic pre-classification cho syntax/import/setup/environment.
- [ ] Dùng LLM classification chỉ cho semantic ambiguity sau deterministic evidence.
- [ ] Route `qa_test_defect` về QA, `product_defect` về Developer,
  `upstream_ambiguity` về human checkpoint và environment về pause.
- [ ] Giữ ổn định scenario/test identity qua repair; không sinh lại cả suite nếu
  chỉ một scenario cần sửa.
- [ ] Chặn Developer sửa QA-owned acceptance tests trong product repair.

Acceptance:

- Một test sai import được QA tự sửa mà không gọi Developer.
- Một behavior implementation sai tạo Developer feedback với test/evidence cố định.
- QA không thể làm yếu assertion hoặc ghi ra ngoài policy để đạt pass.
- Machine-readable report chứng minh đúng scenario/current hashes đã chạy.

## L4 — Designer Architecture Loop

Mục tiêu: Designer điều tra kiến trúc hiện hữu và sửa design theo delta có phạm vi.

- [ ] Cấp read-only repo/CodeGraph/artifact tools theo project profile.
- [ ] Định nghĩa structured design delta cho component, class, sequence, API và data projections.
- [ ] Harness apply/render candidate; model không ghi PlantUML artifact trực tiếp.
- [ ] Chạy deterministic preflight sau mỗi delta và trả finding có subject ID.
- [ ] Giữ semantic consensus độc lập sau khi deterministic candidate hợp lệ.
- [ ] Detect repeated delta/fingerprint và oscillation; không rewrite toàn bộ để né blocker.
- [ ] Route requirement gap về Phase 5/human boundary, không tự sửa approved requirement.

Acceptance:

- Brownfield design trích dẫn repo evidence thay vì phát minh component/API.
- Repair một blocker không làm mất mapping đã pass trước đó.
- Design delta replay cho cùng input tạo graph nhất quán.

## L5 — Requirement Context Loop

Mục tiêu: Requirement có thể thu thập evidence có kiểm soát từ nhiều nguồn và chỉ
hỏi người dùng khi evidence thực sự thiếu/mâu thuẫn.

- [ ] Chuẩn hóa source inventory và read/search adapters cho fixture, Jira, docs và repo evidence.
- [ ] Tạo provenance ledger bind source revision/hash, excerpt reference và requirement IDs.
- [ ] Cho Refiner đề xuất bounded requirement delta; Python tiếp tục sở hữu stable ID và validation.
- [ ] Phân biệt missing evidence, source unavailable, source conflict và business decision.
- [ ] Clarification phải nêu evidence đã kiểm tra, affected IDs và điều gì human cần quyết định.
- [ ] Context thay đổi sau approval không tự mutation run; giao Phase 6 invalidation trong tương lai.

Acceptance:

- Requirement có thể tìm evidence trước khi hỏi clarification.
- Evidence mâu thuẫn tạo human decision, không chọn nguồn tùy ý.
- Mọi canonical requirement truy ngược được source/assumption.

## L6 — Validator Evidence Upgrade

Mục tiêu: thống nhất evidence vocabulary và routing nhưng giữ từng Validator độc lập,
read-only và deterministic-first.

- [ ] Tạo read-only Validator registry cho artifact, repo, CodeGraph, AST và verification evidence.
- [ ] Chuẩn hóa finding fields: canonical subject, evidence, origin, severity,
  repair owner, repairability, confidence/quorum và snapshot fingerprint.
- [ ] Giữ rule blocker bất biến qua semantic review; disputed finding không block.
- [ ] Dùng failure classification chung để route về đúng Requirement, Designer,
  Planner/Developer, QA, environment hoặc human owner.
- [ ] Giữ per-validator budgets và consensus policy; không tạo một mega-validator.
- [ ] Public quality report chỉ lộ summary/evidence refs đã sanitize.

Acceptance:

- Validator bị từ chối mọi write tool.
- Cùng snapshot và deterministic inputs tạo cùng hard findings.
- Inconclusive không tự tiêu repair iteration hoặc tạo producer feedback.

## L7 — Integrated Rollout Và Cutover

- [ ] Chạy shadow comparison giữa baseline và role loop mới theo từng role.
- [ ] Thu metrics: pass/repair convergence, false routing, tool calls, tokens,
  wall time, resume success và policy violations.
- [ ] Controlled TypeScript full-stack E2E: Requirement -> Design -> Plan ->
  UI/API/DB code -> QA -> cross-layer trace -> Draft PR.
- [ ] Thử transport interruption, Docker/service failure, stale hash và restart.
- [ ] Cutover từng role độc lập; persisted session không đổi executor giữa chừng.
- [ ] Chỉ xóa fallback sau regression, controlled live run và rollback review.

## Cutover Và Rollback Boundary Theo Role

| Lát | Cutover | Rollback boundary |
| --- | --- | --- |
| L1 Shared core | Bọc Developer hiện hành với behavior parity trước, chưa mở tool mới | Tắt admission vào contract mới; session đã bind executor/version nào thì không đổi giữa chừng |
| L2 Planner/Developer | Bật theo feature flag và session contract version cho run mới | Ngừng tạo session L2 mới; session đang chạy hoàn tất/pause theo executor đã bind hoặc chờ operator xử lý |
| L3 QA | Bật QA loop cho run mới sau shadow comparison | Run mới quay về generator + outer Verification baseline; QA-owned artifact đã phát hành không chuyển quyền cho Developer |
| L4 Designer | Bật theo run/phase mới, giữ versioned design artifact | Run mới quay về draft/update_design hiện hành; không rewrite artifact của run đang mở để rollback |
| L5 Requirement | Bật context loop trước Refiner cho run mới | Run mới quay về source snapshot + Refiner V2; stable requirement ID và checkpoint cũ không đổi |
| L6 Validator | Chạy shadow trước khi finding mới có decision authority | Thu hồi decision authority của evidence loop, quay về deterministic/hybrid pipeline hiện hành; candidate artifact vẫn read-only |

Rollback ở đây là dừng admission/cutover cho run mới, không phải đổi executor hoặc schema
giữa một persisted session. Phase 5 vẫn là owner duy nhất của checkpoint reopen.

## Dependency Với Các Roadmap Khác

| Hạng mục | Dependency |
| --- | --- |
| TypeScript B1 System Design Bundle | L4 design delta/capability contract |
| TypeScript B2 Component-aware Plan | L2 execution unit contract |
| TypeScript B3 Multi-stage Verification | L1 tool contract; cung cấp stage cho L2/L3 |
| TypeScript B4 Browser QA | L3 QA loop và environment classification |
| TypeScript B5 Cross-layer Trace | L6 evidence/finding contract |
| Phase 5 Request Change | Cần cho upstream checkpoint reopen từ L4/L5/L6 |
| Phase 6 Dynamic Context | Dùng provenance ledger L5, không nằm trong scope hiện tại |

Java runtime cleanup của Chặng A không thuộc roadmap này và không được đánh dấu
hoàn tất bởi bất kỳ slice L1-L7 nào.

## Lát Dự Kiến Đầu Tiên Khi Roadmap Được Kích Hoạt

Lát nhỏ đầu tiên là **L1.1 — canonical role session/tool-result contracts bọc
Developer hiện hành**, không thêm tool mới và không đổi default behavior. Đây mới là
định hướng/acceptance cấp roadmap; kế hoạch triển khai chi tiết cho L1.1 phải được lập và
review riêng trước khi sửa runtime. Lát này chỉ hoàn tất khi manifest/journal cũ được ánh
xạ rõ, parity tests pass và rollback không làm active session đổi executor.

Roadmap hiện đang được giữ ở trạng thái đã note. A4 Java runtime-only cleanup
và Chặng A đã hoàn tất, nhưng mốc này không tự xếp lịch L1.1; chỉ lập kế hoạch
chi tiết hoặc triển khai khi chủ dự án chủ động kích hoạt lại roadmap.
