# Phase 3 Checklist

Checklist này dùng khi triển khai Phase 3: design approved -> implementation
plan. Không tick chỉ vì tài liệu đã viết xong; chỉ tick khi code/test hoặc smoke
tương ứng đã hoàn thành.

## 1. CLI/API Approval Contract

Mục đích: human/Niko có thể duyệt design checkpoint và kích hoạt Phase 3 bằng
contract JSON ổn định.

- [x] `approve-checkpoint` chỉ mở Phase 3 khi run đang `waiting_design_approval`.
- [x] Nếu checkpoint không phải `design_approval`, trả `checkpoint_not_current`.
- [x] Success trả JSON stdout, không trộn log tự do.
- [x] Error trả JSON `ok=false`.
- [x] `get-status` đọc được state Phase 3 mới.

## 2. Approved Design Input

Mục đích: Phase 3 dùng requirement/design đã duyệt, không tự refine hoặc design
lại.

- [x] Đọc approved `requirement_packet_v1`.
- [x] Đọc approved `design_artifact_v{n}`.
- [x] Đọc `requirement_design_trace_v{n}` và graph liên quan.
- [x] Ghi `approved_artifacts.design_artifact`.
- [x] Nếu thiếu design đã duyệt, trả lỗi rõ và không chạy Developer.

## 3. Target Repo Guard

Mục đích: Phase 4 có nơi code an toàn; Phase 3 khóa code lineage bằng
branch/worktree riêng nhưng chưa sửa source/test, commit/push hoặc tạo PR.

- [x] Có target repo path/remote ban đầu: `D:\Dự án cá nhân\Do_an_II\SDLC_Target_eTour` / `LuongAnh1/SDLC_Target_eTour`.
- [x] Có config target repo path/remote trong SDLC Harness runtime/config.
- [x] Có guard không thao tác nhầm repo `SDLC_Harness`.
- [x] Có guard worktree dirty trước khi mở plan checkpoint.
- [x] Plan ghi rõ target repo/base branch/planned branch.
- [x] Duyệt design tạo `run_workspace_v1` từ remote base SHA đã fetch.
- [x] Canonical repo không bị checkout hoặc dùng làm working directory của run.

## 4. Developer.plan

Mục đích: tạo implementation plan có cấu trúc từ graph đã duyệt, không sinh code
mù và không tự sửa graph.

- [x] Tạo prompt/module Developer riêng cho Phase 3.
- [x] Sinh `implementation_plan_v1.md`.
- [x] Developer đọc requirement/design/trace đã duyệt.
- [x] Plan có bảng mapping design element -> planned code target.
- [x] Plan có test intent hoặc test command dự kiến.
- [x] Plan có assumptions/open questions và out-of-scope.
- [x] Plan dùng branch/worktree đã tạo cho run nhưng không commit hoặc sửa
  source/test trong Phase 3.

## 5. Validator.review_plan

Mục đích: kiểm plan đủ thông tin trước khi human duyệt và trước khi Phase 4 sửa
code.

- [x] Validator kiểm coverage: design class/method/message quan trọng có planned code target.
- [x] Validator kiểm traceability: requirement-design trace được phản ánh vào plan.
- [x] Validator kiểm executability: repo/branch/path/test intent đủ rõ.
- [x] Validator kiểm scope: plan không bịa thêm ngoài requirement/design.
- [x] Validator chạy hybrid: rule hard gate trước, LLM review semantic/content sau.
- [x] LLM review đối chiếu requirement packet, design artifact, trace graph và structured plan.
- [x] Validator LLM review chịu được JSON bọc prose/fence; invalid JSON còn lại
  trở thành review fail có report thay vì làm workflow vỡ state.
- [x] Có config `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT` để giới hạn số vòng repair plan.
- [x] Mỗi vòng repair ghi artifact version riêng `implementation_plan_v{n}` và review report tương ứng.
- [x] Sinh `implementation_plan_review_v1.json` hoặc quality report tương đương.
- [x] Nếu plan fail, workflow repair plan hoặc `blocked`, không mở checkpoint duyệt.

## 6. Tool Sync Phase 3

Mục đích: plan được đẩy sang đúng tool review, còn GitHub để Phase 4.

- [x] Tạo/cập nhật Confluence page cho implementation plan.
- [x] Jira comment ngắn có link Confluence, run id và trạng thái chờ duyệt.
- [x] Ghi `tool_sync_report_v3.json` với `phase=implementation_plan_review`.
- [x] Confluence render bảng review thật từ structured plan, không hiển thị bảng Markdown thô như paragraph.
- [x] Phase 3 không reuse target Phase 2; chỉ re-sync target trong report v3.
- [x] Re-sync không tạo comment/page trùng nếu report v3 đã có external id.
- [x] Nếu Jira comment v3 bị xóa thủ công, re-sync tự tạo comment mới.
- [x] Partial failure không xóa artifact local.
- [x] Không commit/push/tạo GitHub PR trong Phase 3; local run branch chỉ khóa
  workspace identity cho plan và Phase 4.

## 7. Workflow State Và Niko Reporting

Mục đích: Niko báo được Phase 3 đang ở đâu mà không hiểu nội bộ Developer.plan/
Validator.review_plan.

- [x] Ghi state `implementation_planning`.
- [x] `pending_checkpoint.type=implementation_plan_review` cho Slice 1 trước khi sửa code.
- [x] `allowed_next_actions` gồm `approve_checkpoint`, `request_change`, `cancel_run`, `get_status`.
- [x] `latest_artifacts.implementation_plan` trỏ tới plan mới nhất.
- [x] `quality_reports.implementation_plan_review` trỏ tới review mới nhất.
- [x] Niko hiển thị link plan và checkpoint approve plan ngắn gọn.

## 8. Test Và Acceptance

Mục đích: Phase 3 phải chạy được từ terminal trước khi live qua Niko.

- [x] Unit test approve design checkpoint happy path.
- [x] Unit test missing approved design.
- [x] Unit test target repo guard.
- [x] Unit test Developer.plan mapping format.
- [x] Unit test Validator.review_plan missing design element.
- [x] Unit test Validator.review_plan scope violation.
- [x] Unit test LLM/hybrid review happy path.
- [x] Unit test repair loop pass sau một vòng repair.
- [x] Unit test repair loop tôn trọng `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT=0`.
- [x] Unit test sync implementation plan Jira/Confluence.
- [x] Unit test blocked khi plan review fail quá giới hạn repair.
- [x] Tạo private target repo GitHub `LuongAnh1/SDLC_Target_eTour` với Java skeleton tối thiểu.
- [x] Smoke test CLI từ run Phase 2 thật sang `waiting_implementation_plan_review`.
- [x] Smoke test qua Niko sau khi CLI pass.
- [x] Live Telegram/Niko Phase 3 từ Jira tới `waiting_implementation_plan_review` pass với run `run_20261013_160421_f661babf`.
- [x] Ghi nhận bài học timeout: approve design -> implementation plan cần timeout Niko đủ lớn, live pass với `SDLC_HARNESS_TIMEOUT_SECONDS=300`.

## 9. Smart Developer + CodeGraph Context

Mục đích: Developer.plan không chỉ đoán file từ graph, mà đọc target repo bằng
CodeGraph để lập plan có evidence, implementation steps và proposed files rõ hơn.

- [x] Phase 3 tự chuẩn bị `.codegraph/` trong run worktree trước khi collect context.
- [x] CodeGraph init/collect lỗi thì block bằng error contract rõ ràng.
- [x] Có lệnh setup riêng `prepare-target-repo --init-codegraph` cho debug thủ
  công, nhưng run mới không phụ thuộc lệnh này.
- [x] Dirty guard bỏ qua state `.codegraph/` nhưng vẫn chặn thay đổi source/test/docs.
- [x] Developer.plan nhận `repo_context` từ CodeGraph.
- [x] Developer LLM sinh `repo_evidence`, `implementation_steps`, `proposed_files`.
- [x] Parser chấp nhận JSON object khi LLM bọc thêm prose/code fence quanh output.
- [x] Nếu LLM vẫn trả invalid JSON, Developer.plan fallback về deterministic
  graph mapping và ghi cảnh báo parse trong structured plan.
- [x] Có probe DeepSeek raw output không mutate runtime, lưu vào `.debug/`.
- [x] Prompt Developer.plan không yêu cầu LLM lặp lại toàn bộ base mapping; Python
  giữ mapping đầy đủ, LLM chỉ trả enrich/override ngắn để tránh JSON bị cắt.
- [x] Nếu Developer.plan hoặc repair_plan fail, state chuyển `failed`, xóa
  checkpoint cũ và ghi `last_error` có `run_id`.
- [x] Structured plan lưu `planning_mode=llm_developer_with_codegraph_context_v1`.
- [x] Unit test missing CodeGraph index.
- [x] Unit test Phase 3 pass: `rtk python -m pytest tests/test_phase3.py`.

## 10. Requirement Contract V2 - Slice 4B

- [x] Phase 3 đọc frozen requirement từ `approved_artifacts`, không đọc nhầm projection mới nhất.
- [x] V2 đối chiếu canonical revision/model ref, saved requirement graph và exact stable ID set trong trace.
- [x] Tamper V2 fail trước khi tạo worktree hoặc mutate state bằng `requirement_v2_phase3_input_invalid`.
- [x] Developer prompt đặt canonical model trước packet projection.
- [x] Python tạo lại `requirement_coverage` sau LLM mapping merge và sau mỗi repair.
- [x] Validator V2 hard-gate ID thiếu/trùng/tự bịa, target ngoài trace, thiếu code target hoặc test intent.
- [x] Markdown và Confluence render Requirement Coverage.
- [x] State/response giữ `requirement_contract_version=v2` tại `waiting_implementation_plan_review`.
- [x] Gate V2 được chuyển sang Phase 4 với `requirement_v2_phase4_not_ready` và không mutate state.
- [x] V1 regression tiếp tục dùng contract mặc định và schema artifact hiện có.
- [ ] Về sau bổ sung redact/filter hoặc local model để tránh gửi source snippets nhạy cảm ra LLM ngoài.
