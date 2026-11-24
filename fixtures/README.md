# Fixtures

Folder này giữ fixture tối thiểu để SDLC Harness Phase 1 có thể chạy độc lập với repo TraceDev.

## eTour UC1

- Nguồn gốc: `D:\Dự án cá nhân\Do_an_II\ISSTA2026-TraceDev\data\eTour\req\UC1.txt`.
- Mục đích: input đầu tiên cho slice `use case -> requirement_packet -> checkpoint`.
- Ghi chú: fixture này chỉ dùng cho MVP/Phase 1. Khi loader ổn, có thể thêm cấu hình để đọc trực tiếp từ dataset bên ngoài.

## Conduit UC1-UC5

- Nguồn gốc: UC do owner viết cho repo đích `SDLC_Target_Conduit` (RealWorld "Conduit", profile
  `typescript_fullstack_v1`), dataset `conduit` (`start --dataset conduit --use-case UC1`).
- Nội dung: UC1 chỉ tác giả sửa/xóa bài; UC2 feed theo người được follow + đếm tổng đúng; UC3 sửa tiêu đề/slug và
  tag; UC4 bài nháp (có migration); UC5 sửa comment tại chỗ.
- Dataset chỉ chọn nguồn UC; repo đích Developer sửa vẫn do `SDLC_TARGET_REPO_PATH` / `SDLC_TARGET_REPO_REMOTE` chọn.
