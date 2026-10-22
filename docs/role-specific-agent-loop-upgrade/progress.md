# Progress And Execution Log

Tài liệu này ghi công việc khảo sát, quyết định và triển khai của roadmap
role-specific agent loops. Mốc docs không được dùng để suy ra runtime code đã hoàn tất.

## 2026-10-22 — Baseline Và Scope

- Baseline là SDLC Harness sau TypeScript profile/full-stack foundation và default
  Developer tool-loop cutover.
- Tại thời điểm khảo sát, Chặng A vẫn còn Java runtime-only cleanup;
  Chặng B full-stack chưa triển khai.
- Chủ dự án chốt tạo roadmap độc lập, bao phủ toàn bộ Requirement, Designer,
  Planner/Developer, QA và Validator.
- Phạm vi hiện tại là docs-only; không đổi workflow, API, prompt hoặc config.

## 2026-10-22 — Current-flow Survey

- Dùng CodeGraph trước, sau đó đọc call sites cụ thể của Phase 1-4.
- Xác nhận Requirement V2 là structured generation với Python-owned validation
  và bounded repair/clarification.
- Xác nhận Designer dùng outer producer-validator repair loop, deterministic
  preflight và semantic consensus; Designer không tự gọi repo/design tools.
- Xác nhận Developer mặc định dùng JSON-action tool loop có exact path, manifest,
  journal, rollback và pause/resume nhưng chưa tự chạy verification trong loop.
- Xác nhận QA sinh suite one-shot, Harness apply/run, runtime fail hiện route sang
  Developer trước khi Validator và QA chạy lại.
- Xác nhận Validator là deterministic hoặc hybrid read-only; hướng cải tiến là
  evidence tools, không phải mutation autonomy.

## 2026-10-22 — Owner Decisions

- Chọn roadmap độc lập thay vì Chặng C của TypeScript full-stack.
- Bao phủ toàn bộ role thay vì chỉ bốn role còn lại hoặc QA-only.
- Giữ TypeScript full-stack làm acceptance target nhưng không hard-code loop theo stack.
- Giữ Harness ownership cho state/checkpoint/artifact/policy.
- Giữ Validator read-only và thêm QA-owned repair boundary.

## 2026-10-22 — Docs Authored

- Tạo index, current-flow audit và target architecture.
- Tạo capability contract cho Requirement, Designer, Planner/Developer, QA và Validator.
- Tạo session/state/artifact/idempotency/failure-routing proposal.
- Tạo implementation roadmap Gate 0 + L1-L7 cùng dependency matrix.
- Tạo test/evaluation strategy và Gate 0 checklist.
- Liên kết roadmap mới từ roadmap tổng và tài liệu TypeScript full-stack.

## 2026-10-22 — Gate 0 Technical Review

- Đối chiếu tài liệu với baseline commit `a5357f6` và call site/test hiện hành của
  Requirement, Designer, Planner/Developer, Verification Runner, QA và Validator.
- Sửa mô tả Developer budget: hiện có giới hạn số action cùng output/context mỗi lần
  gọi, nhưng chưa có cumulative token/time budget cho toàn session.
- Làm rõ resume hiện giữ filesystem/journal và archive attempt cũ, không replay toàn bộ
  working transcript cho lượt model tiếp theo.
- Xác nhận QA runtime failure hiện vẫn route chung về Developer; việc phân loại
  `qa_test_defect` và `product_defect` là thay đổi tương lai của L3, không phải behavior
  đã tồn tại.
- Bổ sung cutover/rollback boundary riêng cho L1-L6; persisted session không đổi executor
  giữa chừng và Phase 5 vẫn sở hữu checkpoint reopen.
- Chạy targeted regression: **204 passed** trong **155.17 giây**.
- Kiểm tra Markdown relative links và `git diff --check`: đạt.
- Kết luận kỹ thuật ban đầu: `ready_for_owner_approval`; chưa có runtime slice nào được triển khai.

## 2026-10-22 — Gate 0 Owner Approval Và Scheduling Boundary

- Chủ dự án duyệt Gate 0 để ghi nhận roadmap và boundary.
- Approval không xếp lịch L1.1 và không có nghĩa kế hoạch chi tiết L1.1 đã được lập.
- Roadmap role-specific agent loop được park sau Gate 0; không triển khai runtime code.
- Repo quay lại A4 Java runtime-only cleanup trong roadmap TypeScript full-stack
  trước khi xem xét bất kỳ runtime slice L1-L7 nào.

## 2026-10-22 — TypeScript Chặng A Hoàn Tất

- A4 Java runtime-only cleanup đã hoàn tất tại commit `80ee0f9`.
- Trạng thái roadmap role-specific agent loop không tự thay đổi theo mốc này:
  Gate 0 đã duyệt, roadmap vẫn park và L1.1 chưa có kế hoạch chi tiết.

## 2026-10-22 — Điều Chỉnh Upstream-first Và Vertical Graph

- Chủ dự án quyết định không nâng Developer tool-loop trước; Developer hiện hành
  được giữ làm baseline trong khi contract thượng nguồn được làm rõ.
- Rà lại Phase 2 và xác nhận Designer hiện nhận requirement cùng component-routing
  directive từ profile, nhưng chưa khảo sát target repo/CodeGraph/schema/route để
  phân biệt AS-IS với TO-BE.
- Đổi roadmap từ thứ tự shared-core/Developer-first sang Requirement scenario và
  surface -> frozen workspace/repo discovery -> repo-aware Designer -> vertical
  trace Validator -> Plan/Developer -> QA.
- Bổ sung tài liệu [Vertical-slice Trace Graph](vertical-slice-trace-graph.md):
  UI/API/rule/data là các cột trong Design/Code, không phải phase mới; mỗi
  scenario là một directed subgraph có thể rẽ nhánh.
- Chốt canonical graph do Python kiểm toàn cục, còn LLM chỉ nhận bounded subgraph
  theo scenario/finding. Trace graph liên kết CodeGraph bằng anchors thay vì sao
  chép toàn bộ call graph.
- Bổ sung contract định hướng cho scenario/surface, repo landscape, typed design
  graph, vertical trace graph và trạng thái `reuse|modify|add|no_change`.
- Đây là thay đổi docs-only; không có runtime code, prompt, API hoặc config nào
  được triển khai bởi lần cập nhật này.

## Trạng Thái Tiếp Theo

- Gate 0 đã duyệt; chưa có runtime slice L1-L7 nào được đánh dấu hoàn tất.
- Chặng A đã hoàn tất; thứ tự roadmap mới đã được ghi nhận nhưng không tự kích
  hoạt runtime implementation.
- Khi chủ dự án đưa roadmap trở lại lịch, phần đầu tiên cần lập kế hoạch chi tiết
  là L1.1 Requirement scenario/surface contract, không phải bọc Developer loop.

## Mẫu Ghi Cho Các Lần Sau

```text
## YYYY-MM-DD — <slice hoặc quyết định>

- Baseline/đầu vào:
- Phạm vi đã làm:
- Quyết định:
- File/artifact/state thay đổi:
- Test/evidence:
- Việc chưa làm:
```
