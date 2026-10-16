# Phase 1 Artifacts

## Canonical Requirement

`requirement_model_v2_r<n>.json` là nguồn sự thật duy nhất. Model giữ stable IDs,
source evidence, assumptions, open questions, human decisions và validation status.

## Source Artifacts

- `use_case_packet_v1.md`: bản raw use case dễ đọc.
- `source_snapshot_v1.json`: source text, source ref và fingerprint để validator
  kiểm tra grounding và phát hiện tampering.

Hai tên này là version schema của source artifact, không phải Requirement Contract V1.

## Deterministic Projections

- `requirement_packet_v2_r<n>.md`: bản review cho human/Jira/Confluence.
- `formal_use_case_v2_r<n>.txt`: projection máy đọc cho downstream context.
- `requirement_decisions_v2_r<n>.json`: audit các câu trả lời clarification.
- `requirement_validation_v2_r<n>.json`: report schema, grounding, references và
  blocking questions.

Projection được render từ canonical model, không gọi LLM riêng và không được ghi
ngược semantics vào JSON.

## Sync Artifact

`tool_sync_report_v1.json` ghi mode, trạng thái và remote target của lần sync. Đây
là sync-report schema V1, độc lập với Requirement Contract.

## Ownership

SDLC_Harness tạo, validate, version và sync các artifact. Niko chỉ đọc JSON status,
hiển thị checkpoint/câu hỏi và chuyển control action của người dùng.
