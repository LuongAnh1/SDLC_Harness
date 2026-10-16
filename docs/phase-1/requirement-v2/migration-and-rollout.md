# Requirement V2 Migration Và Rollout

## Nguyên Tắc Tương Thích

Requirement V2 chỉ áp dụng cho run mới. Không rewrite artifact, state hoặc trace
của run V1 đã tạo.

Mỗi run ghi rõ:

```json
{
  "requirement_contract_version": "v1"
}
```

hoặc:

```json
{
  "requirement_contract_version": "v2"
}
```

Run legacy thiếu field được hiểu là `v1`.

## Chọn Contract Khi Tạo Run

Cấu hình rollout:

```text
SDLC_REQUIREMENT_CONTRACT_VERSION=v1|v2
```

- Default trong giai đoạn triển khai là `v1`.
- Contract được snapshot vào state lúc tạo run và không đổi giữa chừng.
- `start-or-get-run` vẫn idempotent theo `source_ref`. Nếu source đã có active
  run V1, lệnh trả run V1 thay vì tạo song song một run V2.
- Controlled smoke V2 dùng source mới hoặc run cũ đã hoàn tất/cancelled đúng
  lifecycle; không xóa active-run index bằng tay.

## Downstream Dual Read

Phase 2-4 dùng một reader nội bộ chuẩn hóa input:

```text
run V1
  -> requirement_packet_v1.md + formal_use_case_v1.txt
  -> LegacyRequirementInput

run V2
  -> requirement_model_v2_r<n>.json
  -> CanonicalRequirementInput
  -> packet/formal chỉ dùng cho review/debug
```

Với run V2:

- `approved_artifacts.requirement_model` là artifact bắt buộc.
- Designer, requirement-design trace và các Validator đọc canonical JSON.
- Formal/Markdown projection không được ghi đè field trong JSON.
- Trace graph dùng stable requirement IDs từ model V2.

Với run V1, hành vi hiện tại được giữ nguyên để run đang chạy có thể hoàn tất.

## Artifact Và State Compatibility

Các key JSON công khai giữ ổn định:

```text
latest_artifacts.requirement_packet
latest_artifacts.formal_use_case
pending_checkpoint
allowed_next_actions
summary_for_user
```

V2 bổ sung:

```text
requirement_contract_version
latest_artifacts.requirement_model
latest_artifacts.requirement_decisions
pending_checkpoint.current_question
pending_checkpoint.remaining_blocking_count
```

Caller V1 có thể bỏ qua field mới. Chỉ caller muốn điều khiển clarification mới
cần hiểu action `answer_clarification`.

## Rollout Theo Lát

1. **Schema/renderers:** tạo model types, validators và deterministic projections;
   chưa nối workflow, default vẫn V1.
2. **Phase 1 V2:** nối Refiner structured output, clarification state/action,
   artifact revision và mock sync.
3. **Downstream dual-read:** Phase 2 Designer/Validator nhận canonical V2; Phase
   3/4 giữ stable IDs trong trace/work packets.
4. **Niko integration:** Niko hiển thị current question và gọi action answer;
   SDLC Harness vẫn sở hữu mọi policy/mutation.
5. **Controlled smoke:** chạy fixture và Jira UC2 qua từng clarification tới
   requirement approval, sau đó sang Design để xác nhận không còn drift.
6. **Default switch:** đổi default thành V2 sau khi regression và controlled
   smoke pass; giữ V1 reader tới khi không còn run legacy cần tiếp tục.

## Rollback

- Đặt default về V1 chỉ ngăn tạo run V2 mới.
- Run V2 đã tồn tại vẫn phải đọc status, trả lời clarification, sync hoặc cancel
  được; không downgrade artifact sang V1.
- Nếu downstream V2 chưa an toàn, approval gate dừng trước Phase 2 với lỗi rõ;
  không tự chuyển canonical JSON thành legacy text rồi tiếp tục âm thầm.
- Không xóa revision/decision đã tạo trong rollback.

## Điều Kiện Chuyển Default Sang V2

- Unit test schema, grounding, blocking policy, renderer và state transition pass.
- Integration test CLI JSON và downstream dual-read pass.
- Controlled smoke UC2 chứng minh duplicate criterion được human quyết định và
  alternative/exception flow không quay lại Main Flow.
- Jira/Confluence re-sync cập nhật đúng target hiện có.
- Niko hỏi từng câu và không cho approve sớm.
- Tài liệu Phase 1 hiện hành được cập nhật từ “planned” sang “implemented”.

