# Progress And Execution Log

Execution log này chỉ ghi công việc đã thực hiện. Roadmap chưa chạy nằm trong
`implementation-plan.md` và không được đánh dấu hoàn tất tại đây.

## 2026-10-19 - Baseline Và Scope

- Xác nhận `SDLC_Harness` ở branch `main`, HEAD `e07c9be`, worktree sạch trước
  khi viết tài liệu.
- Đọc `AGENTS.md`, root `README.md`, `docs/architecture.md`,
  `docs/roadmap.md`, flow Phase 3-4 và tài liệu QA/workspace liên quan.
- Chốt phạm vi phiên này là docs-only; không sửa code/config/runtime và không
  tạo target repo.

## 2026-10-19 - Current Flow Survey

- Dùng CodeGraph trước khi tìm text để lần theo Phase 1-4, Developer, Validator,
  Verification Runner, QA và target preparation.
- Đối chiếu HTTP API-only boundary với application/workflow docs.
- Xác nhận Phase 1-2 phần lớn độc lập target; Java coupling tập trung ở Phase
  3-4 và fake fixtures/sync wording.
- Xác nhận target preparation chỉ validate Git/remote/clean state và CodeGraph,
  không scaffold project.

## 2026-10-19 - Java Coupling Survey

- Rà source/test roots, default mappings, work-packet/apply guards, repair
  context, Java signature regex, JDK discovery, Java test entrypoint execution
  và QA path validation.
- Phân biệt prompt legacy/reference với prompt được dựng tại runtime.
- Kiểm tra repo `SDLC_Target_eTour`: Java source/test tối thiểu và
  `scripts/test.ps1` là bootstrap thực tế.

## 2026-10-19 - Full-stack Gap Survey

- Xác nhận design hiện hành tập trung class/sequence graph, chưa có UI/API/data
  contract.
- Xác nhận plan/work packet hiện là tập file phẳng, chưa có component/dependency.
- Xác nhận Verification Runner chọn một baseline command và chưa sở hữu DB,
  service hoặc browser lifecycle.
- Xác nhận code trace/QA chưa chứng minh luồng UI -> API -> persistence -> E2E.

## 2026-10-19 - Owner Decisions

- Chọn roadmap hai chặng: TypeScript profile trước, full-stack workflow sau.
- Chọn repo mới `SDLC_Target_TypeScript`; giữ `SDLC_Target_eTour` riêng.
- Chọn React/Vite + Fastify + PostgreSQL/Prisma, npm + Vitest và browser E2E với
  Playwright.
- Chọn infra skeleton trong target repo và một vertical feature cho mỗi run.
- Chọn không duy trì Java execution profile sau cutover; legacy policy phải được
  khóa rõ trước khi triển khai.

## 2026-10-19 - Docs Authored

- Tạo front door cho hướng TypeScript full-stack.
- Ghi current-flow/Java/full-stack gap audit có evidence theo owner/symbol.
- Ghi target direction ở trạng thái proposal, không mô tả như runtime hiện hành.
- Ghi implementation roadmap với toàn bộ checklist để mở.
- Ghi execution log này; chưa chạy implementation test hoặc live workflow nào.

## 2026-10-19 - Gate 0 Review

- Đối chiếu lại audit với config/target preflight, run/workspace state, Phase 2
  artifact writer, Phase 3 structured plan/Validator, Phase 4 Verification
  Runner/code trace/QA và tests hiện hành.
- Phát hiện run chưa bind execution profile bất biến; raw test command còn đi từ
  implementation plan vào runner; cleanup và legacy-cutover policy chưa đủ rõ.
- Khóa `profile_snapshot_v1`, target manifest handshake, schema versioning,
  stage-ID-to-fixed-argv policy và `always_run` cleanup.
- Khóa legacy Java policy: giữ read/audit/sync-existing/cancel/cleanup, chặn
  action sinh artifact mới sau cutover và không auto-migrate run.
- Chốt canonical remote dự kiến, protected bootstrap files và acceptance evidence
  riêng cho Chặng A/B trong `gate-0-review.md`.
- Đánh dấu Gate 0 hoàn tất ở mức tài liệu. Chưa sửa code/config/runtime, chưa tạo
  target repo và chưa chạy TypeScript live workflow.
- Chạy `rtk python -m pytest`: `401 passed, 1 failed` trong 722.77 giây; failure
  duy nhất là `ConnectionAbortedError [WinError 10053]` tại HTTP socket của
  `test_write_validation_auth_and_missing_operation_errors_are_json`.
- Chạy lại đúng test lỗi: `1 passed in 0.76s`; chạy lại toàn bộ
  `tests/test_write_api.py`: `6 passed in 6.33s`. Lỗi full run không tái hiện và
  không phát sinh từ thay đổi tài liệu; log vẫn giữ kết quả full run thay vì mô
  tả thành `402 passed`.

## 2026-10-19 - A1.1 Language/Project Profile Registry

- Thêm immutable contract `LanguageProfile`, `ComponentProfile`,
  `ProjectProfile` và `ProfileRegistry` dưới `sdlc_harness/profiles/`.
- Đăng ký `typescript_v1` và `typescript_fullstack_v1` với component roots cho
  web/API/contracts/database/E2E, protected/required paths và ordered stage IDs.
- Registry chỉ chứa declarative policy; không có raw command, không được import
  bởi workflow và chưa đổi config/default target.
- Thêm fail-closed validation cho identifier, suffix, duplicate/overlapping
  ownership, protected-path overlap, POSIX-relative path và missing language
  reference. Unknown profile dùng error code ổn định.
- Targeted test sau schema-version guard:
  `rtk python -m pytest tests/test_profiles.py -q` -> `8 passed in 0.04s`.
- Full regression trên revision cuối: `rtk python -m pytest` ->
  `409 passed, 1 failed in 755.27s`; failure duy nhất tiếp tục là
  `ConnectionAbortedError [WinError 10053]` tại cùng HTTP socket test đã ghi ở
  Gate 0, trong khi `tests/test_profiles.py` pass đủ 8 test trong full run.
- Rerun đúng test lỗi -> `1 passed in 0.77s`; rerun toàn bộ
  `tests/test_write_api.py` -> `6 passed in 6.08s`. Failure không tái hiện và
  không nằm trên dependency/call path của package profile mới.
- Đánh dấu duy nhất checklist registry của A1 hoàn tất. Chưa thêm
  `SDLC_PROJECT_PROFILE`, `profile_snapshot_v1`, target manifest reader hoặc
  runtime integration.

## 2026-10-19 - Việt Hóa Chú Thích Đầu File

- Rà module docstring đầu file trong `sdlc_harness/`, `tests/` và `scripts/`;
  các tiêu đề/mô tả tiếng Anh được chuyển sang tiếng Việt, giữ nguyên thuật ngữ
  kỹ thuật, tên symbol, error code và contract máy đọc.
- Không sửa prompt nghiệp vụ, chuỗi giao tiếp API hay logic runtime.
- Kiểm tra cú pháp toàn bộ bằng
  `rtk python -m compileall -q sdlc_harness tests scripts` -> pass.
- Chạy lại `rtk python -m pytest tests/test_profiles.py -q` ->
  `8 passed in 0.04s`.

## 2026-10-19 - A1.2a Project Profile Config Selection

- Thêm `SDLC_PROJECT_PROFILE` vào target config schema và `.env.example`; option
  được lấy từ profile registry, mặc định rỗng để giữ Java baseline trong giai
  đoạn migration.
- Mở rộng `Settings` bằng `project_profile_id: str | None`. Giá trị khác rỗng
  được resolve fail-closed qua registry; `.env`/OS/override dùng profile lạ trả
  `project_profile_not_supported`.
- Config API chấp nhận/reset `typescript_fullstack_v1`, từ chối option lạ bằng
  `config_invalid` trước khi ghi. Runtime config file bị can thiệp trực tiếp với
  option lạ tiếp tục fail `config_corrupted`.
- Chưa ghi `profile_snapshot_v1`, chưa thêm metadata vào run/status và chưa nối
  profile vào Phase 1-4 hoặc workspace.
- Targeted regression:
  `rtk python -m pytest tests/test_profiles.py tests/test_runtime_config.py tests/test_config_api.py -q`
  -> `24 passed in 4.09s` trên revision cuối.
- Full regression: `rtk python -m pytest` ->
  `413 passed in 775.82s (0:12:55)`.

## Mẫu Ghi Cho Các Lần Sau

```text
## YYYY-MM-DD - <slice hoặc quyết định>

- Baseline/commit:
- Thay đổi đã thực hiện:
- Lệnh kiểm tra và kết quả:
- Artifact/run/PR evidence:
- Quyết định hoặc deviation so với plan:
- Việc còn lại:
```
