# Phase 4 Progress

## 2026-10-15 - Slice 4.9D: Code Review Approval

Đã nối `approve-checkpoint` tại state `code_review_ready` vào Phase 4. Workflow
giữ per-run lock, xác minh publication/workspace/live PR cùng number, branch,
head SHA và base branch, rồi gọi `gh pr ready` khi PR còn Draft. PR đã Ready được
xử lý idempotent; PR closed, merged hoặc lệch identity fail closed trước mutation.

Approval ghi `github_code_review_approval_v1.json` riêng, giữ nguyên publication
artifact, rồi cập nhật Jira/Confluence với `review_status=code_review_approved`.
Nếu Atlassian sync lỗi sau khi GitHub đã Ready, state chuyển
`code_review_approval_sync_failed`; `sync-run` retry mà không gọi lại GitHub.
Worktree vẫn active và không có code path merge/auto-merge.

Kết quả kiểm thử: GitHub/Code Review suite `16 passed`; full regression
`188 passed`; `git diff --check` pass. Xác minh live chỉ đọc sau implementation:
run `run_20261013_160421_f661babf` vẫn `code_review_ready`, workspace `active`;
PR #1 vẫn `OPEN/Draft`, head `088416465cc7139255aafa120a0b5bb8f17cb971`,
base `main`. Chưa chạy live approval.

## 2026-10-15 - Slice 4.9C: Workspace Lifecycle Và Cleanup

Đã thêm CLI `cleanup-run-workspace --run-id` và lifecycle
`active -> cleanup_pending -> cleaned`. Cleanup chỉ được mở khi GitHub xác nhận PR
đã merge bởi human hoặc run đã mang trạng thái `cancelled`; worktree dirty bị
chặn và V1 không có force. Trước khi dọn, Harness chứng minh commit còn ở remote
base hoặc đúng remote branch, sau đó remove/prune worktree rồi xóa local ref bằng
expected SHA. Remote branch không bị xóa.

Artifact giữ reason/timestamp/PR merge audit; retry từ `cleanup_pending` hội tụ
theo trạng thái Git thực tế và lần gọi sau `cleaned` là idempotent. Cleanup không
đổi phase/status/checkpoint của workflow. Test Git cục bộ bao phủ merged,
cancelled, dirty, missing recovery copy, partial failure/retry và JSON CLI.

Kết quả tại mốc 4.9C: workspace suite `42 passed`, full regression `182 passed`.
Live evidence read-only xác nhận PR #1 vẫn `OPEN/Draft`, worktree sạch và nguyên
vẹn; chưa chạy cleanup live vì run chưa đủ điều kiện. Slice 4.9D sau đó đã hoàn
tất ở mức code/test như mục phía trên.

## 2026-10-15 - Slice 4.9B: Legacy Run Adoption

Đã thêm CLI `adopt-run-workspace --run-id` và GitHub PR reader chỉ đọc để xác
minh runtime publication, remote, base/head OID, branch, Draft state và local Git
cùng một identity. Workspace manager thực hiện adoption dưới registry lock và
per-run lock, fail closed bằng `workspace_adoption_failed`, rollback checkout và
worktree chưa có ownership khi persistence lỗi.

Live run `run_20261013_160421_f661babf` đã được adopt thành công. Canonical target
repo hiện ở `main`; branch `feature/sdlc-1-implementation` nằm trong worktree riêng
và giữ nguyên HEAD `088416465cc7139255aafa120a0b5bb8f17cb971`. Draft PR #1 vẫn
OPEN/Draft, run vẫn `code_review_ready`. Lần gọi thứ hai trả `adopted=false`, xác
nhận idempotency. Full regression sau thay đổi: `174 passed`.

Phần lifecycle/cleanup sau mục này đã hoàn thành trong Slice 4.9C; approval
Draft -> Ready đã hoàn thành trong Slice 4.9D ở trên, không merge.

## 2026-10-15 - Slice 4.9A3: Phase 3/4 Dùng Run Workspace End-to-End

Đã nối creation engine vào design approval và chuyển toàn bộ đường chạy code của
Phase 4 sang worktree theo `run_id`. Developer, Verification Runner, Validator,
QA, repair, revalidate, GitHub publisher và Code Review re-sync đều chạy dưới
per-run lock; current branch của canonical repo không còn là nguồn sự thật.

DeveloperWorkPacket snapshot workspace identity. Sau publish, manager refresh
`head_sha`, PR number/URL vào `run_workspace_v1.json` và state. Jira/Confluence
chỉ nhận artifact ref cùng branch/commit/PR identity, không nhận machine-local
path. Run cũ chưa có workspace fail closed và chờ Slice 4.9B adoption; A3 không
chạm live run/Draft PR hiện có.

Kết quả kiểm thử: workspace suite `15 passed`; full regression Phase 1-4
`167 passed`. Không gọi LLM thật, không sync Jira/Confluence/GitHub thật và không
chạm live run/Draft PR hiện có.

## 2026-10-15 - Slice 4.9A2: Git Worktree Creation Engine

Đã triển khai engine độc lập để tạo workspace riêng theo `run_id`: fetch remote
base, chốt SHA, tạo branch/worktree có run suffix, ghi artifact/state, resolve
Git identity và giữ per-run action lock. Rollback chỉ chạy khi worktree vẫn sạch
và HEAD chưa rời base; không dùng force-delete.

Integration test dùng bare remote cục bộ đã khóa fetch SHA mới nhất,
idempotency, hai run không dùng chung branch/path, dirty/remote/path/branch guard,
`.codegraph` allowance, identity drift và rollback khi persistence lỗi. Tại thời
điểm kết thúc A2, engine chưa nối vào Phase 3/4; phần đó sau này đã hoàn thành ở
A3 phía trên. Live run/Draft PR cũ vẫn chưa được adopt.

Kết quả kiểm thử sau slice: `160 passed`; không gọi LLM thật, không sync tool và
không chạm live run/Draft PR hiện có.

## 2026-10-15 - Tài Liệu Slice 4.9: Run Workspace Isolation

Đã chốt tài liệu prerequisite trước Code Review approval và Phase 5:

- mỗi run sở hữu một Git worktree và branch có run suffix;
- run mới tạo từ `origin/main` vừa fetch, resume không tự rebase/reset;
- mọi action code phải resolve workspace theo `run_id` và có per-run lock;
- run hiện tại `run_20261013_160421_f661babf` cùng Draft PR #1 sẽ được adopt,
  không sinh lại code và không đổi HEAD;
- cleanup là lệnh explicit sau human merge hoặc cancel, không xóa remote branch;
- approve chỉ chuyển Draft PR thành Ready for review, còn merge luôn do human;
- request-change Phase 5 giữ nguyên workspace/branch/PR của run.

Tài liệu nằm tại `docs/phase-4/workspace-isolation/`. Đây là ghi chép tại thời
điểm contract mới được chốt; các mục 4.9B-D sau đó đã hoàn thành như nhật ký phía
trên.

## 2026-10-13 - Slice 4.1: Approve Plan To DeveloperWorkPacket

Mục tiêu của slice này là mở Phase 4 thật nhỏ: duyệt implementation plan đã qua
Validator.review_plan, rồi chuẩn bị input máy đọc được cho Developer.act.

Đã làm:

- `approve-checkpoint` dispatch được từ `waiting_implementation_plan_review`.
- Workflow kiểm checkpoint type là `implementation_plan_review`.
- Workflow đọc approved `implementation_plan`, `implementation_plan_structured`,
  `implementation_plan_review` và các artifact design/trace liên quan.
- Workflow kiểm target repo vẫn sạch, đúng remote và có `.codegraph/`.
- Sinh `developer_work_packet_v1.json`.
- State chuyển sang `implementation_ready`.
- Target repo chưa bị sửa: chưa tạo branch, chưa ghi code, chưa commit và chưa PR.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: pass 5/5.

Việc tiếp theo:

- Slice 4.2: Developer.act đọc `developer_work_packet_v1.json`, tạo branch và sinh
  code change theo output contract.
- Sau đó mới đến Verification Runner, code traceability, QA/Tester loop và Draft PR.

## 2026-10-14 - Slice 4.2: Developer.act Applies Code Changes

Mục tiêu của slice này là biến DeveloperWorkPacket đã duyệt thành code change thật
trong target repo để anh có thể review trực tiếp trước khi chạy test/trace/PR.

Đã làm:

- Thêm CLI `continue-run --run-id <run_id>`.
- `continue-run` xử lý state `implementation_ready`.
- Workflow đọc `developer_work_packet_v1.json`.
- Workflow kiểm target repo sạch trước khi thao tác.
- Workflow tạo hoặc checkout planned branch.
- Thêm `DeveloperActAgent` và prompt `Developer Act v1`.
- Developer.act trả JSON `changed_files_json_v1` với file content đầy đủ.
- Workflow chặn file ngoài `output_contract.allowed_paths`.
- Workflow apply file vào target repo.
- Sinh `code_change_report_v1.md`.
- State chuyển sang `implementation_written`.

Chưa làm trong slice này:

- Chưa chạy Verification Runner/build/unit test.
- Chưa sinh requirement-design-code trace.
- Chưa commit.
- Chưa mở Draft PR.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: pass 10/10.
- Live test 2026-10-14 với run `run_20261013_160421_f661babf` đã checkout
  branch `feature/sdlc-1-implementation`, apply đủ 12 file theo
  `developer_work_packet_v1.json` và sinh `code_change_report_v1.md`.
- Sau khi cài JDK 21, compile/runtime test target repo pass với `All tests
  passed`. Lỗi phát hiện trong lúc test: script test ban đầu truyền absolute
  path có dấu `D:\Dự án cá nhân\...` vào `javac`, gây mojibake path. Đã sửa
  script target repo sang repo-relative paths; đồng thời bổ sung constraint cho
  DeveloperWorkPacket/Developer.act: code, test và helper script phải chạy được
  trong repo path có khoảng trắng hoặc Unicode, không hard-code absolute path và
  dùng UTF-8 rõ ràng khi toolchain hỗ trợ.

Việc tiếp theo:

- Slice 4.3: Verification Runner đọc code change, chạy command compile/unit test
  của target repo hoặc ghi skipped/failure rõ ràng. QA/Tester Agent acceptance
  loop là bước sau khi baseline compile/unit/trace đã ổn.

## 2026-10-14 - Slice 4.3: Verification Runner

Mục tiêu của slice này là chạy được compile/unit test command của target repo sau
khi Developer.act đã apply code, rồi ghi kết quả rõ ràng thay vì báo pass mù.

Đã làm:

- Thêm package `sdlc_harness.verification` với `VerificationRunner`.
- `continue-run` dispatch theo state:
  - `implementation_ready` vẫn chạy Developer.act như Slice 4.2.
  - `implementation_written` chạy Verification Runner.
- Runner chọn command từ `developer_work_packet_v1.json`/`test_intents`, fallback
  về `powershell -ExecutionPolicy Bypass -File scripts/test.ps1`.
- Runner capture stdout, stderr, exit code, duration và phân loại `passed`,
  `failed`, `skipped`.
- Sinh `test_report_v1.md`.
- Nếu pass: state chuyển sang `implementation_tested`.
- Nếu fail/skipped: state chuyển sang `blocked` với `last_error` là
  `test_failed` hoặc `test_skipped`.
- Verification cho phép target repo dirty vì đây là trạng thái mong muốn sau khi
  Developer.act apply code.

Chưa làm trong slice này:

- Chưa có repair loop Developer khi compile/unit test fail.
- Chưa chạy Validator code trace.
- Chưa chạy QA/Tester Agent acceptance/system test.
- Chưa commit, chưa mở Draft PR và chưa sync Phase 4 lên tool.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: pass 15/15.

Việc tiếp theo:

- Slice 4.4: Validator kiểm requirement/design -> code/unit test trace, hoặc
  chuẩn bị Developer repair loop nếu muốn xử lý fail trước khi trace.

## 2026-10-14 - Docs Sync: Repair Loop Trước QA/Tester

Mục tiêu của lượt đồng bộ docs này là sửa điểm nhập nhằng sau Slice 4.3 và 4.4:
compile/unit pass mới chứng minh code chạy được; Validator trace + LLM review mới
chứng minh code/test bám approved design. Nếu Validator đã phát hiện code/test
thiếu link, workflow phải quay lại Developer repair trước, chưa được nhảy sang
QA/Tester Agent.

Đã chốt lại trong tài liệu:

- `Verification Runner` chỉ chạy compile/unit test của target repo và sinh
  `test_report_v1.md`.
- `Validator` kiểm approved requirement/design -> code/unit test trace và sinh
  `requirement_design_code_trace_v1.json`.
- `Developer repair loop` là gate ngay sau Validator fail do code/test thiếu:
  Developer sửa code/test theo missing links, Verification Runner chạy lại, rồi
  Validator kiểm lại.
- `QA/Tester Agent` là gate sau khi compile/unit + trace đã ổn định: kiểm
  acceptance/system behavior từ use case và sinh `qa_test_report_v1.md`.
- `code_review_ready` chỉ được ghi sau khi có test report, trace report, QA report,
  commit/Draft PR và sync Phase 4.
- `implementation_testing` hiện là umbrella state cho các sub-step test/trace/QA/repair;
  chưa tách state code mới trong lượt docs-only này.

Việc tiếp theo:

- Slice 4.4: triển khai Validator code trace.
- Slice 4.5: chuẩn hóa Validator feedback cho Developer.
- Slice 4.6: triển khai Developer repair loop cho code/test.
- Slice 4.7: triển khai QA/Tester Agent report sau khi trace repair loop pass.
- Sau đó mới đến commit/Draft PR/sync Phase 4.

## 2026-10-14 - Slice 4.4: Validator Code Trace

Mục tiêu của slice này là chặn trường hợp code compile/unit pass nhưng chưa có bằng
chứng approved design đã đi tới source/test file trong target repo.

Đã làm:

- `continue-run` xử lý tiếp state `implementation_tested`.
- Thêm `CodeTraceValidator` hybrid rule + LLM dưới vai Validator.
- Validator đọc `developer_work_packet_v1.json`, target repo và mapping
  `design_mappings_by_file`.
- Validator sinh static evidence trước, sau đó bắt buộc gọi LLM/DeepSeek review
  graph requirement -> design -> code -> test.
- Sinh `requirement_design_code_trace_v1.json`.
- Nếu trace pass: state chuyển sang `implementation_traced`.
- Nếu trace fail ở Slice 4.4 ban đầu: state chuyển sang `blocked`,
  `last_error=implementation_traceability_failed` và artifact vẫn ghi
  `missing_links`. Slice 4.5 đã thay phần repairable code/test fail này bằng
  state `implementation_repair_needed`.
- Nếu LLM review phát hiện semantic gap do code/test, Slice 4.5 ghi feedback
  repair thay vì block chung chung.
- `latest_artifacts.requirement_design_code_trace` và
  `quality_reports.implementation_traceability` được cập nhật.
- `build_developer_work_packet` tự bổ sung file từ `design_mappings` vào output
  contract để Developer.act được phép tạo đủ file mà Validator sẽ kiểm.
- Fake Developer.act được cập nhật để tạo đủ các file chính trong mapping test.
- Fake LLM được cập nhật cho `Code Trace LLM Review v1` để unit test không gọi API thật.

Lưu ý egress:

- Project owner đã xác nhận cho phép Validator gửi raw source/test snippet giới
  hạn sang DeepSeek để review code trước khi human review.
- `CodeTraceValidator` hiện đưa source/test snippet vào `Code Trace LLM Review v1`
  với giới hạn dung lượng prompt để tránh gửi cả repo.

Chưa làm trong slice này:

- Chưa chạy QA/Tester Agent acceptance/system test.
- Chưa có repair loop Developer khi trace fail. Slice 4.5 đã có feedback artifact
  cho Developer, nhưng chưa tự gọi Developer update code.
- Chưa commit target repo, chưa mở Draft PR và chưa sync Phase 4.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: pass 21/21.
- Live smoke với DeepSeek thật cho case semantic fail đã pass kỳ vọng: code/test
  file tồn tại nhưng thiếu confirmation, duplicate-submit guard, notification và
  test assertion; Validator gửi source/test snippet, LLM trả `ok=false`,
  confidence `0.95`, `snippet_included=true` và sinh 9 missing links.

Việc tiếp theo:

- Slice 4.5: chuẩn hóa Validator feedback thành `implementation_repair_feedback_v1.json`
  trước, chưa tự sửa code/test.
- Slice 4.6: Developer repair loop dùng feedback đó để sửa code/test, chạy lại
  Verification Runner và Validator trong giới hạn cấu hình.
- Slice 4.7: QA/Tester Agent sinh/chạy acceptance/system evidence và ghi
  `qa_test_report_v1.md` sau khi compile/unit + trace đã pass ổn định.
- Sau đó mới đến commit/Draft PR và sync Phase 4.

## 2026-10-14 - Slice 4.5: Validator Feedback Cho Developer

Mục tiêu của slice này là không nhảy thẳng vào repair loop rộng. Trước tiên,
Validator phải chuẩn hóa feedback đủ rõ để Developer có thể sửa code/test ở
slice sau mà không phải đoán từ `blocked` chung chung.

Đã làm:

- Khi `CodeTraceValidator` pass, luồng giữ nguyên: state=`implementation_traced`.
- Khi Validator fail vì missing link có thể sửa ở code/test, workflow chuyển sang
  state=`implementation_repair_needed` thay vì `blocked`.
- Sinh `implementation_repair_feedback_v1.json`.
- Feedback gom `missing_links`, `llm_review.summary`, `llm_review.risks`, trace
  artifact, branch, target repo và `allowed_paths`.
- `latest_artifacts.implementation_repair_feedback` trỏ tới feedback mới.
- `quality_reports.implementation_repair` tóm tắt việc cần repair cho Niko đọc.
- `last_error` để `null` ở state repair-needed vì đây là trạng thái chờ xử lý,
  không phải lỗi hệ thống.
- Nếu trace fail vì input graph/artifact nền không đủ điều kiện repair, workflow
  vẫn `blocked`.
- Tại thời điểm Slice 4.5, `continue-run` từ `implementation_repair_needed` còn
  trả `implementation_repair_not_implemented` để tránh hiểu nhầm là đã có auto
  repair. Slice 4.6 sau đó đã thay trạng thái này bằng vòng Developer.repair thật.

Chưa làm trong slice này:

- Chưa gọi Developer sửa code/test từ feedback.
- Chưa version hóa vòng repair `code_change_report_v2`, `test_report_v2`,
  `requirement_design_code_trace_v2`.
- Chưa chạy lại Verification Runner/Validator sau repair.
- Chưa chạy QA/Tester Agent, chưa commit, chưa mở Draft PR.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: pass.
- `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py -q`: pass.

Việc tiếp theo:

- Slice 4.6: dùng `implementation_repair_feedback_v1.json` làm input để Developer
  repair code/test trong `allowed_paths`, sau đó chạy lại Verification Runner ->
  Validator trong giới hạn cấu hình.

## 2026-10-14 - Slice 4.6: Developer Repair Loop V1

Mục tiêu của slice này là biến `implementation_repair_needed` thành một vòng
repair thật, nhưng vẫn giữ phạm vi nhỏ: chỉ repair code/test theo feedback của
Validator, chưa chạy QA/Tester Agent, chưa commit và chưa mở Draft PR.

Đã làm:

- Thêm cấu hình `SDLC_IMPLEMENTATION_REPAIR_LIMIT` và CLI
  `--implementation-repair-limit`.
- Thêm `DeveloperActAgent.repair(...)` với prompt `Developer Repair v1`.
- `continue-run` từ `implementation_repair_needed` đọc
  `developer_work_packet_v1.json` và `implementation_repair_feedback_v{n}`.
- Workflow kiểm số vòng repair trước khi gọi Developer.
- Developer.repair chỉ được apply file trong `output_contract.allowed_paths`.
- Mỗi vòng repair ghi artifact version mới:
  `code_change_report_v2.md`, `test_report_v2.md`,
  `requirement_design_code_trace_v2.json` nếu là vòng đầu.
- Sau repair, workflow chạy lại Verification Runner.
- Nếu Verification Runner pass, workflow chạy lại Validator code trace + LLM
  review.
- Nếu trace pass, state quay về `implementation_traced` và
  `quality_reports.implementation_repair.needed=false`.
- Nếu trace vẫn repairable và còn lượt, workflow ghi feedback version mới rồi
  giữ `implementation_repair_needed`.
- Nếu hết lượt hoặc repair/test/trace không thể tiếp tục an toàn, workflow
  `blocked` với `last_error` rõ.
- Report Markdown v2/v3 hiển thị đúng title version và metadata repair
  iteration/feedback.

Chưa làm trong slice này:

- Chưa tạo feedback loop cho compile/unit fail xảy ra trước khi có trace
  feedback.
- Chưa chạy QA/Tester Agent acceptance/system test.
- Chưa commit target repo, chưa mở Draft PR và chưa sync Phase 4 lên GitHub/Jira/
  Confluence.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: 26 passed.
- `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py -q`: 109 passed.

Việc tiếp theo:

- Slice 4.7: triển khai QA/Tester Agent sau khi compile/unit + Validator trace
  đã pass ổn định.
- Sau QA mới đến commit/Draft PR/sync Phase 4.

## 2026-10-14 - Slice 4.6.1: Refresh DeveloperWorkPacket Trước Repair

Live test DeepSeek thật cho thấy Developer.repair đang dùng
`developer_work_packet_v1.json`, tức snapshot được tạo trước khi Developer.act ghi
code. Vì vậy nhiều file trong `current_files` vẫn rỗng hoặc là nội dung cũ, dù
target repo đã có code mới. Điều này làm Developer dễ rewrite theo bối cảnh cũ và
repair loop khó hội tụ.

Đã làm:

- Thêm refresh packet trước mỗi vòng Developer.repair.
- Mỗi vòng repair ghi `developer_work_packet_v{n}.json` mới, trong đó
  `current_files` và `proposed_files.current_content` được đọc lại từ target repo.
- `latest_artifacts.developer_work_packet` trỏ tới packet mới nhất sau refresh.
- `code_change_report_v{n}.md` và `test_report_v{n}.md` ghi rõ packet repair đã dùng.
- Nếu Developer.repair có raw LLM response, workflow lưu
  `developer_repair_raw_response_v{n}.txt` để debug.
- Bổ sung regression test bắt trường hợp stale snapshot: packet gốc còn nội dung cũ,
  còn packet repair version mới phải thấy nội dung thật sau Developer.act.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: 28 passed.
- `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py -q`: 111 passed.

Ghi chú live test:

- Một lần chạy copy runtime với DeepSeek thật đã chứng minh repair sửa đúng feedback
  semantic, nhưng bị block vì Verification Runner không thấy `javac` trong môi
  trường script. Đây là vấn đề môi trường verification riêng, không phải bằng chứng
  repair loop đã/không hội tụ.

## 2026-10-14 - Slice 4.6.2: Java Env Auto-Detect Cho Verification Runner

Live test kế tiếp xác nhận máy đã có JDK tại
`C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot`, nhưng session chạy
workflow vẫn ưu tiên Java 8 JRE cũ và không có `JAVA_HOME`, nên `scripts/test.ps1`
không tìm thấy `javac`.

Đã làm:

- `VerificationRunner` tự dựng env cho subprocess test thay vì dùng nguyên env
  hiện tại.
- Runner ưu tiên `SDLC_JAVA_HOME`, sau đó `JAVA_HOME`, sau đó dò các thư mục JDK
  phổ biến trên Windows như Eclipse Adoptium, Java, Microsoft, BellSoft, Zulu và
  Amazon Corretto.
- Khi tìm thấy JDK, runner tự prepend `<JDK>\bin` vào `PATH` và set `JAVA_HOME`
  cho test command.
- Bổ sung regression test mô phỏng terminal không có `javac` trong `PATH` nhưng có
  `SDLC_JAVA_HOME`; Verification Runner phải chạy pass thay vì báo skipped.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: 29 passed.

Kiểm tra máy hiện tại:

- `_verification_env()` tự tìm thấy
  `C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot\bin\javac.EXE`.
- Chạy lại Verification Runner trực tiếp trên target copy của live repair trước đó
  cho kết quả `status=passed`, stdout `All tests passed`; lỗi `test_skipped`
  không còn tái hiện.

## 2026-10-14 - Live Test Runtime Thật: Repair Limit 5

Chạy trên runtime thật `run_20261013_160421_f661babf` với `--llm-mode deepseek`
và `--implementation-repair-limit 5`.

Kết quả:

- Vòng code trace đầu tiên chuyển state từ `implementation_tested` sang
  `implementation_repair_needed`.
- Developer.repair chạy đủ 5 iteration và tạo các artifact đến version v6:
  `developer_work_packet_v6.json`, `code_change_report_v6.md`,
  `test_report_v6.md`, `requirement_design_code_trace_v6.json`,
  `developer_repair_raw_response_v6.txt`.
- `test_report_v6.md` cho thấy Verification Runner đã pass:
  `Status: passed`, stdout `All tests passed`.
- Workflow kết thúc `blocked` với `implementation_repair_limit_exceeded` vì
  Validator code trace vẫn báo 7 missing link.

Nhận định:

- Lỗi môi trường `javac` đã được xử lý.
- Vấn đề còn lại không giống compile/unit failure. Validator LLM liên tục nói
  controller snippet bị truncate, test evidence không hiển thị đủ, và một số
  mapping sequence đang trỏ vào symbol yếu/sai như `#true`, `#false`,
  `notifyError/notifySuccess` ở sai class.
- Trước khi tiếp tục tăng repair loop, nên sửa tầng Validator evidence:
  đảm bảo LLM review thấy đầy đủ nội dung file liên quan và làm sạch mapping
  sequence side-effect/boolean response để tránh Developer sửa vòng quanh.

## 2026-10-14 - Slice 4.6.3: Sửa Validator Evidence/Mapping

Sau live test repair limit 5, tầng Validator được cập nhật để phân biệt lỗi
code/test thật với lỗi evidence hoặc trace mapping yếu.

Đã làm:

- Tăng evidence budget cho `CodeTraceValidator` và ưu tiên gửi full content của
  controller, test chính, service/repository liên quan.
- `llm_review.review_context` ghi metadata evidence: số file, tổng chars, danh
  sách path, `chars_sent/chars_total`, và `content_truncated`.
- Normalize sequence mapping yếu:
  - `#true/#false` được xem là boolean branch evidence, không phải symbol code.
  - `notifySuccess/notifyError` bị map nhầm qua `ETOURConnectionService` được
    normalize về `NotificationService`.
  - display-message side effect được đánh dấu là semantic side effect thay vì yêu
    cầu method `display*Message` literal.
- Prompt Code Trace LLM Review yêu cầu model không báo truncated nếu metadata cho
  biết file đã gửi full, và dùng `trace_mapping_issue`/`evidence_incomplete` cho
  lỗi thuộc Validator/trace graph.
- Workflow Phase 4 không đưa `trace_mapping_issue` hoặc `evidence_incomplete` vào
  Developer.repair; thay vào đó block bằng `validator_evidence_or_mapping_issue`.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: 31 passed.
- `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py -q`: 114 passed.

Kiểm tra DeepSeek thật không ghi state:

- Chạy trực tiếp `CodeTraceValidator` trên target repo hiện tại với
  `developer_work_packet_v6.json`.
- `review_context.code_snippets_truncated=false`, 12 file được gửi full content,
  tổng 37221 chars.
- Lỗi "controller snippet truncated" không còn xuất hiện. Validator còn báo 3
  semantic gaps cụ thể hơn: auth check trong `confirmDeletion`, delete theo id,
  và test logout. Đây là tín hiệu code/test hoặc requirement-design policy thật,
  không còn là lỗi evidence bị cắt.

## 2026-10-14 - Slice 4.6.4: Revalidate/Resume Run Bị Block Do Repair Limit

Mục tiêu của slice này là xử lý trường hợp live run đã `blocked` vì
`implementation_repair_limit_exceeded`, nhưng sau đó Validator/evidence logic đã
được sửa. Trước slice này, `continue-run --implementation-repair-limit 7` vẫn bị
chặn bằng `run_not_continuable` vì state đã là `blocked`.

Đã làm:

- Thêm CLI `revalidate-implementation`.
- Lệnh này chỉ cho chạy khi `status=blocked` và
  `last_error.error_code=implementation_repair_limit_exceeded`.
- Revalidate refresh `DeveloperWorkPacket` từ target repo hiện tại, chạy lại
  Verification Runner, chạy lại Validator code trace, rồi ghi artifact version mới.
- Revalidate không tăng `implementation_repair_iterations` vì chưa có vòng
  Developer.repair mới.
- Nếu trace pass, state chuyển `implementation_traced`.
- Nếu trace còn repairable và limit mới còn lượt, state chuyển lại
  `implementation_repair_needed` với feedback mới.
- Nếu trace vẫn repairable nhưng limit mới không còn lượt, state giữ `blocked`
  và `allowed_next_actions` có `revalidate_implementation`.
- Mỗi vòng repair sau revalidate dùng `_next_phase4_artifact_version(...)` để
  không ghi đè artifact cũ.

Kết quả test:

- `rtk proxy python -m pytest tests/test_phase4.py -q`: 35 passed.

Live test runtime thật:

- Chạy `revalidate-implementation` trên `run_20261013_160421_f661babf` với
  `--llm-mode deepseek --implementation-repair-limit 7`.
- Run chuyển từ `blocked` sang `implementation_repair_needed`.
- Iteration vẫn là `5`, artifact mới là `developer_work_packet_v7.json`,
  `test_report_v7.md`, `requirement_design_code_trace_v7.json` và
  `implementation_repair_feedback_v7.json`.
- Validator sau khi revalidate còn 3 missing links rõ hơn, tập trung vào path
  duplicate submission chưa gọi `notifyError("Operation already in progress")`.
- Chạy tiếp đúng một vòng `continue-run` với limit 7.
- Developer.repair iteration 6 pass Verification Runner và Validator code trace:
  `status=implementation_traced`, `last_error=null`, `missing_count=0`,
  `developer_work_packet_v8.json`, `code_change_report_v8.md`,
  `test_report_v8.md`, `requirement_design_code_trace_v8.json`.

Việc tiếp theo:

- Phase 4 vẫn chưa khóa vì chưa có QA/Tester Agent, chưa commit target repo và
  chưa mở Draft PR.
## 2026-10-14 - Slice 4.7A: Live DeepSeek QA Generated Tests

Goal: verify that QA/Tester no longer only reviews in prose. It must generate tests, apply them to the target repo, and let Verification Runner decide pass/fail.

Executed:

- Command: `rtk proxy py -3.13 -m sdlc_harness.cli continue-run --run-id run_20261013_160421_f661babf --llm-mode deepseek`.
- Previous state: `implementation_traced`.
- Result state: `qa_tested`.
- New artifacts: `qa_generated_tests_v1.json`, `qa_test_execution_v1.md`, `qa_test_report_v1.md`.
- QA generated one test file: `src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageAcceptanceTest.java`.
- Verification Runner ran `powershell -ExecutionPolicy Bypass -File scripts/test.ps1`, exit code 0, stdout `All tests passed`.
- `quality_reports.qa_acceptance` recorded `ok=true`, 8 scenarios, and 5 risk notes.

Conclusion:

- The DeepSeek happy path for QA-generated acceptance tests passed.
- Slice 4.7A still intentionally does not implement the large repair loop from QA failure back to Developer.
- A separate QA fail/skipped live case is still needed before implementing the QA repair loop.


## 2026-10-14 - Slice 4.7B: QA Repair Loop

Da lam:

- Them `SDLC_QA_REPAIR_LIMIT` va CLI `continue-run --qa-repair-limit`.
- QA execution fail co the chuyen sang `qa_repair_needed` thay vi block ngay.
- Workflow ghi `qa_repair_feedback_v{n}.json` de Developer.repair doc duoc.
- `continue-run` tu `qa_repair_needed` chay Developer.repair -> Verification Runner
  -> Validator code trace -> QA-generated tests lai.
- Neu QA pass sau repair thi state=`qa_tested`; neu het limit thi block bang
  `qa_repair_limit_exceeded`.

Chua lam:

- Chua commit/Draft PR/sync Phase 4.
- Live DeepSeek fail -> Developer repair -> Validator -> QA pass đã được xác nhận
  ở mục ngay bên dưới bằng runtime copy `run_live_qa_repair_20261014_03`.

## 2026-10-14 - Live DeepSeek QA Repair Và Required-Test Execution

Mục tiêu: khóa Slice 4.7B bằng một lỗi acceptance thật trên runtime copy và xác
nhận vòng lớn QA -> Developer -> Runner -> Validator -> QA hội tụ.

Phát hiện trong lần chuẩn bị:

- `scripts/test.ps1` compile tất cả source/test nhưng chỉ execute
  `CulturalHeritageRepositoryTest`.
- Vì vậy QA-generated acceptance file có thể chưa chạy mà workflow vẫn báo pass.
- Verification Runner đã được sửa để execute trực tiếp mọi class trong
  `required_test_files` sau baseline command; regression test khóa false positive.

Live run `run_live_qa_repair_20261014_03`:

- runtime và target repo được copy cô lập; run/repo gốc không nhận defect seed;
- seed lỗi `cancelDeletion` xóa item;
- QA-generated class fail với `cancel must not delete the item`;
- state chuyển `qa_repair_needed`, ghi `qa_repair_feedback_v2.json`;
- DeepSeek Developer repair iteration 1, Verification Runner pass, Validator pass;
- QA sinh/chạy lại required class và pass;
- state cuối `qa_tested`, `qa_repair_iterations=1`, `last_error=null`.

Việc tiếp theo: commit/Draft PR thật và sync checkpoint code review để mở
`code_review_ready`. Live `skipped` case còn là hardening test riêng.

## 2026-10-15 - Slice 4.8A: GitHub Publication (Trước Live Test)

Đã triển khai:

- adapter GitHub sở hữu commit, push và Draft PR;
- publication manifest chỉ cho phép source/test thuộc Developer packet, QA test
  files và verification support scripts;
- commit có trailer `SDLC-Run` và được tái sử dụng khi retry;
- Draft PR được tạo mới hoặc cập nhật theo head/base branch, không tạo trùng;
- partial failure giữ evidence trong `github_code_review_v1.json`;
- workflow chuyển `qa_tested -> code_review_published`, còn Jira/Confluence sync
  và `code_review_ready` được giữ cho Slice 4.8B.

Kiểm thử trước live:

- `tests/test_github_sync.py`: 4 test pass trước khi bổ sung regression chạy lại
  workflow từ `code_review_published`;
- `tests/test_phase4.py`: 43 test pass;
- full suite và live GitHub publication sẽ được ghi tiếp sau khi chạy.

## 2026-10-15 - Slice 4.8A: Live GitHub Publication

Run thật: `run_20261013_160421_f661babf`.

Kết quả lần đầu:

- state chuyển `qa_tested -> code_review_published`;
- target branch `feature/sdlc-1-implementation` được push;
- commit `088416465cc7139255aafa120a0b5bb8f17cb971` có subject
  `SDLC-1 Implement approved use case` và trailer `SDLC-Run`;
- tạo Draft PR `#1`: `https://github.com/LuongAnh1/SDLC_Target_eTour/pull/1`;
- publication manifest gồm 14 file source/test/verification đúng evidence;
- target working tree sạch sau publication;
- `github_code_review_v1.json` ghi đầy đủ branch, commit, push và PR.

Kiểm tra idempotency bằng lần chạy thứ hai:

- state vẫn là `code_review_published`;
- commit SHA không đổi và tổng commit vẫn là `3`;
- GitHub vẫn chỉ có một Draft PR mở cho branch;
- artifact ghi `commit.operation=reused` và
  `pull_request.operation=updated`.

Kết quả test tại mốc 4.8A trước live: toàn bộ `130/130` unittest pass. Khi đó
Slice 4.8B vẫn còn nhiệm vụ sync PR/test/trace summary về Jira/Confluence và tạo
checkpoint `code_review_ready`; phần này đã được hoàn thành ở mục 4.8B bên dưới.

## 2026-10-15 - Slice 4.8B: Code Review Checkpoint Sync (Trước Live Test)

Đã triển khai:

- `code_review_published -> code_review_syncing -> code_review_ready`;
- mock/real adapter sinh `tool_sync_report_v4.json`, tham chiếu GitHub Draft PR
  và sync tóm tắt test/Validator trace/QA sang Jira + Confluence;
- partial failure chuyển `code_review_sync_failed`, giữ evidence và cho
  `sync-run` retry;
- `pending_checkpoint.type=code_review`, nhưng chưa mở approve/request-change;
- Jira comment và Confluence page dùng identity ổn định `source_ref + phase`;
- khi report local mất, adapter tìm target remote bằng marker; legacy target chỉ
  được migrate nếu tìm thấy duy nhất một bản, nếu nhập nhằng thì fail closed bằng
  `remote_sync_ambiguous`;
- Jira source loader bỏ comment do Harness sinh để run cũ không nhiễm vào source
  của Requirement Refiner;
- CLI side-effect mặc định `--sync-mode mock`; ghi tool thật phải chọn rõ `real`
  hoặc `both`. Niko có `NIKO_SDLC_SYNC_MODE` và local demo đang chọn `real`.

Kiểm thử trước live:

- full suite SDLC_Harness: `136/136` pass;
- Niko SDLC client + dashboard: `26/26` pass;
- đã cover remote recovery, legacy migration/ambiguity, partial failure/retry,
  CLI default mock và workflow mở checkpoint Code Review.

Nguyên nhân các lần CLI test trước đây tạo thêm Design review có run id khác:

- xóa `.runtime` làm mất active-run index và external id trong sync report;
- title/identity remote cũ gắn với `run_id`, nên adapter không tìm lại target;
- Jira source loader còn nạp chính comment Harness vào source, kéo run id cũ vào
  requirement packet mới.

Live gate còn lại: chạy run hiện tại với `--sync-mode real`, xác nhận đúng một
Jira comment và một Confluence page Code Review; chạy lại `sync-run` phải giữ
nguyên external ID và Draft PR `#1`.

## 2026-10-15 - Slice 4.8B: Live Code Review Sync

Run: `run_20261013_160421_f661babf`.

Lần sync đầu:

- state chuyển `code_review_published -> code_review_syncing -> code_review_ready`;
- Jira tạo Code Review comment `10104`;
- Confluence tạo page `2129921` với title ổn định
  `SDLC Harness - SDLC-1 - Code Review Checkpoint`;
- GitHub chỉ được tham chiếu tới Draft PR `#1`, không publish lại;
- `pending_checkpoint.type=code_review` và action chỉ gồm
  `get_status`, `sync_run`, `cancel_run`.

Lần `sync-run --sync-mode real` thứ hai:

- Jira `operation=updated`, external ID vẫn là `10104`;
- Confluence `operation=updated`, external ID vẫn là `2129921`;
- Draft PR vẫn là `#1` và state vẫn `code_review_ready`;
- không chạy nhánh Design sync.

Kết luận: Slice 4.8B đã pass code test và live test. Bước tiếp theo mới thiết kế
human approve/request-change cho Code Review checkpoint; 4.8B không giả lập hai
action này khi contract chưa tồn tại.
