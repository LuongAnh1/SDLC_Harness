# Target Architecture

## Kiến Trúc Tổng Quát

```text
HTTP API / CLI
  -> Application Service
  -> Phase Workflow
      -> Role Loop Executor
          -> Role Capability Policy
          -> Role Tool Registry
          -> DeepSeek LLM Client
          -> Session Manifest + Journal
      -> Deterministic Gate / Validator
      -> Artifact Store + Workflow State
      -> Human Checkpoint / Tool Sync
```

Role Loop Executor chỉ sở hữu cơ chế lặp: transcript, action/result, budget,
pause/resume và completion proposal. Phase workflow vẫn sở hữu thứ tự nghiệp vụ,
artifact version, checkpoint và route sang role khác.

## Ranh Giới Sở Hữu

### Harness

- resolve immutable project/language profile;
- chọn role và capability policy;
- thực thi tool sau khi validate input;
- giới hạn path, command, thời gian, call/token budget;
- ghi session state, journal, artifact và event;
- quyết định pass, repair, pause, blocked hoặc human checkpoint;
- không tin `complete` của model nếu completion evidence chưa đủ.

### Role Agent

- đọc task/evidence đã cấp;
- chọn tool tiếp theo trong allowlist;
- diễn giải tool result;
- đề xuất delta/mutation trong đúng scope;
- khai báo complete, escalate hoặc clarification kèm evidence.

### Deterministic Gate Và Validator

- kiểm schema, policy, graph, AST, signature và machine-readable execution;
- không sửa artifact đang đánh giá;
- không chuyển warning thành blocker nếu policy không cho phép;
- không cho semantic judgment phủ quyết hard evidence.

## Canonical Loop

```text
load immutable inputs
-> build role session manifest
-> model observes task + bounded context
-> model requests one typed tool action
-> Harness authorizes and executes
-> append normalized tool result + evidence reference
-> repeat until:
     completion evidence passes
     clarification/human decision is required
     role boundary requires handoff
     budget is exhausted
     retriable transport/environment failure pauses session
     non-retriable policy failure blocks session
```

Không cho model gửi raw shell command. Verification được gọi bằng stage ID từ
profile registry. Source/repo reads phải trả bounded content cùng provenance.

## Shared Internal Contracts

Các tên dưới đây là contract định hướng; schema chi tiết chỉ được phát hành ở
lát L1 và phải versioned:

- `role_loop_session_v1`: identity, role, run/profile snapshot digest, input
  artifact digests, budgets, status và resume cursor.
- `role_capability_policy_v1`: role, allowed tool IDs, read/write roots,
  verification stages, limits và completion requirements.
- `role_tool_call_v1`: call ID, tool ID, typed arguments và observation cursor.
- `role_tool_result_v1`: call ID, status, bounded output, evidence refs, duration
  và retryability.
- `role_completion_v1`: outcome `complete|handoff|clarification|blocked`, covered
  units, evidence refs, unresolved items và requested next owner.
- `role_failure_classification_v1`: owner `role|product|upstream|environment|human`,
  repairability, severity và routing recommendation.

Các contract này là nội bộ. HTTP API hiện hành không đổi trong Gate 0/L1. Nếu cần
expose status sau này, chỉ thêm field sanitized vào response hiện có.

## Security Và Determinism

- Tool registry được tạo từ code-reviewed policy, không từ prompt hay model output.
- Path được normalize và kiểm tra lại tại thời điểm thực thi.
- Write tool dùng optimistic hash/precondition; conflict không tự overwrite.
- Tool result không mang secret, absolute local path hoặc raw unbounded log.
- Retry transport dùng cùng call/session identity; không phát hành mutation mới
  khi kết quả trước còn mơ hồ.
- Mọi mutation có before/after hash và journal record.
- Validator registry không đăng ký write tool.

## Failure Routing Chuẩn

| Failure owner | Ví dụ | Route |
| --- | --- | --- |
| Role output | JSON/delta/test do role sinh sai | Cùng role sửa trong budget |
| Product | Acceptance đúng nhưng implementation sai | Developer repair |
| Upstream artifact | Requirement/design đã duyệt mâu thuẫn | Dừng và reopen checkpoint |
| Environment | Docker/service/network/LLM transport lỗi | Pause/resume, không tăng repair count |
| Human decision | Thiếu business rule hoặc scope choice | Clarification/checkpoint |
| Policy/security | Tool/path/contract không được phép | Fail closed và block |

## Quan Hệ Với Project Profile

Role capability là language-neutral; project profile cung cấp dữ liệu cụ thể:

- component/source/test roots;
- tool adapter/extractor phù hợp;
- approved verification stage IDs;
- QA test ownership policy;
- topology và runtime-service capabilities.

Vì vậy TypeScript có thể dùng CodeGraph, TypeScript AST, Vitest và Playwright,
trong khi một profile tương lai thay adapter mà không thay workflow semantics.
