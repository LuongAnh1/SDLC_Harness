# Requirement V2 Migration Và Retirement V1

## Trạng Thái Hiện Tại

Từ ngày 2026-10-16, Requirement Contract V2 là contract runtime duy nhất của
SDLC Harness. Mọi run mới được ghi `requirement_contract_version: "v2"`.

Đã gỡ:

- biến cấu hình `SDLC_REQUIREMENT_CONTRACT_VERSION`;
- CLI option `--requirement-contract-version`;
- Requirement Refiner/Validator V1 và bốn prompt V1;
- writer cho formal check, requirement packet và trace graph V1;
- `LegacyRequirementInput` cùng downstream dual-read V1/V2.

## Runtime Contract

```text
source snapshot
  -> Requirement Refiner structured JSON
  -> requirement_model_v2_r<n>.json
  -> Python validation + clarification
  -> requirement_packet_v2_r<n>.md
  -> formal_use_case_v2_r<n>.txt
  -> approval -> Phase 2-4
```

`Settings.requirement_contract_version` và field cùng tên trong state vẫn tồn
tại với giá trị cố định `v2` để giữ JSON status ổn định cho Niko. Override cũ bị
bỏ qua. Reader downstream từ chối state ghi version khác V2 bằng
`requirement_contract_unsupported`.

## Run Lịch Sử

Artifact V1 cũ không bị xóa khỏi `.runtime`; chúng vẫn là bằng chứng audit. Harness
không tiếp tục một run V1 qua Phase 2-4. Nếu cần chạy lại use case cũ, operator
tạo run V2 mới từ source gốc.

Tên `use_case_packet_v1` và `tool_sync_report_v1` không thuộc Requirement
Contract V1. Chúng là version schema độc lập của raw source packet và sync report,
nên được giữ để tránh đổi contract ngoài phạm vi.

## Rollback

Không còn runtime switch quay về V1. Rollback code phải là một thay đổi Git có
chủ đích; không thể bật bằng `.env` hoặc CLI.
