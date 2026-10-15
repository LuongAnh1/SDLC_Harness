# Migration Run Hiện Tại Sang Git Worktree

Run `run_20261013_160421_f661babf` và Draft PR `#1` đã tồn tại trước khi có
worktree-per-run. Run này phải được adopt để tiếp tục Code Review approval và
Phase 5 mà không chạy lại requirement/design/code từ đầu.

## Điều Kiện Trước Adoption

- Runtime state tồn tại và đang trỏ đúng `source_ref=jira_issue:SDLC-1`.
- `developer_work_packet`, GitHub report và Code Review checkpoint cùng trỏ một
  planned branch và Draft PR duy nhất.
- Target repo có đúng `origin` đã cấu hình.
- Branch hiện có đã push và PR head branch khớp branch đó.
- Working tree sạch; không stash/reset tự động.

Nếu có nhiều branch/PR cùng khớp hoặc thiếu identity, dừng bằng
`workspace_adoption_failed` và yêu cầu human chọn/sửa thủ công.

## Quy Trình Adoption

```text
adopt-run-workspace(run_id)
  -> acquire worktree-registry lock + per-run lock
  -> load runtime/GitHub artifacts
  -> query Git branch + Draft PR identity
  -> verify branch, remote, head SHA and PR head match
  -> if canonical repo currently checks out legacy branch:
       require clean tree, then checkout configured base branch
  -> git worktree add <workspace_path> <existing_branch>
  -> write run_workspace_v1.json
  -> update latest_artifacts.run_workspace
  -> run read-only verification in new worktree
  -> release locks
```

Adoption không tạo commit, không push, không cập nhật PR body và không sync
Jira/Confluence. Nó chỉ thay đổi nơi local workflow thao tác code.

## Xác Minh Sau Adoption

- Worktree được `git worktree list --porcelain` nhận diện.
- Branch trong worktree khớp branch của Draft PR #1.
- HEAD SHA trước và sau adoption giống nhau.
- Canonical repo không còn checkout branch của run.
- Verification Runner read-only cho kết quả tương đương report gần nhất.
- `get-status` trả workspace identity nhưng vẫn giữ `code_review_ready`.

Nếu lỗi xảy ra sau khi ownership artifact đã được ghi, giữ worktree/artifact để
lần chạy sau reconcile an toàn; không tự xóa branch/PR.

## Rollback

Trước khi ownership artifact được ghi, rollback chỉ remove worktree khi worktree
vẫn sạch và HEAD còn đúng commit đã xác minh. Nếu canonical repo ban đầu đang giữ
legacy branch, rollback sẽ checkout lại branch đó sau khi remove worktree. Không
reset commit, không xóa local/remote branch và không dùng force.

## Kết Quả Live Adoption 2026-10-15

- Run: `run_20261013_160421_f661babf`.
- Canonical checkout sau adoption: `main` tại
  `b47cbd5992b063cb7efbfb69f40d13073de62bf5`.
- Worktree của run giữ branch `feature/sdlc-1-implementation` tại
  `088416465cc7139255aafa120a0b5bb8f17cb971`.
- GitHub PR #1 vẫn mở, vẫn Draft và giữ nguyên head/base OID.
- State vẫn `code_review_ready`; lần chạy adoption thứ hai trả `adopted=false`.
- `scripts/test.ps1` chạy trong adopted worktree báo `All tests passed`; worktree
  vẫn sạch và HEAD vẫn là `088416465cc7139255aafa120a0b5bb8f17cb971`.

