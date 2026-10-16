# Requirement V2 Implementation Progress

## Cập nhật Slice 4B - 2026-10-16

Slice 4B đã nối canonical Requirement V2 qua Phase 3 đến
`waiting_implementation_plan_review`.

- Phase 3 đọc frozen artifacts từ checkpoint requirement và kiểm tra lại canonical
  model/projection trước khi dùng.
- Saved requirement graph và design trace phải khớp revision, model ref, nội dung
  canonical graph và exact stable ID set; sai lệch trả
  `requirement_v2_phase3_input_invalid` trước khi tạo workspace hoặc mutate state.
- Developer prompt ưu tiên canonical JSON. Python dựng `requirement_coverage` sau
  mapping merge, gồm requirement ID/content, approved design targets, resolved design
  element IDs, planned code targets và test intents.
- Validator V2 hard-gate coverage thiếu/trùng/tự bịa, target ngoài approved trace,
  thiếu code target hoặc test intent. Repair dựng lại coverage deterministically.
- Markdown/Confluence render coverage; response giữ `requirement_contract_version=v2`.
- Gate cũ trước Phase 3 đã được chuyển sang Phase 4 với mã
  `requirement_v2_phase4_not_ready`; checkpoint Phase 3 được giữ nguyên khi bị chặn.
  Slice 4C sau đó đã gỡ gate tạm và thay bằng canonical input validation.

Boundary tiếp theo là Phase 4 canonical work packet/Developer/Tester/code trace.
Niko/Telegram, live DeepSeek và live Jira smoke chưa nằm trong Slice 4B. Default vẫn V1.

Test tập trung Phase 3 + requirement reader: `43 passed`. Full regression cuối:
`251 passed in 244.06s`. Lượt full đầu tiên lộ một test cũ còn kỳ vọng gate Phase 3;
đã cập nhật theo boundary Phase 4 và chạy lại toàn bộ thành công.
Coverage resolver sau đó được test thêm với class attribute target; hai test V2 happy
path/repair đều pass.

## Cập nhật Slice 4A - 2026-10-16

Slice 4A đã nối Requirement V2 qua Phase 2 với contract dual-read:

```text
V1 packet/formal -> legacy Phase 2 path
V2 canonical model + verified projections -> Designer/Validator -> design checkpoint
```

### Đã triển khai

- Thêm reader nội bộ phân biệt `LegacyRequirementInput` và
  `CanonicalRequirementInput`; thiếu contract version vẫn dùng V1.
- Reader V2 fail closed nếu model/checkpoint lệch revision, source fingerprint
  sai, model không còn normalized/valid, còn blocking question, hoặc packet/formal
  không khớp deterministic projection.
- Reader V1 giữ contract mặc định; nếu state đã tham chiếu formal artifact nhưng
  file bị mất thì fail closed bằng `approved_requirement_not_found`.
- Designer V2 đặt canonical JSON ở đầu prompt làm source of truth; packet/formal
  chỉ được gắn nhãn review projection.
- Validator tạo graph từ canonical model, giữ stable ID `PRE/MF/AF/EF/POST/BR/AC/QR`
  và không đưa unknown/assumption/open question vào traceability requirement set.
- Trace, missing-link report và quality report ghi mode `traceability_v2`, contract
  version, canonical revision và artifact refs.
- Bỏ gate cũ trước Phase 2. Run V2 có thể tới `waiting_design_approval`.
- Tại thời điểm chốt 4A, đặt gate `requirement_v2_phase3_not_ready` trước Phase 3;
  Slice 4B phía trên đã gỡ gate này và chuyển boundary sang Phase 4.

### Boundary tại thời điểm chốt Slice 4A (đã được Slice 4B supersede)

- Phase 3-4 chưa consume canonical Requirement V2 hoặc truyền stable ID qua work packet.
- Niko route/formatter và controlled Jira/Telegram smoke chưa nằm trong slice này.
- Default `SDLC_REQUIREMENT_CONTRACT_VERSION` vẫn là `v1`.

### Kiểm tra

- Test reader V1/V2, revision identity, source fingerprint, projection tampering và
  blocking model.
- Test Designer canonical-first, Validator stable ID và V2 Phase 2 integration.
- Test repair loop V2 xác nhận canonical model được giữ từ draft sang repair,
  stable IDs không đổi giữa hai vòng và checkpoint mở ở design artifact V2.
- Test V2 design approval dừng đúng gate Phase 3; test V1 hiện có tiếp tục chạy.

Kết quả test:

```bash
rtk python -m pytest tests/test_requirement_input_v2.py \
  tests/test_requirement_clarification_v2.py tests/test_phase1.py \
  tests/test_phase2.py tests/test_phase3.py -q
```

Kết quả regression Phase 1-3: `114 passed`.

```bash
rtk python -m pytest -q
```

Kết quả toàn bộ: `243 passed` trong `291.65s`. `git diff --check` không phát
hiện whitespace error.

Sau full suite trên, bổ sung riêng regression repair loop V2 và chạy lại nhóm
Requirement V2 + Phase 2: `56 passed`; riêng file dual-reader/V2: `11 passed`.

## Cập nhật Slice 3 - 2026-10-16

Slice 3 đã hoàn tất ở mức SDLC Harness:

```text
UseCaseSource
  -> Requirement Model V2 r1
  -> waiting_requirement_clarification
  -> answer từng current question
  -> Requirement Model V2 r<n+1> + decision provenance
  -> waiting_requirement_approval
```

Default vẫn là Requirement V1. Gate cũ trước Phase 2 đã được thay bởi reader
dual-read trong Slice 4A; lịch sử dưới đây mô tả trạng thái khi Slice 3 vừa hoàn tất.

### Đã triển khai

- Thêm `SDLC_REQUIREMENT_CONTRACT_VERSION=v1|v2`; mặc định và fallback giá trị sai
  đều là `v1`, đồng thời snapshot contract version vào state của run mới.
- Nối nhánh V2 vào `PhaseOneWorkflow` mà không thay đổi nhánh V1.
- Lưu source snapshot bất biến kèm SHA-256 fingerprint để mọi revision validate
  trên cùng đầu vào.
- Chọn current question bằng Python theo dependency, category priority và stable ID.
- Thêm state/checkpoint `waiting_requirement_clarification` và CLI action
  `answer-clarification`.
- LLM chỉ đề xuất delta cho đúng `question.affects`; Python sở hữu revision,
  decision ID, provenance, submission hash, status và reference mutation.
- Answer `accepted` đóng câu hỏi; answer `insufficient` được audit và tạo follow-up
  mà không sửa requirement.
- Mỗi answer hợp lệ tạo bundle revision bất biến gồm model, packet, formal use case,
  validation report và decision projection.
- Cùng question và cùng answer sau normalize là idempotent: không gọi LLM và không
  tạo revision mới.
- Thêm advisory lock theo run để ngăn hai mutation cùng sửa state/artifact.
- `sync-run`, mock sync và real Atlassian sync nhận clarification context, current
  question và next action nhưng giữ cùng remote identity của requirement phase.
- Chặn approval khi còn blocking question. Khi hết blocking question, checkpoint
  chuyển sang `waiting_requirement_approval`; approval sang Phase 2 vẫn bị chặn rõ
  cho tới Slice 4 downstream dual read.

### Vấn đề gặp trong quá trình triển khai

- `python -m compileall` không ghi được `__pycache__` trong SDLC_Harness vì sandbox
  chỉ cấp quyền ghi trực tiếp cho repo Niko. Đã kiểm tra 63 file bằng `ast.parse`
  không tạo bytecode; sau đó pytest import và chạy toàn bộ module thành công.
- Test artifact Slice 2 ban đầu kỳ vọng bốn key. Slice 3 bổ sung
  `requirement_decisions`, nên expectation được cập nhật thành năm artifact.
- Không phát sinh regression ở Phase 1 V1 hoặc Phase 2-4.

### Kết quả kiểm tra

```bash
rtk python -m pytest tests/test_requirement_clarification_v2.py \
  tests/test_requirement_refiner_v2.py tests/test_phase1.py -q
```

Kết quả tập trung sau khi bổ sung case chuyển hết câu hỏi, config fallback, source
fingerprint, revision conflict, sync context và bốn boundary test: `61 passed`.

```bash
rtk python -m pytest -q
```

Kết quả toàn bộ cuối cùng: `233 passed` trong `339.66s`. `git diff --check` không
phát hiện whitespace error.

Boundary test bổ sung xác nhận:

- model không có blocking question đi thẳng tới requirement approval;
- non-blocking question vẫn được render nhưng không chặn approval;
- sync lỗi sau answer không làm mất revision và `sync-run` retry không gọi lại LLM;
- CLI JSON chạy liên tục `start-or-get-run -> answer-clarification -> get-status`.

### Việc tiếp theo sau Slice 3

Triển khai downstream dual read: Phase 2 đọc canonical Requirement V2, giữ stable
IDs qua design/trace/work packet, rồi mới cho phép approval V2 đi tiếp. Niko
integration và controlled Jira/Telegram smoke thực hiện sau gate này.

Ngày cập nhật: 2026-10-16

## Lịch sử triển khai đến Slice 2

Trước Slice 3, canonical model và refiner đã hoàn tất ở mức code và test:

```text
UseCaseSource
  -> Requirement Model V2 structured output
  -> parse / bounded recovery hoặc repair
  -> Python validation + blocking policy
  -> revisioned canonical model + deterministic projections + audit report
```

Ở thời điểm kết thúc Slice 2, refiner và runtime artifact writer V2 mới có API
độc lập, chưa nối vào `PhaseOneWorkflow`, CLI hoặc runtime state. Phần tích hợp
SDLC Harness đã được hoàn tất trong cập nhật Slice 3 phía trên; Niko vẫn thuộc
lát triển khai sau. Luồng Phase 1 V1 tiếp tục là mặc định.

## Đã triển khai

- Thêm `RequirementModelV2` cùng enum cho requirement, question, decision và
  validation status.
- Thêm `from_dict()`/`to_dict()` có defensive copy và lỗi parse ổn định
  `requirement_model_v2_invalid`.
- Thêm validator nhiều lớp:
  - schema và enum;
  - stable ID/prefix/duplicate/reference;
  - evidence quote đối chiếu source snapshot sau normalize whitespace;
  - provenance cho `explicit`, `human_decision` và `unknown`;
  - Python recompute `effective_blocking`;
  - dependency cycle;
  - guardrail tách main/alternative/exception và assumption.
- Thêm structured validation issue gồm `code`, `path`, `message` để bounded
  repair ở slice sau có thể dùng trực tiếp.
- Thêm deterministic renderer cho Requirement Packet V2 và Formal Use Case V2.
- Thêm fixture canonical UC2 và test độc lập cho parse, validation, blocking,
  evidence, references, flow separation và rendering.
- Export API V2 qua `sdlc_harness.agents.requirements`; không sửa API V1.
- Thêm `RequirementModelV2Refiner` gọi structured output bằng `json_mode=True`
  và `SDLC_LLM_STRUCTURED_MAX_TOKENS`.
- Python ghi đè `schema_version`, revision, run id, source metadata/fingerprint,
  decisions và validation để LLM không thể giả provenance của con người.
- Thêm tối đa một lần gọi phục hồi: format recovery cho JSON bị cắt/không parse
  được, hoặc validator repair cho schema, reference và evidence grounding.
- Thêm attempt metadata gồm purpose, `finish_reason`, usage, parse status,
  JSON mode và output-token limit.
- Thêm cross-projection validator đối chiếu byte-for-byte với canonical renderer.
- Thêm artifact bundle revisioned:
  - `requirement_model_v2_r<n>.json`;
  - `requirement_packet_v2_r<n>.md`;
  - `formal_use_case_v2_r<n>.txt`;
  - `requirement_validation_v2_r<n>.json`.

## Quyết định kỹ thuật

- Dùng dataclass và validator Python chuẩn, không thêm dependency.
- Nested wire objects được giữ dưới dạng dictionary trong root dataclass để
  contract còn có thể mở rộng trong quá trình rollout V2; validator khóa field,
  enum và reference theo tài liệu hiện tại.
- Validator không mutate payload đầu vào. Nó trả canonical model mới đã được
  Python ghi lại `effective_blocking` và `validation`.
- Parser không ép kiểu scalar; revision dạng chuỗi bị từ chối. Actor, evidence và
  question reference bên trong flow đều được kiểm tra fail-closed.
- Blocking question hợp lệ trả trạng thái `clarification_required`; đây không
  phải validation error.
- Renderer chỉ đọc canonical model, không gọi LLM và không ghi runtime artifact.
- `clarification_required` là kết quả refiner hợp lệ và được phép ghi artifact;
  clarification không bị coi là lỗi cần LLM repair.
- Mỗi lần refine có tối đa hai LLM calls. Nếu lần thứ hai vẫn bị cắt, không parse
  được hoặc không hợp lệ thì fail closed bằng error code riêng.
- Lỗi semantic như exception nằm trong main flow hoặc assumption được xác nhận
  không được gửi cho LLM repair.
- Artifact writer không cập nhật state; việc nối workflow và tạo revision tiếp
  theo thuộc các slice sau.

## Lỗi gặp trong quá trình triển khai

- Một lần chỉnh helper kiểm tra enum để lại ký tự `+` ở chữ ký hàm, khiến test
  collection báo `SyntaxError`. Đã sửa ngay và chạy lại test V2 thành công.
- Full suite ban đầu lộ 6 failure trong `tests/test_phase4.py`. Nguyên nhân là
  fake Code Trace LLM còn dùng contract cũ, nên finding thiếu `blocking`, owner,
  action và grounded evidence bị Python hạ thành non-blocking observation.
- Đã cập nhật hai fake validator theo contract hardening hiện tại. Sáu test từng
  fail, toàn bộ Phase 4 và full suite hiện đều pass.
- Slice 2 không phát sinh lỗi code hoặc regression. Test tập trung, Phase 1 V1 và
  full suite đều pass ngay sau khi hoàn thiện implementation.

## Đã kiểm tra

```bash
rtk python -m pytest tests/test_requirement_model_v2.py -q
```

Kết quả cuối: `14 passed`.

```bash
rtk python -m pytest tests/test_phase1.py -q
```

Kết quả: `34 passed`.

```bash
rtk python -m pytest -q
```

Kết quả lần đầu: `200 passed, 6 failed`. Sau khi đồng bộ fake Code Trace
LLM với contract mới:

```bash
rtk python -m pytest tests/test_phase4.py -q
rtk python -m pytest -q
```

Kết quả cuối: Phase 4 `46 passed`; full suite `206 passed`.

Sau Slice 2:

```bash
rtk python -m pytest tests/test_requirement_model_v2.py tests/test_requirement_refiner_v2.py -q
```

Kết quả: `25 passed`.

```bash
rtk python -m pytest tests/test_phase1.py -q
```

Kết quả regression V1: `34 passed`.

```bash
rtk python -m pytest -q
```

Kết quả toàn bộ: `217 passed`.

`git diff --check` không phát hiện whitespace error trước khi cập nhật tài liệu;
sẽ chạy lại ở bước chốt.

## Kế hoạch Slice 3 đã hoàn thành

Slice 3 đã triển khai clarification state/action: chọn blocking question hiện
tại, nhận answer có question ID, lưu decision provenance, tạo revision mới và
chỉ mở requirement approval khi không còn blocking question.

Chưa chuyển config mặc định sang V2 cho tới khi workflow, clarification và
downstream dual-read hoàn tất.

## 2026-10-16 - Slice 4C: Requirement V2 qua Phase 4

Đã mở Phase 4 cho run V2 và giữ stable requirement ID xuyên Developer work
packet, code change evidence, Validator code trace, QA scenario và Code Review:

- Phase 4 đọc lại frozen canonical model và kiểm contract/revision/artifact refs,
  trace và coverage trước khi ghi work packet;
- `developer_work_packet_v1` giữ wire name cũ nhưng có thêm contract, canonical
  requirements, coverage và projection theo file;
- Developer output có `covered_requirement_ids`; Python chặn ID ngoài coverage;
- code trace V2 sinh đúng một mapping cho mỗi canonical ID và ghi coverage stats;
- QA V2 bắt buộc hợp `requirement_ids` của các scenario phủ đúng canonical set;
- PR/Jira/Confluence Code Review hiển thị contract revision và coverage count.

Trong lúc triển khai, fixture QA fake cũ không trả stable IDs nên hard gate báo
`qa_requirement_coverage_invalid`. Fake runtime đã được sửa để lấy IDs trực tiếp
từ canonical QA packet, không hard-code theo UC2.

Default vẫn là V1. Chưa chạy controlled live smoke V2 với DeepSeek/Jira/GitHub và
chưa đổi Niko clarification routing trong slice này.

Regression cuối của Slice 4C: Phase 3/4 `81 passed`, Code Review/GitHub sync
`16 passed`, toàn bộ suite `257 passed`; `git diff --check` sạch.

### Bổ sung kiểm thử E2E và repair - 2026-10-16

Đã bổ sung regression test chạy contract V2 xuyên suốt Phase 4 tới
`code_review_approved`, cùng test ép một vòng Developer/code-trace repair và một
vòng QA repair. Cả hai test xác nhận revision và canonical stable ID set không
đổi giữa work packet, trace, QA, PR và Code Review sync.

Test đầu tiên phát hiện Jira/Confluence Code Review mới chỉ hiển thị coverage
count. Mock và real renderer đã được bổ sung danh sách stable IDs cụ thể để
reviewer đối chiếu trực tiếp.

Kết quả cuối: nhóm Phase 3/4/GitHub/Code Review `100 passed`; toàn bộ suite
`259 passed`. Controlled live smoke V2 vẫn chưa chạy.

## Requirement V1 Retirement (2026-10-16)

- [x] Controlled E2E UC2 hoàn tất trên Requirement Contract V2.
- [x] Cố định runtime contract ở V2 và gỡ config/CLI selector.
- [x] Gỡ refiner, validator, prompt, writer và downstream reader V1.
- [x] State không phải V2 fail closed bằng `requirement_contract_unsupported`.
- [ ] Chạy regression sau thay đổi retirement khi được yêu cầu.
