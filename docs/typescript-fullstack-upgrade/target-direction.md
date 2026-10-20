# Target Direction

> Trạng thái: A1.1 đã triển khai contract/registry thuần; A1.2a đã thêm config
> selection fail-closed; A1.2b đã thêm snapshot theo run và downstream settings
> binding; A1.2c đã thêm target manifest preflight cùng workspace binding
> fail-closed; A1.2d đã hard-retire run Java thiếu snapshot. Profile rỗng chỉ
> còn phục vụ read/audit/sync-existing/cleanup. A2/A3.1-A3.4a và target bootstrap
> local A4.1a đã hoàn tất; full-stack infra, signature comparison, remote/cutover
> và các behavior Chặng B dưới đây vẫn chưa hoàn tất.

## Nguyên Tắc

- Giữ nguyên boundary Niko -> HTTP API -> SDLC Harness.
- Giữ một run, một worktree, một branch và một PR cho một vertical feature.
- Tách language concern khỏi project topology và workflow state.
- Command chạy thật đến từ profile được code-review, không do LLM tự tạo.
- Human vẫn duyệt requirement, system design và implementation plan trước khi
  Developer sửa code.

## Ownership Và Schema Boundary

- Workflow core sở hữu phase/state/checkpoint, operation/idempotency, repair
  budget và artifact lifecycle; không biết path hay command TypeScript cụ thể.
- Language profile sở hữu extension, prompt directive, symbol/contract extractor
  và diagnostic classification.
- Project profile sở hữu component topology, editable/protected paths, target
  manifest expectation và verification stage graph.
- Target repo sở hữu infra skeleton cùng package scripts/config đã review.
- LLM chỉ đề xuất artifact/code/test trong approved scope; không chọn profile,
  không sinh shell command và không quản lý secret/port/process.

Mỗi contract có schema version riêng. Không tái sử dụng tên V1 cho payload mới;
reader phải dispatch theo version và fail closed với version không biết. Các
artifact V1 lịch sử vẫn giữ nguyên để audit.

## Profile Model Đề Xuất

```text
LanguageProfile
  language id
  source/test extensions
  prompt directives
  symbol/contract extractor
  diagnostic classifier

ProjectProfile
  profile id
  language profile ref
  component topology
  editable roots and file policy
  setup/build/test stages
  test discovery/execution policy
  project manifest checks
```

Profile đầu tiên dự kiến là `typescript_fullstack_v1`. Chỉ profile này được
triển khai ở cutover đầu, nhưng workflow nhận profile qua contract thay vì import
logic TypeScript trực tiếp.

A1.1 đã đăng ký `typescript_v1` và `typescript_fullstack_v1` bằng immutable
dataclass, component/editable/protected paths và verification stage IDs. A1.2a
đã thêm `SDLC_PROJECT_PROFILE`: giá trị khác rỗng được resolve fail-closed qua
registry. Từ A1.2d, giá trị rỗng không được mở run mới. A1.2b
đã đóng băng profile ID/schema cùng target remote/base branch cho run mới có
selector; registry vẫn không chứa raw command.

Niko không chọn profile theo câu chat. Harness resolve profile từ config, kiểm
target repo phù hợp rồi snapshot profile identity vào run/workspace để retry và
audit dùng cùng một contract.

`profile_snapshot_v1` hiện được tạo ngay khi mở run, trước lần ghi state đầu tiên
và trước LLM, chứa profile/schema IDs cùng target remote/base branch. Action sau
được bind lại từ snapshot dưới run lock; status chỉ công khai classification và
profile IDs. Khi provision workspace cho profiled run, A1.2c đối chiếu manifest
với registry/filesystem rồi bổ sung manifest schema/digest, base SHA và resolved
component roots; resolve/lock sau đó fail closed nếu binding bị drift.

## Target Project Đề Xuất

Repo mới: `SDLC_Target_TypeScript`.

Canonical remote dự kiến là
`https://github.com/LuongAnh1/SDLC_Target_TypeScript.git`, base branch `main`.
Repo local đã được tạo ở A4.1a; canonical remote vẫn chưa được tạo.

```text
apps/web/              React + Vite frontend
apps/api/              Fastify backend
packages/contracts/    shared DTO/schema
packages/db/           Prisma schema, migrations and persistence
tests/e2e/             Playwright browser scenarios
compose.yaml           PostgreSQL test service
.sdlc/target-profile.json  target/profile handshake
```

Repo cung cấp infrastructure skeleton, health check, app shell và test harness.
Một feature nghiệp vụ không được cài sẵn; Developer phải tạo thay đổi thật ở
những component được approved plan yêu cầu.

Manifest là assertion của target repo, còn registry trong Harness là policy
chuẩn. Preflight phải đối chiếu chính xác hai phía và snapshot digest. Manifest,
package scripts/lockfile, tool configs, Compose và CI được bảo vệ mặc định; V1
không cho feature run tự thêm dependency. Business source/test, Prisma
schema/migrations và approved Playwright specs là các editable roots theo
component policy.

Từ A3.1, `developer_work_packet_profiled_v1` chụp metadata
`editable_path_policy_v1` vào output contract. Phase 4 không tin metadata này
một mình: mỗi lần build, refresh, act hoặc repair đều resolve lại policy từ
immutable run snapshot, so khớp metadata rồi kiểm tra từng allowed path. Editable
root là biên trên; Developer vẫn chỉ được ghi exact file trong `allowed_paths`
của approved implementation plan.

## Design Bundle Đề Xuất Cho Phase 2

Class/sequence graph có thể tiếp tục là evidence, nhưng design checkpoint cần
thêm bốn projection:

- **Component topology**: web, API, shared contracts, database và integration
  boundaries.
- **UI contract**: route/screen, states, user actions, validation và observable
  outcomes.
- **API contract**: endpoint, request/response, error cases và authorization
  assumption nếu có.
- **Data contract**: entity, relation, constraint, persistence operation,
  migration và seed intent.

Trace Phase 2 phải nối canonical requirement ID vào các element trên. Nếu source
không đủ để quyết định public API, data constraint hoặc UI behavior thì workflow
phải mở clarification/human checkpoint; Designer không tự bịa.

## Implementation Plan Đề Xuất Cho Phase 3

Plan tương lai cần biểu diễn work item có cấu trúc:

```text
work_item_id
component_id
depends_on
approved requirement/design refs
proposed files
migration impact
verification stage ids
acceptance evidence expected
```

Dependency mặc định của một vertical feature:

```text
shared contract
  -> database schema/migration
  -> backend endpoint/service/persistence
  -> frontend route/component/action
  -> integration/browser acceptance
```

Đây là dependency graph, không phải yêu cầu Developer luôn viết file theo một
thứ tự cứng. Validator phải chặn missing component, target ngoài profile, cycle
và requirement không có downstream acceptance evidence.

## Verification Và QA Đề Xuất Cho Phase 4

Verification không còn là một raw command. Project profile khai báo ordered
stage IDs. Plan và LLM chỉ được tham chiếu ID; registry đã code-review mới được
map ID sang argv, cwd, environment allowlist và timeout. Không dùng shell string
nội suy từ model.

Project profile giữ full-stack stage catalog để target manifest khai báo
capability. `VerificationStagePolicy v1` của Chặng A chỉ kích hoạt smoke suite
`typecheck -> unit`. Khi sang Chặng B phải phát hành policy/profile schema version
mới để mở rộng active suite; không đổi ý nghĩa của policy đã snapshot cho run cũ.
Registry A3.2 hiện resolve hai stage này lần lượt thành `npm run typecheck` và
`npm run test:unit`, cwd root, timeout 120 giây và environment allowlist. Runner
preflight `package.json`/script/npm, chạy argv list với `shell=False`, dừng ở lỗi
đầu tiên và không fallback sang raw command hay Java runner cho profiled packet.
A3.3 bổ sung `QaTestPolicy v1`: QA profiled sinh co-located Vitest files theo
component rule, unit stage truyền exact file list cùng JSON reporter và Runner
đối chiếu `testResults` trước khi công nhận `verified_test_files`. Report thiếu,
không hợp lệ hoặc khác tập selected đều fail; exit code đơn lẻ không còn là bằng
chứng generated test đã chạy. Playwright/browser evidence vẫn thuộc Chặng B.
Full-stack suite đích khi đó là:

```text
dependency_setup
database_ready
migrate_and_seed
typecheck
unit
api_database_integration
frontend_build
service_health
browser_e2e
cleanup
```

Mỗi stage trả status/evidence riêng rồi mới được aggregate thành test report.
Database chỉ dùng instance/schema test theo run, migration từ trạng thái rỗng,
seed deterministic và cleanup kể cả khi fail.

`cleanup` là finally/`always_run`, không phải stage chỉ chạy khi các stage trước
pass. PostgreSQL Compose project, database/schema và service ports phải có
identity riêng theo run; credential test là ephemeral và không được ghi vào
public artifact.

QA browser V1 dùng Chromium và phải chứng minh cùng một scenario qua:

```text
user action
  -> frontend state/request
  -> API response
  -> database effect
  -> final UI state
```

Nếu lỗi nằm ở code/test, repair quay lại Developer và chạy lại downstream
stages. Nếu API/data/UI contract đã duyệt mâu thuẫn, workflow block để human sửa
design thay vì vá code quanh contract sai.

Vitest/Playwright phải xuất machine-readable report để chứng minh file/test vừa
sinh thực sự được discover và execute. Exit code của một baseline command hoặc
việc file đã được compile/build không đủ làm acceptance evidence.

## Traceability Đề Xuất

```text
Requirement ID
  -> UI route/component/action
  -> API endpoint
  -> handler/service
  -> database entity/query/migration
  -> unit/integration/browser test
```

TypeScript deterministic evidence nên dùng compiler AST/API. Regex chỉ phù hợp
cho hint hoặc fallback không blocking. Semantic Validator vẫn review trên
approved claims và Verification evidence như consensus flow hiện hành.

A3.4a hiện thực phần nền này bằng `CodeTracePolicy v1` và helper Node cố định.
Policy lấy identity từ immutable workspace snapshot, chỉ cho source production và
co-located unit test đúng component; helper load `typescript` từ chính target,
không dùng global compiler và không fallback substring khi runtime/package/evidence
không hợp lệ. AST evidence exact cho interface/class/function/method được lưu trong
Code Trace artifact và đưa vào semantic review. A4.1a đã pin TypeScript 6.0.3 và
smoke extractor thật trên target local. Việc đối chiếu đầy đủ signature
(parameter, return, generic, async, optional/rest) vẫn thuộc A3.4b.

## Tương Thích Và Không Thuộc Phạm Vi Hiện Tại

- HTTP action, operation, idempotency và checkpoint không cần đổi chỉ để hỗ trợ
  project profile.
- Status đã thêm `execution_profile` theo hướng additive và không công khai
  target identity; artifact public mới vẫn phải được thêm có chủ ý vào Read API
  allowlist và được sanitize.
- Không tạo microservice orchestration hoặc nhiều repo cho FE/BE ở V1.
- Không cho một run xây toàn bộ ứng dụng hoặc nhiều feature độc lập.
- Không dùng database thật/shared/prod cho agent verification.
- Không triển khai Java runtime profile sau cutover theo quyết định hiện tại.
- Run thiếu profile snapshot được xem là `legacy_java_v1`. Chúng vẫn
  đọc/audit/sync artifact đã có và cleanup được, nhưng approve/continue/
  revalidate sinh artifact mới phải fail `legacy_execution_profile_retired`.
- Không tự migrate Java run. `cancel_run` chưa thuộc HTTP contract của lát này;
  start cùng source đang có active legacy run phải fail thay vì tạo run chồng.
