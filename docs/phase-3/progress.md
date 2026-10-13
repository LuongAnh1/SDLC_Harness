# Phase 3 Progress

Tài liệu này ghi lại tiến độ triển khai Phase 3 theo từng lát nhỏ để tránh nhầm
giữa phần đã chạy được và phần vẫn nằm trong kế hoạch.

## 2026-10-13 - Slice 1: mở Phase 3 và sinh implementation plan

Mục tiêu của lát này là nối design checkpoint đã duyệt sang Phase 3, sinh
implementation plan, và chưa đụng vào code thật trong target repo.

Đã hoàn thành:

- Thêm dispatcher `CheckpointApprovalWorkflow` để `approve-checkpoint` tự chọn
  Phase 2 hoặc Phase 3 theo `status` hiện tại của run.
- Thêm `PhaseThreeWorkflow` cho trạng thái `waiting_design_approval`.
- Đọc approved `requirement_packet`, `design_artifact`, trace và graph refs từ
  state Phase 2.
- Thêm config target repo:
  - `SDLC_TARGET_REPO_PATH`
  - `SDLC_TARGET_REPO_REMOTE`
  - `SDLC_TARGET_BASE_BRANCH`
  - `SDLC_IMPLEMENTATION_BRANCH_PREFIX`
- Thêm guard target repo:
  - không được trỏ nhầm vào repo `SDLC_Harness`;
  - target phải là git repo;
  - remote `origin` phải khớp config;
  - worktree phải sạch trước khi harness sinh code ở slice sau.
- Sinh artifact `implementation_plan_v1.md`.
- Ghi state `current_phase=implementation_planning`,
  `status=waiting_implementation_plan_review`,
  `pending_checkpoint.type=implementation_plan_review`.
- Test pass:
  - `rtk proxy python -m pytest tests/test_phase3.py`
  - `rtk proxy python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py`

Chưa làm trong lát này:

- Chưa tạo branch trong target repo.
- Chưa sinh hoặc sửa code.
- Chưa tạo commit.
- Chưa chạy Tester/code trace.
- Chưa tạo Draft PR.
- Chưa có Validator.review_plan thật cho implementation plan.
- Chưa sync implementation plan lên Jira/Confluence.
- GitHub thuộc Phase 4, nên Phase 3 chưa sync GitHub.

