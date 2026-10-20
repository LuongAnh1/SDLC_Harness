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
| Phase 3 | `agents/developer/planner.py` | Prompt dựng inline | Active | A2.2a: invariant dùng chung + `LanguagePromptDirective` + `PlanningPathPolicy` |
| Phase 3 | `agents/validator/plan_review.py` | Prompt dựng inline | Active | A2.2a: dùng cùng directive đã resolve từ run snapshot |
| Phase 4 | `agents/developer/actor.py` | Prompt dựng inline | Active, còn Java-heavy | A2.2b sau khi A3 khóa editable roots/work packet |
| Phase 4 | `agents/developer/execution.py` | Prompt tool-loop dựng inline | Active, còn Java assumptions | A2.2b/A3; phải dùng execution guard trước khi đổi output |
| Phase 4 | `agents/validator/code_trace.py` | Prompt dựng inline | Active, còn Java path/symbol evidence | A3 cùng TypeScript AST contract |
| Phase 4 | `agents/tester/qa.py` | Prompt dựng inline | Active, còn Java test generation | A3 cùng Vitest discovery và generated-test evidence |
| Fake mode | `llm/client.py::fake_completion` | Dispatch theo marker prompt | Active trong regression | Phase 3 theo directive ở A2.2a; Phase 4/QA để A2.2b/A3 |

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
