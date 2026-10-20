# Gate 0 Review

## Kết Quả

Gate 0 được review ngày 2026-10-19 trên baseline `main` commit `e07c9be`.
Kết quả là **pass sau khi bổ sung contract tài liệu**. Kết quả này chỉ đóng gate
thiết kế; không có nghĩa Chặng A đã được triển khai hoặc được phép tự động
cutover runtime.

Review đã đối chiếu docs với source và tests hiện hành của Phase 1-4,
`TargetRepoPreparationWorkflow`, run workspace, Developer plan/work packet,
Verification Runner, Validator code trace, QA generation và HTTP read/status
boundary.

## Phát Hiện Và Điều Chỉnh

| Mức | Phát hiện khi review | Điều chỉnh đã khóa |
| --- | --- | --- |
| Blocker | Run hiện chưa có execution profile; trước khi workspace được provision, target selection phụ thuộc `Settings`, còn workspace V1 chỉ snapshot repo identity | Tạo `profile_snapshot_v1` ngay khi mở run và dùng snapshot cho mọi action sau đó |
| Blocker | Run cũ không có profile có thể bị tiếp tục sau khi default target đã đổi | Phân loại run thiếu snapshot là `legacy_java_v1`; sau cutover chỉ cho read/audit/sync artifact đã có, cancel và workspace cleanup; chặn approve/continue/revalidate sinh thêm artifact |
| High | Plan hiện có raw `test_intents.command`, nên chỉ đổi chuỗi Java thành npm vẫn để LLM ảnh hưởng command execution | Plan mới chỉ tham chiếu stage ID; registry đã code-review map stage ID sang argv/cwd/env/timeout cố định |
| High | Profile và target skeleton chưa có handshake machine-readable | Target repo phải có manifest bất biến `.sdlc/target-profile.json`; preflight đối chiếu chính xác profile, component roots, protected files và required capabilities |
| High | Các artifact mới chưa có chiến lược version/reader | Mỗi contract có schema version riêng; không ghi đè nội dung V1 dưới cùng version; unknown version fail closed |
| High | `cleanup` đang được mô tả như stage cuối thông thường | Cleanup là `always_run`/finally stage và có evidence riêng, kể cả setup/test/E2E fail hoặc timeout |
| Medium | Acceptance Chặng A/B còn định tính | Bổ sung evidence matrix có điều kiện pass cụ thể cho profile smoke và vertical web feature |
| Medium | Artifact mới có thể không xuất hiện qua Read API vì allowlist hiện fail closed | Mọi artifact public mới phải được thêm có chủ ý vào read allowlist, được sanitize và có contract test với Niko |

## Ownership Đã Chốt

| Owner | Sở hữu | Không sở hữu |
| --- | --- | --- |
| Workflow core | phase/state/checkpoint, operation/idempotency, repair budget, artifact lifecycle | syntax TypeScript, npm command hoặc component path cụ thể |
| Language profile | extension, prompt directive, symbol/contract extractor, diagnostic classification | component topology, database/service lifecycle |
| Project profile | component roots, editable/protected policy, manifest expectation, verification stage graph | business feature và command do LLM phát minh |
| Target repo | infra skeleton, package scripts/config đã review, application source và tests | workflow/checkpoint state |
| LLM agents | đề xuất design/work item/code/test trong approved scope | shell command, profile selection, secret/port/process ownership |
| Niko | HTTP action và trình bày status/checkpoint | profile resolution, target validation, artifact generation hoặc workspace |

## Versioning Và Run Binding

Run mới phải snapshot tối thiểu:

```text
profile_snapshot_v1
  project_profile_id: typescript_fullstack_v1
  project_profile_schema: project_profile_v1
  language_profile_id: typescript_v1
  language_profile_schema: language_profile_v1
  target_remote
  target_base_branch
```

Khi tạo workspace, snapshot được bổ sung `target_manifest_schema`, manifest
digest, base SHA và resolved component roots. Config thay đổi sau đó không được
đổi profile hoặc target identity của run. Mismatch giữa state, workspace,
manifest và registry phải fail closed trước khi LLM hoặc subprocess chạy.

Các artifact mới dùng version riêng, dự kiến:

- `design_bundle_v1` cho component/UI/API/data projections;
- `implementation_plan_structured_v2` cho profiled plan chỉ tham chiếu stage ID;
- version implementation-plan kế tiếp cho work item/component/dependency Chặng B;
- `developer_work_packet_profiled_v1` trong field `schema_version`, tách khỏi
  field `version` đang biểu diễn revision của packet;
- `verification_report_v2` cho ordered stage results;
- `requirement_implementation_trace_v2` cho cross-layer trace;
- `qa_generated_tests_v2` và `qa_test_report_v2` cho browser acceptance.

Tên cuối có thể đổi trong code review, nhưng nguyên tắc bắt buộc là reader
dispatch theo version, unknown version bị từ chối và artifact V1 lịch sử không
bị rewrite.

## Legacy Java Policy

Sau cutover:

1. run không có `profile_snapshot_v1` được nhận diện là `legacy_java_v1`;
2. status, artifact read/export và audit vẫn hoạt động;
3. `sync_run` chỉ được đồng bộ artifact/checkpoint đã tồn tại, không gọi agent
   hoặc verification;
4. cancel và workspace cleanup vẫn được phép;
5. approve, continue và mọi revalidate có thể sinh artifact/code mới phải trả
   lỗi ổn định `legacy_execution_profile_retired`;
6. không tự chuyển Java artifact/workspace sang TypeScript;
7. muốn làm tiếp cùng Jira issue thì cancel run cũ, sau đó mở run mới để nhận
   profile TypeScript hiện hành.

Trong thời gian triển khai Chặng A có thể giữ đường Java hiện hành sau một
feature flag để regression, nhưng đường đó phải bị xóa khỏi runtime trước
cutover. Đây là migration scaffolding tạm thời, không phải Java profile được hỗ
trợ sau cutover.

## Target Bootstrap Binding

Canonical target dự kiến:

```text
repository: SDLC_Target_TypeScript
remote: https://github.com/LuongAnh1/SDLC_Target_TypeScript.git
base branch: main
package manager: npm workspaces with committed package-lock.json
manifest: .sdlc/target-profile.json
```

Repo/remote chưa được tạo trong Gate 0. A4 phải tạo và kiểm tra quyền truy cập
trước khi đổi default runtime. Node/npm/TypeScript/dependency versions phải được
pin tại bootstrap bằng manifest, `package.json`/lockfile và file version phù hợp;
không lấy `latest` trong mỗi run.

Manifest, package scripts, lockfile, TypeScript/Vite/Vitest/Playwright config,
Compose config và CI là protected mặc định. V1 skeleton phải cài sẵn dependency
cần thiết; feature run không tự thêm package. Business source, unit/integration
tests, Prisma schema/migrations và approved Playwright specs là editable roots
theo component policy.

## Acceptance Evidence Đã Chốt

| Gate | Evidence bắt buộc |
| --- | --- |
| Chặng A profile smoke | profile/manifest/base-SHA snapshot; ít nhất một production `.ts` change và một Vitest test do run tạo; typecheck và unit pass; stable requirement ID trace đến code/test; Draft PR được tạo; regression HTTP operation/checkpoint pass |
| Chặng B vertical feature | design bundle đủ UI/API/data/component links; Prisma migration chạy từ DB rỗng; API/DB integration pass; frontend build pass; Playwright Chromium thực thi đúng generated/approved spec; cùng scenario chứng minh user action → API → persisted DB effect → final UI state; cleanup pass; Draft PR và human Ready-for-review flow pass |

Exit code của một command chung không đủ. Báo cáo phải ghi từng stage, argv
đã resolve, cwd, timeout, bounded output/artifact refs và expected generated test
thực sự được runner report là đã execute.

## Việc Cố Ý Để Lại Cho Chặng Triển Khai

- Exact Node/dependency versions, sau khi kiểm tra toolchain máy chạy.
- Tạo GitHub repo/remote và cấp quyền push.
- Chọn Jira use case cụ thể cho controlled live run.
- Chi tiết type/schema Python cuối cùng, miễn tuân theo ownership và versioning
  đã khóa ở trên.

Những việc này không làm Gate 0 bị mở lại; chúng là prerequisite/evidence của
A4 và live acceptance, không phải quyết định kiến trúc còn mơ hồ.
