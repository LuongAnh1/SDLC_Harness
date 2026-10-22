# Implementation Plan

> Đây là roadmap triển khai tương lai. Việc tạo hoặc sửa bộ tài liệu không đồng
> nghĩa các runtime slice bên dưới đã hoàn tất.

## Quyết Định Thứ Tự Triển Khai

Roadmap dùng thứ tự **upstream-first**:

~~~text
Requirement scenario/surface contract
-> frozen read-only workspace + repo discovery
-> repo-aware Designer + vertical design graph
-> vertical trace Validator
-> component-aware Plan + Developer execution loop
-> QA + multi-stage verification
-> integrated rollout
~~~

Lý do: Developer chỉ có thể sửa đúng khi plan biết component; plan chỉ đúng khi
design phản ánh code hiện hữu; design chỉ đúng khi Requirement đã làm rõ scenario
và surface cần tác động. Developer tool-loop đang chạy được giữ làm baseline và
sẽ được điều chỉnh sau khi các contract thượng nguồn ổn định.

## Gate 0 — Docs-first Review

- [x] Ghi current-flow audit cho Requirement, Design, Planning/Development, QA và Validator.
- [x] Định nghĩa kiến trúc đích và capability boundary theo role.
- [x] Định nghĩa state/artifact, failure routing và test strategy ở mức proposal.
- [x] Đối chiếu lần cuối từng nhận định với code/tests hiện hành.
- [x] Review không trùng ownership với TypeScript full-stack Chặng B và Phase 5.
- [x] Chủ dự án duyệt Gate 0 trước runtime implementation.
- [x] Bổ sung vertical-slice trace graph và đổi roadmap sang upstream-first.

Kết quả review được ghi trong [Gate 0 Review](gate-0-review.md). Gate 0 chỉ đóng
ambiguity tài liệu; không tự cho phép cutover bất kỳ role nào.

## L1 — Requirement Scenario And Surface Contract

Mục tiêu: làm rõ use case thành các scenario có thể trace và xác định lớp chức
năng nào phải được xem xét trước khi thiết kế.

- [ ] Phát hành schema additive cho scenario, acceptance criterion và stable ID.
- [ ] Với mỗi scenario, ghi trạng thái `required|not_applicable|clarification_required`
  cho UI, API, business rule và data, kèm lý do.
- [ ] Chuẩn hóa provenance ledger cho fixture, Jira, docs và approved evidence.
- [ ] Cấp bounded source read/search tools; không cấp production-code mutation.
- [ ] Cho Refiner đề xuất requirement delta; Python tiếp tục sở hữu stable ID,
  validation và canonical mutation.
- [ ] Phân biệt missing evidence, source unavailable, source conflict và business
  decision trước khi hỏi clarification.
- [ ] Phát hành các session/tool-result primitives tối thiểu cần cho Requirement
  loop; chưa trừu tượng hóa mọi role ngay từ đầu.

Acceptance:

- Mỗi use case có ít nhất một scenario và acceptance criterion có thể kiểm tra.
- Surface vắng mặt phải là `not_applicable` có lý do, không phải bị bỏ quên.
- Requirement có thể tìm evidence trước khi hỏi người dùng.
- Requirement không đoán file/class/route cụ thể và không sửa target repo.

## L2 — Frozen Workspace And Repo Discovery

Mục tiêu: tạo ảnh chụp AS-IS đáng tin cậy để Designer không thiết kế chỉ từ
requirement hoặc project profile hard-code.

- [ ] Tạo/freeze run workspace read-only cho bước khảo sát sau Requirement approval.
- [ ] Inventory manifest, package manager, workspace layout, source/test roots,
  route, UI entry point, database schema/migration và verification config.
- [ ] Dùng CodeGraph khi repo đã có index; dùng bounded search/AST adapter làm
  fallback và ghi rõ provenance.
- [ ] Phát hành `repo_landscape_v1` với snapshot SHA/hash, discovered stack,
  component map, symbol anchors và uncertainty.
- [ ] Phân biệt profile expectation với observed repository evidence.
- [ ] Fail closed hoặc yêu cầu human decision khi target manifest và repo thực tế
  mâu thuẫn đáng kể.

Acceptance:

- Cùng snapshot tạo cùng deterministic inventory.
- Designer có thể biết component/API/data nào đã tồn tại trước khi đề xuất TO-BE.
- Discovery không sửa target repo và không tự khởi tạo project.
- Read-only là quyền tool của L2/L3; base snapshot được giữ bằng SHA/hash, còn
  Developer ở L5 mới được cấp write scope và phát hành evidence version mới.
- Thiếu CodeGraph không làm mất khả năng khảo sát, nhưng fallback được ghi nhãn.

## L3 — Repo-aware Designer And Vertical Design Graph

Mục tiêu: Designer thực hiện AS-IS -> GAP -> TO-BE và phát hành design có thể lần
theo từng scenario qua UI/API/rule/data.

- [ ] Cấp read-only artifact/repo/CodeGraph/schema/route/test tools theo profile.
- [ ] Định nghĩa typed design nodes và edges cho UI interaction/state, API
  contract, business rule/service, data operation và interaction flow.
- [ ] Mỗi design element ghi disposition `reuse|modify|add|no_change` cùng repo
  evidence hoặc lý do.
- [ ] Designer chỉ đề xuất structured delta; Harness apply vào candidate graph.
- [ ] Render component/API/data/sequence/class/state projection từ canonical graph
  theo nhu cầu của use case; không bắt buộc mọi view.
- [ ] Chạy deterministic preflight sau mỗi delta và semantic review độc lập sau
  khi candidate hợp lệ.
- [ ] Detect repeated delta/fingerprint và oscillation; route requirement gap về
  human/Phase 5 boundary.

Acceptance:

- Brownfield design không phát minh component/API đã có thể kiểm tra trong repo.
- Use case backend-only không bị ép tạo UI view; use case full-stack không thể bỏ
  qua một surface `required`.
- Repair một blocker giữ nguyên mapping đã pass.
- Cùng input/delta tạo canonical graph và projection nhất quán.

## L4 — Vertical Trace Validator

Mục tiêu: kiểm tra coverage theo scenario và surface từ Requirement qua Design,
Implementation tới Verification mà không biến Validator thành producer.

- [ ] Phát hành versioned trace node/edge vocabulary và graph partition key.
- [ ] Kiểm schema, dangling edge, orphan node, required-surface coverage và stale
  evidence bằng deterministic Python.
- [ ] Áp dụng phase-aware coverage: Design chưa bị đòi code/test; Plan dùng planned
  units; Implementation/QA mới lần lượt đòi code anchors và execution evidence.
- [ ] Tạo subgraph theo use case/scenario/finding cho semantic review; không nạp
  toàn bộ graph vào một prompt.
- [ ] Liên kết trace graph tới CodeGraph bằng file/symbol anchors, không sao chép
  toàn bộ call graph.
- [ ] Chuẩn hóa finding: canonical subject, evidence, severity, repair owner,
  repairability, confidence/quorum và snapshot fingerprint.
- [ ] Giữ rule blocker bất biến qua semantic review; disputed finding không block.
- [ ] Giữ từng Validator độc lập và read-only; không tạo mega-validator.

Acceptance:

- Có thể lần từ một scenario tới artifact đã tồn tại ở phase hiện hành và quay ngược lại.
- Nhánh success/error thiếu artifact bắt buộc được phát hiện đúng gate, không báo
  sớm khi phase tạo artifact đó chưa chạy.
- Cùng snapshot tạo cùng deterministic finding ledger.
- `inconclusive` không tự tạo producer repair hoặc tiêu repair iteration.

## L5 — Component-aware Plan And Developer Execution Loop

Mục tiêu: chuyển design graph thành execution units nhỏ, rồi nâng Developer loop
thành inspect -> implement -> verify -> diagnose -> complete.

- [ ] Phase 3 phát hành ordered units theo scenario/component/dependency thay vì
  một task tổng quát.
- [ ] Bind unit với requirement/design node, exact path policy, signature/contract,
  expected graph edges và test intent.
- [ ] Cho Planner đọc approved graph và repo evidence nhưng không sửa repo.
- [ ] Ánh xạ Developer manifest/journal hiện hành sang shared session/tool-result
  contract với behavior parity.
- [ ] Thêm CodeGraph query read-only và `verification.run_stage(stage_id)`.
- [ ] Persist current unit, completed units, evidence và current hashes để resume.
- [ ] Completion gate từ chối `complete` nếu mapping hoặc required verification
  của unit chưa đủ.
- [ ] Phân loại implementation defect, upstream design/plan conflict và environment
  failure trước repair.

Acceptance:

- Feature nhiều file/layer được chia thành các unit có dependency rõ.
- Developer được đọc file liên quan ngoài direct error path nhưng chỉ ghi exact
  approved paths.
- Verification fail thuộc Developer được chẩn đoán và sửa trong cùng session.
- Upstream conflict không bị che bằng workaround sai contract.

## L6 — QA Investigation And Verification Loop

Mục tiêu: QA chứng minh từng scenario/nhánh, tự sửa QA-owned test lỗi và giao
product defect về đúng Developer unit.

- [ ] Tạo QA capability policy từ immutable `QaTestPolicy` và project profile.
- [ ] Cho QA inspect bounded trace/code evidence và chỉ quản lý run-owned test.
- [ ] Mở approved unit/integration/browser stages qua registry; không nhận raw command.
- [ ] Thêm deterministic pre-classification cho syntax/import/setup/environment.
- [ ] Dùng LLM classification chỉ khi semantic ambiguity còn tồn tại.
- [ ] Route `qa_test_defect` về QA, `product_defect` về Developer,
  `upstream_ambiguity` về checkpoint và environment về pause.
- [ ] Giữ scenario/test identity qua repair; không sinh lại toàn bộ suite khi chỉ
  một nhánh lỗi.
- [ ] Gắn machine-readable execution evidence vào đúng trace nodes/current hashes.

Acceptance:

- Test sai import được QA sửa mà không gọi Developer.
- Implementation sai tạo Developer feedback với test/scenario evidence cố định.
- QA không thể làm yếu assertion hoặc ghi ngoài policy để đạt pass.
- Required success/error branches có execution evidence hợp lệ.

## L7 — Integrated Rollout And Cutover

- [ ] Chạy shadow comparison theo từng slice/role với fixture cố định.
- [ ] Thu metrics: coverage, pass/repair convergence, false routing, tool calls,
  tokens, wall time, resume success và policy violations.
- [ ] Controlled TypeScript full-stack E2E: Requirement scenario -> discovery ->
  Design graph -> Plan -> UI/API/DB code -> QA -> vertical trace -> Draft PR.
- [ ] Thử transport interruption, Docker/service failure, stale hash và restart.
- [ ] Cutover từng role/slice độc lập; persisted session không đổi executor giữa chừng.
- [ ] Chỉ xóa fallback sau regression, controlled live run và rollback review.

## Cutover Và Rollback Boundary Theo Slice

| Lát | Cutover | Rollback boundary |
| --- | --- | --- |
| L1 Requirement | Bật schema/loop cho run Phase 1 mới; giữ reader tương thích artifact cũ | Run mới quay về Requirement V2 hiện hành; checkpoint và stable ID cũ không đổi |
| L2 Discovery | Bật read-only discovery sau Requirement approval | Ngừng phát hành `repo_landscape_v1`; không làm thay đổi workspace hay approved requirement |
| L3 Designer | Bật theo run Phase 2 mới, giữ versioned design artifact | Run mới quay về draft/update_design hiện hành; không rewrite active artifact |
| L4 Validator | Chạy shadow trước khi finding mới có decision authority | Thu hồi authority của vertical finding; candidate artifact vẫn read-only |
| L5 Plan/Developer | Bật theo session contract version cho run mới | Không đổi executor của persisted session; session cũ hoàn tất/pause theo executor đã bind |
| L6 QA | Bật cho run mới sau shadow comparison | Quay về generator + outer Verification baseline; QA-owned artifact không chuyển quyền cho Developer |

Rollback là dừng admission cho run mới, không đổi executor/schema giữa một
persisted session. Phase 5 vẫn là owner của checkpoint reopen.

## Dependency Với Các Roadmap Khác

| Hạng mục | Dependency |
| --- | --- |
| TypeScript B1 System Design Bundle | L1 scenario/surface + L2 discovery + L3 design graph |
| TypeScript B2 Component-aware Plan | L3 approved graph + L5 execution-unit contract |
| TypeScript B3 Multi-stage Verification | Cung cấp stage registry cho L5/L6 |
| TypeScript B4 Browser QA | L6 QA loop và environment classification |
| TypeScript B5 Cross-layer Trace | L4 vertical trace graph/evidence contract |
| Phase 5 Request Change | Cần cho upstream checkpoint reopen từ L3-L6 |
| Phase 6 Dynamic Context | Dùng provenance ledger L1/L2, không nằm trong scope hiện tại |

Java runtime cleanup của Chặng A không thuộc roadmap này và không được đánh dấu
hoàn tất bởi bất kỳ slice L1-L7 nào.

## Lát Dự Kiến Đầu Tiên Khi Roadmap Được Kích Hoạt

Lát nhỏ đầu tiên là **L1.1 — Requirement scenario/surface contract**:

- chỉ bổ sung schema additive và deterministic validation;
- chưa mở repo tools và chưa thay đổi Designer/Developer runtime;
- giữ HTTP/checkpoint hiện hành;
- dùng fixture/tests để chứng minh stable ID, scenario coverage và trạng thái
  `required|not_applicable|clarification_required`.

Kế hoạch chi tiết L1.1 phải được review riêng trước khi sửa runtime. Shared loop
core chỉ được trích ra ở mức tối thiểu mà L1.1/L1.x thực sự cần; không bọc lại
Developer trước chỉ để tạo abstraction.
