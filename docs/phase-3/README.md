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
-> đọc frozen Requirement V1 hoặc canonical Requirement V2 đã duyệt
-> đọc approved design_artifact_v{n}
-> đọc requirement-design trace và graph liên quan
-> với V2: đối chiếu revision, stable IDs và nội dung graph trước khi tạo worktree
-> state=implementation_planning
-> Developer.plan tạo implementation_plan_v1
-> Python tạo requirement_coverage từ canonical ID -> design trace -> code/test target
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

### Validator Hybrid V1

Validator Phase 3 hiện chạy theo thứ tự:

```text
ImplementationPlanValidator.review_rules
  -> hard gate coverage/traceability/executability/scope bằng graph
ImplementationPlanValidator.review_llm
  -> đọc requirement packet + design artifact + trace graph + structured plan
  -> đánh giá semantic gap, chất lượng kế hoạch triển khai và test intent
DeveloperPlanAgent.repair_plan
  -> nếu review fail và còn lượt repair, tạo version plan kế tiếp
```

Rule review là cửa chặn cứng. Nếu plan thiếu mapping cơ bản hoặc tham chiếu design
element ngoài graph đã duyệt thì LLM review bị skip và workflow đi vào repair/block.
LLM review chỉ chạy khi rule đã pass, để kiểm phần rule khó thấy: plan có giải
thích đủ cách biến design thành code không, có bỏ sót hành vi quan trọng không,
test intent có đủ rõ để Phase 4 chạy không.

Số vòng repair đọc từ `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`, mặc định `2`.
Mỗi vòng ghi artifact version riêng:

```text
implementation_plan_v1.md
implementation_plan_review_v1.json
implementation_plan_v2.md
implementation_plan_review_v2.json
...
```

Chỉ version cuối đã pass mới được sync lên Jira/Confluence và mở checkpoint
`waiting_implementation_plan_review`.

## Tool Sync Phase 3

Phase 3 sync plan, không sync code:

- Confluence lưu implementation plan đầy đủ để review dài.
- Jira nhận comment ngắn: plan đã tạo/chờ duyệt, kèm link Confluence và `run_id`.
- GitHub target repo được fetch để tạo branch/worktree riêng cho run trước khi
  lập plan; Phase 3 chưa sửa source/test, chưa commit/push và chưa tạo PR.
- Confluence page render các phần review chính từ
  `implementation_plan_structured_v1.json`: decision needed, execution steps,
  code targets, test plan, validator summary và assumptions/open questions.
  Markdown gốc chỉ nằm ở phụ lục audit để tránh bảng Markdown bị hiển thị như
  text thường.
- Phase 3 dùng target riêng của `tool_sync_report_v3.json`; không reuse Jira
  comment/Confluence page của requirement hoặc design checkpoint. Re-sync cùng
  Phase 3 mới update target v3 cũ.
- Nếu Jira comment v3 từng được tạo nhưng đã bị xóa thủ công, re-sync sẽ tạo
  comment mới và cập nhật `external_id` trong report v3.

## Đầu Ra Bắt Buộc

Sau khi Phase 3 pass, một run phải có thêm:

```text
.runtime/runs/<run_id>/artifacts/implementation_plan_v1.md
.runtime/runs/<run_id>/artifacts/implementation_plan_structured_v1.json
.runtime/runs/<run_id>/artifacts/implementation_plan_review_v1.json
.runtime/runs/<run_id>/sync/tool_sync_report_v3.json
```

Nếu Validator yêu cầu repair, hậu tố có thể là `v2`, `v3`, ... và
`latest_artifacts` luôn trỏ tới version mới nhất. Các version cũ được giữ lại để
audit quá trình Developer sửa plan theo feedback của Validator.

Trong slice hiện tại, cả plan artifacts, review report và `tool_sync_report_v3.json`
đã được sinh. Sync thật/mock đều thuộc SDLC_Harness; Niko chỉ đọc trạng thái và
link từ JSON response.

Và `state.json` phải có:

```text
current_phase=implementation_planning
status=waiting_implementation_plan_review
latest_artifacts.implementation_plan
latest_artifacts.implementation_plan_structured
quality_reports.implementation_plan_review
last_sync.phase=implementation_plan_review
pending_checkpoint.type=implementation_plan_review
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

Checkpoint này nghĩa là human cần duyệt kế hoạch implement trước khi Phase 4 được
phép sửa target repo.

## Requirement Contract V2 Trong Phase 3

Phase 3 dùng `approved_artifacts` để đọc lại requirement đã được đóng băng tại
checkpoint Phase 1. Với V2, canonical model là source of truth; packet/formal chỉ là
projection đã được kiểm tra. Trước khi tạo worktree hoặc đổi state, workflow đối chiếu:

- contract version, canonical revision và model artifact ref;
- requirement graph đã lưu với graph dựng lại từ canonical model;
- tập stable ID trong class trace và sequence trace;
- trace metadata với đúng canonical revision.

Sai lệch dừng bằng `requirement_v2_phase3_input_invalid` và không mutate state.
Structured plan vẫn giữ marker `implementation_plan_structured_v1` để tương thích,
nhưng có thêm `requirement_contract` và `requirement_coverage`. Coverage được Python
dựng lại sau mỗi vòng Developer repair, không lấy trực tiếp từ nội dung LLM.

Validator V2 hard-gate requirement ID thiếu, trùng, tự bịa, design target ngoài
approved trace, hoặc requirement chưa có code target/test intent. Markdown và
Confluence cùng render bảng coverage để human review.

## Quan Hệ Với Phase 4

```text
Jira issue SDLC-1
  -> requirement/design checkpoint trên Confluence
  -> implementation plan checkpoint trên Confluence
  -> Phase 3 tạo run workspace và implementation plan
  -> Phase 4 dùng workspace đó để tạo GitHub Draft PR
```

Phase 4 sẽ đọc `implementation_plan_v1` cùng approved design graph, rồi mới chạy
`Developer.act`, Tester và Validator code trace.

Requirement V2 hiện được phép đi tiếp từ checkpoint này sang Phase 4. Canonical
work packet, Developer, Tester và code trace cùng consume stable requirement IDs.
Default contract vẫn là V1 cho tới controlled live smoke V2.

## Không Làm Trong Phase 3

- Không tạo GitHub Issue mới.
- Không sửa source/test, commit, push hoặc tạo Draft PR; branch/worktree riêng
  đã được tạo để khóa đúng code lineage cho run.
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

## Smart Developer V1: CodeGraph Preflight Và Data Boundary

Smart Developer V1 dùng CodeGraph làm nguồn hiểu target repo chính. Khi human
duyệt design checkpoint, Phase 3 tạo run worktree từ remote base đã chốt rồi tự
chạy bước chuẩn bị CodeGraph trong worktree đó. Nếu CodeGraph init/collect thất
bại, workflow block với lỗi preflight tương ứng; canonical repo không bị ghi
metadata hoặc checkout branch khác.

Lệnh `prepare-target-repo --init-codegraph` vẫn hữu ích để chẩn đoán thủ công,
nhưng không còn là prerequisite cho run mới. Dirty guard bỏ qua metadata
`.codegraph/` trong run worktree nhưng vẫn chặn thay đổi source/test/docs ngoài
workflow. Phase 3 chỉ đọc code, sinh/sync plan; Phase 4 mới sửa code và mở PR.

Lưu ý bảo mật V1: để Developer LLM lập plan thông minh, SDLC Harness tạm thời gửi
CodeGraph evidence/source snippets của target repo vào prompt DeepSeek. Đây là
thỏa thuận tạm cho demo hiện tại. Các phase sau cần thêm cơ chế lọc/redact
context, cấu hình bật/tắt gửi source, hoặc chuyển Developer planning sang local
model nếu repo chứa dữ liệu nhạy cảm.

