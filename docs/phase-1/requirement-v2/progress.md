# Requirement V2 Implementation Progress

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

Default vẫn là Requirement V1. Run V2 dừng an toàn trước Phase 2 bằng lỗi
`requirement_v2_downstream_not_ready` cho tới khi downstream dual read được triển
khai ở slice tiếp theo.

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
