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


## Package Layout

- `interfaces/`: public CLI/entrypoint. Giữ `sdlc_harness/cli.py` làm wrapper để không phá lệnh Niko gọi.
- `workflows/`: graph/workflow theo phase. Workflow không tự chứa prompt dài hoặc adapter chi tiết.
- `agents/requirements/`: Requirement Refiner/Validator và prompt templates. Phase 1 đã sinh cả `formal_use_case_v1.txt` theo grammar TraceDev và `requirement_packet_v1.md` cho checkpoint người duyệt.
- `sources/`: fixture/dataset/Jira loaders. Niko không đọc source trực tiếp.
- `storage/`: `.runtime` state store và active-run index.
- `artifacts/`: artifact writers.
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


## Source Ownership

Niko không trực tiếp kéo use case/Jira/dataset cho workflow SDLC. Niko gọi SDLC Harness bằng `start_or_get_run` hoặc `get_status`. SDLC Harness sở hữu việc load source, gọi Requirement Refiner, validate, sync tool và trả status/sync result.


<!-- SDLC_NIKO_SYNC_START -->
## Niko Integration Boundary

Niko_Agent là conversational/control harness. SDLC_Harness là nơi sở hữu pipeline
SDLC Phase 1: source intake từ fixture/Jira, Requirement Refiner, validator,
repair loop, runtime state, artifact và Jira/Confluence sync. Khi Niko gọi repo
này, contract phải là CLI JSON ổn định; không in log tự do ra stdout.

Không chuyển logic đọc Jira, ghi artifact, sync Confluence/Jira hoặc validate
requirement packet sang Niko. Nếu cần trả thêm dữ liệu cho Telegram, mở rộng
payload JSON của SDLC_Harness trước rồi mới sửa client bên Niko.
<!-- SDLC_NIKO_SYNC_END -->

