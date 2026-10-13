# Phase 3 - Design Đến Implementation Plan

Phase 3 bắt đầu sau khi người dùng duyệt checkpoint design của Phase 2.
Mục tiêu của phase này là biến `design_artifact_v{n}` và graph/trace đã duyệt
thành một `implementation_plan_v1.md` đủ rõ để human/Niko review trước khi agent
được phép sửa target repo.

Điểm khác biệt quan trọng so với TraceDev: TraceDev chạy thẳng từ use case tới
code rồi đọc log/output. SDLC Harness phải dừng ở checkpoint, lưu state, sync
tool và trả status cho Niko. Phase 3 vì vậy là lớp handoff/checkpoint của
harness: Developer lập kế hoạch code, Validator kiểm kế hoạch có bám graph không,
human duyệt, rồi Phase 4 mới được đụng vào code.

Implementation plan không phải graph mới và không tự sửa graph chính. Nó chỉ ánh
xạ từ approved requirement/design/trace graph sang kế hoạch triển khai dự kiến:
target repo, planned branch, code area, design-to-code mapping, test intent,
assumptions và out-of-scope.

## Luồng Phase 3

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_design_approval
-> đọc approved requirement_packet_v1
-> đọc approved design_artifact_v{n}
-> đọc requirement-design trace và graph liên quan
-> state=implementation_planning
-> Developer.plan tạo implementation_plan_v1
-> Validator.review_plan kiểm coverage/traceability/executability/scope
-> sync implementation plan lên Confluence và comment trạng thái lên Jira
-> status=waiting_implementation_plan_review
```

Nếu Validator phát hiện plan bỏ sót design element, thiếu test intent, bịa scope
ngoài requirement/design, hoặc không đủ thông tin để triển khai an toàn, workflow
phải repair plan trong giới hạn cấu hình hoặc chuyển `blocked` với `last_error`
rõ. Không âm thầm sửa requirement/design graph trong implementation plan.

## Vai Trò Chính Trong Phase 3

### Developer

Developer chạy ở mode `plan`, không phải `act`. Đầu vào là approved requirement,
approved design, graph và trace của Phase 2. Đầu ra là `implementation_plan_v1.md`
có cấu trúc để người và Validator cùng đọc được.

Implementation plan cần có tối thiểu:

- target repo/branch dự kiến;
- các file/class/method dự kiến tạo hoặc sửa;
- bảng mapping từ design element sang code target;
- test intent hoặc test command dự kiến;
- assumptions/open questions;
- out-of-scope.

### Validator

Validator chạy ở mode `review_plan`. Nó không kiểm code ở Phase 3. Nó kiểm plan
có đủ thông tin để sang Phase 4 hay không:

- Coverage: design element quan trọng đã có planned code target chưa?
- Traceability: requirement/design trace có được phản ánh vào plan không?
- Executability: repo, branch, file path, test intent có đủ rõ không?
- Scope control: plan có bịa thêm ngoài requirement/design không?

## Tool Sync Phase 3

Phase 3 sync plan, không sync code:

- Confluence lưu implementation plan đầy đủ để review dài.
- Jira nhận comment ngắn: plan đã tạo/chờ duyệt, kèm link Confluence và `run_id`.
- GitHub target repo chỉ được kiểm tra/nhắc trong plan; chưa tạo branch, commit
  hoặc PR.

## Đầu Ra Bắt Buộc

Sau khi Phase 3 pass, một run phải có thêm:

```text
.runtime/runs/<run_id>/artifacts/implementation_plan_v1.md
.runtime/runs/<run_id>/artifacts/implementation_plan_review_v1.json
.runtime/runs/<run_id>/sync/tool_sync_report_v3.json
```

Và `state.json` phải có:

```text
current_phase=implementation_planning
status=waiting_implementation_plan_review
latest_artifacts.implementation_plan
quality_reports.implementation_plan_review
last_sync.phase=implementation_plan_review
pending_checkpoint.type=implementation_plan_review
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

Checkpoint này nghĩa là human cần duyệt kế hoạch implement trước khi Phase 4 được
phép sửa target repo.

## Quan Hệ Với Phase 4

```text
Jira issue SDLC-1
  -> requirement/design checkpoint trên Confluence
  -> implementation plan checkpoint trên Confluence
  -> Phase 4 mới tạo GitHub Draft PR
```

Phase 4 sẽ đọc `implementation_plan_v1` cùng approved design graph, rồi mới chạy
`Developer.act`, Tester và Validator code trace.

## Không Làm Trong Phase 3

- Không tạo GitHub Issue mới.
- Không tạo branch/commit/Draft PR.
- Không sinh hoặc sửa code.
- Không chạy Tester/code trace.
- Không merge PR tự động.
- Không xử lý request change động từ người dùng.
- Không invalidate requirement/design khi tool bên ngoài đổi; đó là Phase 6.
- Không mở đa ngôn ngữ. Java/eTour vẫn là target ban đầu để bám TraceDev.
- Không để Niko tự thao tác GitHub hoặc tự sửa code.

## Tài Liệu Trong Folder Này

- `state-and-artifacts.md`: state machine, artifact, checkpoint và sync report
  cho implementation planning.
- `developer-plan-validator-loop.md`: cách Developer.plan và Validator.review_plan
  phối hợp trước khi code.
- `phase-3-checklist.md`: checklist triển khai implementation planning checkpoint.

