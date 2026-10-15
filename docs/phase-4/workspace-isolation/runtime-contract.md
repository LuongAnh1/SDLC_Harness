# Run Workspace Runtime Contract

`run_workspace_v1.json` là nguồn sự thật máy đọc được để mọi bước Phase 4/5
resolve đúng repository workspace. Current branch của canonical repo không phải
nguồn sự thật.

## Artifact Schema

```json
{
  "version": "run_workspace_v1",
  "run_id": "run_20261013_160421_f661babf",
  "source_ref": "jira_issue:SDLC-1",
  "workspace_mode": "git_worktree_v1",
  "canonical_repo": "<machine-local configured repo>",
  "worktree_path": "<resolved run worktree>",
  "branch": "feature/sdlc-1-f661babf",
  "base_ref": "origin/main",
  "base_sha": "<git sha>",
  "head_sha": "<git sha>",
  "remote": "<normalized origin>",
  "provisioning_mode": "created_for_run",
  "pr_number": 1,
  "pr_url": "<GitHub PR URL>",
  "lifecycle": "active",
  "created_at": "<UTC ISO timestamp>",
  "updated_at": "<UTC ISO timestamp>"
}
```

`canonical_repo` và `worktree_path` là dữ liệu machine-local, không sync lên
Jira/Confluence. Tool sync chỉ dùng branch, commit và PR identity cần cho review.

## State Và Artifact References

Sau khi workspace được tạo/adopt:

```text
latest_artifacts.run_workspace=<path tới run_workspace_v1.json>
workspace.workspace_mode=git_worktree_v1
workspace.provisioning_mode=created_for_run | adopted_legacy
workspace.lifecycle=active
workspace.branch=<recorded branch>
workspace.base_ref=<recorded remote base ref>
workspace.base_sha=<fetched remote base SHA>
workspace.head_sha=<observed workspace HEAD>
workspace.remote=<normalized origin>
workspace.pr_number=<PR number or null>
workspace.pr_url=<PR URL or empty>
```

`developer_work_packet_v{n}` phải tham chiếu `run_workspace` và dùng
`worktree_path` đã resolve. Các report code/test/trace/QA phải ghi ít nhất
`run_id`, `branch` và `head_sha` quan sát được.

Không lưu workspace path vào prompt LLM nếu không cần thiết. LLM chỉ cần file
content, repo-relative paths và branch/commit identity phù hợp với nhiệm vụ.

## Ensure Workspace Contract

Workspace manager hiện có bốn API nội bộ:

- `create_for_run(run_id)`: tạo mới hoặc trả lại workspace hợp lệ đã có;
- `resolve(run_id)`: xác minh identity nhưng không giữ action lock;
- `locked_workspace(run_id)`: xác minh và giữ per-run lock trong toàn bộ thao tác
  đọc/sửa code của caller;
- `refresh_locked(record, ...)`: cập nhật `head_sha` và PR identity sau Git
  mutation khi caller vẫn đang giữ lock.

Từ Slice 4.9A3, design approval gọi `create_for_run` trước khi lập implementation
plan. Phase 3/4 dùng `locked_workspace` cho action đọc/sửa/publish code; không
được chỉ `resolve()` rồi nhả lock trước khi thao tác.

Mọi action chạm code phải chạy cùng guard:

```text
ensure_run_workspace(run_id)
  -> load state + run_workspace_v1
  -> acquire workspace-<run_id>.lock
  -> verify worktree is registered by Git
  -> verify worktree branch == artifact.branch
  -> verify origin == artifact.remote
  -> verify path is inside configured worktree root
  -> return validated workspace handle
```

Guard được dùng trước:

- Developer.act và Developer.repair;
- Verification Runner;
- Validator code trace/revalidate;
- QA generation/execution;
- publication và Code Review re-sync;
- Phase 5 rerun.

Không tự checkout branch khác bên trong một existing run worktree. Branch mismatch
là lỗi identity, không phải tín hiệu để Harness sửa repo âm thầm.

## CLI Contract

### `adopt-run-workspace --run-id <run_id>`

Dùng một lần cho legacy run đã có branch/PR trước khi worktree isolation tồn tại.
Lệnh phải idempotent: nếu artifact và worktree hợp lệ đã tồn tại, trả status hiện
tại mà không tạo worktree thứ hai.

### `cleanup-run-workspace --run-id <run_id>`

Xác minh merged/cancelled và clean worktree, remove worktree, prune registry và
đánh dấu `lifecycle=cleaned`. Không xóa remote branch. Local branch chỉ được xóa
sau khi worktree đã remove và điều kiện lifecycle hợp lệ.

Các lệnh trả JSON stdout ổn định để Niko có thể gọi sau này; debug log không được
trộn vào stdout.

## Error Contract

| Error code | Ý nghĩa |
| --- | --- |
| `run_workspace_missing` | Run cần thao tác code nhưng chưa có workspace artifact hợp lệ. |
| `run_workspace_invalid` | Artifact/schema/run id không hợp lệ hoặc thiếu trường bắt buộc. |
| `run_workspace_mismatch` | Artifact không thuộc runtime source/canonical repo đang được resolve. |
| `run_workspace_locked` | Một action khác đang sở hữu lock của cùng run. |
| `workspace_branch_mismatch` | Worktree không đứng trên branch đã ghi trong artifact. |
| `workspace_branch_conflict` | Branch tồn tại nhưng không chứng minh được thuộc cùng run. |
| `workspace_remote_mismatch` | Origin khác remote đã duyệt/cấu hình. |
| `workspace_path_invalid` | Worktree path nằm ngoài root hoặc không được Git đăng ký. |
| `workspace_dirty` | Create/adopt/cleanup gặp thay đổi chưa commit không an toàn. |
| `workspace_fetch_failed` | Không fetch được remote base cho run mới. |
| `workspace_creation_failed` | Git không tạo/xác minh được worktree mới trước khi ghi ownership artifact. |
| `workspace_creation_partial` | Tạo workspace lỗi và rollback bảo thủ không thể hoàn tất; cần người kiểm tra thủ công. |
| `workspace_adoption_failed` | Legacy branch/run/PR identity không đủ rõ để adopt. |
| `workspace_cleanup_not_allowed` | Run/PR chưa đạt điều kiện cleanup. |

Mọi lỗi identity đều fail closed và không mutate repository.

## Tool Sync Boundary

`canonical_repo` và `worktree_path` chỉ tồn tại trong artifact local. Jira và
Confluence nhận artifact ref ổn định cùng branch/commit/PR identity; renderer
phải redact mọi dòng local path trong nội dung implementation plan trước khi
gửi ra ngoài.

