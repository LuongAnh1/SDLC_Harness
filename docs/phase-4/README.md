# Phase 4 - Implementation Plan Đến Code/Test Và Draft PR

Phase 4 bắt đầu sau khi human/Niko duyệt checkpoint
`implementation_plan_review` của Phase 3. Mục tiêu của phase này là triển khai
code thật trong target repo, chạy test/validation, kiểm trace requirement ->
design -> code -> test, rồi mở Draft PR để human review.

Phase 4 mới là nơi GitHub trở thành tool sync chính. Phase 3 chỉ chuẩn bị plan và
kiểm target repo; Phase 4 mới tạo branch, commit và Draft PR.

## Slice 4.1 Đã Chốt

Slice đầu của Phase 4 hiện chỉ làm bước chuyển tiếp an toàn:

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_implementation_plan_review
-> đọc implementation_plan_structured_v{n}
-> kiểm target repo vẫn sạch và có CodeGraph index
-> ghi developer_work_packet_v1.json
-> status=implementation_ready
```

Slice này chưa tạo branch, chưa sửa code, chưa commit và chưa mở PR. Mục đích
là biến implementation plan đã được duyệt thành input máy đọc được cho
Developer.act ở slice tiếp theo, thay vì để Developer parse Markdown hoặc
Confluence.

## Luồng Phase 4

```text
approve_checkpoint(run_id)
-> kiểm tra run đang ở waiting_implementation_plan_review
-> chuẩn bị developer_work_packet_v1.json
-> đọc approved implementation_plan_v1
-> đọc approved design graph và requirement-design trace
-> tạo branch trong target repo
-> Developer.act tạo/sửa code dựa trên plan
-> ghi code_change_report_v1
-> Tester chạy compile/test hoặc ghi skipped rõ ràng
-> ghi test_report_v1
-> Validator kiểm requirement -> design -> code -> test trace
-> ghi requirement_design_code_trace_v1.json
-> tạo Draft PR trên GitHub
-> sync PR/test/trace summary về Jira và Confluence
-> status=code_review_ready
```

Nếu code không cover design, test fail không có repair path, hoặc trace code-test
thiếu nghiêm trọng, workflow phải `blocked` với `last_error` rõ.

## Developer.act

Developer.act không được sinh code từ design trần. Đầu vào bắt buộc là:

- approved `implementation_plan_v{n}`;
- approved `design_artifact_v{n}`;
- class/sequence graph đã duyệt;
- requirement-design trace;
- target repo status đã được guard.

Code change report cần nói rõ branch, commit, file đổi, lý do đổi và phần
requirement/design nào được cover.

## Tester Và Validator

Tester tạo `test_report_v1.md`. Report phải phân biệt rõ `passed`, `failed` và
`skipped`. Nếu máy chưa có JDK hoặc target repo chưa có test harness đủ mạnh,
report vẫn phải ghi skipped với lý do, không được trình bày như pass.

Validator tạo `requirement_design_code_trace_v1.json`, tối thiểu nối:

```text
requirement id
  -> design class/method/message
  -> source file/class/function
  -> test file/case nếu có
```

Nếu trace fail do code chưa cover, Developer có thể repair trong giới hạn cấu
hình. Nếu fail do requirement/design mơ hồ, workflow phải quay về checkpoint phù
hợp ở Phase 5, không tự sửa design trong Phase 4.

## Đầu Ra Bắt Buộc

```text
.runtime/runs/<run_id>/artifacts/developer_work_packet_v1.json
.runtime/runs/<run_id>/artifacts/code_change_report_v1.md
.runtime/runs/<run_id>/artifacts/test_report_v1.md
.runtime/runs/<run_id>/artifacts/requirement_design_code_trace_v1.json
.runtime/runs/<run_id>/sync/tool_sync_report_v4.json
```

Và `state.json` phải có:

```text
current_phase=code_review
status=code_review_ready
latest_artifacts.code_change_report
latest_artifacts.test_report
latest_artifacts.requirement_design_code_trace
quality_reports.implementation_traceability
last_sync.phase=code_review
pending_checkpoint.type=code_review
allowed_next_actions=[approve_checkpoint, request_change, cancel_run, get_status]
summary_for_user
```

Checkpoint `code_review` nghĩa là human cần review PR/code/test report. Nó không
có nghĩa PR đã được merge.

## Không Làm Trong Phase 4

- Không tạo GitHub Issue mới.
- Không merge PR tự động.
- Không xử lý request change động; đó là Phase 5.
- Không invalidate context ngoài tool; đó là Phase 6.
- Không mở đa ngôn ngữ. Java/eTour vẫn là target ban đầu để bám TraceDev.
- Không để Niko tự thao tác GitHub hoặc tự sửa code.

## Tài Liệu Trong Folder Này

- `phase-4-checklist.md`: checklist triển khai code/test/Draft PR.
- `state-and-artifacts.md`: state machine và artifact Phase 4.
- `github-code-collaboration.md`: policy dùng GitHub branch/commit/Draft PR.

