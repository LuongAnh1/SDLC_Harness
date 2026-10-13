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

## 2026-10-13 - Slice 3.5: probe DeepSeek raw output cho Developer.plan

Mục tiêu của lát này là kiểm tra trực tiếp DeepSeek đang trả gì khi tạo
implementation plan, nhưng không mutate `.runtime` và không sync tool.

Đã hoàn thành:

- Thêm script `scripts/probe_developer_plan_deepseek.py` để dựng đúng prompt
  Developer.plan từ artifact của run hiện có, gọi DeepSeek thật và lưu
  `.prompt.txt`, `.response.json`, `.content.txt`, `.parse.json` trong `.debug/`.
- Thêm `.debug/` vào `.gitignore` để raw prompt/response không lọt vào git.
- Probe live cho run `run_20261013_143231_8e1a39d4` cho thấy DeepSeek bắt đầu
  trả JSON đúng, nhưng response bị cắt với `finish_reason=length` ở 8192
  completion tokens.
- Nguyên nhân chính không phải model thêm prose, mà là prompt yêu cầu trả quá
  nhiều `design_mappings` và rule cũ bảo model preserve toàn bộ base mapping.
- Chỉnh prompt Developer.plan: Python giữ base mapping đầy đủ, LLM chỉ trả phần
  enrich/override ngắn; giới hạn repo evidence, implementation steps, proposed
  files và test intents.
- Giảm CodeGraph evidence gửi vào prompt từ 24000 xuống 12000 ký tự để giảm khả
  năng output lan man và tiết kiệm token.
- Probe live sau khi rút prompt trả `finish_reason=stop`, `completion_tokens=2649`
  và parse OK với 5 implementation steps, 4 proposed files, 5 mapping overrides,
  5 test intents.

## 2026-10-13 - Slice 3.6: khóa Phase 3 reporting qua Niko

Mục tiêu của lát này là để Niko đọc được checkpoint `implementation_plan_review`
và trả lời ngắn gọn cho người dùng, thay vì chỉ hiển thị trạng thái kỹ thuật hoặc
danh sách artifact quá dài.

Đã hoàn thành:

- Niko nhận diện `waiting_implementation_plan_review` là trạng thái chờ review
  implementation plan trước khi sang code/PR.
- Reply SDLC của Niko hiển thị link Confluence/Jira từ `last_sync.targets`, mã run,
  checkpoint `implementation_plan_v3`, số vòng repair và số issue còn mở.
- Artifact list trong reply Niko được gom theo priority để implementation plan,
  implementation plan review, design artifact và requirement packet hiện trước;
  phần còn lại được gộp bằng `+N artifact khác`.
- Niko không hứa mở Phase 4 trong lát này. Với checkpoint implementation plan,
  reply chỉ nhắc anh review plan và ghi rõ Phase 4 tạo code/PR chưa mở trong Niko.
- Smoke CLI:
  - `rtk proxy python -m sdlc_harness.cli get-status --run-id run_20261013_143231_8e1a39d4`
  - Kết quả: `current_phase=implementation_planning`,
    `status=waiting_implementation_plan_review`,
    `last_sync.phase=implementation_plan_review`, sync Confluence/Jira đều `synced`.
- Smoke qua NikoApp:
  - Prompt: `run_20261013_143231_8e1a39d4 den dau roi em?`
  - Route: `sdlc_get_status`.
  - Reply có link implementation plan Confluence, Jira comment, repair iterations `2`,
    open issues `0`, và checkpoint `implementation_plan_v3`.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py -q` trong `SDLC_Harness`
    -> 25 passed.
  - `rtk proxy python -m pytest tests/test_niko_app.py tests/test_sdlc_client.py -q`
    trong `Niko_Agent` -> 31 passed.

Lưu ý:

- Live Telegram chưa cần chạy lại ở lát này vì smoke đã đi qua `NikoApp.handle_message`
  và gọi `SDLC_Harness` thật. Nếu muốn khóa tuyệt đối trước Phase 4, có thể nhắn bot
  hỏi status run trên Telegram thêm một lần.
- Artifact/comment có run id lạ trên Jira/Confluence trước đó nhiều khả năng là từ
  một lần chạy thử cũ đã bắn request lên Atlassian nhưng local `.runtime` bị xóa sau đó.
  Phase sau nên cân nhắc sync ledger/idempotency rõ hơn để live test dễ kiểm soát.

## 2026-10-13 - Slice 3.7: live Telegram/Niko khóa implementation plan checkpoint

Mục tiêu của lát này là khóa Phase 3 bằng một lượt live test từ Niko/Telegram thay vì chỉ smoke CLI: bắt đầu từ Jira issue, duyệt requirement, duyệt design, để SDLC_Harness sinh implementation plan và sync checkpoint review.

Đã hoàn thành:

- Run lỗi tham chiếu: `run_20261013_155407_9a45a4de` bị timeout khi Niko vẫn cấu hình `SDLC_HARNESS_TIMEOUT_SECONDS=120`. Run này kẹt ở `implementation_plan_repairing`, chưa mở checkpoint mới và chưa sync Phase 3.
- Runtime config Niko đã tăng timeout SDLC lên `300s` để đủ thời gian cho approve design -> implementation plan khi Developer/Validator repair nhiều vòng.
- Run pass: `run_20261013_160421_f661babf` đi tới `current_phase=implementation_planning`, `status=waiting_implementation_plan_review`.
- Artifact cuối là `implementation_plan_v4`; `implementation_plan_review` pass với `repair_iterations=3`, `issue_count=0`, `last_error=null`.
- Real sync Phase 3 pass với `last_sync.phase=implementation_plan_review`, `status=synced`, Confluence page `1048768` và Jira comment `10079`.
- Niko runtime log ghi đủ route `sdlc_start_from_jira` và hai lần `sdlc_approve_checkpoint`; lệnh approve design hoàn tất trong timeout 300s.

Kết luận:

- Phase 3 hiện đã khóa ở mức implementation plan checkpoint live qua Niko.
- Phase 4 code/test/Draft PR vẫn chưa mở; bước tiếp theo là approve implementation plan để bắt đầu code trong target repo.
