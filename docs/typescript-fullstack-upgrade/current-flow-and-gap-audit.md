# Current Flow And Gap Audit

## Baseline Và Cách Đọc

Audit này dùng baseline `SDLC_Harness` commit `e07c9be` trên `main`. Kết luận
được đối chiếu từ docs, CodeGraph, source và tests; không suy từ tên package.

Các nhãn dùng trong tài liệu:

- **Hiện hành**: behavior đã có trong code/runtime.
- **Java coupling**: behavior chỉ đúng với target Java hiện tại.
- **Single-project coupling**: behavior giả định một layout hoặc một verification
  command cho toàn repo.
- **Đề xuất**: hướng tương lai, chưa triển khai.

## Luồng Hiện Hành

```text
Niko / Dashboard
  -> HTTP API
  -> durable operation + ApplicationService
  -> Phase 1 Requirement
  -> Phase 2 Design
  -> Phase 3 Implementation Plan
  -> Phase 4 Developer / Verification / Validator / QA
  -> GitHub publication + Jira/Confluence checkpoint
```

| Phase | Owner/evidence chính | Artifact/checkpoint hiện hành | Mức phụ thuộc target |
| --- | --- | --- | --- |
| Intake/Requirement | `workflows/phase1.py`, `agents/requirements/` | canonical Requirement V2, clarification/approval | Hầu như độc lập ngôn ngữ |
| Design | `workflows/phase2.py`, `agents/designer/`, `agents/validator/traceability.py` | PlantUML, class/sequence graph, requirement-design trace | Thiên về class/sequence model, chưa mô tả đầy đủ web topology |
| Implementation planning | `workflows/phase3.py`, `agents/developer/planner.py`, `agents/validator/plan_review.py` | versioned plan + hybrid review + human checkpoint | Java và layout target bị hard-code |
| Implementation/QA | `workflows/phase4.py`, `agents/developer/`, `verification/runner.py`, `agents/tester/qa.py`, `agents/validator/code_trace.py` | code/test/trace/QA reports, Draft PR, Code Review approval | Java, một repo layout và một baseline command |

HTTP/CLI cùng đi qua application service. Niko hiện dùng HTTP API-only; CLI là
operator/debug interface. Worktree-per-run, durable operations, idempotency,
checkpoint và sync ownership không cần thay đổi chỉ vì đổi target language.

## Java Coupling Inventory

| Khu vực | Dẫn chứng hiện hành | Vấn đề cần tài liệu hóa cho bước sau |
| --- | --- | --- |
| Config/target | `core/config.py` và `core/config_schema.py` mặc định `SDLC_Target_eTour`; `.env.example` mô tả JDK fallback | Cần project profile và target identity mới, không để language ẩn trong defaults |
| Run/profile binding | State khởi tạo ở `workflows/phase1.py` chỉ snapshot requirement/LLM metadata; `RunWorkspaceRecord` snapshot repo identity sau khi provision nhưng không có execution profile | Profile/target contract phải được bind từ lúc mở run để config cutover không đổi ngữ nghĩa run đang tồn tại |
| Source discovery | `workflows/phase3.py::_target_source_files` chỉ quét `src/main/java` và `src/test/java` | Source/test roots và extensions phải thuộc profile |
| Test command | `workflows/phase3.py` truyền cố định `scripts/test.ps1` ở mọi plan/replan path | Plan không được sở hữu raw command cố định theo Java |
| Plan mapping | `agents/developer/planner.py::_java_path`, `_default_java_file`, `_test_path_for_main` | File mapping/test mapping phải dùng layout policy của project |
| Plan validation | `agents/validator/plan_review.py::_normalized_java_path` và các Java-root rules | Rule gate phải đọc profile/path contract thay vì tên thư mục Java |
| Work packet | `agents/developer/work_packet.py::ALLOWED_CODE_PATH_PREFIXES` chỉ cho hai Java roots | Allowlist phải được dựng từ approved component roots |
| Apply guard | `workflows/phase4.py::_normalize_code_path` và `_normalize_qa_test_path` chặn ngoài `.java` roots | Production/test/DB/config paths cần scope rõ theo component |
| Repair context | `agents/developer/repair_context.py` chỉ đọc symbol reference trong file `.java` | Symbol extraction và diagnostic hints phải do language profile sở hữu |
| Deterministic validator | `agents/validator/code_contract.py` dùng `JAVA_METHOD_RE` và Java interface rules | TypeScript cần AST-based contract evidence; không thay bằng regex TypeScript |
| Verification | `verification/runner.py` dò JDK, Java `main()`, `.class` và chạy `java -cp build` | Setup, discovery, execution và error classification phải tách khỏi runner core |
| QA generation | `agents/tester/qa.py::_qa_generation_prompt` yêu cầu Java; Phase 4 chỉ apply `src/test/java/*.java` | QA path, framework và execution evidence phải theo project profile |
| Fake mode | `llm/client.py` trả Java paths/source/tests cố định | Fixture fake phải phản ánh cùng contract với target hiện hành |
| Sync/artifact prose | `artifacts/writers.py` và `sync/atlassian.py` còn câu chữ Java | Wording phải dùng profile/component metadata, không suy từ language |

Các file prompt TraceDev như `agents/developer/prompts/developer.txt` và
`agents/tester/prompts/tester.txt` chứa Java rất đậm, nhưng tìm call site cho thấy
chúng chủ yếu còn là reference/availability artifact. Prompt đang chạy được dựng
trực tiếp trong planner, developer tool loop, code trace và QA. Khi triển khai
sau này phải lập usage map trước khi sửa/xóa prompt legacy để tránh coi mọi file
prompt là runtime-equivalent.

## Target Bootstrap Hiện Hành

`TargetRepoPreparationWorkflow` chỉ xác minh repo tồn tại, Git remote đúng,
worktree sạch và tùy chọn chuẩn bị CodeGraph. Nó không scaffold project.

Bootstrap thật nằm trong repo `SDLC_Target_eTour`:

- hai Java source file dưới `src/main/java`;
- một Java test class có `main()` dưới `src/test/java`;
- `scripts/test.ps1` compile bằng `javac` và chạy một class;
- không có dependency manager hoặc web runtime.

Do đó project bootstrap và project profile là hai concern riêng: profile mô tả
contract mà Harness hiểu; target repo cung cấp infrastructure phù hợp contract.

## Full-stack Gap Inventory

| Concern | Hiện trạng | Khoảng trống để xây web FE/BE/DB |
| --- | --- | --- |
| System design | Class và sequence graph | Chưa có component topology, UI contract, API contract hoặc data model/migration contract |
| Planning unit | Danh sách step/file/mapping phẳng | Chưa có component owner, dependency giữa contracts/DB/API/UI và migration impact |
| Work packet | Một allowlist toàn run | Chưa biểu diễn work item theo component nhưng vẫn giữ cùng worktree/branch |
| Verification | Chọn command đầu tiên trong `test_intents` | Chưa có ordered stages cho setup, typecheck, unit, integration, build, service và browser |
| Environment | Chỉ subprocess command trong target path | Chưa có DB lifecycle, migration/seed, health check, port/process ownership hoặc cleanup |
| QA | Sinh source test rồi yêu cầu runner execute | Chưa có browser action, API/DB evidence, screenshot/log hoặc service orchestration |
| Trace | Requirement -> design element -> code/test | Chưa trace UI action -> endpoint -> service -> persistence -> E2E |
| Repair | Diagnostic/Validator feedback về allowed files | Chưa xác định component/downstream stages cần rerun sau mutation |
| Publication | Một test/trace/QA summary | Chưa tổng hợp evidence theo component và cross-layer scenario |
| HTTP artifact exposure | `application/read_service.py::PUBLIC_ARTIFACT_KEYS` là allowlist fail-closed | Artifact design/verification/trace mới phải được thêm có chủ ý, sanitize và contract-test cho Niko |

## Kết Luận Audit

Đổi `.java` thành `.ts` là chưa đủ. Hướng đúng cần ba lớp tách biệt:

1. workflow/checkpoint/state chung, tiếp tục độc lập target;
2. language/project profile, sở hữu layout/toolchain/static evidence;
3. full-stack artifact và verification topology, sở hữu quan hệ FE/BE/DB.

Nếu gộp ba lớp thành một `TypeScriptRunner` lớn, Harness chỉ thay hard-code Java
bằng hard-code TypeScript và vẫn không biểu diễn được web application.
