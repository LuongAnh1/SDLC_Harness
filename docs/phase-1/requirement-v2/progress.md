# Requirement V2 Implementation Progress

Ngày cập nhật: 2026-10-16

## Trạng thái hiện tại

Slice 2 đã triển khai xong ở mức code và test:

```text
UseCaseSource
  -> Requirement Model V2 structured output
  -> parse / bounded recovery hoặc repair
  -> Python validation + blocking policy
  -> revisioned canonical model + deterministic projections + audit report
```

Refiner và runtime artifact writer V2 đã có API độc lập. Slice này chưa nối vào
`PhaseOneWorkflow`, CLI, runtime state hoặc Niko. Luồng Phase 1 V1 vẫn là luồng
runtime mặc định.

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

## Việc tiếp theo

Slice 3 sẽ triển khai clarification state/action: chọn blocking question hiện
tại, nhận answer có question ID, lưu decision provenance, tạo revision mới và
chỉ mở requirement approval khi không còn blocking question.

Chưa chuyển config mặc định sang V2 cho tới khi workflow, clarification và
downstream dual-read hoàn tất.
