# Checklist đồng bộ tài liệu với Niko

Ngày tạo: 2026-10-13

## Mục tiêu

Đảm bảo tài liệu SDLC_Harness khớp với Niko_Agent sau khi Niko đã gọi được
Phase 1 qua CLI JSON và Phase 1 đã có đường chạy thật với Jira/Confluence.

## 1. Ranh giới với Niko

Mục đích: người đọc hiểu SDLC_Harness là nơi sở hữu pipeline SDLC, còn Niko chỉ
điều phối hội thoại và format kết quả.

- [x] Ghi rõ Niko gọi SDLC_Harness qua CLI JSON.
- [x] Ghi rõ SDLC_Harness sở hữu source intake, requirement refinement, artifact
  runtime, state và tool sync.
- [x] Ghi rõ response trả về Niko phải là JSON máy-máy, không in text tự do ra
  stdout.

## 2. Phase 1 và Phase 2 hiện tại

Mục đích: khóa lại trạng thái Phase 1/2 để không nhầm Niko là nơi chạy pipeline
SDLC hoặc hiểu Phase 2 vẫn chỉ là tài liệu.

- [x] Hỗ trợ nguồn fixture eTour.
- [x] Hỗ trợ nguồn Jira issue thật qua Atlassian API.
- [x] Sinh formal use case theo hướng TraceDev.
- [x] Sinh requirement packet cho checkpoint người duyệt.
- [x] Validator có repair loop một vòng khi requirement packet thiếu phần bắt
  buộc.
- [x] Sync Jira comment và Confluence page thật khi cấu hình Atlassian đầy đủ.
- [x] Mock sync chỉ còn dùng cho test/fallback.
- [x] Phase 2 thiết kế đã triển khai tới `waiting_design_approval`.
- [x] Niko gọi được `approve-checkpoint` và format design checkpoint ngắn gọn.
- [x] Phase 2 chưa mở coding/Phase 3, request_change hoặc dynamic context.

## 3. Chú thích đầu file source

Mục đích: mỗi module chính tự nói rõ vai trò để về sau tách phase/tool không bị
trộn trách nhiệm.

- [x] `sdlc_harness/interfaces/cli.py`: contract CLI JSON cho Niko.
- [x] `sdlc_harness/workflows/phase1.py`: điều phối Phase 1.
- [x] `sdlc_harness/sources/jira.py`: đọc issue thật, không sync ngược.
- [x] `sdlc_harness/sync/runner.py`: chọn mock/real sync.
- [x] `sdlc_harness/sync/atlassian.py`: adapter ghi Jira/Confluence.
- [x] `sdlc_harness/agents/requirements/refiner.py`: refiner + repair loop.

## 4. Kiểm tra sau đồng bộ

Mục đích: đảm bảo docs/comment không làm lệch behavior.

- [x] Chạy `rtk python -m unittest discover -s tests -p "test*.py" -v`.
- [x] Kiểm tra không commit `.runtime`, `.env`, token hoặc artifact riêng tư.

