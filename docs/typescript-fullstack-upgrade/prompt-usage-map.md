# Prompt Usage Map

Tài liệu này phân biệt prompt được runtime gọi thật với prompt chỉ còn là tài
liệu tham chiếu. Mục tiêu là chuyển đổi theo call site và profile contract,
không sửa hàng loạt file chỉ vì chúng có nội dung Java.

## Quy tắc ownership

- Invariant của agent role giữ JSON schema, scope, traceability, repair và human
  checkpoint; invariant không chọn ngôn ngữ hoặc component.
- Language profile chọn `prompt_directive_id`; registry directive cung cấp phần
  hướng dẫn theo ngôn ngữ.
- Project/path policy cung cấp component roots, suffix và deterministic target.
- LLM chỉ nhận context đã resolve; không được tự chọn profile, ngôn ngữ hoặc
  ghi đè component target.

## Runtime usage map

| Phase | Owner/call site | Nguồn prompt | Trạng thái | Hướng migration |
| --- | --- | --- | --- | --- |
| Phase 1 | `agents/requirements/refiner_v2.py`, `clarification_v2.py` | File template được `read_text` | Active, độc lập target implementation | Giữ ngoài Chặng A trừ khi phát hiện target-language coupling mới |
| Phase 2 | `agents/designer/designer.py` | File template được `read_text` | Active, chủ yếu design/PlantUML | Giữ language-neutral; component projection thuộc Chặng B |
| Phase 2 | `agents/validator/traceability.py` | `traceability_class.txt`, `traceability_sequence.txt` | Active | Giữ invariant traceability; không gắn target language |
| Phase 3 | `agents/developer/planner.py` | Prompt dựng inline | Active | A2.2a/A2.3: invariant + language/path directive; test intent chỉ tham chiếu profile stage ID |
| Phase 3 | `agents/validator/plan_review.py` | Prompt dựng inline | Active | A2.2a/A2.3: cùng directive và fail-closed verification stage policy từ snapshot |
| Phase 4 | `agents/developer/actor.py` | Prompt dựng inline | Profile-aware; Java chỉ ở legacy compatibility | A2.2b resolve directive/path/signature từ work packet trước act/repair |
| Phase 4 | `agents/developer/execution.py` | Prompt tool-loop dựng inline | Profile-aware manifest/action context; chưa nối vào Phase 4 workflow | A2.2b phát hành manifest V6; executor cutover là lát riêng |
| Phase 4 | `agents/validator/code_trace.py` | Prompt dựng inline | Profiled run dùng exact TypeScript AST symbol/signature evidence; legacy branch giữ Java compatibility | A3.4b đã bind plan V3/work packet V2 và giữ deterministic mismatch ngoài quyền phủ quyết của LLM |
| Phase 4 | `agents/tester/qa.py` | Prompt dựng inline | Profiled run dùng TypeScript/Vitest; legacy branch giữ Java compatibility | A3.3 dùng QA policy, exact selected files và Vitest JSON evidence; browser QA để B4 |
| Fake mode | `llm/client.py::fake_completion` | Dispatch theo marker prompt | Active, profile-aware trong regression | Phase 3/4, QA và Code Trace đọc contract thật trong prompt; Java chỉ cho legacy packet |

## File reference-only

Các file sau không có runtime call site. Trong đó `developer.txt`, `tester.txt`
và `validator.txt` chỉ còn availability assertion trong `tests/test_phase1.py`;
các file update/edit còn lại không được runtime đọc:

- `agents/developer/prompts/developer.txt`
- `agents/developer/prompts/update_code.txt`
- `agents/developer/prompts/update_code_tester.txt`
- `agents/tester/prompts/tester.txt`
- `agents/tester/prompts/edit_source_file.txt`
- `agents/validator/prompts/validator.txt`

Chúng được giữ làm TraceDev reference trong lát A2.2a. Không dùng nội dung các
file này để kết luận prompt runtime vẫn là Java, và không nối lại chúng vào
workflow nếu chưa có contract/profile test tương ứng.

## Trạng thái A2.2a

- `typescript_prompt_v1` đã được resolve fail-closed từ language profile của
  immutable run snapshot.
- Developer.plan và Validator.review_plan dùng cùng directive; phần invariant
  không hard-code path/suffix TypeScript.
- Path ví dụ được sinh từ `PlanningPathPolicy`, không do LLM chọn.
- Fake Developer.plan nhận biết directive ID và sinh TypeScript paths.
- Prompt Phase 4, QA và Code Trace vẫn được đánh dấu pending, không bị coi là đã
  chuyển đổi trong lát này.

## Trạng thái A2.2b

- Developer batch act/repair resolve language context từ profiled work packet,
  chọn path ví dụ từ exact `allowed_paths` và fail closed khi editable path
  policy không khớp approved signature profile.
- Tool-loop manifest V6 bind directive, profile identity và per-file approved
  signatures vào input hash/action context; test path nhận diện cả Java legacy
  lẫn `.test.*`, `.spec.*`, `__tests__` của profile hiện hành.
- Fake Developer dùng chung deterministic renderer với direct agent fallback;
  fake QA chọn component root/suffix từ `QaTestPolicy`, fake Code Trace dùng
  static mappings trong review input thay vì Java path cố định.
- `DeveloperToolLoopExecutor` vẫn chưa được nối vào `PhaseFourWorkflow`; việc
  chọn/cutover executor là lát độc lập để không trộn prompt migration với thay
  đổi control flow.

## Trạng thái A2.3

- Project profile cung cấp stage catalog; Phase 3 không chứa mapping stage sang
  lệnh thực thi.
- `VerificationStagePolicy v1` khóa active stage list ở `typecheck -> unit` từ
  frozen project snapshot. Full-stack catalog không đồng nghĩa mọi stage đang
  bắt buộc; Chặng B phải phát hành policy/profile version mới để kích hoạt thêm.
- Developer plan/repair và fake profiled dùng `test_intents[].stage_id`; prompt
  cấm `command`, `argv`, `cwd`, `env` và timeout.
- Validator so exact ordered stage list với frozen snapshot, chặn stage do LLM
  tự tạo và mọi execution field trong profiled plan.
- Profiled plan dùng `implementation_plan_structured_v2`; review bind SHA-256
  của artifact và Phase 4 kiểm lại contract trước khi tạo work packet.
- Markdown/Confluence hiển thị stage ID; artifact legacy command-based vẫn được
  đọc bởi nhánh compatibility đã bị public Application Service retire.

## Trạng thái A3.1

- Work packet profiled đã dùng editable roots từ frozen profile thay cho prefix
  Java và lưu metadata `editable_path_policy_v1` để audit.
- Refresh, batch apply, implementation repair và QA repair đều resolve lại cùng
  policy từ run snapshot; packet thiếu/sai policy hoặc allowed path ngoài profile
  bị chặn trước khi ghi file.
- Prompt/fake Developer.act và Code Trace vẫn Java-heavy theo đúng A2.2b; QA
  profiled đã được chuyển riêng ở A3.3. A3.1 chỉ mở guard an toàn, chưa tuyên bố
  toàn bộ Phase 4 TypeScript đã chạy được.

## Trạng thái A3.2

- Verification Runner nhận stage policy resolve từ frozen snapshot; work packet
  chỉ được mang stage ID, không được cung cấp executable data.
- Registry trong tầng verification sở hữu fixed argv/cwd/env allowlist/timeout
  cho `typecheck -> unit`; subprocess luôn dùng argv list và `shell=False`.
- Test report, quality report và repair feedback mang ordered stage evidence;
  flat fields cũ vẫn được giữ để đọc artifact legacy.
- A3.2 ban đầu giữ `required_test_files` fail closed; A3.3 đã thay guard tạm đó
  bằng exact Vitest execution và machine-readable evidence.

## Trạng thái A3.3

- `QaTestPolicy v1` resolve từ immutable profile snapshot, chọn framework Vitest,
  co-located test roots/suffixes và không cho QA mở rộng sang Playwright/E2E.
- Prompt, parser và fake QA profiled sinh TypeScript/Vitest; Java prompt/fake chỉ
  còn ở nhánh compatibility cho artifact legacy.
- Apply chặn path ngoài policy, duplicate và ghi đè test có sẵn không do cùng run
  sở hữu; retry chỉ dùng ownership đọc từ artifact QA trước đó cùng policy.
- Registry unit stage cố định JSON reporter. Runner truyền exact generated paths,
  đọc `testResults` và chỉ ghi `verified_test_files` khi report khớp chính xác;
  missing/invalid/mismatched report đều fail dù process trả exit code 0.
- QA execution/report/quality evidence công bố riêng selected và verified files.
  Browser QA vẫn là lát Chặng B.

## Trạng thái A3.4

- `CodeTracePolicy v1` resolve từ immutable workspace-bound snapshot và khóa
  source/test discovery theo project/language profile. Test evidence của Chặng A
  chỉ nhận co-located `.test.ts`/`.test.tsx`, không nhận Playwright/E2E.
- Node helper là code-reviewed asset được đóng gói cùng Validator, chỉ load package
  `typescript` của target và trả evidence có version/extractor/profile/path rõ ràng.
  Thiếu runtime/package, parse diagnostic hoặc payload sai contract đều fail closed;
  profiled run không fallback sang regex hay substring.
- Code Trace dùng exact AST declaration names cho interface/class/function/method,
  đưa evidence vào artifact và semantic-review prompt; rule yêu cầu JUnit đã được
  thay bằng test-framework-neutral invariant. Java path/text/signature checks chỉ
  còn ở nhánh legacy.
- A3.4b đã phát hành `implementation_signature_contract_v1` trong plan V3,
  mang nguyên qua profiled work packet V2 và so exact parameter order/type,
  optional/rest, return, generic cùng async. Phase 3 phải phát hành entry; không
  suy type từ PlantUML khi thiếu.
- Controlled smoke trên target local pin TypeScript 6.0.3 pass cho function/method
  thật; deliberate mismatch tạo `contract_signature_mismatch`. Plan V2/packet V1
  lịch sử tiếp tục symbol-only, không auto-migrate.
