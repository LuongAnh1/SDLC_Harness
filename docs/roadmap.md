# Roadmap SDLC Harness

Roadmap này đi theo hướng triển khai từng vertical slice. Mỗi phase phải tạo được một đoạn flow có thể chạy, có trạng thái và có nơi để Niko đọc lại.

## Phase 0 - Nền Tài Liệu Và Contract

Mục đích: chốt ranh giới repo, artifact, state và contract Niko gọi.

Tài liệu chi tiết: `docs/phase-0/README.md`. Phase này chỉ được xem là xong khi checklist trong `docs/phase-0/phase-0-checklist.md` được review và tick đủ những quyết định nền.

Việc cần làm:

- Chốt vai trò Niko và SDLC Harness.
- Chốt Java là target language ban đầu.
- Chốt một use case là đơn vị workflow đầu tiên.
- Chốt API tối thiểu: `start_run`, `get_status`, `approve_checkpoint`, `request_change`, `resume_run`, `cancel_run`.
- Chốt artifact tối thiểu: requirement packet, design artifact, trace graph, implementation artifact, test report, tool sync report.

## Phase 1 - Use Case Đến Requirement Packet

Mục đích: chạy được lát cắt đầu từ một use case eTour/SMOS/iTrust tới requirement packet có checkpoint.

Tài liệu chi tiết: `docs/phase-1/README.md`. Phase này chỉ tick hoàn thành khi CLI/runtime/artifact chạy thật, mock sync pass cho test, real Jira/Confluence sync pass khi cấu hình Atlassian, và checklist trong `docs/phase-1/phase-1-checklist.md` pass.

Luồng:

```text
start-or-get-run(UC1) hoặc start-from-jira(SDLC-1)
-> SDLC Harness kiểm tra active run theo source_ref
-> nếu cần thì load fixture hoặc fetch Jira issue
-> Requirement Refiner LLM tạo requirement_packet_v1
-> Requirement Validator kiểm tra tối thiểu
-> sync Jira/Confluence mock
-> status=waiting_requirement_approval
```

Kết quả mong muốn:

- Có `run_id`.
- Có state truy vấn được.
- Có artifact `requirement_packet_v1` do Requirement Refiner tạo.
- Có sync report.
- Niko có thể hỏi status và báo người dùng duyệt.

Nâng cấp Requirement V2 đã được chốt ở mức thiết kế tại
`docs/phase-1/requirement-v2/`: structured JSON là canonical artifact, Open
Questions có blocking policy/decision provenance, Niko hỏi từng câu trước
requirement approval, và Formal/Markdown được render từ cùng model. V2 chỉ áp
dụng cho run mới sau rollout gate; implementation hiện chưa bắt đầu.

## Phase 2 - Requirement Packet Đến Design

Mục đích: sau khi người dùng duyệt requirement, tạo design artifact và checkpoint tiếp theo.

Tài liệu chi tiết: `docs/phase-2/README.md`. Phase này chỉ bắt đầu triển khai
code sau khi đã chốt vòng Designer/Validator, state/artifact versioning và
checklist trong `docs/phase-2/phase-2-checklist.md`.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved requirement_packet_v1
-> đọc formal_use_case_v1 nếu có
-> Designer tạo design_artifact_v1
-> Parser tạo class/sequence graph từ PlantUML
-> Validator tạo requirement-design trace từ requirement/class/sequence graph
-> nếu thiếu link: Validator tạo quality report, Designer update design, rồi kiểm lại
-> sync design checkpoint theo `docs/phase-2/sync-design-checkpoint.md`
-> status=waiting_design_approval
```

Nếu Phase 2 phát hiện requirement thiếu nghiêm trọng, không tự refine requirement trong Phase 2. Workflow phải chuyển về `waiting_requirement_approval` hoặc `blocked` để người dùng sửa/duyệt lại.

Kết quả mong muốn:

- Có design artifact dễ đọc.
- Có trace requirement-design.
- Có missing-link report và quality report của Validator.
- Có trạng thái chờ duyệt design.
- Niko đọc được summary để báo lại.

## Phase 3 - Design Đến Implementation Plan

Mục đích: sau khi người dùng duyệt design checkpoint, tạo một bản kế hoạch triển
khai trước khi agent được phép sửa target repo. Phase này là checkpoint riêng của
harness, không phải bước có sẵn trong TraceDev.

Tài liệu chi tiết: `docs/phase-3/README.md`. Phase này chỉ sinh và kiểm
`implementation_plan_v1`, chưa tạo branch/code/commit/Draft PR.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved requirement/design/trace graph
-> Developer.plan tạo implementation_plan_v1
-> Validator.review_plan hybrid: rule hard gate + LLM content review
-> nếu fail và còn lượt repair: Developer.repair_plan tạo implementation_plan_v2/v3
-> sync plan lên Confluence và comment trạng thái lên Jira
-> status=waiting_implementation_plan_review
```

`implementation_plan_v1` chỉ ánh xạ từ graph hiện tại ra kế hoạch code dự kiến:
target repo/branch, file/class/method dự kiến, test intent, assumptions và out of
scope. Nó không tự sửa requirement/design graph. Nếu plan phát hiện design thiếu
hoặc mâu thuẫn, workflow phải `blocked` hoặc quay về checkpoint phù hợp.

Kết quả mong muốn:

- Có `implementation_plan_v1.md`.
- Có plan review report của Validator.
- Có sync report Phase 3 cho Jira/Confluence.
- State dừng ở `waiting_implementation_plan_review`.
- Chưa tạo branch/code/commit/Draft PR.

## Phase 4 - Implementation Plan Đến Code/Test/Draft PR

Mục đích: sau khi implementation plan được duyệt, Developer triển khai code và
unit test dựa trên plan và approved design; Harness chạy compile/unit test,
Validator kiểm traceability; nếu code/test thiếu link thì Developer repair trước;
chỉ sau khi compile/unit + trace ổn định mới chạy QA/Tester Agent kiểm
acceptance/system behavior.

Tài liệu chi tiết: `docs/phase-4/README.md`.

Luồng:

```text
approve_checkpoint(run_id)
-> đọc approved implementation_plan_v1 + approved design graph
-> sinh developer_work_packet_v1.json
-> continue-run sinh/apply code change
-> tạo branch trong target repo
-> Developer.act tạo/sửa code và unit test dựa trên plan
-> Verification Runner chạy compile/unit test hoặc ghi skipped rõ ràng
-> nếu compile/unit fail và còn vòng repair: feedback về Developer, chạy lại runner
-> Validator kiểm approved requirement -> approved design -> code -> unit test trace bằng static evidence + LLM graph review bắt buộc
-> nếu code/test thiếu so với design: refresh developer_work_packet_v{n}, feedback về Developer, chạy lại runner -> Validator trong giới hạn cấu hình
-> nếu design đã duyệt mâu thuẫn/không triển khai được: blocked/reopen design có human approval
-> QA/Tester Agent kiểm acceptance/system behavior từ use case
-> nếu QA fail và còn vòng repair: feedback về Developer, chạy lại runner -> Validator -> QA
-> tạo Draft PR trên GitHub
-> sync PR/test/trace summary về Jira và Confluence
-> status=code_review_ready
```

Kết quả mong muốn:

- Có `developer_work_packet_v1.json` và state tạm `implementation_ready` sau Slice 4.1.
- Có `code_change_report_v1.md` và state tạm `implementation_written` sau Slice 4.2.
- Có branch thật và code change thật trong target repo; commit/Draft PR là lát sau.
- Có `code_change_report_v{n}.md`.
- Có `test_report_v{n}.md`.
- Có `requirement_design_code_trace_v1.json` với `mode=hybrid_rule_llm_code_trace_v1`
  và `llm_review`.
- Có `implementation_repair_feedback_v1.json` khi trace fail vì code/test thiếu.
- Có Developer repair loop cho trace fail do code/test thiếu trước khi QA/Tester chạy;
  mỗi vòng repair refresh `developer_work_packet_v{n}`, ghi artifact v2/v3, chạy
  lại Verification Runner rồi Validator.
- Có QA/Tester Agent sinh acceptance/system test, bắt buộc execute test vừa sinh
  và ghi `qa_generated_tests_v{n}.json`, `qa_test_execution_v{n}.md`,
  `qa_test_report_v{n}.md`.
- Có vòng repair lớn QA -> Developer -> Verification Runner -> Validator -> QA;
  live DeepSeek đã hội tụ tới `qa_tested` sau một lỗi acceptance thật.
- Slice 4.8A đã live-test commit/push/Draft PR thật và ghi
  `github_code_review_v1.json`.
- Slice 4.8B đã có `tool_sync_report_v4.json`, Jira/Confluence Code Review
  checkpoint và state `code_review_ready`; live re-sync giữ nguyên remote ID.
- Slice 4.9A-C đã hoàn tất worktree/branch/lock theo run, legacy adoption và
  cleanup explicit; run hiện tại/PR #1 được adopt mà không đổi HEAD.
- Slice 4.9D đã triển khai approve Code Review: chỉ chuyển Draft PR thành Ready
  for review, ghi artifact approval và sync trạng thái `code_review_approved`.
  Merge luôn do human; request-change mở Phase 5.
- Controlled live smoke qua Niko đã đưa run `run_20261013_160421_f661babf` tới
  `code_review_approved`; PR #1 vẫn OPEN/chưa merge, Jira comment `10104` và
  Confluence page `2129921` đã nhận trạng thái sau approval.

## Platform Upgrade - API Và Ops Dashboard (Trước Phase 5)

Mục đích: thay transport Niko -> SDLC Harness từ subprocess CLI bằng HTTP API có
operation bền vững, đồng thời thêm dashboard quan sát run/flow/artifact/config.

Tài liệu chi tiết nằm tại `docs/platform-api-dashboard/README.md`. Mốc này không
đổi workflow Phase 1-4 và không chiếm Phase 5. Mốc đã hoàn thành ngày 2026-10-19:
Niko dùng HTTP API-only sau contract test và controlled live E2E; CLI tiếp tục
hoạt động độc lập cho operator/debug.

Luồng đích:

```text
Niko / Dashboard -> HTTP API -> Operation Manager -> Application Service
CLI -----------------------------------------------> Application Service
Application Service -> existing workflows/state/artifacts/sync
```

## Hướng Tiếp Theo - TypeScript Full-stack (Đang Triển Khai Chặng A)

Trước khi mở Phase 5, repo đang triển khai một hướng nâng cấp riêng cho execution
target: bỏ hard-code Java bằng language/project profile, sau đó mở rộng workflow
để một Jira issue/use case triển khai được vertical feature có frontend,
backend và database.

Tài liệu nguồn nằm tại
[`docs/typescript-fullstack-upgrade/`](typescript-fullstack-upgrade/README.md).
Hướng đã chốt dùng repo mới `SDLC_Target_TypeScript`, React/Vite, Fastify,
PostgreSQL/Prisma, Vitest và Playwright. Profile contract, target bootstrap,
controlled live smoke, default TypeScript cutover và Java runtime cleanup đã
hoàn tất. Run mới mặc định dùng `typescript_fullstack_v1` cùng
Developer tool-loop; historical Java run chỉ còn read/audit, sync artifact đã
có và cleanup workspace.

Roadmap tương lai chia hai chặng:

1. TypeScript profile foundation và target bootstrap;
2. full-stack design/planning/verification/trace cho một vertical feature.

Chặng A đã hoàn tất; Chặng B chưa bắt đầu.

## Hướng Song Song - Role-specific Agent Loops (Docs-first)

Repo có roadmap độc lập tại
[`docs/role-specific-agent-loop-upgrade/`](role-specific-agent-loop-upgrade/README.md)
để phân biệt one-shot generation, workflow repair loop và model-tool loop, rồi
nâng cấp quyền/tool/completion/failure routing theo từng vai trò Requirement,
Designer, Planner/Developer, QA và Validator.

Roadmap đã được điều chỉnh theo thứ tự upstream-first: Requirement scenario/
surface -> repo discovery read-only -> repo-aware Design -> vertical trace
Validator -> Plan/Developer -> QA. Vertical graph nối từng scenario qua các lớp
UI/API/rule/data tới code và test thay vì chỉ nối Requirement -> class/sequence ->
code symbol.

Roadmap này không phải Chặng C của TypeScript full-stack và không thay thế Phase
5/6. TypeScript full-stack là acceptance target hiện tại; dependency được nối rõ
với B1 Design Bundle, B2 Component-aware Plan, B3 Multi-stage Verification, B4
Browser QA và B5 Cross-layer Trace. Gate 0 của roadmap agent-loop đã được duyệt
để ghi nhận hướng và boundary. Roadmap vẫn đang park; Chặng A hoàn
tất không tự động xếp lịch hoặc tạo kế hoạch triển khai chi tiết cho L1.1
Requirement scenario/surface contract.

## Phase 5 - Sửa Tại Checkpoint

Mục đích: người dùng có thể yêu cầu sửa ngay tại checkpoint và hệ thống rerun đúng phase.

Ví dụ:

```text
request_change(run_id, "đổi actor thành Admin")
-> tạo requirement_packet_v2 hoặc design_artifact_v2
-> chạy Validator lại
-> sync lại tool
-> giữ status ở checkpoint tương ứng
```

Với change ở code-review checkpoint, Phase 5 phải giữ nguyên worktree, branch và
PR của run. Nếu PR đã Ready for review, Harness đưa PR về Draft trước khi sửa;
sau Developer -> Runner -> Validator -> QA và sync, run quay lại
`code_review_ready`. Harness không merge PR.

## Phase 6 - Context Động Từ Tool

Mục đích: context thay đổi ngoài checkpoint cũng được phát hiện và invalidate đúng phần.

Nguồn thay đổi:

- Jira comment mới.
- Confluence page sửa.
- PR review mới.
- Chat bổ sung acceptance criteria.

## Phase 7 - Nhiều Use Case Và Impact Analysis

Mục đích: xử lý quan hệ chéo giữa use case.

- Suy luận shared component/class từ trace links.
- Tạo impact graph.
- Khi một use case đổi, phát hiện use case khác có thể bị ảnh hưởng.
- Chạy regression/trace check cho nhóm liên quan.


## Nhật Ký Mốc Lịch Sử

> Các khối cập nhật bên dưới là snapshot theo thời điểm và được giữ để truy vết.
> Trạng thái hiện hành nằm trong các phase ở phía trên và trong
> [báo cáo đồng bộ ngày 2026-10-15](maintenance/2026-10-15-niko-sdlc-phase4-doc-sync.md).

> Cập nhật 2026-10-13: Phase 1 hiện đã có đường chạy thật từ Jira issue,
> Requirement Refiner, validator/repair loop và sync Jira/Confluence. Niko gọi
> SDLC_Harness qua CLI JSON và chỉ format kết quả cho Telegram. Phase 2
> thiết kế/coding vẫn chưa bắt đầu.

> Cập nhật 2026-10-13: đã viết tài liệu triển khai Phase 2 trong
> `docs/phase-2/`, tập trung vào vòng Designer/Validator, traceability graph,
> missing-link feedback và state/artifact versioning. Chưa triển khai code Phase 2.

> Cập nhật 2026-10-13: Phase 2 đã có lát code V0 cho
> `approve-checkpoint`: đọc requirement packet đã duyệt, sinh fake design
> artifact/trace/report, mock sync `tool_sync_report_v2` và chuyển state sang
> `waiting_design_approval`. Designer/Validator thật, repair loop, real sync và
> Niko approve route vẫn chưa triển khai.

> Cập nhật 2026-10-13: Phase 2 đã có lát V1 cho Designer/parser: Designer Agent
> dùng prompt TraceDev-style, fake mode sinh PlantUML ổn định, parser tạo
> class/sequence graph thật và workflow ghi `parser_only_v1` /
> `traceability_pending_v1`. Validator traceability LLM và repair loop vẫn là
> bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V2 cho Validator traceability:
> requirement graph, class trace, sequence trace và missing-link report được ghi
> ở mode `traceability_v1`. Repair loop `Designer.update_design`, real sync
> idempotent và Niko approve route vẫn là bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V3 cho Designer repair loop:
> Validator missing-link report được đưa ngược lại cho Designer, artifact sinh
> theo version `v1/v2/v3`, pass thì mở `design_approval`, hết 2 vòng repair vẫn
> thiếu link thì `status=blocked`. Real sync idempotent và Niko approve route
> vẫn là bước tiếp theo.

> Cập nhật 2026-10-13: Phase 2 đã có lát V4 cho cấu hình và real sync:
> `SDLC_DESIGN_REPAIR_LIMIT` điều khiển số vòng repair, `sync-run` tự chọn
> requirement/design checkpoint, real Jira/Confluence sync tạo hoặc update target
> theo `external_id`, và unit test đã khóa create/update/partial failure. Cần
> smoke real Atlassian Phase 2 và nối Niko route duyệt checkpoint.

> Cập nhật 2026-10-13: Phase 2 đã được khóa trước Phase 3 ở mức static
> approve-only. `approve-checkpoint` chạy qua Niko, Design/Validator/repair loop
> dừng ở `waiting_design_approval`, PlantUML app macro là output diagram chính,
> Mermaid để backlog, và regression test đã khóa trường hợp requirement không
> traceable bằng `status=blocked` + `last_error` rõ.

> Cập nhật 2026-10-13: đã viết lại ranh giới Phase 3 thành implementation
> planning checkpoint. Phase 3 sinh và kiểm `implementation_plan_v1`; Phase 4
> mới sở hữu branch/code/test/Draft PR.

> Cập nhật 2026-10-13: đã tạo target test repo private
> `LuongAnh1/SDLC_Target_eTour` cùng cấp với `Niko_Agent` và `SDLC_Harness`.
> Repo này là code collaboration lane ban đầu cho Phase 4.

> Cập nhật 2026-10-13: Phase 3 đã có Slice 1 cho `approve-checkpoint` từ
> `waiting_design_approval`: dispatcher chọn đúng workflow, guard target repo
> kiểm tra path/remote/dirty worktree, sinh `implementation_plan_v1.md`, ghi
> `approved_artifacts` và dừng ở `waiting_implementation_plan_review`. Chưa tạo
> branch/code/commit/test report/Draft PR.

> Cập nhật 2026-10-13: Phase 3 đã có Slice 2/3 cho Developer.plan có cấu trúc,
> Validator.review_plan và sync Jira/Confluence. Run Phase 3 hiện sinh
> `implementation_plan_v1.md`, `implementation_plan_structured_v1.json`,
> `implementation_plan_review_v1.json`, `tool_sync_report_v3.json` và ghi
> `last_sync.phase=implementation_plan_review`.

> Cập nhật 2026-10-13: Phase 3 đã chuyển `Validator.review_plan` sang hybrid
> rule + LLM. Rule gate kiểm graph/schema trước, LLM review kiểm semantic gap
> giữa requirement/design/trace và implementation plan. `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`
> điều khiển số vòng `Developer.repair_plan`; mỗi vòng ghi artifact version
> riêng và chỉ version cuối pass mới sync Jira/Confluence.

> Cập nhật 2026-10-13: Phase 4 được tách ra thành phase mới để khóa phần
> code/test/Draft PR sau khi implementation plan được duyệt.

> Cập nhật 2026-10-13: Phase 3 đã khóa bằng live Telegram/Niko run
> `run_20261013_160421_f661babf`. Niko gọi SDLC_Harness từ Jira issue, duyệt
> requirement, duyệt design, SDLC sinh `implementation_plan_v4`, Validator pass
> sau 3 vòng repair và sync Jira/Confluence ở checkpoint
> `waiting_implementation_plan_review`. Tại thời điểm khóa Phase 3, Phase 4 vẫn
> chưa mở tự động từ Niko; các lát code/test/trace/PR được triển khai sau đó.

> Cập nhật 2026-10-14: Phase 4 đã có Slice 4.1/4.2/4.3. Harness duyệt
> implementation plan thành `developer_work_packet_v1.json`, Developer.act apply
> code thật vào target repo, rồi `continue-run` chạy Verification Runner để sinh
> `test_report_v1.md`. State mới sau test pass là `implementation_tested`; code
> trace Validator, QA/Tester Agent, commit và Draft PR vẫn là các slice sau.

> Cập nhật 2026-10-14: Phase 4 đã có Slice 4.4. `continue-run` từ
> `implementation_tested` sinh `requirement_design_code_trace_v1.json`, cập nhật
> `quality_reports.implementation_traceability` và chuyển state sang
> `implementation_traced` khi static evidence + LLM graph review cùng pass.
> Cập nhật 2026-10-14: Slice 4.5 đã thêm `implementation_repair_needed` và
> `implementation_repair_feedback_v1.json` khi trace fail do code/test thiếu.
> Slice 4.6 đã nối `continue-run` từ state đó sang Developer.repair một vòng:
> repair trong `allowed_paths`, ghi `code_change_report_v2`/`test_report_v2`/
> `requirement_design_code_trace_v2`, chạy lại Verification Runner rồi Validator,
> và tôn trọng `SDLC_IMPLEMENTATION_REPAIR_LIMIT`. QA/Tester Agent, commit/Draft
> PR và sync Phase 4 vẫn là các slice sau.
> Cập nhật sau live DeepSeek 2026-10-14: repair loop đã refresh
> `developer_work_packet_v{n}` từ target repo trước khi gọi Developer.repair để
> tránh stale `current_files` từ packet v1.

> Cập nhật 2026-10-14: sau khi sửa tầng Validator evidence/code, live revalidate
> đã đưa run Phase 4 từng bị `implementation_repair_limit_exceeded` về
> `implementation_traced`. `revalidate-implementation` refresh packet từ target
> repo hiện tại, chạy lại Verification Runner và Validator, rồi chỉ mở lại
> `implementation_repair_needed` nếu vẫn còn gap repairable và còn lượt. QA/Tester
> Agent, commit/Draft PR và sync Phase 4 vẫn là các lát kế tiếp.

> Cập nhật 2026-10-14: Slice 4.7A/4.7B đã triển khai QA/Tester Agent sinh test,
> apply test vào target repo và bắt buộc execute từng class QA sau baseline test.
> Live DeepSeek `run_live_qa_repair_20261014_03` đã chứng minh nhánh acceptance
> fail -> `qa_repair_needed` -> Developer repair -> Verification Runner ->
> Validator -> QA pass, kết thúc ở `qa_tested`. Lát kế tiếp là commit/push target
> branch, tạo Draft PR, sync Jira/Confluence và mở checkpoint
> `code_review_ready`; QA `skipped` còn là hardening test riêng.
