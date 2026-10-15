# Phase 4 Checklist

Checklist này dùng khi triển khai Phase 4: approved implementation plan -> code,
test, trace và Draft PR. Không tick các mục này trong Phase 3.

## 1. Approval Contract

Mục đích: human/Niko có thể duyệt implementation plan checkpoint và kích hoạt
Phase 4 bằng JSON contract ổn định.

- [x] `approve-checkpoint` chỉ mở Phase 4 khi run đang `waiting_implementation_plan_review`.
- [x] Nếu checkpoint không phải `implementation_plan_review`, trả `checkpoint_not_current`.
- [x] Success trả JSON stdout, không trộn log tự do.
- [x] Error trả JSON `ok=false`.
- [x] `get-status` đọc được state Phase 4 mới.

## 2. Approved Plan Input

Mục đích: Phase 4 dùng plan đã duyệt, không tự lập kế hoạch lại.

- [x] Đọc approved `implementation_plan_v{n}`.
- [x] Đọc approved `design_artifact_v{n}`.
- [x] Đọc graph và requirement-design trace liên quan.
- [x] Nếu thiếu plan đã duyệt, trả lỗi rõ và không chạy Developer.act.

## 2.1 Developer Work Packet V1

Mục đích: đóng gói implementation plan đã duyệt thành input máy đọc được cho
Developer.act, tránh để bước code phải parse Markdown/Confluence.

- [x] Sinh `developer_work_packet_v1.json`.
- [x] Packet chứa `implementation_steps`, `proposed_files`, `design_mappings_by_file`, `test_intents` và `repo_evidence`.
- [x] Packet snapshot nội dung hiện tại của file mục tiêu đã tồn tại.
- [x] Packet có `output_contract` yêu cầu Developer trả changed/new files theo JSON.
- [x] Sau Slice 4.1, state=`implementation_ready` và chưa tạo branch/code/PR.

## 3. Git Target Repo

Mục đích: code sinh ra có nơi lưu trữ/review thật, tách khỏi repo harness.

- [x] Tạo branch theo issue/run.
- [x] Không thao tác khi target repo dirty.
- [x] Commit có issue key.
- [x] Không tạo GitHub Issue.
- [x] Có Draft PR thật; mock/local report chỉ được đánh dấu là chưa khóa Phase 4.

## 4. Developer.act

Mục đích: tạo code change có thể review, dựa trên plan đã duyệt.

- [x] Tạo prompt/module Developer.act riêng cho Phase 4.
- [x] Developer đọc `developer_work_packet_v1.json` từ implementation plan + approved design graph.
- [x] Developer tạo/sửa code trong target repo.
- [x] Sinh `code_change_report_v1.md`.
- [x] Report liệt kê file đổi, branch và reasoning chính; commit để slice sau.
- [x] Code change đủ cụ thể để reviewer kiểm tra thủ công trước khi Draft PR.
- [x] DeveloperWorkPacket và Developer.act prompt có constraint: code/test/helper
  script phải chạy được trong repo path có khoảng trắng hoặc Unicode; dùng
  repo-relative paths, không hard-code absolute path, và dùng UTF-8 rõ ràng khi
  toolchain hỗ trợ.

## 5. Verification Runner Và Validation

Mục đích: code output có kiểm chứng compile/unit rõ ràng, kể cả khi test bị
skipped. Đây là test runner của repo target, không phải AI QA/Tester.

- [x] Tạo Verification Runner/test runner adapter.
- [x] Chạy build/unit test nếu target repo hỗ trợ.
- [x] Sinh `test_report_v1.md`.
- [x] Report phân biệt `passed`, `failed`, `skipped`.
- [x] Nếu test fail, workflow không báo pass.
- [x] Verification Runner phải chạy được trong repo path có khoảng trắng hoặc
  Unicode; mọi compile/test command nên dùng repo-relative paths và UTF-8 rõ ràng
  khi toolchain hỗ trợ.
- [ ] Nếu compile/unit test fail và còn vòng repair, feedback quay về Developer;
  sau repair phải chạy lại Verification Runner.

## 6. Code Traceability

Mục đích: chứng minh requirement/design đã đi tới code/test.

- [x] Sinh `requirement_design_code_trace_v1.json`.
- [x] Trace requirement -> design element -> source file/class/function.
- [x] Trace requirement -> test file/case nếu có.
- [x] Bắt buộc gọi LLM/DeepSeek review graph requirement -> design -> code -> test
  sau static evidence.
- [x] Trace report ghi `mode=hybrid_rule_llm_code_trace_v1`,
  `llm_required=true` và `llm_review`.
- [x] Nếu LLM review phát hiện semantic gap do code/test, workflow chuyển sang
  `implementation_repair_needed` dù static evidence pass.
- [x] Quyết định policy gửi raw code/test snippet cho LLM: project owner đã xác
  nhận cho phép Validator gửi snippet giới hạn sang DeepSeek để review.
- [x] Missing link được ghi riêng.
- [x] Nếu trace fail vì code/unit test thiếu so với approved design, ghi feedback
  máy đọc được cho Developer; vòng repair thật dùng feedback này chạy ở Slice 4.6.
- [x] Validator evidence gửi full content cho controller/test chính khi còn trong
  budget, và ghi metadata `content_truncated/chars_sent/chars_total` để LLM không
  nhầm evidence bị cắt.
- [x] Boolean response và notification/display side-effect trong sequence diagram
  được normalize thành evidence semantic, không ép Developer tạo literal symbol
  như `#true`, `#false` hoặc `display*Message`.
- [ ] Nếu trace fail vì approved design mâu thuẫn/không triển khai được,
  state=`blocked` hoặc reopen design checkpoint có human approval; không tự sửa
  design trong Phase 4.

## 6.1 Validator -> Developer Repair Loop

Mục đích: khi Validator lead phát hiện code/test thiếu link so với approved
design, workflow phải giao feedback lại Developer sửa trước khi chạy QA/Tester.

- [x] Chuyển `missing_links`, `llm_review.summary`, `llm_review.risks` và trace
  artifact thành feedback máy đọc được cho Developer.
- [x] Khi code trace fail nhưng lỗi nằm ở code/test, state chuyển thành
  `implementation_repair_needed` và ghi `implementation_repair_feedback_v1.json`.
- [x] `continue-run` từ `implementation_repair_needed` gọi Developer.repair một vòng
  thay vì trả `implementation_repair_not_implemented`.
- [x] Developer repair code/test trong `allowed_paths`, không tự sửa requirement
  hoặc approved design.
- [x] Trước mỗi vòng Developer.repair, workflow refresh `current_files` từ target
  repo thật và ghi `developer_work_packet_v{n}` để tránh dùng snapshot cũ.
- [x] Mỗi vòng repair ghi `code_change_report_v{n}`, `test_report_v{n}` và
  `requirement_design_code_trace_v{n}` riêng; không ghi đè artifact cũ.
- [x] Sau Developer repair, workflow chạy lại Verification Runner.
- [x] Nếu Verification Runner pass, workflow chạy lại Validator code trace + LLM
  review trước khi cho phép sang QA/Tester.
- [x] Repair loop tôn trọng giới hạn cấu hình; hết lượt vẫn fail thì
  state=`blocked` với `last_error` rõ.
- [x] `SDLC_IMPLEMENTATION_REPAIR_LIMIT` và CLI `--implementation-repair-limit`
  cấu hình số vòng Developer.repair.
- [x] Có lệnh `revalidate-implementation` cho run bị block bởi
  `implementation_repair_limit_exceeded`; lệnh này chạy lại Verification Runner +
  Validator bằng logic mới, không sửa tay runtime state.
- [x] Revalidate không reset `implementation_repair_iterations`; nếu tăng limit
  thì chỉ mở thêm số vòng còn lại và ghi artifact version mới, không ghi đè file
  cũ.
- [x] Nếu Validator xác định lỗi là `trace_mapping_issue` hoặc
  `evidence_incomplete`, workflow block bằng `validator_evidence_or_mapping_issue`
  và không ép Developer vá code vòng quanh.
- [ ] Nếu Validator xác định lỗi nằm ở approved design/requirement cấp nghiệp vụ,
  workflow block hoặc reopen checkpoint có human approval, không ép Developer vá
  code.

## 6.2 QA/Tester Agent Loop

Mục đích: kiểm acceptance/system behavior từ use case sau khi compile/unit,
Validator trace và repair loop đã pass ổn định. Đây mới là vai Tester/QA gần
thực tế doanh nghiệp.

- [x] Viết tài liệu triển khai `docs/phase-4/qa-tester-agent.md` để chốt input,
  output, state, fake/LLM mode và repair path trước khi code.
- [x] Chốt rõ hai vòng lặp: vòng nhỏ Code -> Verification Runner -> Validator;
  vòng lớn Code -> Verification Runner -> Validator -> QA/Tester.
- [x] QA/Tester Agent đọc use case/requirement packet đã duyệt, code/test hiện
  tại trong target repo, Verification Runner report và trace summary do
  Validator bảo vệ.
- [x] QA/Tester Agent sinh hoặc chạy acceptance/system-level scenarios.
- [x] Sinh `qa_test_report_v1.md` với `pass/fail/skipped` và evidence rõ.
- [x] QA/Tester Agent không sửa code trực tiếp; chỉ ghi failure evidence.
- [x] QA/Tester prompt dùng compact packet kiểu TraceDev, không nhét full
  design/implementation plan/code trace vào prompt; packet có metadata
  `content_truncated`, `chars_sent`, `chars_total`, `packet_mode`; nếu output LLM
  không parse được thì workflow block và ghi `qa_tester_raw_response_v1.txt`.
- [x] Note rõ `content_truncated=true` sau compact packet chủ yếu là code file
  vượt budget, không phải lỗi mặc định; hướng tiếp theo là selective fetch đúng
  file/hàm khi QA thiếu evidence.
- [x] Nếu QA fail và còn vòng repair, feedback quay về Developer.
- [x] Sau Developer repair, workflow chạy lại Verification Runner -> Validator ->
  QA/Tester Agent.
- [x] Nếu QA fail quá giới hạn repair, state=`blocked` với `last_error` rõ.

## 7. Tool Sync Phase 4

Mục đích: context implementation được đẩy sang GitHub và sync ngược về Jira/
Confluence bởi SDLC Harness.

- [x] Tạo hoặc cập nhật Draft PR trên GitHub.
- [x] PR description có Jira link, Confluence link, `run_id`, summary, tests và trace.
- [x] Sync Jira comment với PR link/status.
- [x] Sync Confluence code review checkpoint.
- [x] Ghi `tool_sync_report_v4.json` với `phase=code_review`.
- [x] Re-sync không tạo PR/comment/page trùng nếu đã có external id; khi mất
  report local, adapter tìm lại target bằng marker ổn định `source_ref + phase`.
- [x] Partial failure không xóa artifact local và chuyển sang
  `code_review_sync_failed` để `sync-run` retry.
- [x] CLI mặc định `--sync-mode mock`; chỉ ghi tool thật khi caller chọn rõ
  `real` hoặc `both`.
- [x] Jira source loader loại comment checkpoint do Harness sinh, không đưa
  output cũ quay lại Requirement Refiner.

## 8. Workflow State Và Niko Reporting

Mục đích: Niko báo được Phase 4 đang ở đâu mà không hiểu nội bộ Developer,
Verification Runner, Validator hoặc QA/Tester.

- [x] Ghi state `implementation_writing`.
- [x] Ghi state trung gian `implementation_written` sau khi apply code nhưng chưa test/commit/PR.
- [x] Ghi state `implementation_testing`.
- [x] Ghi state trung gian `implementation_traced` sau khi Validator code trace pass.
- [x] Ghi state trung gian `implementation_repair_needed` khi Validator fail do
  thiếu link có thể sửa ở code/test.
- [x] Ghi state trung gian `qa_tested` sau khi QA/Tester pass.
- [x] Ghi state trung gian `qa_repair_needed` khi QA/Tester fail do code/test có
  thể sửa.
- [x] Ghi state trung gian `code_review_published` sau commit/push/Draft PR và
  trước Jira/Confluence sync.
- [x] Ghi state cuối `code_review_ready` khi sync pass.
- [x] `latest_artifacts` trỏ tới `test_report` mới nhất sau Verification Runner.
- [x] `latest_artifacts` trỏ tới report/trace mới nhất sau Validator code trace.
- [x] `latest_artifacts` trỏ tới `developer_work_packet` mới nhất sau mỗi vòng
  repair.
- [x] `latest_artifacts` trỏ tới `qa_test_report` sau QA/Tester Agent.
- [x] `quality_reports` có traceability summary.
- [x] `quality_reports` có QA acceptance summary.
- [x] `pending_checkpoint.type=code_review`.
- [x] Slice 4.8B chỉ quảng bá action đã có thật:
  `get_status`, `sync_run`, `cancel_run`; approve/request-change để slice sau.
- [ ] Niko hiển thị PR link, test summary và checkpoint code review ngắn gọn.

## 9. Test Và Acceptance

Mục đích: Phase 4 phải chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve implementation plan checkpoint happy path.
- [x] Unit test missing approved implementation plan.
- [x] Unit test `continue-run` từ `implementation_ready` sang `implementation_written`.
- [x] Unit test Developer.act chặn file ngoài `allowed_paths`.
- [x] Unit test blocked khi target repo dirty trước Developer.act.
- [x] Unit test `continue-run` từ `implementation_written` sang `implementation_tested`.
- [x] Unit test Verification Runner `passed/failed/skipped`.
- [x] Unit test branch/commit/PR payload formatting.
- [x] Unit test PR description chứa đủ link/context.
- [x] Unit test test report `passed/failed/skipped`.
- [x] Unit test trace requirement-design-code-test.
- [x] Unit test repair-needed khi trace fail do thiếu test/source/code evidence.
- [x] Unit test repair-needed khi LLM code-trace review phát hiện semantic gap.
- [x] Unit test blocked khi trace fail vì lỗi không repair được ở code/test.
- [x] Unit test `continue-run` từ `implementation_repair_needed` gọi
  Developer.repair, ghi artifact v2 và pass trace khi repair thành công.
- [x] Unit test repair refresh packet từ target repo hiện tại trước khi gọi
  Developer.repair, không dùng stale `current_files`.
- [x] Unit test repair loop tôn trọng `SDLC_IMPLEMENTATION_REPAIR_LIMIT=0`.
- [x] Unit test repair loop chặn output ngoài `allowed_paths`.
- [x] Unit test repair loop block khi Verification Runner fail sau repair.
- [x] Unit test revalidate blocked repair-limit: pass luôn, reopen
  `implementation_repair_needed`, reject blocked state không đúng loại, và CLI
  dispatch `revalidate-implementation`.
- [x] Unit test QA/Tester report pass và fail.
- [x] Unit test QA/Tester LLM output bị cắt/JSON lỗi thì block và ghi raw response.
- [x] Unit test Validator code-trace parse được JSON bị bọc trong prose để giảm
  lỗi output không sạch từ LLM.
- [ ] Unit test QA/Tester report `skipped`.
- [x] Unit test blocked khi QA fail quá giới hạn repair.
- [x] Unit test sync mock/local GitHub report.
- [x] Unit test `code_review_published -> code_review_ready` và report v4.
- [x] Unit test mất report local vẫn khôi phục remote ID theo marker.
- [x] Unit test legacy migration chỉ nhận một target duy nhất; nhiều target trả
  `remote_sync_ambiguous` và không tự xóa remote artifact.
- [x] Unit test partial failure giữ evidence và retry sang `code_review_ready`.
- [x] Unit test CLI mặc định mock dù `.env` đang để real.
- [x] Live GitHub PR create/update bằng GitHub CLI thật.
- [ ] Smoke test CLI từ run Phase 3 thật sang `implementation_ready` cho Slice 4.1.
- [x] Live compile/runtime test target repo sau Developer.act pass khi ép JDK 21
  vào PATH tạm: `All tests passed`.
- [x] Ghi nhận và sửa lỗi test script không chịu được đường dẫn Unicode bằng cách
  chuyển sang repo-relative paths trong `SDLC_Target_eTour/scripts/test.ps1`.
- [x] Live compact QA packet với DeepSeek thật trên runtime copy: happy path từ
  `implementation_traced` sang `qa_tested`, runtime thật không bị mutate.
- [x] Live QA fail với DeepSeek thật trên runtime copy có lỗi hành vi rõ; run `run_live_qa_repair_20261014_03` chuyển `implementation_traced -> qa_repair_needed -> qa_tested` sau một vòng Developer repair.
- [ ] Live QA `skipped` trên runtime copy có môi trường kiểm thử thiếu dependency/compiler rõ ràng.
- [x] Smoke test CLI từ run thật `run_20261013_160421_f661babf` sang
  `code_review_ready`.
- [ ] Smoke test qua Niko sau khi CLI pass.


## QA Slice 4.7A Update - 2026-10-14

Purpose: lock the difference between Validator and QA. Validator reviews the
requirement/design/code/test trace. QA now generates independent acceptance or
system tests and lets the runner execute them.

- [x] `QaTesterAgent.generate_tests()` replaces the review-only path.
- [x] QA output must parse as JSON with `summary`, `test_files`, and `scenarios`.
- [x] Test file paths are guarded to `src/test/java/*.java`.
- [x] Workflow writes `qa_generated_tests_v{n}.json`.
- [x] Workflow applies QA tests into the target repo before running the runner.
- [x] Workflow writes `qa_test_execution_v{n}.md`.
- [x] QA pass/fail/skipped comes from Verification Runner after QA tests are applied.
- [x] `latest_artifacts` includes `qa_generated_tests`, `qa_test_execution`, and `qa_test_report`.
- [x] Unit test Phase 4 pass: `py -3.13 -m unittest discover -s tests -p test_phase4.py -v`.
- [x] Live DeepSeek generated-test QA on a real runtime: `run_20261013_160421_f661babf` moved from `implementation_traced` to `qa_tested`; QA generated 1 acceptance test file and Verification Runner passed.
- [x] QA repair loop from QA failure report back to Developer.


## QA Slice 4.7B Update - 2026-10-14

Purpose: close the first large Code -> Runner -> Validator -> QA repair loop
without opening GitHub PR/sync yet.

- [x] `SDLC_QA_REPAIR_LIMIT` and `continue-run --qa-repair-limit` control QA repair loops.
- [x] QA execution fail can move to `qa_repair_needed` with `qa_repair_feedback_v{n}.json`.
- [x] `continue-run` from `qa_repair_needed` calls Developer.repair, then reruns Verification Runner, Validator code trace, and QA tests.
- [x] QA skipped and invalid QA JSON still block; they do not enter Developer repair.
- [x] QA fail after the repair limit blocks with `qa_repair_limit_exceeded`.
- [x] Verification Runner executes every Java class listed in the newly applied QA test files; a baseline script that only compiles them cannot produce a false pass.
- [x] Live DeepSeek repair evidence: the required QA class failed on cancel deleting the item, Developer repaired production/test code, Validator passed, and the regenerated QA class passed.

## GitHub Slice 4.8A Update - 2026-10-15

Mục đích: publish đúng code/test đã qua QA lên GitHub mà chưa trộn Jira/
Confluence checkpoint vào cùng một lát.

- [x] Dựng publication manifest từ Developer allowed paths, QA-generated tests
  và verification support scripts.
- [x] Chặn file dirty ngoài manifest; không dùng `git add -A`.
- [x] Tạo/tái sử dụng commit có issue key và trailer `SDLC-Run`.
- [x] Push planned branch và tạo/cập nhật đúng Draft PR đang mở.
- [x] Ghi `github_pr_body_v1.md` và `github_code_review_v1.json`.
- [x] Giữ partial evidence và trạng thái retryable `code_review_publish_failed`.
- [x] Retry publication từ `code_review_publish_failed` theo hướng idempotent;
  `code_review_published` chuyển sang bước sync 4.8B, không publish GitHub lại.
- [x] Unit test local bare remote + fake GitHub CLI cho create/update idempotency.
- [x] Unit test workflow `qa_tested -> code_review_published` và retry partial failure.
- [x] Live test thật trên `run_20261013_160421_f661babf`: commit
  `088416465cc7139255aafa120a0b5bb8f17cb971`, Draft PR `#1`.
- [x] Xác nhận chạy lại không tạo commit hoặc Draft PR trùng: commit count giữ
  nguyên `3`, artifact ghi `commit.operation=reused` và
  `pull_request.operation=updated`.
- [x] Slice 4.8B code + unit test sync Jira/Confluence và mở
  `code_review_ready` (`136/136` test pass).
- [x] Slice 4.8B live sync run thật: Jira comment `10104`, Confluence page
  `2129921`; re-sync trả `operation=updated` và giữ nguyên cả hai external ID.

## Slice 4.9 - Run Workspace Isolation

Mục đích: khóa branch/worktree identity theo run trước khi mở approve Code Review
và request-change Phase 5. Checklist chi tiết nằm tại
`workspace-isolation/checklist.md`.

- [x] Viết contract kiến trúc worktree-per-run, runtime artifact và migration.
- [x] Chốt branch theo issue/source slug + run suffix.
- [x] Chốt run hiện tại/PR #1 được adopt, không chạy lại từ đầu.
- [x] Chốt explicit cleanup sau human merge hoặc cancel; không xóa remote branch.
- [x] Chốt invariant Harness chỉ Draft -> Ready, human luôn tự merge.
- [ ] Triển khai Slice 4.9A workspace manager và guards.
- [ ] Triển khai Slice 4.9B adopt legacy run.
- [ ] Triển khai Slice 4.9C lifecycle/cleanup.
- [ ] Triển khai Slice 4.9D Code Review approval.
