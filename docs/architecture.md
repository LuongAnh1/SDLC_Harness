# Kiến Trúc SDLC Harness

## Vai Trò

SDLC Harness là lớp workflow execution đứng dưới Niko. Nó không thay thế Niko, mà cung cấp một backend có trạng thái để Niko gọi khi người dùng yêu cầu thực hiện một use case/task trong quy trình phát triển phần mềm.

```text
User
  <-> Niko_Agent
      <-> SDLC_Harness
          <-> Jira / Confluence / GitHub-GitLab / fixture adapters
```

## Ranh Giới Với Niko

Niko giữ vòng lặp hội thoại:

- nhận tin nhắn;
- hiểu intent;
- đọc status từ SDLC Harness;
- hỏi lại người dùng;
- gọi action phù hợp;
- trả lời bằng ngôn ngữ tự nhiên.

SDLC Harness giữ vòng lặp công việc:

- chạy phase;
- tạo artifact;
- chạy Validator;
- sync tool;
- tạo checkpoint;
- lưu trạng thái và trace.

Niko có thể nói "đang ở đâu rồi" vì nó đọc trạng thái từ Harness, nhưng Niko không phải nguồn sự thật của workflow state.

## Thành Phần Chính

```text
Workflow State
  run_id
  current_phase
  pending_checkpoint
  allowed_next_actions
  last_error

Artifact Store
  use_case_packet_v1
  refined_requirement_v1
  design_artifact_v1
  trace_graph_v1
  implementation_plan_v{n}
  implementation_plan_review_v{n}
  developer_work_packet_v{n}
  code_change_report_v{n}
  test_report_v{n}
  requirement_design_code_trace_v{n}
  implementation_repair_feedback_v{n}
  qa_test_report_v1

Tool Sync Records
  jira_issue_key
  confluence_page_id
  git_branch
  pull_request_url
  sync_status

Execution Graph
  Requirement Refiner
  Designer
  Validator
  Developer
  Tester
```

## Hai Loop

```text
SDLC Harness loop
  phase -> artifact -> validate -> sync tool -> checkpoint/state

Niko conversation loop
  user message -> read state -> decide intent -> call harness -> reply
```

Ví dụ:

```text
User: chạy UC1
Niko -> start_or_get_run(UC1)
Harness -> requirement_packet_v1, waiting_requirement_approval
Niko -> báo anh duyệt

User: oke chạy tiếp
Niko -> approve_checkpoint(run_id)
Harness -> design_artifact_v1, waiting_design_approval
Niko -> báo trạng thái mới

User: duyệt design
Niko -> approve_checkpoint(run_id)
Harness -> implementation_plan_v{n}, waiting_implementation_plan_review
Niko -> hỏi anh duyệt kế hoạch implement

User: duyệt plan
Niko -> approve_checkpoint(run_id)
Harness -> developer_work_packet_v1, implementation_ready
Niko -> báo đã sẵn sàng bước code

User: chạy tiếp Phase 4
Niko -> continue_run(run_id)
Harness -> Developer.act -> Verification Runner -> Validator code trace
Harness -> nếu trace thiếu link code/test: Developer.repair -> revalidate trong giới hạn cấu hình
Harness -> implementation_traced khi compile/unit + trace pass
Harness -> QA sinh/chạy acceptance test
Harness -> nếu QA fail: Developer.repair -> Runner -> Validator -> QA trong giới hạn cấu hình
Harness -> qa_tested khi acceptance/system behavior pass
Niko -> báo kết quả code/test/trace/QA, chưa báo PR nếu GitHub sync chưa chạy
```

Trong Phase 3, `implementation_plan_v{n}` có thể tăng version nếu Validator hybrid
phát hiện semantic gap và `Developer.repair_plan` còn lượt sửa theo
`SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`. Chỉ version pass rule + LLM review mới
được sync lên Jira/Confluence.

Phase 4 hiện đã chạy từ implementation plan tới Code Review approval: tạo
worktree/branch theo run, sinh/apply code, chạy Verification Runner, Validator
code trace hybrid rule + LLM, QA acceptance, publish Draft PR và sync checkpoint
Jira/Confluence. Human approval chuyển đúng Draft PR thành Ready for review, ghi
`github_code_review_approval_v1.json`, sync trạng thái `code_review_approved` và
giữ worktree active. Harness không merge hoặc bật auto-merge; request-change vẫn
thuộc Phase 5.

## Tool Plane

| Tool plane | Vai trò |
| --- | --- |
| Jira | control plane cho issue, status, comment, approval |
| Confluence | knowledge plane cho requirement/design/trace docs |
| GitHub/GitLab | code plane cho branch, commit, PR/MR, CI; không thay Jira issue nguồn |
| Niko/Ops | conversation, dashboard, trace, human checkpoint |

Ban đầu các tool này có thể được mô phỏng bằng fixture hoặc local Markdown để chứng minh flow trước.

## Source Ownership

Khi người dùng yêu cầu thực hiện một use case, Niko không trực tiếp kéo dữ liệu từ fixture/Jira/dataset. Niko gọi SDLC Harness bằng `start_or_get_run(source_ref)`. SDLC Harness quyết định source đã có active run chưa, có cần load/refine lại không, sync đã thành công chưa, rồi trả status cho Niko.
