# Phase 4 Progress

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
- `continue-run` từ `implementation_repair_needed` trả
  `implementation_repair_not_implemented` để tránh hiểu nhầm là đã có auto repair.

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
