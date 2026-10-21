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

## 2026-10-20 - A2.2a Phase 3 Prompt Directive

- Baseline: commit `5053db2`; A2.1b đã khóa planning path theo profile.
- Lập `prompt-usage-map.md`, xác nhận Developer/Tester prompt `.txt` chỉ là
  reference-only; prompt Phase 3/4 đang hoạt động được dựng tại call site.
- Thêm registry `LanguagePromptDirective` và `typescript_prompt_v1`; resolve từ
  frozen run snapshot, fail closed khi directive không tồn tại hoặc mismatch.
- Developer.plan và Validator.review_plan giữ invariant riêng của role, nhận
  cùng language directive và path context từ `PlanningPathPolicy`. Path ví dụ
  được sinh từ component rule thay vì hard-code một project path.
- Fake Developer.plan chọn TypeScript output bằng directive ID. Fake Phase 4/QA
  vẫn giữ nguyên và được tách thành A2.2b sau execution guard của A3.
- Targeted regression ban đầu phát hiện một direct-call test chỉ truyền planning
  policy. Agent boundary đã bổ sung resolver từ language profile cho direct
  caller, còn workflow thật vẫn truyền directive từ snapshot.
- Verification: prompt/profile/planning-path targeted `26 passed`; Phase 3 và
  implementation-plan contract `43 passed`; fast suite `178 passed, 1 skipped,
  291 deselected in 11.99s`; full parallel 4 worker `469 passed, 1 skipped in
  95.25s`; `compileall` package thành công. Test skip vẫn là symlink escape trên
  Windows không có quyền tạo symlink.
- HTTP API, state/checkpoint và artifact contract không thay đổi; raw
  verification command vẫn thuộc lát A2 tiếp theo.

## 2026-10-20 - A2.3 Verification Stage ID

- Baseline: commit `60584ca`; A2.2a đã chuyển prompt Phase 3 sang language
  directive nhưng profiled plan vẫn nhận raw PowerShell command.
- Thêm `VerificationStagePolicy` bất biến theo project-profile identity trong
  snapshot; policy chỉ chứa ordered active stage IDs và không chứa dữ liệu thực thi.
- Developer plan/repair, prompt và fake profiled phát `verification_stage_ids`
  cùng `test_intents[].stage_id`; Markdown/Confluence hiển thị stage thay command.
- Validator chặn exact-list mismatch, stage thiếu/không hợp lệ và mọi field
  `command`/`argv`/`cwd`/`env`/timeout trong profiled plan.
- Work packet chuyển tiếp stage IDs nhưng A2.3 chưa sửa Runner; mapping fixed
  argv/cwd/env/timeout, execution và editable-root guard vẫn thuộc A3.
- Nhánh command-based chỉ được giữ để đọc/test artifact legacy đã bị public
  Application Service retire; không dùng cho run có project profile.
- Verification: targeted profile/prompt/Phase 3 `31 passed`; Phase 3 và
  implementation-plan contract `43 passed`; fast suite `182 passed, 1 skipped`;
  full parallel cuối `473 passed, 1 skipped in 101.79s`; `compileall` thành công.
- Một lượt full trung gian gặp Windows local HTTP connection abort tại một ca
  Write API; ca đó pass khi chạy riêng và full rerun ngay sau đó pass toàn bộ.
- Không có live run hoặc external sync; HTTP API/action contract không đổi.

## 2026-10-20 - Pre-A3 Logic Review Và Hardening

- Review A2.3 phát hiện profiled plan đã đổi `command` sang `stage_id` nhưng vẫn
  dùng schema V1; đồng thời profile công bố full-stack stage list dù acceptance
  Chặng A chỉ yêu cầu typecheck/unit.
- Chuyển profiled plan sang `implementation_plan_structured_v2`; legacy plan giữ
  V1. Developer work packet thêm `schema_version=developer_work_packet_profiled_v1`
  riêng, không chiếm field `version` đang dùng cho revision/repair.
- Review report bind SHA-256 và contract version của structured plan; Phase 4
  chặn artifact bị đổi hoặc khác schema trước khi chuẩn bị work packet.
- Giữ nguyên full-stack stage catalog của `typescript_fullstack_v1`; khóa active
  `VerificationStagePolicy v1` của Chặng A còn `typecheck -> unit`. Chặng B phải
  phát hành policy/profile version mới thay vì đổi ngầm contract của run cũ.
- Legacy Java Verification Runner fail-closed khi nhận packet stage-only, tránh
  chạy `scripts/test.ps1` nếu editable-root guard được mở trước stage executor A3.
- Test symlink chỉ skip lỗi quyền/không hỗ trợ đã biết; OSError bất thường được
  phát lại để không che lỗi setup hoặc regression.
- Verification: contract/profile trọng điểm `37 passed`; Phase 3-4 `96 passed`;
  profile/manifest/workspace `81 passed, 1 skipped`; full parallel 4 worker
  `477 passed, 1 skipped in 70.47s`; `compileall` package và `git diff --check`
  thành công. Skip duy nhất vẫn là Windows không có quyền tạo symlink; Node/JDK
  conditional tests đều chạy và pass trong full suite.

## 2026-10-20 - A3.1 Profile-owned Editable Path Guards

- Baseline: commit `4f91cd6`; profiled work packet đã có schema riêng nhưng
  build/refresh/apply vẫn bị khóa bởi prefix Java.
- Thay prefix đó bằng `PlanningPathPolicy` resolve từ immutable run snapshot.
  `developer_work_packet_profiled_v1.output_contract` lưu metadata
  `editable_path_policy_v1` gồm profile identity, policy version, component roots
  và protected paths để audit.
- Build, refresh, batch apply, implementation repair, QA repair và revalidate đều
  dùng lại policy từ state; thiếu policy, metadata mismatch hoặc file ngoài
  editable roots/sai file kind bị chặn trước khi đọc hay ghi target file.
- Giữ hai lớp scope: profile editable root là biên trên, exact `allowed_paths`
  trong approved plan là quyền ghi thực tế. Nhánh Java chỉ còn compatibility cho
  artifact legacy đã bị retirement guard chặn khỏi execution công khai.
- Test thêm đủ web/API/contracts/database/Prisma/E2E roots và các trường hợp
  protected/outside root, sai suffix, traversal, absolute path, backslash,
  missing/tampered policy và direct apply với packet bị sửa.
- Verification: test mới trọng điểm `13 passed`; regression profile/Phase 4
  `105 passed`; full parallel 4 worker `488 passed, 1 skipped in 56.51s`;
  `compileall` và `git diff --check` thành công. Skip duy nhất vẫn là symlink
  escape trên Windows thiếu quyền tạo symlink.
- Không chạy live workflow hoặc external sync. A2.2b vẫn mở; lát kế tiếp của A3
  là registry fixed argv/cwd/env/timeout và Verification Runner cho
  `typecheck -> unit`.

## 2026-10-20 - A3.2 Profile-owned Verification Runner

- Baseline: commit `960b575` đã đóng A3.1 editable path guards trước khi bắt đầu
  thay đổi runner.
- Thêm executable registry riêng ở tầng verification. Immutable stage policy chỉ
  mang ID; registry code-reviewed mới map `typecheck -> npm run typecheck` và
  `unit -> npm run test:unit`, cwd root, timeout 120 giây và env allowlist.
- Profiled runner preflight target/package scripts/npm, chạy argv list với
  `shell=False`, dừng ở fail/timeout đầu tiên và không đọc raw execution field
  từ plan/work packet. Contract drift bị chặn trước subprocess.
- Phase 4 truyền policy resolve từ frozen snapshot vào cả initial verification,
  migration/revalidate, implementation repair và QA execution. QA generated
  test TypeScript vẫn fail closed đến lát Vitest kế tiếp.
- `VerificationResult` giữ flat fields legacy và thêm planned/executed stage IDs
  cùng per-stage argv/cwd/timeout/status/output evidence. Markdown report,
  quality report và repair feedback đã mang evidence này; repair objective dùng
  stage lỗi làm identity ổn định.
- Verification: test mới `9 passed`; profile/plan/Phase 3-4/events regression
  `150 passed`; full parallel 4 worker `497 passed, 1 skipped in 81.18s`;
  `compileall` thành công. Skip duy nhất vẫn là symlink escape trên Windows thiếu
  quyền tạo symlink.
- Không chạy live workflow, không external sync và chưa bootstrap target
  TypeScript. Việc kế tiếp trong A3 là chuyển QA generation/application sang
  Vitest; A2.2b prompt/fake Developer.act và Code Trace vẫn mở.

## 2026-10-20 - A3.3 Profile-owned Vitest QA Evidence

- Baseline: commit `097961e` đã đóng A3.2 profile-owned Verification Runner; QA
  profiled khi đó vẫn fail closed nếu truyền generated test files.
- Thêm `QaTestPolicy v1` suy ra từ immutable planning/profile snapshot. Policy
  khóa framework Vitest và co-located `.test.ts`/`.test.tsx` cho
  web/API/contracts/database; Playwright/E2E vẫn ngoài phạm vi Chặng A.
- QA prompt/parser/fake profiled sinh TypeScript/Vitest và lưu generation profile.
  Java generation/apply chỉ còn ở nhánh compatibility cho artifact legacy.
- Apply chặn duplicate, path ngoài policy và ghi đè test có sẵn không do cùng run
  sở hữu. Retry chỉ được cập nhật các path đọc từ QA artifact trước đó có metadata
  trùng đúng policy hiện hành.
- Unit stage nhận exact generated file list, thêm fixed Vitest JSON reporter từ
  executable registry và dùng temporary report path không ghi vào artifact.
  Runner chỉ công nhận `verified_test_files` sau khi `testResults` khớp chính xác
  với `selected_test_files`; missing/invalid/mismatched report đều fail kể cả exit
  code 0. Cách gọi `--reporter=json --outputFile=...` đã được đối chiếu với tài liệu
  Vitest chính thức.
- QA generated/execution/test/quality reports ghi rõ framework, selected files,
  verified files và per-stage evidence. HTTP/action/checkpoint contract không đổi.
- Verification: targeted A3.3/Phase 4 `32 passed`; `compileall` thành công;
  `git diff --check` sạch ngoài cảnh báo line-ending. Full parallel cuối chạy với
  capture tắt đạt `508 passed, 1 skipped in 105.86s`. Một lượt capture trước đó
  gặp đúng Windows local HTTP `WinError 10053` đã biết ở Write API; ca này và full
  suite đều pass khi chạy lại không capture. Skip duy nhất vẫn là symlink escape
  thiếu quyền trên Windows.
- Không chạy live workflow, không external sync và chưa bootstrap target
  TypeScript. Việc tiếp theo trong A3 là TypeScript AST contract evidence cho Code
  Trace; Developer.act/tool-loop còn thuộc A2.2b.

## 2026-10-20 - Quyết Định Tách A3.4 TypeScript AST

- Baseline: commit `1359b80` đã đóng A3.3 profile-owned Vitest QA evidence.
- Máy có Node nhưng chưa có `tsc`/package `typescript`; target repo
  `SDLC_Target_TypeScript` cũng chưa được bootstrap. Không thay compiler AST bằng
  regex blocking và không kéo A4 vào cùng lát.
- Tách A3.4a để nối immutable Code Trace policy, profile-aware discovery và AST
  bridge fail-closed trước. A3.4b chỉ đóng checklist khi target A4 đã pin
  TypeScript, extractor chạy thật và signature comparison pass.
- A3.4a không push, không live workflow, không external sync và sẽ để uncommitted
  sau khi triển khai để review riêng.

## 2026-10-20 - A3.4a Profile-owned TypeScript AST Bridge

- Baseline: commit `1359b80` đã đóng A3.3; thay đổi A3.4a được giữ uncommitted để
  review riêng theo kế hoạch.
- Thêm `CodeTracePolicy v1` resolve từ immutable workspace-bound snapshot. Policy
  chỉ nhận production source trong component roots và co-located
  `.test.ts`/`.test.tsx`; Java path, browser E2E, traversal và path không canonical
  bị chặn bằng error code ổn định.
- Thêm Node helper dùng TypeScript Compiler API của chính target để trích
  interface/class/function/method cùng parameter/return/generic/async/optional/rest
  metadata. Python bridge dùng fixed helper argv, target cwd, env allowlist,
  timeout và `shell=False`; helper được khai báo package data.
- Bridge validate version, extractor ID, profile metadata, exact requested paths,
  diagnostics và symbol schema. Thiếu Node/package `typescript`, parser lỗi hoặc
  evidence sai đều fail closed; không dùng regex/substr làm blocking fallback.
- Code Trace profiled dùng exact AST name/qualified name cho symbol existence,
  ghi `symbol_evidence` vào artifact/quality report và semantic-review prompt.
  AST evidence cũng tham gia snapshot fingerprint của finding ledger để thay đổi
  extractor/evidence không bị coi là cùng một code-validation snapshot.
  Java source normalization, text search và Java signature regex chỉ còn chạy cho
  artifact legacy. Prompt bỏ giả định JUnit và dùng invariant trung lập framework.
- Verification: A3.4a unit `11 passed`; profile/Code Trace consensus/Phase 4
  regression `81 passed`; full parallel `519 passed, 1 skipped in 56.38s`;
  `py_compile`, Node `--check` và `git diff --check` đều sạch. Skip duy nhất vẫn là
  symlink escape trên Windows thiếu quyền tạo symlink.
- Không chạy live workflow, không external sync, không push và chưa bootstrap
  target TypeScript. A3.4b vẫn mở cho extractor thật trên target đã pin
  `typescript` và full signature comparison; A2.2b Developer.act/tool-loop cũng
  chưa thuộc lát này.

## 2026-10-20 - Kế Hoạch A4.1a Local Stage-A Target Bootstrap

- Baseline: commit `5108194` đã đóng A3.4a TypeScript AST bridge; worktree Harness
  sạch trước khi ghi kế hoạch A4.1a.
- Target local dự kiến tại `../SDLC_Target_TypeScript`, branch `main`, chưa tạo
  GitHub remote và không push trong lát này.
- Phạm vi chỉ đủ contract Chặng A: exact target manifest/component roots, npm
  workspaces, committed lockfile, fixed `typecheck`/`test:unit`, một API health
  source cùng co-located Vitest test để chạy runner và AST extractor thật.
- Khóa toolchain theo máy và npm registry đã kiểm tra: Node `24.18.0`, npm
  `11.16.0`, TypeScript `6.0.3`, Vitest `5.0.5`, Vite `8.3.4`, `@types/node`
  `24.19.2`; không dùng range hoặc `latest` trong package metadata.
- Ban đầu đã thử bản registry mới nhất TypeScript `7.0.2`, nhưng actual AST smoke
  cho thấy package chỉ public unstable AST API và không còn Compiler API contract
  mà helper A3.4a dùng. Lát này pin `6.0.3` thay vì đổi Harness sang API unstable.
- React application shell, Fastify server, Prisma/PostgreSQL lifecycle,
  Playwright, full CI, canonical remote, config cutover và live workflow đều để
  A4.1b/lát sau. A4.1a không đóng toàn bộ A4 hoặc A3.4b.

## 2026-10-20 - A4.1a Local Stage-A Target Bootstrap

- Baseline Harness là `5108194`; target local được tạo tại
  `../SDLC_Target_TypeScript`, branch `main`, commit `111b72c`, không có remote
  và không push.
- Target có exact `.sdlc/target-profile.json`, năm component roots, npm
  workspaces, protected config, committed lockfile và API health fixture/test tối
  thiểu. Đây là contract/evidence fixture, chưa phải business feature.
- Toolchain cuối cùng: Node `24.18.0`, npm `11.16.0`, TypeScript `6.0.3`, Vitest
  `5.0.5`, Vite `8.3.4`, `@types/node` `24.19.2`. `npm ci`, `npm run typecheck`
  và `npm run test:unit` đều pass; Vitest chạy một file với hai assertions.
- Manifest preflight tạo digest
  `sha256:dcbdbfbae37ea6b5637ffd41df6ef8f60323480949053eafa283ed6067710b94`
  và bind base SHA `111b72cb6af92cbd2774d6ac82377b6fef79a20b`. Profiled Runner chạy đúng
  thứ tự `typecheck -> unit` và xác nhận `apps/api/src/health.test.ts` đã execute.
- AST helper đã load `typescript` từ chính target và đọc được interface,
  function, class, method cùng generic/optional/rest/async return metadata từ
  source thật. Kết quả này chỉ xác nhận bridge; checklist signature comparison
  đầy đủ vẫn thuộc A3.4b.
- Actual Vitest 5 report làm lộ lỗi Runner: parser so số lượng `set(status)` với
  số assertions nên hai test cùng pass vẫn bị coi là fail. Điều kiện đã đổi sang
  kiểm từng assertion có status `passed`; regression fixture nay chứa hai passing
  assertions. Targeted regression `29 passed`; full Harness regression
  `519 passed, 1 skipped in 103.03s`.
- Không tạo remote, không push, không đổi target config, không chạy live workflow
  hoặc external sync. A4.1b vẫn giữ React/Fastify/Prisma/PostgreSQL/Playwright/CI;
  bước code tiếp theo là A3.4b trên toolchain đã pin.

## 2026-10-20 - Kế Hoạch A3.4b Full TypeScript Signature Contract

- Baseline Harness `8e47d3d`, target local `111b72c`; cả hai worktree sạch.
- Đóng toàn bộ A3.4b trong một lát. Phase 3 sở hữu nested
  `implementation_signature_contract_v1` và human duyệt cùng
  `implementation_plan_structured_v3`; không mở rộng PlantUML/Phase 2.
- Profiled packet mới là `developer_work_packet_profiled_v2`, mang nguyên
  signature contract cùng approved-plan binding. V2/V1 lịch sử vẫn đọc/chạy theo
  symbol-only behavior, không auto-migrate.
- Comparator dùng exact target AST theo path/qualified symbol/kind và so
  parameter type/order/optional/rest, return, generic, async; chỉ chuẩn hóa
  whitespace, không tự coi type alias hoặc `T[]`/`Array<T>` là tương đương.
- Deterministic mismatch tạo `contract_signature_mismatch`; missing symbol vẫn là
  `missing_code_symbol`. LLM không được phủ quyết static evidence và profiled run
  không fallback regex/substr.
- Controlled smoke chỉ đọc target local với `readHealth` và
  `HealthService.check`; không sửa target, không push/remote/live workflow/sync.

## 2026-10-20 - A3.4b Full TypeScript Signature Contract

- Baseline Harness `8e47d3d`; target local giữ nguyên commit `111b72c`, worktree
  sạch sau smoke và không có remote.
- Phase 3 profiled phát hành `implementation_plan_structured_v3` với nested
  `implementation_signature_contract_v1`. Contract bind design element, canonical
  source path, qualified symbol/kind, parameter order/type/optional/rest, return,
  generic và async; digest tham gia review/work-packet fingerprint.
- Builder chỉ canonicalize/bind output của Phase 3. Nếu producer bỏ sót method
  entry thì plan fail closed; không lấy type từ class graph/PlantUML Phase 2.
  Fake Phase 3 producer đã được nâng theo cùng contract để regression không
  dựa vào fallback không tồn tại trong production.
- Phase 4 phát hành `developer_work_packet_profiled_v2`, giữ nguyên contract đã
  duyệt và so với TypeScript Compiler AST. Một overload khớp là đủ; parameter
  name chỉ là evidence. Chỉ whitespace được chuẩn hóa, nên `T[]` không
  tự động tương đương `Array<T>` hay type alias.
- Missing exact declaration sinh `missing_code_symbol`; sai signature sinh
  `contract_signature_mismatch`, luôn blocking và owner `developer`. Semantic LLM
  nhận static evidence nhưng không thể gỡ blocker tất định. Plan V2/packet V1
  lịch sử tiếp tục AST symbol-only; Java regex chỉ còn cho legacy Java.
- Controlled read-only smoke trên `apps/api/src/health.ts` trích 1 file/4 symbols:
  contract đúng cho `readHealth` và `HealthService.check` có 0 finding; đổi
  return type trong contract tạo đúng `contract_signature_mismatch`.
- Verification: nhóm Phase 3/signature/prompt `56 passed`; planning paths + Write
  API rerun `19 passed`; full parallel `534 passed, 1 skipped in 46.72s`.
  Skip duy nhất vẫn là symlink escape thiếu quyền trên Windows. `compileall`,
  Node `--check` và `git diff --check` đều sạch ngoài cảnh báo line-ending.
- Một full run tuần tực trước khi sửa fixture gặp local HTTP `WinError 10053`
  đã biết; toàn bộ Write API và full parallel đều pass khi chạy lại.
- Không đổi HTTP API, không chạy live workflow/external sync, không sửa target,
  không tạo remote, không push. Phần code Chặng A tiếp theo là A2.2b cho
  Developer.act/tool-loop; A4.1b vẫn giữ full-stack infra skeleton.

## 2026-10-20 - Kế Hoạch A2.2b Developer Execution Prompt Và Fake

- Baseline Harness `becd185`; worktree sạch, branch `main` đang ahead remote 8
  commit. Target local giữ nguyên `111b72c` và không thuộc phạm vi sửa đổi.
- Lát này resolve language directive và approved signature từ profiled
  `DeveloperWorkPacket`, dùng chung cho Developer batch và tool-loop; prompt
  profiled không còn ví dụ hay giả định Java.
- Fake Developer, QA và Code Trace phải đọc contract trong prompt thay vì trả
  path/nội dung Java cố định. Legacy packet tiếp tục giữ compatibility Java.
- Tool-loop chỉ được nâng manifest/prompt contract và test ordering; chưa được
  nối vào `PhaseFourWorkflow`, chưa đổi execution mode mặc định.
- Acceptance: targeted prompt/fake/tool-loop tests, một fake profiled Phase 4
  regression, full pytest, compileall, Node helper check và git diff check đều
  pass. Không chạy live workflow, external sync, remote hoặc push.

## 2026-10-20 - A2.2b Developer Execution Prompt Và Fake

- Baseline Harness `becd185`; target local `111b72c` không bị sửa, không có
  remote/live workflow/external sync nào được gọi.
- Thêm resolver fail-closed cho Developer generation context từ profiled packet;
  act/repair prompt dùng TypeScript directive, exact allowed path và approved
  signature contract thay cho ví dụ Java cố định.
- Tool-loop phát hành manifest V6, đưa profile/signature vào durable input hash,
  action context và source-before-test ordering. Executor chưa được nối vào
  `PhaseFourWorkflow`, config/default mode giữ nguyên.
- Fake Developer/QA/Code Trace nay đọc packet/policy/static mapping trong prompt;
  deterministic profiled output sinh TypeScript/Vitest, còn fake Java cũ chỉ
  phục vụ packet legacy.
- Verification: nhóm prompt/tool-loop/QA/Code Trace/Phase 4 `102 passed`; full
  parallel `539 passed, 1 skipped in 96.78s`; `compileall`, Node `--check` và
  `git diff --check` pass. Skip duy nhất là symlink escape thiếu quyền trên
  Windows như baseline.
- Việc còn lại: nối `DeveloperToolLoopExecutor` vào initial act,
  implementation repair và QA repair trong một lát control-flow riêng; sau đó
  mới cân nhắc đổi execution mode mặc định.

## 2026-10-20 - Kế Hoạch A3.5 Phase 4 Tool-loop Integration

- Baseline Harness `83519e8`; A2.2b đã được commit riêng trước khi thay đổi
  control-flow. Target local giữ nguyên `111b72c` và không thuộc phạm vi sửa đổi.
- Nối `DeveloperToolLoopExecutor` vào initial act, implementation repair và QA
  repair khi `SDLC_DEVELOPER_EXECUTION_MODE=tool_loop`; `legacy` vẫn là mặc định.
- Bind executor/mode/iteration vào `developer_execution` để retry dùng lại đúng
  durable session dù config đổi. Manifest/journal là evidence của tool-loop;
  batch legacy giữ raw-response và atomic apply hiện tại.
- Retriable failure phải pause và giữ iteration; non-retriable failure phải
  block. Tool-loop repair no-change hợp lệ vẫn phải chạy lại Verification, Code
  Trace và QA như đường repair hiện tại.
- Acceptance gồm targeted tool-loop/Phase 4/API regression, full parallel
  pytest, compileall, Node helper syntax check và git diff check. Không live run,
  external sync, remote, push hoặc đổi default mode trong lát này.

## 2026-10-21 - A3.5 Phase 4 Tool-loop Integration

- Baseline A2.2b đã được tách thành commit `83519e8`; A3.5 chưa commit để review.
- `PhaseFourWorkflow` dùng tool-loop cho initial act, implementation repair và
  QA repair khi flag là `tool_loop`; default `legacy` và batch path giữ nguyên.
- `developer_execution_state_v1` bind executor/mode/iteration/artifact version.
  Pause/resume giữ nguyên durable session và frozen work packet dù config đổi.
- Tool-loop ghi trực tiếp worktree, không qua `_apply_developer_changes`; report
  dùng manifest/result và ghi before/after SHA-256. Manifest nâng V7 để hash-bind
  QA-owned dirty baseline mà không thêm các path đó vào Developer write scope.
- Retriable/non-retriable failure lần lượt pause/block; fake execution dùng đúng
  act/repair contract, cho phép repair subset/no-change như real executor.
- Verification: A3.5 targeted `26 passed`; Phase 4/tool-loop/API/application
  regression `152 passed`; full parallel `546 passed, 1 skipped in 84.60s`;
  compileall, hai Node `--check` và `git diff --check` pass. Skip duy nhất vẫn là
  symlink escape thiếu quyền trên Windows.
- Không chạy live workflow, external sync, remote/push, không sửa target repo và
  chưa đổi `SDLC_DEVELOPER_EXECUTION_MODE` mặc định.
- Review sau triển khai phát hiện repair-context chỉ quét symbol reference trong
  `.java`. Bộ lọc này đã được thay bằng `source_suffixes` của language profile;
  packet legacy suy suffix từ approved paths. Diagnostic paths tiếp tục chỉ là
  hint, Developer vẫn có thể sửa mọi related path thuộc `allowed_paths`.
- Verification sau review: Developer/profile regression `39 passed`; full
  parallel `547 passed, 1 skipped in 59.53s`. Skip vẫn là ca symlink Windows đã
  biết; không có live workflow, external sync hoặc thay đổi target repo.

## 2026-10-21 - A3.5 Logic-Audit Hardening

- Rà lại chuỗi state transition từ Developer act/repair qua Verification, Code
  Trace và QA; không mở lại hạng mục source context của implementation plan.
- Lỗi `HarnessError` phát sinh khi tool-loop dựng/kiểm tra contract nay giữ nguyên
  error code và được đổi thành non-retriable `DeveloperExecutionError`; workflow
  ghi `blocked` thay vì `implementation_paused` rồi retry vô hạn.
- Last-resort recovery của implementation repair chỉ pause durable tool-loop.
  Batch legacy gặp lỗi nội bộ sẽ block và không tự bind/chuyển executor sang
  tool-loop giữa execution session.
- Code Trace failure sau QA repair nay phân loại giống initial/revalidate path:
  missing link do Developer sở hữu và còn lượt sẽ ghi implementation feedback,
  chuyển `implementation_repair_needed`; hết lượt hoặc lỗi không repairable mới
  block với action policy tương ứng.
- Bổ sung ba regression test cho contract failure, legacy executor preservation
  và QA-repair -> implementation-repair routing. Targeted Phase 4/tool-loop:
  `90 passed`; full parallel `550 passed, 1 skipped in 62.92s`. Skip duy nhất vẫn
  là ca symlink Windows thiếu quyền đã biết; `compileall` và `git diff --check`
  đều pass.

## 2026-10-21 - Kế Hoạch A4.1b.1 Web/API Skeleton

- Baseline Harness `fea1898`, target local `111b72c`; cả hai worktree sạch trước
  khi triển khai.
- Tách A4.1b thành ba lát: web/API/contracts, database/Compose và Playwright/CI.
  Lát đầu chỉ dựng React/Vite, Fastify và shared health contract chạy thật.
- Pin exact React `19.3.1`, React DOM `19.3.1`, React types `19.3.0`, Vite React
  plugin `6.1.2`, Fastify `5.12.7`, `@fastify/cors` `11.3.1` và `tsx` `4.23.15`;
  cập nhật lockfile bằng npm, không dùng range hoặc `latest`.
- Thêm web entry/build, Fastify app factory/server, `GET /health`, shared contract
  và test bằng React server render cùng Fastify injection. Giữ nguyên symbol
  baseline `readHealth`/`HealthService.check` phục vụ AST evidence.
- Không đổi profile/schema, HTTP API của Harness hay active verification suite;
  chưa triển khai Prisma/PostgreSQL, Playwright, CI, remote, config cutover hoặc
  live workflow. Checklist A4.1b tổng chỉ đóng sau đủ ba lát.
- Acceptance dự kiến: `npm ci`, typecheck, unit và web build của target; manifest
  preflight, regression Harness, `compileall` và `git diff --check` đều pass.

## 2026-10-21 - A4.1b.1 Web/API Skeleton

- Target bootstrap được đóng riêng tại commit `73860a6` trên branch `main` của
  `SDLC_Target_TypeScript`; chưa tạo remote và chưa push.
- Giữ nguyên Harness runtime/profile/schema và target manifest. Target bổ sung
  React/Vite application shell, Fastify app factory/server, `GET /health` và
  package contracts làm workspace dependency dùng chung cho web/API.
- Web health client kiểm payload runtime thay vì chỉ ép kiểu TypeScript; unit test
  bao phủ payload hợp lệ/sai contract, React server render và Fastify injection.
  Symbol baseline `readHealth`/`HealthService.check` cùng chữ ký cũ được giữ lại.
- Dependency trực tiếp được pin đúng kế hoạch; npm `allowScripts` chỉ cho phép
  đúng `esbuild@0.28.2` phục vụ Vite. `npm ci` cài sạch 102 package, audit 107
  package và không có vulnerability; `npm ls --depth=0 --workspaces` xác nhận
  đúng exact versions cùng local contracts workspace.
- Target verification: typecheck pass; Vitest `4 files, 6 tests` pass; Vite build
  18 module pass; HTTP smoke thật qua `npm run start:api` trả `{"status":"ok"}`.
- Harness manifest preflight pass với digest không đổi
  `sha256:dcbdbfbae37ea6b5637ffd41df6ef8f60323480949053eafa283ed6067710b94`;
  profile/manifest/runner regression `32 passed`; full parallel regression
  `550 passed, 1 skipped in 114.86s`; `compileall` pass. Skip duy nhất vẫn là ca
  symlink Windows thiếu quyền đã biết.
- Chưa triển khai Prisma/PostgreSQL/Compose lifecycle, Playwright, CI, remote,
  config cutover hoặc live workflow. A4.1b tổng vẫn mở cho A4.1b.2/A4.1b.3.

## 2026-10-21 - Kế Hoạch A4.1b.2 Prisma/PostgreSQL Skeleton

- Baseline Harness `db378f4`, target local `73860a6`; cả hai worktree sạch trước
  khi triển khai. Docker Desktop engine `29.9.1` và Compose `v5.6.0` đã sẵn sàng.
- Dựng database workspace bằng Prisma `7.10.1`, PostgreSQL image
  `postgres:17.11-alpine3.24`, driver adapter `pg` và generated client nằm ngoài
  git. `packages/db/prisma7.config.ts` là protected path mới của profile/manifest.
- Schema bootstrap chỉ có `SystemMetadata` để chứng minh migration/query thật.
  Initial migration tạo bảng; seed upsert `bootstrap_version=1` để chạy lặp không
  sinh bản ghi trùng.
- Compose không khóa `container_name` hay project name; dùng named volume,
  healthcheck và host port mặc định `54329`. Controlled smoke dùng project riêng
  `sdlc-a41b2-smoke`, migrate từ volume rỗng, seed hai lần, kiểm chính xác một
  bootstrap row rồi luôn dọn project/volume.
- Giữ `typecheck -> unit` là active Verification Runner suite. Không nối API
  health vào database, không mở B3 database stages, không đổi Harness HTTP API,
  không tạo remote/push/cutover hay chạy live SDLC workflow.
- Acceptance dự kiến: npm install/lockfile reproducible, Prisma generate/validate,
  target typecheck/unit/web build, controlled live DB smoke, manifest preflight,
  targeted và full Harness regression, `compileall` cùng `git diff --check` pass.

## 2026-10-21 - A4.1b.2 Prisma/PostgreSQL Skeleton

- Target commit `f2cce01` thêm Prisma `7.10.1`, adapter PostgreSQL, schema trung lập
  `SystemMetadata`, initial migration, seed upsert `bootstrap_version=1`, lazy
  client, DB health query và smoke assertion. Generated client nằm ngoài git;
  `npm ci` tự generate qua postinstall và audit báo `0 vulnerabilities`.
- Compose dùng image `postgres:17.11-alpine3.24`, named volume, healthcheck, không
  khóa container/project name. Port dự kiến `55432` nằm trong dải Windows reserved
  `55338-55437`, nên default đã đổi đồng bộ sang `54329` thay vì chỉ override lúc
  test.
- Controlled smoke project `sdlc-a41b2-smoke` pass: container healthy, migration
  áp dụng từ volume rỗng, seed chạy hai lần, query trả đúng một row
  `bootstrap_version=1`; khối cleanup đã xóa container, network và volume.
- Target verification sau clean install: Prisma validate pass; typecheck pass;
  Vitest `6 files, 10 tests` pass; Vite build `18 modules` pass. Dependency tree
  xác nhận toàn bộ Prisma/pg/dotenv/tsx dùng đúng exact versions đã pin.
- Harness profile và target manifest cùng bảo vệ `packages/db/prisma7.config.ts`;
  manifest preflight pass với digest
  `sha256:9ca5e0628f55ca155c79881897478f6dfa06c12084ff7b31a8520464d0ac3c8f`.
  Targeted profile/manifest/planning/runner regression `44 passed`; full parallel
  regression `550 passed, 1 skipped in 81.03s`; `compileall`, Compose config và
  `git diff --check` pass. Skip duy nhất vẫn là ca symlink Windows thiếu quyền đã
  biết.
- Active Verification Runner vẫn là `typecheck -> unit`; chưa nối API `/health`
  vào DB, chưa bật B3 database stages, chưa tạo remote/push/cutover hoặc chạy live
  SDLC workflow. A4.1b tổng vẫn mở cho A4.1b.3 Playwright/CI.

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
