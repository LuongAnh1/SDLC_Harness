# Test Performance Optimization

## Mục Tiêu

Giảm thời gian phản hồi của test suite mà không làm yếu các contract Git,
workspace hoặc hành trình Phase 1-4. Trên máy baseline hiện tại:

- full serial `pytest -q`: không quá 360 giây;
- full parallel `pytest -n 4 --dist load -q`: không quá 240 giây;
- fast suite `pytest -m "not integration and not journey" -q`: không quá 60 giây.

Không dùng skip/xfail, giảm timeout hoặc mock Git hàng loạt để đạt ngân sách.

## Baseline 2026-10-20

- Full suite: `438 passed in 736.48s`.
- `tests/test_phase4.py`: `58 passed in 382.87s`.
- `tests/test_phase3.py`: `35 passed in 145.58s`.
- `tests/test_workspace_git.py`: `32 passed in 116.81s`.
- Ba file trên chiếm khoảng 87,6% thời gian full suite.
- Collection chỉ mất `0.34s`; nút thắt nằm ở execution.
- Phase 3/4 dựng bare remote và target repository trong `setUp` của mọi test,
  kể cả test chỉ kiểm tra helper thuần. Nhiều test Phase 4 chạy lại toàn bộ
  Phase 1-3 để tạo checkpoint đầu vào.
- Suite chưa có marker phân tầng và môi trường chưa cài `pytest-xdist`.

## Cách Triển Khai

1. Khai báo marker `integration` và `journey`, test dependencies và các lệnh
   fast/full rõ ràng.
2. Tách setup Git khỏi test thuần; test chỉ tạo topology khi thật sự cần.
3. Dùng Git template bất biến theo worker rồi tạo remote/canonical repo riêng
   cho từng test, không chia sẻ branch hoặc worktree mutable.
4. Dùng workflow-state factory để tạo checkpoint đúng tầng bằng production
   artifact contracts; giữ một nhóm journey nhỏ chạy xuyên Phase 1-4.
5. Chỉ bật xdist sau khi serial pass và mọi tài nguyên mutable đã được cô lập.

## Gate An Toàn

- Giữ đầy đủ hành vi đang được 438 test bao phủ; nếu hợp nhất test phải ghi rõ
  mapping cũ-mới trong execution log.
- Git workspace tests vẫn chạy Git thật cho create/fetch/worktree/rollback/cleanup.
- Journey tests vẫn chạy handoff thật giữa các phase.
- Full serial pass trước full parallel; full parallel phải pass hai lần liên tiếp.
- Ghi top durations và before/after vào `progress.md` trước khi commit.

## Kết Quả 2026-10-20

- Fast suite: `151 passed, 287 deselected in 16.29s`; đạt ngân sách 60 giây.
- Full parallel lần 1: `438 passed in 135.60s`; lần 2:
  `438 passed in 137.55s`; đạt ngân sách 240 giây và gate chống flaky.
- Full serial: `438 passed in 407.64s`; giảm 44,7% so với baseline
  `736.48s`, nhưng chưa đạt mục tiêu 360 giây.
- Deviation serial được chấp nhận cho lát này vì thời gian còn lại tập trung ở
  ba journey thật và workspace Git contract suite. Không mock thêm các guard
  create/adopt/rollback/cleanup chỉ để đạt số đo.
- Lệnh full regression khuyến nghị cho vòng phát triển là
  `pytest -n 4 --dist load -q`; serial vẫn là đường chẩn đoán và release gate
  bổ sung khi cần kiểm tra phụ thuộc thứ tự.

