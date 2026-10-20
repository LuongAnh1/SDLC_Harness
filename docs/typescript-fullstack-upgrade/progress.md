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

## 2026-10-19 - A1.2b Run-scoped Profile Snapshot

- Baseline: commit `d2bd1c3` (`feat: add project profile selector`).
- Thêm immutable `ProfileSnapshotV1` với version `profile_snapshot_v1`, profile
  ID/schema, language ID/schema và target remote/base branch. Reader chấp nhận
  additive field cho A1.2c nhưng fail closed với shape/version hoặc registry
  mismatch bằng `profile_snapshot_invalid`/`profile_snapshot_mismatch`.
- Run mới có `SDLC_PROJECT_PROFILE` ghi snapshot trước lần persist state đầu và
  trước LLM; selector rỗng không thêm snapshot. Active run không được backfill
  khi config đổi.
- `ApplicationService` đọc snapshot dưới run lock và bind lại
  `project_profile_id`, target remote và base branch trước mọi action mutation,
  sync hoặc workspace. Run thiếu snapshot vẫn dùng Java baseline trong giai đoạn
  migration; policy `legacy_execution_profile_retired` chưa được bật.
- Status/detail/list thêm `execution_profile` additive với classification
  `profiled`, `legacy_java_v1` hoặc `invalid`; không công khai target remote,
  branch hoặc local path. Status vẫn đọc được nếu snapshot hỏng, còn mutation bị
  chặn trước workflow dispatch.
- Targeted regression cho profile/Application Service/Phase 1/Read API:
  `87 passed in 16.75s`; HTTP Write API: `6 passed in 5.72s`.
- Full run đầu phát hiện hai CLI workspace mock chưa tạo persisted state:
  `424 passed, 2 failed in 796.05s`. Fixture được sửa để dùng runtime tạm đúng
  precondition; rerun hai test -> `2 passed in 0.21s`.
- Full regression trên revision cuối: `rtk python -m pytest` ->
  `426 passed in 792.39s (0:13:12)`.
- Chưa triển khai A1.2c manifest/base SHA/component roots, legacy retirement
  guard, TypeScript prompt, runner hoặc target repo bootstrap.

## 2026-10-19 - A1.2c Target Manifest Và Workspace Binding

- Baseline: commit `8a5e1bb` (`feat: bind project profiles to workflow runs`).
- Thêm reader bất biến cho `.sdlc/target-profile.json`, đối chiếu chính xác
  profile/language identity, component roots, protected/required paths và
  verification stage IDs với registry.
- Khi provision profiled workspace, tính digest từ raw manifest bytes và bổ sung
  `target_manifest_schema`, `target_manifest_digest`, `base_sha` cùng
  `resolved_component_roots` vào `profile_snapshot_v1` trước CodeGraph/LLM.
- Mọi lần resolve/lock workspace sau đó phải fail closed nếu state, workspace,
  registry hoặc manifest không còn khớp. Run không có snapshot tiếp tục đi theo
  Java migration baseline và không bị buộc có manifest.
- Kiểm thử theo tầng: profile manifest contract, Git workspace rollback/retry,
  Phase 3 preflight ordering, targeted regression rồi full `pytest`.
- Reader chỉ nhận exact `target_profile_manifest_v1`, tính SHA-256 từ raw bytes,
  từ chối unknown field/version, path thiếu và symlink/path thoát khỏi worktree.
- Workspace create/retry ghi binding cùng state reconcile; resolve, lock và
  refresh xác minh lại digest/base SHA/component roots. Manifest mismatch được
  rollback trước CodeGraph/Developer. Rollback branch dùng force-delete chỉ sau
  khi đã chứng minh branch vẫn trỏ đúng fetched base SHA và worktree còn sạch.
- Targeted profile/workspace/Phase 3: `82 passed in 264.66s`; Application
  Service/Read API/Write API: `48 passed in 18.12s`.
- Full regression: `rtk python -m pytest` ->
  `435 passed in 864.38s (0:14:24)`.
- Review follow-up đã chuẩn hóa thứ tự `resolved_component_roots` để thứ tự key
  trong JSON không làm thay đổi ngữ nghĩa/equality của snapshot; đồng thời thêm
  regression test cho manifest có component roots đảo thứ tự.
- `locked_workspace` nay có thể backfill an toàn binding A1.2c cho workspace
  profiled cũ chỉ sau khi manifest, registry, state và workspace đã được xác minh
  đầy đủ dưới lock. `resolve` vẫn read-only và fail closed; regression test bao
  phủ đường nâng cấp từ snapshot pre-binding.
- Sửa vòng import sạch đã tồn tại giữa `profiles` và `core.config` bằng lazy
  public exports trong `sdlc_harness.core`; subprocess test mới xác nhận import
  `sdlc_harness.profiles` trước rồi dùng `Settings`, `load_settings`, `repo_root`
  vẫn hoạt động trong Python process mới.
- Review regression: profile/workspace/import contracts
  `38 passed in 124.18s`; Application Service/profile/Phase 3/Phase 4/Read/Write
  API `153 passed in 618.18s`.
- Full regression sau review: `rtk python -m pytest` ->
  `438 passed in 736.48s (0:12:16)`.
- Checklist A1.2c và state/workspace/registry/manifest fail-closed đã hoàn tất.
- Chưa thuộc lát này: TypeScript prompt/source discovery, verification runner,
  QA/code trace, target repo bootstrap và legacy retirement.

## 2026-10-20 - A1.2d Legacy Run Retirement Guard

- Baseline: commit `6b52714` (`test: optimize integration suite runtime`).
- Chốt hard retirement, không thêm feature flag: run thiếu
  `profile_snapshot_v1` không được sinh thêm requirement/design/code/test
  artifact; run mới phải có `SDLC_PROJECT_PROFILE` trước khi load source hoặc
  gọi LLM.
- Giữ read/audit, sync artifact/checkpoint đã tồn tại và workspace cleanup.
  Không mở rộng `cancel_run` vì action này chưa thuộc HTTP contract hiện hành.
- Lỗi contract dự kiến là `project_profile_required` cho start thiếu profile và
  `legacy_execution_profile_retired` cho mutation trên legacy run; cả hai được
  ánh xạ HTTP `409 Conflict`.
- Legacy workspace sync/cleanup phải dùng identity đã persist trong workspace
  record, không dùng target TypeScript từ config hiện hành và không backfill
  profile snapshot.
- Đã đặt guard tập trung tại `ApplicationService`: start thiếu profile fail trước
  source/LLM; active legacy run cùng source và mọi mutation sinh artifact bị
  chặn trước workflow dispatch. CLI/HTTP dùng lỗi ổn định, HTTP map `409`.
- Read API và application response chỉ project `get_status`/`sync_run` cho
  legacy; persisted state không bị rewrite. Design experiment và workspace
  adoption cũng bị retire; `cancel_run` không được thêm vào contract.
- Code Review sync và cleanup legacy resolve target identity từ persisted
  workspace record. Git integration chứng minh cleanup vẫn dùng đúng Java repo
  sau khi config hiện hành đã đổi sang target khác; record path/base sai fail
  closed và không dispatch cleanup.
- Regression cuối: targeted ban đầu `109 passed in 28.06s`; fast
  `160 passed, 290 deselected in 6.97s`; full parallel 4 worker chạy hai lần
  `450 passed in 62.82s` và `450 passed in 64.60s`. Sau khi bổ sung edge case
  `profile_snapshot: null`, nhóm profile/application/read/write API tiếp tục qua
  `73 passed in 14.75s`; full parallel cuối cùng đạt `452 passed in 55.59s` và
  `compileall` riêng package thành công. Checklist A1.2d hoàn tất; toàn bộ A1 đã
  đóng.

## 2026-10-20 - A2.1a Profile-aware Phase 3 Source Discovery

- Baseline: commit `fefff44` (`feat: retire legacy Java execution profile`).
- Phạm vi lát này chỉ thay source discovery trước CodeGraph/Developer: dùng
  workspace-bound `profile_snapshot_v1`, component editable roots và language
  source/test suffixes; chưa đổi default target, prompt, raw verification
  command hay Plan Validator.
- Kết quả discovery phải là POSIX-relative, unique, deterministic; file ngoài
  approved roots hoặc sai suffix không được đưa vào context. Snapshot profiled
  chưa bind workspace và path resolve thoát workspace phải fail closed.
- Giữ Java discovery cũ chỉ làm compatibility nội bộ cho direct workflow tests
  trong giai đoạn A2-A3; Application Service vẫn hard-retire legacy execution.
- Đã thêm discovery component dưới `repo_context`: snapshot phải workspace-bound;
  source/test suffix lấy từ `LanguageProfile`; roots lấy từ snapshot đã bind.
  Kết quả được dedupe, sort và chuẩn hóa POSIX; missing/escaping path fail closed.
- Phase 3 planning lần đầu và revalidation dùng chung discovery path. Integration
  profiled chứng minh cả hai lần đều nhận `apps/api/src/ExistingService.ts` và
  không nhận source Java nằm ngoài approved component roots.
- Regression: profile/discovery/manifest/Phase 3/plan contract
  `66 passed, 1 skipped in 45.74s`; fast suite
  `166 passed, 1 skipped, 291 deselected in 8.86s`; full parallel 4 worker
  `457 passed, 1 skipped in 54.13s`; `compileall` package thành công. Test skip là
  case symlink escape vì Windows hiện tại không cấp quyền tạo symlink.
- Checklist A2.1a hoàn tất. A2.1b vẫn chưa làm: default targets, test targets và
  Validator path rules hiện còn Java-specific.

## 2026-10-20 - A2.1b Profile-aware Planning Paths

- Baseline: commit `0a8fd94` (`feat: discover phase 3 sources from profile`),
  worktree sạch; branch `main` đang ahead remote 7 commit.
- Đã rà lại `DeveloperPlanAgent`, `ImplementationPlanValidator`, Phase 3 initial
  planning/revalidation, profile snapshot, source discovery và fake Phase 2/3.
- Chốt một planning-path policy bất biến theo `project_profile_id` và
  `language_profile_id`; planner và Validator phải resolve cùng policy từ frozen
  workspace snapshot, không nhận policy do LLM cung cấp.
- Routing class ưu tiên existing symbol duy nhất, sau đó component hint/stereotype
  và name suffix. Thiếu hoặc mâu thuẫn thông tin sẽ fail closed để sửa design;
  không fallback sang API. Output LLM không được ghi đè target/component tất định.
- Phạm vi lát này gồm default source/test target, canonical path union, path hard
  gate và metadata audit của implementation plan. Raw verification command,
  Phase 4 code generation và bootstrap target repo vẫn thuộc lát sau.
- Đã thêm `planning_path_policy_v1` cho `typescript_fullstack_v1`: source/test
  target theo component, Prisma và E2E có suffix riêng; path phải canonical
  POSIX-relative và nằm trong editable root của frozen profile.
- Developer.plan khóa `planned_code_target`/`component_id` do Python resolve,
  loại path LLM ngoài policy, thêm `planning_profile` để audit và tắt Java-only
  final-field adjustment ở run TypeScript. Class không phân loại được, có hint
  mâu thuẫn hoặc trùng nhiều existing symbol sẽ fail closed bằng error code ổn định.
- Validator.review_plan dùng đúng policy truyền từ workspace-bound snapshot cho
  mapping, implementation steps, proposed files, owner symbol và bắt buộc flow/
  repository test target co-located. Nhánh Java chỉ còn compatibility nội bộ.
- Fake Phase 2 đã có canonical component stereotypes; fake Developer.plan tự trả
  TypeScript paths khi prompt mang profile mới. Fake Phase 4/QA vẫn giữ Java vì
  chuyển execution thuộc A3; checklist chuyển toàn bộ prompt/fake fixtures vẫn mở.
- Regression: planning-path unit `7 passed`; profile/manifest/discovery/Phase 3
  `73 passed, 1 skipped in 63.82s`; fast suite `173 passed, 1 skipped, 291
  deselected in 11.73s`; full parallel 4 worker `464 passed, 1 skipped in
  71.66s`; `compileall` package thành công. Test skip vẫn là symlink escape trên
  Windows không có quyền tạo symlink.
- Checklist A2.1b và plan Validator profile-aware hoàn tất. HTTP API, checkpoint
  schema chính và raw verification command không thay đổi.

## 2026-10-20 - Test Performance Optimization Baseline

- Full regression A1.2c: `438 passed in 736.48s`; collection `438 tests` chỉ
  mất `0.34s`, vì vậy thời gian nằm ở execution thay vì discovery/import.
- Đo riêng với `--durations`: Phase 4 `58 passed in 382.87s`, Phase 3
  `35 passed in 145.58s`, workspace Git `32 passed in 116.81s`. Ba file chiếm
  khoảng 87,6% tổng thời gian.
- Nguyên nhân đã xác nhận: bare remote/target repo được dựng lại trong mỗi
  `setUp`, test Phase 4 thường chạy lại toàn bộ checkpoint trước đó, và test
  thuần đang trả cả chi phí Git dù không kiểm tra Git.
- Quyết định: tối ưu fixture/state factory trước, sau đó thêm `pytest-xdist`;
  giữ Git integration và Phase 1-4 journey thật. Ngân sách: serial <= 360 giây,
  parallel 4 worker <= 240 giây, fast suite <= 60 giây.
- Kế hoạch và guardrail chi tiết ở `test-performance-optimization.md`.
- Triển khai test infrastructure:
  - Git repository template bất biến theo worker, runtime checkpoint template
    có rewrite run/path và overlay file cho worktree;
  - fast workspace factory vẫn tạo Git worktree thật nhưng bỏ guard/probe đã có
    workspace contract suite riêng;
  - snapshot Phase 1/2/3/4 để test bắt đầu đúng checkpoint cần kiểm tra;
  - marker `integration`/`journey`, strict marker và optional dependency
    `pytest-xdist`.
- Giữ production provisioning/validation thật cho workspace contract, profile
  preflight, target guard và journey dài đến Code Review Approved.
- Kết quả sau tối ưu:
  - fast: `151 passed, 287 deselected in 16.29s`;
  - parallel 4 worker: `438 passed in 135.60s`, chạy lặp
    `438 passed in 137.55s`;
  - serial: `438 passed in 407.64s`.
- Fast và parallel đạt ngân sách. Serial giảm 44,7% nhưng còn vượt mục tiêu
  360 giây 47,64 giây; không tiếp tục mock Git contract/journey để chạy theo số.

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
