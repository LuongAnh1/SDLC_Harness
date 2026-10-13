# Phase 3 Progress

Tài liệu này ghi lại tiến độ triển khai Phase 3 theo từng lát nhỏ để tránh nhầm
giữa phần đã chạy được và phần vẫn nằm trong kế hoạch.

## 2026-10-13 - Slice 1: mở Phase 3 và sinh implementation plan

Mục tiêu của lát này là nối design checkpoint đã duyệt sang Phase 3, sinh
implementation plan, và chưa đụng vào code thật trong target repo.

Đã hoàn thành:

- Thêm dispatcher `CheckpointApprovalWorkflow` để `approve-checkpoint` tự chọn
  Phase 2 hoặc Phase 3 theo `status` hiện tại của run.
- Thêm `PhaseThreeWorkflow` cho trạng thái `waiting_design_approval`.
- Đọc approved `requirement_packet`, `design_artifact`, trace và graph refs từ
  state Phase 2.
- Thêm config target repo:
  - `SDLC_TARGET_REPO_PATH`
  - `SDLC_TARGET_REPO_REMOTE`
  - `SDLC_TARGET_BASE_BRANCH`
  - `SDLC_IMPLEMENTATION_BRANCH_PREFIX`
- Thêm guard target repo:
  - không được trỏ nhầm vào repo `SDLC_Harness`;
  - target phải là git repo;
  - remote `origin` phải khớp config;
  - worktree phải sạch trước khi harness sinh code ở slice sau.
- Sinh artifact `implementation_plan_v1.md`.
- Ghi state `current_phase=implementation_planning`,
  `status=waiting_implementation_plan_review`,
  `pending_checkpoint.type=implementation_plan_review`.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py`
  - `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py`

Chưa làm trong lát này:

- Chưa tạo branch trong target repo.
- Chưa sinh hoặc sửa code.
- Chưa tạo commit.
- Chưa chạy Tester/code trace.
- Chưa tạo Draft PR.
- Chưa có Validator.review_plan thật cho implementation plan.
- Chưa sync implementation plan lên Jira/Confluence.
- GitHub thuộc Phase 4, nên Phase 3 chưa sync GitHub.

## 2026-10-13 - Slice 2: Developer.plan có cấu trúc và Validator.review_plan

Mục tiêu của lát này là biến implementation plan từ bản nháp đọc được thành
checkpoint có thể kiểm soát bằng graph đã duyệt.

Đã hoàn thành:

- Thêm module `DeveloperPlanAgent` cho Phase 3.
- Sinh thêm `implementation_plan_structured_v1.json` để lưu mapping máy đọc được.
- `implementation_plan_v1.md` có bảng design element -> planned code target,
  test intent, assumptions/open questions và out-of-scope.
- Thêm `ImplementationPlanValidator` kiểm coverage, traceability,
  executability và scope.
- Sinh `implementation_plan_review_v1.json` và lưu vào
  `quality_reports.implementation_plan_review`.
- Nếu Validator fail, workflow chuyển `blocked`, không mở checkpoint duyệt plan.
- Kiểm target repo vẫn chỉ đọc/guard; không tạo branch, không sửa file và không
  tạo PR trong Phase 3.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py`
  - `rtk proxy python -m pytest`

Chưa làm trong lát này:

- Chưa sync implementation plan lên Jira/Confluence.
- Chưa có repair loop bằng LLM cho plan fail; V1 hiện block rõ bằng report.
- Chưa cho approve `implementation_plan_review` để sang Phase 4.

## 2026-10-13 - Slice 3: sync implementation plan lên Jira/Confluence

Mục tiêu của lát này là hoàn thiện phần tool sync của Phase 3: sau khi
Developer.plan và Validator.review_plan pass, implementation plan phải được đẩy
đến nơi review và state phải ghi `last_sync`.

Đã hoàn thành:

- Thêm `mock_sync_implementation_plan_checkpoint(...)`.
- Thêm `real_sync_implementation_plan_checkpoint(...)` cho Jira/Confluence.
- Thêm `sync_implementation_plan_checkpoint(...)` trong sync runner.
- `PhaseThreeWorkflow` gọi sync v3 sau khi Validator.review_plan pass và trước
  khi mở checkpoint `waiting_implementation_plan_review`.
- `sync-run` ưu tiên checkpoint `implementation_plan_review`, không rơi ngược về
  design hoặc requirement.
- Ghi `.runtime/runs/<run_id>/sync/tool_sync_report_v3.json` với
  `phase=implementation_plan_review`.
- Real sync hỗ trợ re-sync idempotent bằng `PUT` khi report v3 đã có
  `external_id`; Phase 3 không reuse target Phase 2 để tránh page/comment design
  bị biến thành implementation plan.
- Nếu Jira comment v3 bị xóa thủ công, re-sync tạo comment mới và ghi lại
  `external_id`.
- Partial failure vẫn giữ local artifacts và trả `partial_failed`.
- Phase 3 vẫn không tạo GitHub branch, commit hoặc PR.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py`
  - `rtk proxy python -m pytest`

Chưa làm trong lát này:

- Chưa cho approve `implementation_plan_review` để sang Phase 4.
- Niko reporting có thể cần polish nếu muốn hiển thị link Confluence/Jira đẹp
  hơn trong Telegram.

## 2026-10-13 - Slice 3.1: polish Confluence implementation plan review

Mục tiêu của lát này là sửa trải nghiệm đọc implementation plan trên Confluence.
Trước đó page dùng raw markdown nên bảng mapping bị hiện như text thường và
reviewer khó hiểu Phase 4 sẽ triển khai gì.

Đã hoàn thành:

- Confluence implementation plan page render từ
  `implementation_plan_structured_v1.json` thay vì phụ thuộc Markdown parser thô.
- Thêm các section dễ review: Decision Needed, Implementation Summary,
  Execution Steps, Code Targets, Test Plan, Validator Review Summary,
  Assumptions/Open Questions và Raw Markdown Artifact.
- Code Targets được group theo file/method target, kèm planned work, evidence và
  test focus để người đọc thấy rõ sẽ sửa ở đâu.
- Raw markdown artifact được giữ trong code macro để audit, không còn làm bảng
  Markdown vỡ layout ở phần review chính.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py`
  - `rtk proxy python -m pytest`

## 2026-10-13 - Slice 3.2: Validator.review_plan hybrid LLM + repair loop

Mục tiêu của lát này là chuyển kiểm tra implementation plan từ rule-only sang
hybrid: rule hard gate giữ an toàn graph, LLM đọc ngữ cảnh requirement/design để
đánh giá chất lượng nội dung plan, rồi Developer có thể repair trong giới hạn cấu
hình trước khi workflow bị `blocked`.

Đã hoàn thành:

- Thêm config `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`, mặc định `2`, có thể truyền
  qua CLI `approve-checkpoint --implementation-plan-repair-limit`.
- `PhaseThreeWorkflow` sinh/review plan theo vòng:
  `Developer.plan -> Validator.review_rules -> Validator.review_llm -> Developer.repair_plan`.
- Rule review vẫn chặn cứng coverage, traceability, executability và scope. Nếu
  rule fail, LLM review bị skip để không hợp thức hóa plan sai graph.
- LLM review nhận `requirement_packet`, `design_artifact`, `requirement_design_trace`,
  `class_graph`, `sequence_graph` và `structured_plan`, rồi trả JSON report.
- Mỗi vòng ghi artifact riêng: `implementation_plan_v{n}.md`,
  `implementation_plan_structured_v{n}.json`, `implementation_plan_review_v{n}.json`.
- Chỉ plan version cuối đã pass mới được sync lên Jira/Confluence và mở checkpoint
  `waiting_implementation_plan_review`.
- Confluence Validator Review Summary hiển thị thêm LLM summary, quality/test
  issue count và số vòng repair.
- Test pass:
  - `rtk python -m pytest tests/test_phase3.py tests/test_phase2.py -q`

Chưa làm trong lát này:

- Developer repair V1 vẫn tái tạo plan deterministic từ graph và ghi feedback;
  chưa dùng prompt LLM riêng để viết lại toàn bộ plan.
- Chưa approve `implementation_plan_review` để sang Phase 4 code/test/Draft PR.

## 2026-10-13 - Slice 3.3: Smart Developer dùng CodeGraph target repo

Mục tiêu của lát này là nâng Developer.plan từ deterministic mapping lên Smart
Developer: đọc target repo qua CodeGraph, đưa repo evidence vào prompt LLM, rồi
sinh implementation plan có steps/proposed files rõ hơn.

Đã hoàn thành:

- Thêm `sdlc_harness/repo_context/codegraph.py` làm adapter CodeGraph cho target
  repo.
- Phase 3 yêu cầu target repo có `.codegraph/`; nếu thiếu thì block bằng
  `target_repo_codegraph_missing`.
- Thêm CLI `prepare-target-repo --init-codegraph` để init CodeGraph ở bước setup
  riêng, không chạy ngầm trong `approve-checkpoint`.
- Dirty guard bỏ qua state `.codegraph/`, nhưng vẫn block thay đổi source/test/docs.
- `DeveloperPlanAgent.plan` nhận `repo_context`, gọi Developer LLM và lưu thêm:
  `planning_mode`, `repo_context`, `repo_evidence`, `implementation_steps`,
  `proposed_files`.
- `DeveloperPlanAgent.repair_plan` truyền lại repo context và feedback để LLM sửa
  plan theo Validator.
- Fake LLM có output Developer plan riêng để test không gọi API thật.
- Test pass:
  - `rtk python -m pytest tests/test_phase3.py` -> 21 passed.

Lưu ý tạm thời:

- V1 gửi CodeGraph evidence/source snippets của target repo sang DeepSeek để lập
  plan thông minh. Chủ dự án đã cho phép cho demo hiện tại.
- Về sau cần thêm redact/filter, cấu hình bật/tắt gửi source, hoặc local model
  cho repo nhạy cảm.

## 2026-10-13 - Slice 3.4: harden Developer.plan JSON và state lỗi

Mục tiêu của lát này là xử lý lỗi live khi DeepSeek trả output không phải JSON
thuần cho `Developer.plan`. Workflow không được để run kẹt ở
`implementation_planning` với checkpoint design cũ sau khi LLM fail.

Đã hoàn thành:

- Parser `Developer.plan` có thể lấy JSON object đầu tiên khi provider bọc thêm
  prose hoặc markdown code fence.
- Nếu provider vẫn trả output không parse được thành JSON, `Developer.plan`
  fallback về mapping deterministic từ graph đã duyệt, ghi
  `planning_mode=deterministic_graph_mapping_after_llm_parse_error_v1` và lưu
  `developer_llm.parse_error` trong structured plan. Validator vẫn kiểm plan sau
  đó, nên fallback không tự động được duyệt nếu thiếu coverage/scope.
- Khi bước `Developer.plan` hoặc `Developer.repair_plan` ném `HarnessError`,
  Phase 3 ghi state `status=failed`, `pending_checkpoint=null`,
  `last_error` có `run_id`, và chỉ còn `get_status`/`cancel_run`.
- Validator.review_plan cũng dùng parser JSON chịu được prose/fence. Nếu LLM
  review vẫn không parse được, workflow ghi review report fail để vòng repair
  hoặc block theo `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`, không làm hỏng state.
- Khi bắt đầu Phase 3 sau design approval, workflow xóa checkpoint design cũ để
  tránh UI/Niko hiểu nhầm là vẫn còn duyệt design.
- Thêm regression test cho parser JSON bọc prose/fence.
- Thêm regression test cho fallback deterministic khi LLM trả invalid JSON.
- Thêm regression test cho trạng thái lỗi Developer.plan không để lại checkpoint
  cũ.
- Thêm regression test cho Validator LLM invalid JSON tạo review report fail thay
  vì vỡ workflow.

Lưu ý test live:

- Run đã bị lỗi trước lát này cần chạy lại từ đầu hoặc được reset state thủ công
  về `waiting_design_approval` trước khi approve lại.

