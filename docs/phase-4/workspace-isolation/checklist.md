# Run Workspace Isolation Checklist

Checklist này là điều kiện bắt buộc trước khi mở Code Review approval và Phase 5.
Không tick mục chỉ vì tài liệu đã mô tả; chỉ tick sau khi code và test tương ứng
đã hoàn thành.

## Slice 4.9A - Workspace Identity Và Guard

Mục đích: mọi action của một run luôn thao tác đúng worktree/branch, không phụ
thuộc branch hiện tại của canonical repo.

- [ ] Tạo Run Workspace Manager và `run_workspace_v1.json`.
- [ ] Thêm `SDLC_TARGET_WORKTREE_ROOT` với default sibling an toàn.
- [ ] Branch mới có issue/source slug và run suffix.
- [ ] Fetch `origin/<base_branch>` và lưu `base_sha` trước khi tạo branch mới.
- [ ] Run mới không tái sử dụng branch của run cũ.
- [ ] Thêm global worktree-registry lock và per-run action lock.
- [ ] Developer, Runner, Validator, QA, revalidate và publisher đều resolve
  workspace theo `run_id`.
- [ ] Branch/path/remote mismatch fail closed trước khi đọc hoặc sửa code.
- [ ] Không còn bước Phase 4/5 nào dùng current branch của canonical repo làm
  nguồn sự thật.

## Slice 4.9B - Adopt Legacy Run

Mục đích: đưa run/PR hiện tại vào kiến trúc mới mà không sinh lại code hoặc làm
thay đổi commit đã review.

- [ ] Có CLI `adopt-run-workspace --run-id` trả JSON ổn định.
- [ ] Xác minh runtime, branch, remote, HEAD và PR head cùng một identity.
- [ ] Adopt run `run_20261013_160421_f661babf` và Draft PR #1.
- [ ] HEAD SHA trước/sau adoption không đổi.
- [ ] `get-status` vẫn là `code_review_ready` sau adoption.
- [ ] Adoption lặp lại idempotent, không tạo worktree thứ hai.
- [ ] Ambiguous/missing identity trả `workspace_adoption_failed` và không mutate.

## Slice 4.9C - Lifecycle Và Cleanup

Mục đích: worktree được giữ đủ lâu cho review/Phase 5 nhưng có đường dọn an toàn,
không tích tụ vô hạn hoặc xóa nhầm code.

- [ ] Có CLI `cleanup-run-workspace --run-id` trả JSON ổn định.
- [ ] Chỉ cleanup khi PR đã merged bởi human hoặc run đã cancelled.
- [ ] Dirty worktree bị chặn; V1 không có force cleanup.
- [ ] Remove worktree và prune registry trước khi xóa local branch.
- [ ] Không xóa remote branch.
- [ ] Ghi lifecycle `cleaned` và giữ audit metadata trong runtime.

## Slice 4.9D - Code Review Approval

Mục đích: human approval đóng Phase 4 mà không trao quyền merge cho Harness.

- [ ] Approve chỉ hợp lệ tại `pending_checkpoint.type=code_review`.
- [ ] Approve chuyển Draft PR thành Ready for review bằng GitHub API/CLI.
- [ ] State chuyển `code_review_ready -> code_review_approved`.
- [ ] Jira/Confluence được sync trạng thái approved/ready.
- [ ] Worktree tiếp tục được giữ sau approval.
- [ ] Không có code path merge hoặc bật auto-merge.
- [ ] Merge luôn do human thao tác trực tiếp trên GitHub.

## Phase 5 Prerequisite - Request Change

Mục đích: yêu cầu sửa tiếp tục trên đúng code lineage đã được review.

- [ ] Request change giữ nguyên run workspace, branch và PR.
- [ ] Feedback được phân loại về requirement/design/plan/code trước khi rerun.
- [ ] Code-only repair chạy Developer -> Runner -> Validator -> QA trong cùng
  worktree rồi push commit mới lên cùng PR.
- [ ] Nếu PR đã Ready, request change đưa PR về Draft trước khi sửa.
- [ ] Sau repair/sync, run quay lại `code_review_ready` để human duyệt lại.

## Test Và Acceptance

Mục đích: chứng minh isolation hoạt động cả khi resume, chạy xen kẽ và migration.

- [ ] Resume cùng run sau khi canonical repo đổi branch vẫn dùng đúng worktree.
- [ ] Hai use case chạy xen kẽ không đổi branch/file của nhau.
- [ ] Run mới của cùng Jira issue có branch khác run cũ.
- [ ] Branch mới có `base_sha` đúng `origin/main` vừa fetch.
- [ ] Dirty workspace run A không khiến run B đọc hoặc sửa nhầm code.
- [ ] Hai action đồng thời của cùng run bị per-run lock chặn.
- [ ] Mismatch path/branch/remote bị chặn ở mọi entrypoint Phase 4.
- [ ] Legacy adoption giữ nguyên commit và PR #1.
- [ ] Phase 5 repair cập nhật cùng branch/PR.
- [ ] Approve gọi Draft -> Ready nhưng không merge.
- [ ] Cleanup từ chối run chưa merged/cancelled và từ chối dirty worktree.
- [ ] Full regression Phase 1-4 pass sau migration.

