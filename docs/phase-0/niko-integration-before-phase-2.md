# Niko Integration Trước Phase 2

Tài liệu này ghi lại những việc cần chuẩn bị ở `Niko_Agent` trước khi SDLC Harness chuyển từ Phase 1 sang Phase 2.

## Quyết Định Chính

- Phase 1 dùng CLI trả JSON để Niko gọi SDLC Harness.
- Thử nghiệm đầu của Phase 1 được phép dùng mock/local sync.
- Trước Phase 2 phải có ít nhất một sync API thật hoặc adapter thật ở mức tối thiểu.
- Sync thuộc SDLC Harness; Niko chỉ đọc status/sync result và phản hồi người dùng.

## Luồng Sync Đúng

```text
SDLC Harness chạy phase
-> tạo artifact
-> sync tool thật hoặc mock tool
-> ghi tool_sync_report
-> Niko gọi get_status(run_id)
-> Niko báo người dùng artifact đã sync thành công/thất bại
```

Niko không tự đẩy requirement/design/code artifact lên Jira/Confluence/GitHub. Nếu Niko tự sync, state sẽ bị chia đôi và khó kiểm soát.

## Việc Cần Sửa Bên Niko Trước Phase 2

- Thêm adapter/tool gọi SDLC Harness CLI.
- Thêm intent/route để nhận các câu như: chạy use case, xem trạng thái, duyệt checkpoint, yêu cầu sửa, hủy run.
- Khi trả lời người dùng, Niko phải đọc `current_phase`, `pending_checkpoint`, `allowed_next_actions`, `last_sync` từ SDLC Harness.
- Không hard-code hành động tiếp theo trong Niko nếu Harness đã trả `allowed_next_actions`.
- Cập nhật dashboard/log nếu cần để thấy SDLC run status và sync result.
- Chuẩn bị runtime/provider cho workflow SDLC, ưu tiên DeepSeek thay vì phụ thuộc FCC cho graph mới.

## Điều Kiện Tối Thiểu Trước Phase 2

- Niko gọi được CLI `get_status` và hiển thị status cho người dùng.
- Niko gọi được `approve_checkpoint` cho checkpoint requirement.
- SDLC Harness có `tool_sync_report` thật hoặc API thật tối thiểu.
- Nếu sync thất bại, Niko báo được lỗi và action tiếp theo thay vì trả lời mơ hồ.
