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
  implementation_artifact_v1
  test_report_v1

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
```

## Tool Plane

| Tool plane | Vai trò |
| --- | --- |
| Jira | control plane cho issue, status, comment, approval |
| Confluence | knowledge plane cho requirement/design/trace docs |
| GitHub/GitLab | code plane cho branch, commit, PR/MR, CI |
| Niko/Ops | conversation, dashboard, trace, human checkpoint |

Ban đầu các tool này có thể được mô phỏng bằng fixture hoặc local Markdown để chứng minh flow trước.

## Source Ownership

Khi người dùng yêu cầu thực hiện một use case, Niko không trực tiếp kéo dữ liệu từ fixture/Jira/dataset. Niko gọi SDLC Harness bằng `start_or_get_run(source_ref)`. SDLC Harness quyết định source đã có active run chưa, có cần load/refine lại không, sync đã thành công chưa, rồi trả status cho Niko.
