# Phase 3 - Design Đến Code Và Draft PR

Phase 3 bắt đầu sau khi người dùng duyệt checkpoint design của Phase 2.
Mục tiêu của phase này là biến `design_artifact_v{n}` đã duyệt thành một thay
đổi code có thể review trong target repo, có test/validation report, có trace
requirement-design-code-test và có Draft PR trên GitHub để chứng minh context đã
được chuyển sang code collaboration lane.

Điểm khác biệt quan trọng so với TraceDev: TraceDev chạy thẳng từ use case tới
code rồi đọc log/output. SDLC Harness phải dừng ở checkpoint, lưu state, sync
tool và trả status cho Niko. Phase 3 vì vậy không chỉ là "LLM sinh code", mà là
đưa context đã duyệt sang repo code thật dưới dạng branch, commit và Draft PR để
human review.

Điểm cần chốt: khi Phase 3 hoàn thành, luồng execution từ requirement tới code
phải tương đương tinh thần TraceDev trong phạm vi một use case. Phase 1 đã đóng
vai Requirement Refiner, Phase 2 đã đóng vai Designer + Validator, và Phase 3
phải bổ sung Developer + Tester + Validator để chạy end-to-end. Phase 4 trở đi
mới là phần cải tiến riêng của harness như request change, context động và impact
analysis.

## Luồng Phase 3

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_design_approval
-> đọc approved requirement_packet_v1
-> đọc approved design_artifact_v{n}
-> đọc requirement-design trace và graph liên quan
-> state=implementation_planning
-> Developer tạo implementation_plan_v1
-> state=implementation_writing
-> Developer tạo/sửa code trong target test repo trên branch riêng
-> ghi code_change_report_v1
-> state=implementation_testing
-> Tester/Validator chạy test/compile/trace check theo khả năng hiện tại
-> ghi test_report_v1 và requirement_design_code_trace_v1
-> sync GitHub Draft PR + Jira/Confluence summary
-> state=code_review_ready
```

Nếu Phase 3 không thể tạo code hoặc không thể chứng minh trace từ requirement
tới code/test, workflow phải chuyển `blocked` với `last_error` rõ. Không tự mở
Phase 4 change request trong Phase 3.

## Ba Vai Trò Chính Trong Phase 3

### Developer

Developer nhận requirement/design đã duyệt và tạo implementation plan trước khi
đụng vào repo code. Sau đó Developer tạo hoặc sửa code trong target repo, ghi rõ
file thay đổi và lý do từng thay đổi. Phạm vi có thể nhỏ để kiểm soát, nhưng vẫn
phải tạo code change thật có thể review bằng branch/commit/Draft PR.

### Tester

Tester chạy compile/test hoặc validation tương ứng với target repo. Nếu chưa có
test thật, report phải ghi rõ `test skipped` kèm lý do; không được trình bày như
đã pass. Khi có thể, Tester nên tạo hoặc cập nhật test liên quan tới use case.

### Validator

Validator giữ vai trò traceability. Nó kiểm tra requirement-design-code-test có
liên kết được với nhau không, tạo missing-link report khi code/test chưa phủ đủ
yêu cầu, và quyết định workflow có thể mở checkpoint review hay phải `blocked`.

## GitHub Không Thay Jira Issue

Trong Phase 3, Jira vẫn là project & issue tracking source. Không tạo GitHub
Issue mới để tránh trùng nguồn công việc. GitHub được dùng cho:

- branch theo issue/run;
- commit code;
- Draft PR;
- code review;
- test/CI signal nếu target repo có hỗ trợ.

Draft PR phải link ngược về Jira issue, Confluence requirement/design checkpoint
và `run_id` để reviewer có đủ context.

## Đầu Ra Bắt Buộc

Sau khi Phase 3 pass, một run phải có thêm:

```text
.runtime/runs/<run_id>/artifacts/implementation_plan_v1.md
.runtime/runs/<run_id>/artifacts/code_change_report_v1.md
.runtime/runs/<run_id>/artifacts/test_report_v1.md
.runtime/runs/<run_id>/artifacts/requirement_design_code_trace_v1.json
.runtime/runs/<run_id>/sync/tool_sync_report_v3.json
```

Và `state.json` phải có:

```text
current_phase=code_review
status=code_review_ready
latest_artifacts.implementation_plan
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
quality_reports.implementation_traceability
last_sync.phase=code_review
pending_checkpoint.type=code_review
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

## Sync Phase 3

SDLC Harness sở hữu việc sync Phase 3. Niko chỉ gọi action và đọc JSON status.
GitHub sync tạo hoặc cập nhật branch/Draft PR do harness sở hữu; Jira/Confluence
nhận summary và link PR để người dùng nhìn được dòng chảy context:

```text
Jira issue SDLC-1
  -> requirement/design checkpoint trên Confluence
  -> GitHub Draft PR
  -> test/trace report
```

## Không Làm Trong Phase 3

- Không tạo GitHub Issue mới.
- Không merge PR tự động.
- Không xử lý request change động từ người dùng.
- Không invalidate requirement/design khi tool bên ngoài đổi; đó là Phase 4/5.
- Không mở đa ngôn ngữ. Java/eTour vẫn là target ban đầu để bám TraceDev.
- Không để Niko tự thao tác GitHub hoặc tự sửa code.
- Không coi mock/local git report là hoàn thành Phase 3; nó chỉ dùng để mở đường
  trước khi có Draft PR thật.

## Tài Liệu Trong Folder Này

- `state-and-artifacts.md`: state machine, artifact, checkpoint và sync report
  dự kiến cho Phase 3.
- `github-code-collaboration.md`: policy dùng GitHub branch/commit/Draft PR và
  ranh giới với Jira issue.
- `developer-tester-validator-loop.md`: cách ánh xạ Developer/Tester/Validator
  từ TraceDev sang workflow có checkpoint.
- `phase-3-checklist.md`: checklist triển khai code/test/live smoke cho Phase 3.

