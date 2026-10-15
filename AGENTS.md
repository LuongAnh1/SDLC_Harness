# SDLC Harness Working Context

Khi làm việc trong repo này, nói tiếng Việt với chủ dự án, tự xưng là "em" và gọi người dùng là "anh".

## Mục Tiêu Repo

Repo này là nơi phát triển lớp **SDLC Harness** cho hướng đồ án Agent Harness hỗ trợ chuyển giao ngữ cảnh trong quy trình phát triển phần mềm.

Không nhầm repo này với `Niko_Agent`:

- `Niko_Agent` là conversational control harness: nhận tin nhắn, hiểu intent, đọc trạng thái, gọi workflow, hỏi người dùng và trả lời.
- `SDLC_Harness` là workflow execution harness: giữ workflow state, artifact version, checkpoint, tool sync, traceability graph và chạy graph requirement-to-code.

## Ranh Giới Kiến Trúc

Luồng mong muốn:

```text
User
  <-> Niko_Agent
      <-> SDLC_Harness
          <-> Jira / Confluence / GitHub-GitLab / fixture adapters
```

Niko có thể gọi các thao tác như:

```text
start_or_get_run(use_case_id | jira_issue_key)
get_status(run_id)
approve_checkpoint(run_id)
request_change(run_id, message)
resume_run(run_id)
cancel_run(run_id)
```

SDLC Harness là nguồn sự thật cho:

- `workflow_state`: run id, phase hiện tại, checkpoint, allowed next actions.
- `artifact_store`: requirement packet, design artifact, trace graph, code/test report.
- `tool_sync_records`: đã đẩy artifact nào lên tool nào, trạng thái sync ra sao.

## TraceDev Reference Principle

Dùng `D:\Dự án cá nhân\Do_an_II\ISSTA2026-TraceDev` như nguồn tham khảo cho execution graph tầng dưới, không xem TraceDev là toàn bộ harness.

TraceDev tham chiếu:

```text
structured use case
  -> Requirement Refiner
  -> Designer
  -> Validator
  -> Developer
  -> Tester
  -> artifacts
```

Điểm cần học:

- role agents theo phase;
- Validator/traceability checks luôn có mặt;
- validate/repair loop;
- dataset eTour/SMOS/iTrust cho requirement-to-code.
- SDLC Harness thêm implementation plan checkpoint giữa design và code:
  `Developer.plan -> Validator.review_plan hybrid -> human approve -> Developer.act`.
  TraceDev gốc không có bước này vì chạy một mạch từ final design sang Developer.
  Phase 3 review dùng rule hard gate trước, LLM content review sau, và repair
  theo `SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT`.

Điểm không copy nguyên xi:

- chạy batch một lượt rồi đọc log thủ công;
- thiếu checkpoint/human approval;
- thiếu tool sync theo phase;
- thiếu workflow state dễ truy vấn;
- thiếu cơ chế context động khi Jira/docs/chat/repo thay đổi.

## Phương Thức Triển Khai

Triển khai theo vertical slice. Mỗi slice chỉ được xem là xong khi có đủ:

```text
state + artifact + checkpoint + sync/mock sync + status cho Niko đọc
```

Không xây full graph âm thầm rồi mới nối Niko/tool. Sau mỗi đoạn chạy được, cập nhật docs/checklist.

## Docs Policy

Tài liệu là nguồn định hướng chính của repo giai đoạn đầu. Khi thay đổi hướng kiến trúc, cập nhật docs trước hoặc cùng lúc với code.

Các file quan trọng:

- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/incremental-delivery-flow.md`
- `docs/tracedev-reference.md`
- `docs/datasets-and-context.md`
- `docs/implementation-checklist.md`
- `docs/phase-1/README.md`
- `docs/phase-2/README.md`
- `docs/phase-3/README.md`
- `docs/phase-4/README.md`


## Package Layout

- `interfaces/`: public CLI/entrypoint. Giữ `sdlc_harness/cli.py` làm wrapper để không phá lệnh Niko gọi.
- `workflows/`: graph/workflow theo phase. Workflow không tự chứa prompt dài hoặc adapter chi tiết.
- `agents/requirements/`: Requirement Refiner/Validator và prompt templates. Phase 1 đã sinh cả `formal_use_case_v1.txt` theo grammar TraceDev và `requirement_packet_v1.md` cho checkpoint người duyệt.
- `sources/`: fixture/dataset/Jira loaders. Niko không đọc source trực tiếp.
- `storage/`: `.runtime` state store và active-run index.
- `artifacts/`: artifact writers.
- `workspaces/`: workspace identity, branch/path policy và lock theo `run_id`.
- `sync/`: mock/real tool sync adapters.
- `core/`, `llm/`: config/errors chung và LLM provider client.

Khi thêm code mới, đặt theo ownership ở trên. Tránh quay lại kiểu tất cả module nằm phẳng dưới `sdlc_harness/`.
## Coding Policy Cho Giai Đoạn Sau

Khi bắt đầu thêm code:

- Ưu tiên Python package đơn giản trước, chưa cần microservice.
- Giai đoạn đầu ưu tiên CLI trả JSON để Niko gọi được bằng subprocess; local package/HTTP API để sau khi contract ổn định.
- Lưu state/artifact bằng SQLite/JSON/Markdown trước khi nghĩ tới lakehouse/KG.
- Tool adapters ban đầu có thể là fixture/mock adapter, nhưng trước Phase 2 cần có ít nhất một sync API thật hoặc adapter thật tối thiểu.
- Viết test cho state transition, artifact version, checkpoint và adapter sync.

## Sync Ownership

Sync artifact lên Jira/Confluence/GitHub thuộc SDLC Harness. Niko chỉ gọi action, đọc `get_status`, nhận `tool_sync_report` và phản hồi người dùng. Không để Niko tự sync artifact SDLC để tránh chia đôi workflow state.

## Run Workspace Ownership

Trước Code Review approval và Phase 5, triển khai contract trong
`docs/phase-4/workspace-isolation/`: mỗi run sở hữu một Git worktree và branch có
run suffix. Canonical target repo chỉ dùng để fetch/quản lý worktree; Developer,
Runner, Validator và QA phải resolve workspace theo `run_id`, không dùng current
branch của canonical repo làm nguồn sự thật.

Slice 4.9A1/A2 đã có schema `run_workspace_v1`, naming có run suffix, config
`SDLC_TARGET_WORKTREE_ROOT`, JSON atomic, OS advisory lock và Git worktree
creation engine. Slice 4.9A3 đã nối engine vào Phase 3/4: design approval tạo
workspace, tự chuẩn bị CodeGraph trong worktree; Developer, Runner, Validator,
QA, revalidate và publisher đều resolve cùng workspace dưới per-run lock.
Canonical repo chỉ dùng để fetch/quản lý worktree, không được checkout để chạy
workflow. Slice 4.9B đã thêm `adopt-run-workspace` và nhận nuôi run legacy/PR #1;
canonical checkout đã về `main`, còn branch cũ nằm trong worktree riêng với HEAD
và Draft PR không đổi. `get-status` chỉ trả workspace identity gọn và không lộ
local path.

Cleanup chỉ qua lệnh explicit sau human merge hoặc cancel, không xóa remote
branch. Harness được phép chuyển
Draft PR thành Ready for review nhưng không bao giờ merge hoặc bật auto-merge.


## Source Ownership

Niko không trực tiếp kéo use case/Jira/dataset cho workflow SDLC. Niko gọi SDLC Harness bằng `start_or_get_run` hoặc `get_status`. SDLC Harness sở hữu việc load source, gọi Requirement Refiner, validate, sync tool và trả status/sync result.


<!-- SDLC_NIKO_SYNC_START -->
## Niko Integration Boundary

Niko_Agent là conversational/control harness. SDLC_Harness là nơi sở hữu pipeline
SDLC: source intake từ fixture/Jira, Requirement Refiner, Designer, Validator,
Developer, QA/Tester, runtime state, artifact và tool sync. Phase 3 chỉ sinh/kiểm
`implementation_plan_v1` và dừng ở `waiting_implementation_plan_review`. Phase 4
hiện đã triển khai code/test, Validator code trace, QA repair loop và GitHub
publication, rồi sync bằng chứng PR/test/trace/QA sang Jira và Confluence để mở
checkpoint `code_review_ready`. Việc approve/request-change checkpoint Code
Review thuộc lát sau và chỉ mở sau Slice 4.9 workspace isolation. Approve chỉ
được chuyển Draft PR thành Ready for review; merge luôn do human. Khi Niko gọi repo này, contract
phải là CLI JSON ổn định; không in log tự do ra stdout.

Các lệnh CLI có side effect mặc định dùng `--sync-mode mock`. Caller phải truyền
`--sync-mode real` rõ ràng mới được ghi lên Jira/Confluence. Remote identity của
checkpoint dùng `source_ref + phase`; khi mất report local, adapter phải tìm lại
marker từ remote, fail closed nếu kết quả nhập nhằng và không tự xóa artifact cũ.

Không chuyển logic đọc Jira, ghi artifact, sync Confluence/Jira hoặc validate
requirement packet sang Niko. Nếu cần trả thêm dữ liệu cho Telegram, mở rộng
payload JSON của SDLC_Harness trước rồi mới sửa client bên Niko.
<!-- SDLC_NIKO_SYNC_END -->

