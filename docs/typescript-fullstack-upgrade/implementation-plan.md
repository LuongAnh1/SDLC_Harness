# Implementation Plan

> Đây là roadmap triển khai, không phải execution log. Gate 0 và toàn bộ A1 đã
> hoàn tất; các checklist còn lại phản ánh công việc chưa thực hiện.

## Gate 0 - Review Tài Liệu (Hoàn Tất 2026-10-19)

- [x] Review current-flow audit với code/tests tại baseline.
- [x] Chốt profile/topology ownership và schema versioning.
- [x] Chốt artifact Phase 2-4 và policy cho Java legacy runs.
- [x] Chốt bootstrap/remote binding của `SDLC_Target_TypeScript`.
- [x] Chốt acceptance evidence cho profile smoke và live vertical feature.

Kết quả và các sửa đổi bắt buộc nằm tại [Gate 0 Review](gate-0-review.md). Gate 0
chỉ đóng design ambiguity; không đánh dấu bất kỳ code/runtime slice nào hoàn
tất và không tự cho phép cutover.

## Chặng A - TypeScript Profile Foundation

### A1. Profile Contract Và Config

- [x] Thêm language/project profile registry và `typescript_fullstack_v1`.
- [x] A1.2a: thêm `SDLC_PROJECT_PROFILE`, resolve fail-closed qua registry và giữ
  mặc định rỗng để chưa đổi Java baseline trong giai đoạn migration.
- [x] A1.2b: tạo `profile_snapshot_v1` ngay lúc mở run và buộc các action sau đó
  dùng snapshot thay vì resolve lại config hiện hành.
- [x] A1.2c: bổ sung manifest digest, base SHA và resolved component roots vào
  snapshot khi provision workspace.
- [x] Nhận diện run thiếu snapshot là `legacy_java_v1` trên status/read API.
- [x] A1.2d: retire Java legacy ngay tại application boundary; chỉ giữ
  read/audit/sync-existing/cleanup, chặn action sinh artifact mới và không mở
  rộng `cancel_run` trong lát này.
- [x] Giữ HTTP API tương thích; thêm `execution_profile` tối thiểu theo hướng
  additive, không công khai target remote hoặc local path.
- [x] Fail closed khi state/workspace/registry/target manifest không khớp.

### A2. Phase 3 Planning

- [x] A2.1a: chuyển source discovery của Phase 3 sang component roots và suffix
  từ immutable run profile snapshot.
- [x] A2.1b: chuyển default targets, test targets và path rules sang profile.
  - Hoàn tất: 2026-10-20.
  - Phạm vi: policy đường dẫn bất biến theo profile, routing component fail-closed,
    canonicalize output Developer.plan và Validator.review_plan dùng cùng policy.
  - Quyết định: class không có existing-symbol duy nhất, stereotype/component hint
    hoặc name suffix không mâu thuẫn sẽ bị chặn để sửa design; không fallback sang
    API và không giao LLM tự chọn component.
- [x] A2.2a: lập prompt usage map; tách invariant Phase 3 khỏi language directive,
  resolve `typescript_prompt_v1` từ immutable snapshot và khóa Developer.plan /
  Validator.review_plan / fake Phase 3 bằng test không còn Java assumption.
  - Hoàn tất: 2026-10-20.
  - File prompt Developer/Tester cũ được xác nhận là reference-only, không có
    runtime call site và chưa bị nối lại vào workflow.
- [x] A2.2b: chuyển prompt/fake fixture đang hoạt động của Developer.act,
  tool-loop, QA và Code Trace sang TypeScript sau khi A3 có editable-root,
  work-packet và execution guard tương ứng.
  - Lát hiện tại chuẩn hóa language/signature context dùng chung cho Developer
    batch và tool-loop, đồng thời chuyển fake Phase 4 sang profile-aware.
  - Chưa nối `DeveloperToolLoopExecutor` vào `PhaseFourWorkflow`, chưa đổi
    `SDLC_DEVELOPER_EXECUTION_MODE` và chưa cutover executor mặc định.
  - Hoàn tất: 2026-10-20. Prompt act/repair lấy path ví dụ từ exact
    `allowed_paths`, resolve `typescript_prompt_v1` từ packet policy và bind
    signature digest; profile/signature mismatch bị chặn trước LLM.
  - Tool-loop phát hành `developer_execution_manifest_v6`, đưa directive cùng
    per-file approved signatures vào input hash/action context và xếp test
    `.test.*`/`.spec.*` sau production source. Session V5 không tự migrate qua
    authority contract mới.
  - Fake Developer/QA/Code Trace đọc packet/policy/static mappings trong prompt;
    TypeScript fixture sinh `.ts`/`.test.ts` và Vitest, còn Java chỉ tồn tại ở
    nhánh packet legacy. Full regression: `539 passed, 1 skipped`.
- [x] A2.3: loại raw command khỏi implementation plan; plan chỉ tham chiếu
  verification stage ID.
  - Hoàn tất: 2026-10-20.
  - Project profile giữ stage catalog bất biến; `VerificationStagePolicy v1`
    resolve ordered smoke suite từ frozen snapshot. Developer plan, repair, fake
    và checkpoint rendering chỉ dùng `stage_id`.
  - Validator fail-closed với raw execution field, stage thiếu/không thuộc
    profile hoặc ordered stage list không khớp snapshot.
  - Profiled plan dùng `implementation_plan_structured_v2`; review lưu SHA-256
    của đúng structured artifact và Phase 4 kiểm lại binding trước khi tạo packet.
  - Chặng A chỉ kích hoạt ordered smoke suite `typecheck -> unit`; stage database,
    service và browser vẫn ở catalog nhưng chỉ được kích hoạt bằng policy/profile
    version của Chặng B.
  - Mapping stage sang fixed argv/cwd/env/timeout và thực thi vẫn thuộc A3.
- [x] Chuyển plan Validator sang profile-aware rules.

### A3. Phase 4 Execution

- [x] Pre-A3 hardening: packet profiled có `schema_version` riêng, Phase 4 kiểm
  lại profile/stage contract và legacy Java Runner fail-closed với packet stage-only.
- [x] A3.1: chuyển work-packet/apply/repair guards sang editable roots của profile.
  - Hoàn tất: 2026-10-20.
  - Profiled work packet lưu `editable_path_policy_v1` trong output contract;
    build, refresh và apply đều resolve policy từ immutable run snapshot và
    fail closed nếu metadata hoặc allowed path bị sửa.
  - Editable root chỉ xác nhận file thuộc vùng có thể sửa; phạm vi ghi thực tế
    vẫn là exact `allowed_paths` đã được implementation plan duyệt.
  - Java path guard chỉ còn cho artifact legacy; prompt/fake Phase 4 vẫn thuộc
    A2.2b.
- [x] A3.2: chuyển Verification Runner sang profile-owned setup/discovery/execution.
  - Hoàn tất: 2026-10-20.
  - Phase 4 resolve `VerificationStagePolicy` từ immutable run snapshot tại mọi
    điểm initial/revalidate/repair/QA verification.
  - Chặng A chạy tuần tự `typecheck -> unit`, dừng tại fail/timeout đầu tiên và
    ghi evidence riêng cho từng stage vào test report.
- [x] Dùng fixed argv/cwd/env allowlist/timeout từ registry, không execute shell
  string do LLM hoặc plan cung cấp.
  - Registry code-reviewed ánh xạ sang `npm run typecheck` và
    `npm run test:unit`, cwd root, timeout 120 giây, env allowlist và `shell=False`.
  - Packet thiếu/sai policy, sai thứ tự stage hoặc chứa raw execution field bị
    chặn trước subprocess; Java command runner chỉ còn cho artifact legacy.
- [x] A3.3: chuyển QA generation/application sang Vitest test files và xác minh
  machine-readable report chứng minh generated test thật sự được execute.
  - Hoàn tất: 2026-10-20.
  - `QaTestPolicy v1` được suy ra từ immutable project/language snapshot, giới hạn
    QA vào co-located `.test.ts`/`.test.tsx` của web/API/contracts/database và
    không mở browser E2E trước Chặng B.
  - QA không được ghi đè test có sẵn ngoài tập file do chính run đã sinh; retry
    chỉ được cập nhật path đã lưu trong artifact QA trước đó cùng policy.
  - Unit stage truyền chính xác generated files cho Vitest, bắt buộc JSON reporter
    và chỉ công nhận `verified_test_files` khi `testResults` khớp đúng tập đã chọn.
    Exit code không còn đủ để kết luận QA generated test đã chạy.
- [x] A3.4a: thêm Code Trace policy, profile-aware source/test discovery và
  TypeScript Compiler AST bridge fail-closed; dùng AST evidence cho exact symbol
  existence nhưng chưa đóng signature contract.
  - Hoàn tất: 2026-10-20.
  - `CodeTracePolicy v1` được resolve từ immutable workspace-bound snapshot;
    source chỉ thuộc component production roots, test chỉ là co-located
    `.test.ts`/`.test.tsx`, browser E2E không được dùng làm unit evidence.
  - Helper Node code-reviewed chỉ load package `typescript` của target, chạy fixed
    argv/cwd/env allowlist/timeout với `shell=False`; thiếu Node/package, parse
    diagnostic hoặc evidence sai contract đều fail closed, không fallback regex.
  - Code Trace artifact và semantic-review prompt nhận AST evidence; exact
    interface/class/function/method name thay cho substring source ở profiled run.
    Java text/signature logic chỉ còn ở nhánh legacy compatibility.
- [x] A3.4b: chạy extractor thật trên target đã pin `typescript`, so đầy đủ
  interface/function/method signature và đóng TypeScript AST contract evidence.
  - Hoàn tất: 2026-10-20.
  - Profiled plan mới phát hành `implementation_plan_structured_v3` chứa nested
    `implementation_signature_contract_v1`; Phase 3 là owner của implementation
    signature được human duyệt, còn Phase 2 PlantUML tiếp tục language-neutral.
    Thiếu entry thì plan fail closed; builder không tự suy luận type từ graph Phase 2.
  - Contract bind design element, source path, qualified symbol/kind và đầy đủ
    parameter type/order/optional/rest, return, generic cùng async expectation.
  - Phase 4 dùng `developer_work_packet_profiled_v2`, kiểm binding/digest và so
    contract với AST evidence tất định; mismatch là Developer blocker, không cho
    semantic review phủ quyết và không fallback regex/substr.
  - Plan V2/packet V1 lịch sử giữ symbol-only compatibility, không auto-migrate;
    Java signature regex chỉ còn ở nhánh legacy.
  - Smoke chỉ đọc target local `111b72c` pass cho `readHealth` và
    `HealthService.check`; mismatch return type tạo đúng blocker tất định.
    Full regression: `534 passed, 1 skipped`.
- [x] A3.5: nối `DeveloperToolLoopExecutor` vào Phase 4 cho Developer act ban
  đầu, implementation repair và QA repair sau feature flag hiện có.
  - `SDLC_DEVELOPER_EXECUTION_MODE=legacy` tiếp tục là mặc định; mỗi execution
    session bind executor vào state để pause/resume không đổi cơ chế giữa chừng.
  - Tool-loop ghi trực tiếp vào worktree và dùng manifest/journal làm evidence;
    workflow không apply lại nội dung như batch legacy.
  - Lỗi retriable chuyển `implementation_paused`, lỗi không retriable chuyển
    `blocked`; repair iteration chỉ tăng sau lần thực thi thành công.
  - Không đổi HTTP action, không chạy live sync và chưa cutover executor mặc định.
  - Hoàn tất: 2026-10-21. Workflow bind `developer_execution_state_v1`, giữ
    packet/artifact version qua resume và ghi manifest/journal vào status.
  - Manifest V7 tách QA-owned dirty baseline có hash khỏi Developer write scope;
    code-change report ghi execution mode cùng before/after SHA-256.
  - Sau review, repair context lấy source suffix từ language profile để dò file
    liên quan theo symbol; diagnostic path vẫn chỉ là gợi ý, còn write boundary
    tiếp tục là toàn bộ `allowed_paths` đã được duyệt.
  - Logic-audit hardening giữ nguyên error code của lỗi contract và block thay vì
    pause/retry; lỗi nội bộ của batch legacy không được đổi executor sang
    tool-loop. Code Trace repairable sau QA repair quay về
    `implementation_repair_needed` khi còn lượt, thay vì block sai luồng.
  - Regression sau hardening: Phase 4/tool-loop `90 passed`; full parallel
    `550 passed, 1 skipped in 62.92s`.

### A4. Target Bootstrap Và Cutover

- [x] A4.1a: tạo repo local `SDLC_Target_TypeScript` đủ contract Chặng A.
  - Branch local `main`, chưa có remote và chưa push.
  - Manifest/profile roots hợp lệ; npm workspace có `typecheck` và `test:unit`.
  - Khóa Node `24.18.0`, npm `11.16.0`, TypeScript `6.0.3`, Vitest `5.0.5`,
    Vite `8.3.4` và `@types/node` `24.19.2` bằng package metadata/lockfile.
  - Có API health source/test tối thiểu để chạy Verification Runner và AST helper
    thật; không coi đây là business feature acceptance.
  - Hoàn tất: 2026-10-20 tại target commit `111b72c`; `npm ci`, typecheck, hai
    Vitest assertions, profiled Verification Runner và AST extraction thật đều
    pass trên base SHA đã snapshot. TypeScript 7.0.2 bị loại sau smoke vì package
    hiện dùng unstable AST export không tương thích Compiler API contract A3.4a.
- [x] A4.1b: hoàn thiện infra skeleton trước full-stack cutover theo ba lát độc lập:
  - [x] A4.1b.1: dựng React/Vite, Fastify và shared contracts chạy thật; giữ active
    verification suite ở `typecheck -> unit` và chưa đổi Harness runtime.
    - Hoàn tất: 2026-10-21. Target pin exact dependency, có web production build,
      Fastify `GET /health`, shared runtime-validated contract và sáu unit tests.
    - Manifest/profile không đổi; `typecheck -> unit` vẫn là active suite. Full
      Harness regression `550 passed, 1 skipped`.
  - [x] A4.1b.2: dựng Prisma/PostgreSQL cùng Compose lifecycle đã review.
    - Dùng model hạ tầng trung lập `SystemMetadata`, migration đầu tiên và seed
      idempotent; không đưa business schema giả vào bootstrap.
    - Pin Prisma/PostgreSQL dependency và image chính xác; Prisma config là
      protected path, generated client không commit.
    - Compose dùng project/volume cô lập, healthcheck và host port mặc định
      `54329`; live smoke phải migrate từ database rỗng, seed hai lần, kiểm đúng
      một bootstrap row và luôn `down --volumes` sau khi chạy.
    - Giữ active verification suite ở `typecheck -> unit`; database lifecycle
      chưa trở thành B3 verification stage và API `/health` chưa phụ thuộc DB.
    - Hoàn tất: 2026-10-21. Prisma validate, target typecheck/unit/build, live
      migrate + seed hai lần + query và cleanup đều pass; full Harness regression
      `550 passed, 1 skipped`.
  - [x] A4.1b.3: dựng Playwright và CI trên skeleton đã khóa dependency/config.
    - [x] A4.1b.3a: pin Playwright/Chromium, dựng local web/API lifecycle, một
      browser smoke và JSON report; chưa kích hoạt `browser_e2e` trong Harness.
      - Hoàn tất: 2026-10-21. Chromium smoke xác minh React -> Fastify health,
        JSON report đúng spec/project/status và hai service port được giải phóng.
    - [x] A4.1b.3b: thêm CI workflow cùng failure artifact trên nền local smoke
      đã ổn định.
      - Hoàn tất: 2026-10-21. Workflow pin immutable action SHA, dùng Node/npm và
        PostgreSQL image exact, chạy DB/typecheck/unit/Chromium smoke, đồng thời
        upload Playwright artifact khi lỗi. Full local parity và GitHub-hosted
        run `39533230388` trên SHA `5afbe16` đều pass.
- [x] Tạo canonical remote `LuongAnh1/SDLC_Target_TypeScript`, base `main`, rồi
  kiểm tra quyền fetch/push trước controlled smoke.
  - Hoàn tất: 2026-10-21. Private remote có default branch `main`; push/fetch
    pass và hosted CI hoàn tất thành công trong 1 phút 28 giây.
- [x] Khóa npm workspace, exact toolchain/dependency versions, lockfile và
  `.sdlc/target-profile.json`; không dùng dependency `latest` theo run.
  - Hoàn tất: 2026-10-21. Protected bootstrap validator kiểm 5 package file,
    exact toolchain cùng 23 dependency spec và lockfile v3; local/hosted CI pass.
- [x] Bảo vệ manifest, package scripts/lockfile, tool configs, Compose và CI;
  skeleton phải cài sẵn dependency cần cho V1 feature.
  - Hoàn tất: 2026-10-21. Registry/manifest khớp 19 protected path tồn tại;
    bootstrap positive/negative tests và full React/Fastify/Prisma/Playwright
    lifecycle pass trên local lẫn GitHub Actions.
- [ ] Chuyển target path/remote sau regression và controlled smoke.
  - Controlled smoke dùng Jira `SDLC-2`, DeepSeek/tool-loop, Jira/Confluence
    real sync và canonical GitHub target; cấu hình TypeScript chỉ được override
    theo process trước khi đổi default.
  - Tiến độ 2026-10-21: cùng run `run_20261021_032521_a7c9d772` đã pass
    Requirement V2, Design V3 và Implementation Plan V5. Hai gap live-only ở
    design component routing và signature repair merge đã được sửa, có regression;
    plan V5 đã được human duyệt và Phase 4 đã chạy. Controlled smoke phát hiện
    dependency chưa được bootstrap trong worktree: `tsc` không tồn tại nhưng bị
    phân loại nhầm thành code defect, làm tiêu hết repair budget trước khi có
    diagnostic TypeScript thật.
  - [x] A4.2a: harden preparation/repair recovery trước khi tiếp tục controlled smoke.
    - Resolve dependency preparation từ executable registry đã review theo project
      profile; workflow không hard-code npm/TypeScript và LLM không được cung cấp argv.
    - Chạy preparation trước verification khi lockfile chưa có success marker tương
      ứng; ghi evidence riêng, không trộn preparation vào ordered stage contract đã
      được Plan V5 duyệt.
    - Dependency cache theo lockfile không được làm stale generated source. Với
      TypeScript/Prisma, chạy thêm profile-owned code-generation preparation
      (`npm run db:generate`) khi Prisma schema/config đổi; marker riêng phải fingerprint
      toàn bộ input sinh client và lỗi schema/generate được phân loại là
      `implementation` để Developer nhận đúng feedback trước `typecheck`.
    - Phân loại lỗi `environment` khác `implementation`: thiếu executable, không khởi
      động được hoặc timeout không được gửi vào Developer repair; lỗi lifecycle có
      diagnostic từ source vẫn được phép tạo repair feedback.
    - `revalidate_implementation` từ `implementation_repair_limit_exceeded` mở một
      repair cycle mới có giới hạn và giữ cumulative audit count, không sửa tăng
      global limit và không tạo run mới.
    - Resume tool-loop sau lỗi atomic state write phải ưu tiên đúng versioned
      Developer Work Packet đã khóa trong execution session; không fallback sang
      packet cũ và không tắt input-drift guard.
    - Repair cycle mới dùng cumulative repair ordinal làm namespace session để
      không đụng `implementation_repair_vN` của cycle cũ; logical iteration trong
      report vẫn bắt đầu lại từ 1 và manifest hash guard giữ nguyên. Ordinal được
      lưu trong `developer_execution`; state cũ thiếu trường này phải chọn namespace
      chưa dùng tiếp theo từ evidence, không đoán lại bằng cycle-local counter.
    - TypeScript signature validator phải hiểu semantics của declaration kind:
      interface method biểu diễn bất đồng bộ bằng `Promise<T>` và không được phép có
      modifier `async`. Contract mới canonicalize `async=false` cho owner
      `interface`; comparator vẫn normalize contract cũ để không tạo blocker bất khả
      thi hoặc khiến Developer thêm wrapper không cần thiết.
    - Lỗi transport/timeout khi Developer tool-loop gọi LLM là lỗi tạm thời: giữ
      nguyên execution session, packet, journal, logical/cumulative repair counters và
      cho phép `continue_run` resume. Không đổi nó thành terminal `blocked`, không mở
      session mới và không tiêu thêm repair budget chỉ vì retry cùng session.
    - Mọi consumer của `DeveloperWorkPacket.output_contract.allowed_paths` trong QA và
      publication phải resolve cùng `PlanningPathPolicy` từ immutable run snapshot;
      không được rơi về Java path guard khi run là TypeScript. QA chỉ đọc code thuộc
      exact `allowed_paths`, còn manifest publication tiếp tục qua path-safety guard
      riêng trước khi commit/push.
    - Bổ sung regression cho bootstrap success/cache/failure classification,
      generated-source refresh, TypeScript interface/Promise signature semantics,
      transient LLM pause/resume, profiled QA/publication path propagation, test report
      evidence và recovery repair cycle; sau đó khôi phục cùng live run.
    - Hoàn tất: 2026-10-21. Cùng live run đã resume qua dependency/code-generation,
      four-cycle Developer repair, Code Trace, profiled QA và publication. QA xác minh
      đúng hai generated acceptance test; Draft PR #1 tại commit `fc80323` có hosted
      CI run `39551258446` xanh, checkpoint Code Review đã sync real. PR vẫn là Draft.
  - Dừng tại từng human checkpoint; Phase 4 phải tạo production `.ts`, Vitest
    test, stable-ID trace và Draft PR có hosted CI xanh. Không mark Ready hoặc
    merge trong lát cutover.
  - Chỉ sau khi smoke pass mới đổi default profile/path/remote trong config,
    schema và `.env.example`; nếu smoke fail thì giữ nguyên default Java cũ.
- [ ] Xóa Java runtime-only behavior và migration flag; giữ historical
  docs/evidence/read path có nhãn.

Điều kiện hoàn tất Chặng A: một feature TypeScript đơn giản đi trọn Phase 1-4;
profile/manifest/base-SHA được snapshot; có production `.ts` change và Vitest
test do run tạo; typecheck/unit, stable-ID trace, Draft PR và HTTP regression đều
pass bằng contract profile mới.

## Chặng B - Full-stack Vertical Feature

### B1. System Design Bundle

- [ ] Thêm component, UI, API và data projections vào Phase 2 artifacts.
- [ ] Thêm deterministic schema/trace checks và Designer repair feedback.
- [ ] Hiển thị bundle tại design checkpoint mà không làm Niko sở hữu artifact.
- [ ] Thêm public artifact keys có chủ ý vào Read API allowlist, sanitize và
  contract-test payload mà Niko đọc.

### B2. Component-aware Plan

- [ ] Tạo version implementation-plan kế tiếp có work item/component/dependency;
  không ghi đè stage-based `implementation_plan_structured_v2` của Chặng A.
- [ ] Kiểm missing layer, dependency cycle, migration impact và acceptance
  coverage.
- [ ] Giữ một human checkpoint trước khi Developer act.

### B3. Multi-stage Verification

- [ ] Mở rộng active verification suite từ `typecheck/unit` sang ordered stage
  graph full-stack và thêm ordered stage result contract.
- [ ] Quản lý PostgreSQL test lifecycle, migration, seed, dynamic resource
  identity và cleanup theo run; cleanup là `always_run` kể cả fail/timeout.
- [ ] Chạy typecheck, unit, API/DB integration và frontend build.
- [ ] Ghi evidence từng stage vào versioned test report.

### B4. Browser QA Và Repair

- [ ] Dựng API/web với readiness check và tài nguyên riêng theo run.
- [ ] Sinh/chạy Playwright Chromium acceptance scenario.
- [ ] Xác minh Playwright machine-readable report chứa đúng generated/approved
  spec, không suy pass chỉ từ exit code.
- [ ] Lưu bounded logs/screenshot references và cross-layer evidence mà không
  lộ credential/path local.
- [ ] Map failure về component và rerun đầy đủ downstream stages sau repair.

### B5. Cross-layer Trace Và Publication

- [ ] Trace Requirement -> UI -> API -> DB -> tests.
- [ ] Validator phân biệt code defect với approved-design defect.
- [ ] PR/Jira/Confluence summary hiển thị component và verification evidence.

Điều kiện hoàn tất Chặng B: một Jira use case tạo thay đổi thật ở frontend,
backend và database; migration, integration và browser E2E đều pass; PR được
human chuyển Ready for review qua contract hiện hành. Harness không merge.

## Test Và Acceptance Dự Kiến

- Profile selection, manifest validation, path traversal và component allowlist.
- Config đổi giữa run không được đổi profile snapshot; manifest/digest/base-SHA
  mismatch phải fail trước LLM/subprocess.
- Legacy Java run chỉ còn đúng action policy; không có automatic migration.
- TypeScript symbol/signature cases gồm interface, optional/rest, generic và
  async return.
- Design bundle thiếu UI/API/data link phải block hoặc repair đúng owner.
- Plan dependency cycle/missing component phải fail deterministic.
- Setup/typecheck/unit/integration/build/E2E failure và timeout phải có stage
  evidence rõ.
- Database phải migrate từ rỗng, seed idempotent và cleanup khi pass/fail/timeout.
- QA test vừa sinh phải được execute thật; baseline exit code không đủ.
- Repair một component phải rerun downstream verification phù hợp.
- API/application regression giữ nguyên operation, polling, idempotency và
  checkpoint mà Niko đang dùng.
- Read API regression kiểm profile metadata/artifact allowlist/sanitization và
  không lộ local path hoặc credential.
- Fake E2E chạy trước controlled DeepSeek; live Jira-to-PR chỉ chạy sau khi tất
  cả gate local pass.

## Rủi Ro Cần Theo Dõi

- Context/prompt phình khi đưa đồng thời UI/API/data/code vào LLM.
- Toolchain setup và browser/database làm operation dài hơn timeout hiện tại.
- Port, process và container leak giữa các run song song.
- Migration hoặc seed không deterministic tạo false failure.
- Trace cross-layer dựa quá nhiều vào semantic LLM thay vì static evidence.
- Xóa Java support quá sớm làm run lịch sử không còn audit/retry an toàn.
