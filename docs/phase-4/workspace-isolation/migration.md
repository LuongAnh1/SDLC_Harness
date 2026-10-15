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

Nếu bước verify cuối thất bại, giữ artifact chẩn đoán và không tự xóa branch/PR.
Việc remove worktree lỗi phải là thao tác cleanup/recovery explicit.

## Rollback

Rollback adoption chỉ remove worktree khi worktree sạch. Runtime artifact được
đánh dấu `lifecycle=adoption_failed`; không checkout branch lại canonical repo,
không reset commit và không xóa remote branch.

