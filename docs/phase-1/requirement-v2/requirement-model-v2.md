# Requirement Model V2

## Vai Trò Canonical

`requirement_model_v2_r<n>.json` là nguồn sự thật duy nhất của requirement cho
run V2. `r<n>` là revision nghiệp vụ, bắt đầu từ `r1` và tăng sau mỗi lần áp một
human decision hoặc một lần sửa model được validator chấp nhận.

Tên schema và revision là hai khái niệm riêng:

```text
schema_version=requirement_model_v2
revision=1, 2, 3, ...
```

`latest_artifacts.requirement_model` luôn trỏ tới revision mới nhất. Artifact
cũ bất biến để audit.

## Schema Khái Niệm

Ví dụ rút gọn cho UC2:

```json
{
  "schema_version": "requirement_model_v2",
  "revision": 1,
  "run_id": "run_...",
  "source": {
    "source_ref": "jira_issue:SDLC-2",
    "artifact_ref": "use_case_packet_v1",
    "fingerprint": "sha256:<digest>"
  },
  "use_case": {
    "id": "UC-1",
    "name": "Insert Cultural Heritage",
    "goal": {
      "id": "GOAL-1",
      "text": "Agency Operator inserts a cultural object into the system.",
      "status": "explicit",
      "evidence_refs": ["E-1"]
    },
    "actors": [
      {
        "id": "ACT-1",
        "name": "Agency Operator",
        "status": "explicit",
        "evidence_refs": ["E-2"]
      }
    ]
  },
  "preconditions": [
    {
      "id": "PRE-1",
      "text": "Agency Operator is logged in.",
      "status": "explicit",
      "evidence_refs": ["E-3"]
    }
  ],
  "flows": {
    "main": [
      {
        "id": "MF-1",
        "actor": "ACT-1",
        "action": "activates the insertion feature",
        "evidence_refs": ["E-4"]
      }
    ],
    "alternatives": [
      {
        "id": "AF-1",
        "trigger": "Agency Operator cancels the operation",
        "steps": [],
        "outcome": null,
        "evidence_refs": ["E-5"],
        "open_question_refs": ["Q-1"]
      }
    ],
    "exceptions": [
      {
        "id": "EF-1",
        "trigger": "Input data is invalid or insufficient",
        "steps": ["System activates the Errored use case"],
        "outcome": null,
        "evidence_refs": ["E-6"],
        "open_question_refs": ["Q-2"]
      }
    ]
  },
  "postconditions": [],
  "business_rules": [
    {
      "id": "BR-1",
      "text": "The system rejects duplicate cultural heritage records.",
      "status": "explicit",
      "evidence_refs": ["E-7"],
      "open_question_refs": ["Q-3"]
    }
  ],
  "acceptance_criteria": [],
  "quality_requirements": [],
  "assumptions": [],
  "open_questions": [
    {
      "id": "Q-3",
      "question": "Tiêu chí nào xác định hai cultural heritage record là duplicate?",
      "category": "data_identity",
      "proposed_blocking": true,
      "effective_blocking": true,
      "affects": ["BR-1"],
      "depends_on": [],
      "status": "open",
      "evidence_refs": ["E-7"]
    }
  ],
  "decisions": [],
  "evidence": [
    {
      "id": "E-7",
      "source_ref": "jira_issue:SDLC-2",
      "location": "description",
      "quote": "The system requirements will not accept duplicates cultural heritage."
    }
  ],
  "validation": {
    "status": "clarification_required",
    "errors": []
  }
}
```

Schema triển khai phải khóa bằng JSON Schema hoặc validator Python tương đương.
Ví dụ trên mô tả field bắt buộc về mặt contract; implementer có thể tách type
nội bộ nhưng không đổi ý nghĩa hoặc tên wire field khi chưa cập nhật tài liệu.

## Quyền Sở Hữu Field

LLM chỉ được sinh nội dung nghiệp vụ, evidence proposal, open question và
`proposed_blocking`. Python workflow sở hữu và tính các field sau:

```text
schema_version
revision
run_id
source.source_ref
source.artifact_ref
source.fingerprint
effective_blocking
answered_at
validation
```

Model không được tự đặt revision, đánh dấu validator pass hoặc ghi một question
là resolved nếu chưa có accepted human decision.

## Contract Theo Collection

Các item requirement dùng field chung `id`, `text`, `status`, `evidence_refs`,
`decision_ref` và `open_question_refs`. `decision_ref` chỉ có khi status là
`human_decision`; `open_question_refs` có thể rỗng.

| Collection | Field bổ sung bắt buộc |
| --- | --- |
| `use_case.goal` | `id` |
| `actors` | `name` thay cho `text` |
| `preconditions`, `postconditions`, `quality_requirements` | Không có |
| `flows.main` | `actor`, `action`; `target` có thể null |
| `flows.alternatives`, `flows.exceptions` | `trigger`, `steps`, `outcome` |
| `business_rules` | Không có |
| `acceptance_criteria` | `verifies` chứa stable IDs được kiểm chứng |
| `assumptions` | `reason`, `affects`; không được có status confirmed |
| `open_questions` | `question`, `category`, `proposed_blocking`, `effective_blocking`, `affects`, `depends_on`, `status`, `answer_guidance` |
| `decisions` | `question_id`, `answer`, `answered_by`, `answered_at`, `affected_requirement_ids`, `status`, `supersedes_decision_id`, `submission_hash` |
| `evidence` | `source_ref`, `location`, `quote` |

Giá trị enum:

```text
requirement status: explicit | human_decision | assumption | unknown
question status: open | resolved | superseded
decision status: accepted | insufficient | superseded
validation status: valid | clarification_required | invalid
```

## ID Và Reference Integrity

- Prefix ổn định: `UC`, `GOAL`, `ACT`, `PRE`, `MF`, `AF`, `EF`, `POST`, `BR`,
  `AC`, `QR`, `ASM`, `Q`, `D`, `E`.
- ID không đổi giữa các revision nếu cùng một thực thể còn tồn tại.
- Question `affects` chỉ được trỏ tới ID tồn tại.
- Acceptance criterion phải khai báo `verifies` tới flow/rule/postcondition mà
  nó kiểm chứng.
- Decision phải trỏ đúng một question và liệt kê các requirement ID được sửa.
- Xóa/thay thế item phải lưu quan hệ supersede trong decision; không tái sử dụng
  ID cũ cho nội dung khác.

## Grounding Policy

Mỗi statement dùng một trong bốn trạng thái:

```text
explicit        có bằng chứng trực tiếp trong source
human_decision  được xác nhận qua clarification
assumption      giả định non-blocking, chưa phải requirement confirmed
unknown         chưa đủ thông tin và phải liên kết open question
```

Quy tắc bắt buộc:

- `explicit` phải có ít nhất một `evidence_ref` hợp lệ.
- `human_decision` phải có `decision_ref` hợp lệ.
- `assumption` không được Designer/Developer biến thành constraint bắt buộc.
- `unknown` không được chứa nội dung đoán; field có thể `null` hoặc rỗng và phải
  có `open_question_ref`.
- Evidence quote phải xuất hiện trong source sau normalization whitespace; model
  không được tự tạo evidence từ Formal Use Case hoặc Requirement Packet.
- Thiếu business value, quality constraint hoặc exception outcome không được
  lấp bằng domain knowledge.

## Blocking Policy

LLM xuất `category`, `proposed_blocking`, `affects` và lý do ngắn. Python tính:

```text
effective_blocking = proposed_blocking
                     OR category thuộc HARD_BLOCKING_CATEGORIES
                     OR affects requirement bắt buộc để test/thiết kế hành vi
```

`HARD_BLOCKING_CATEGORIES` tối thiểu gồm:

```text
business_rule
acceptance_criteria
data_identity
api_contract
observable_behavior
```

Python được nâng non-blocking thành blocking nhưng không tự hạ một question mà
LLM đã đánh blocking. Question wording/UI/documentation/future optimization có
thể non-blocking nếu không ảnh hưởng hành vi quan sát được.

## Decision Provenance

Mỗi câu trả lời được lưu trong `decisions` và projection audit
`requirement_decisions_v2_r<n>.json`:

```json
{
  "id": "D-1",
  "question_id": "Q-3",
  "answer": "Duplicate khi trùng mã định danh cultural heritage.",
  "answered_by": "telegram:<user-id>",
  "answered_at": "<UTC ISO-8601>",
  "affected_requirement_ids": ["BR-1", "AC-2"],
  "supersedes_decision_id": null,
  "submission_hash": "sha256:<normalized-question-answer-hash>"
}
```

Không lưu username/token hoặc platform payload thừa trong artifact. Niko truyền
caller identity tối thiểu; SDLC Harness ghi timestamp và revision.

## Projection Requirement Packet V2

`requirement_packet_v2_r<n>.md` được render deterministically từ canonical JSON,
gồm:

```text
Goal
Actors
Preconditions
Main Flow
Alternative Flows
Exception Flows
Postconditions
Business Rules
Acceptance Criteria
Quality / Non-functional Requirements
Assumptions
Open Questions
Human Decisions
Source Evidence
```

Mỗi mục hiển thị stable ID. Blocking question mở phải có nhãn `BLOCKING`; câu
non-blocking có nhãn `NON-BLOCKING`. Packet không tự thêm hoặc diễn đạt lại rule
bằng LLM sau khi canonical model đã được chấp nhận.

## Formal Use Case V2

`formal_use_case_v2_r<n>.txt` mở rộng grammar TraceDev. Serialization chuẩn:

```text
UsecaseName: Insert Cultural Heritage
Goal:
[GOAL-1] Agency Operator inserts a cultural object into the system.
Actors:
[ACT-1] Agency Operator
Entry Conditions:
[PRE-1] Agency Operator is logged in.
Main Flow:
[MF-1] ACT-1 activates the insertion feature.
Alternative Flows:
[AF-1] Trigger: Agency Operator cancels the operation.
[AF-1.1] UNKNOWN_FROM_SOURCE [Q-1]
[AF-1.OUTCOME] UNKNOWN_FROM_SOURCE [Q-1]
Exception Flows:
[EF-1] Trigger: Input data is invalid or insufficient.
[EF-1.1] System activates the Errored use case.
[EF-1.OUTCOME] UNKNOWN_FROM_SOURCE [Q-2]
Exit Conditions:
[POST-1] System notifies successful inclusion.
Business Rules:
[BR-1] The system rejects duplicate cultural heritage records.
Quality Requirements:
[QR-1] UNKNOWN_FROM_SOURCE [Q-4]
Open Questions:
[Q-1] BLOCKING | observable_behavior | What happens after cancellation?
```

Quy tắc serialization:

- Section luôn xuất hiện theo đúng thứ tự trên.
- Item một dòng dùng `[ID] <text>`.
- Alternative/exception dùng `[FLOW-ID] Trigger:`, các bước dùng
  `[FLOW-ID.<n>]`, outcome dùng `[FLOW-ID.OUTCOME]`.
- Open question dùng `[Q-ID] BLOCKING|NON-BLOCKING | <category> | <question>`.
- Section không có item và không cần clarification ghi `[NONE]`.
- Field thiếu cần làm rõ ghi `UNKNOWN_FROM_SOURCE [Q-<id>]`.
- Evidence không lặp trong formal text; parser đối chiếu ID với canonical JSON.

Alternative/exception không được đưa vào Main Flow hay Exit Conditions. Parser
V2 chấp nhận marker unknown nhưng approval gate vẫn chặn nếu question tương ứng
là blocking.

Formal V2 là projection phục vụ trace/debug và compatibility. Designer V2 không
dùng Formal V2 để ghi đè canonical JSON.

## Validation Layers

V2 chạy theo thứ tự:

1. JSON/schema validation.
2. ID/reference integrity.
3. Evidence grounding với source snapshot.
4. Blocking policy do Python tính lại.
5. Cross-field semantics: happy path không chứa exception; postcondition không
   chứa cancel/error; assumption không được đánh confirmed.
6. Render projections.
7. Parse projections và so ID/content với canonical model.

Lỗi schema/grounding/reference được đưa về bounded Refiner repair. Blocking
question hợp lệ không phải lỗi validator; nó chuyển workflow sang clarification.

