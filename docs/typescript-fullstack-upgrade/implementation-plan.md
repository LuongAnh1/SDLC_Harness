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
- [ ] A2.2b: chuyển prompt/fake fixture đang hoạt động của Developer.act,
  tool-loop, QA và Code Trace sang TypeScript sau khi A3 có editable-root,
  work-packet và execution guard tương ứng.
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
- [ ] Chuyển work-packet/apply/repair guards sang editable roots của profile.
- [ ] Chuyển Verification Runner sang profile-owned setup/discovery/execution.
- [ ] Dùng fixed argv/cwd/env allowlist/timeout từ registry, không execute shell
  string do LLM hoặc plan cung cấp.
- [ ] Chuyển QA generation/application sang Vitest test files và xác minh report
  chứng minh generated test thật sự được execute.
- [ ] Thêm TypeScript AST contract evidence cho code trace.

### A4. Target Bootstrap Và Cutover

- [ ] Tạo repo infra skeleton `SDLC_Target_TypeScript`.
- [ ] Tạo canonical remote `LuongAnh1/SDLC_Target_TypeScript`, base `main`, rồi
  kiểm tra quyền fetch/push trước controlled smoke.
- [ ] Khóa npm workspace, exact toolchain/dependency versions, lockfile và
  `.sdlc/target-profile.json`; không dùng dependency `latest` theo run.
- [ ] Bảo vệ manifest, package scripts/lockfile, tool configs, Compose và CI;
  skeleton phải cài sẵn dependency cần cho V1 feature.
- [ ] Chuyển target path/remote sau regression và controlled smoke.
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
