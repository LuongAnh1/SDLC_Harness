# Run Workspace Và Branch Isolation

Tài liệu này chốt kiến trúc quản lý target repository trước khi mở Code Review
approval và Phase 5. Mục tiêu là mỗi workflow run luôn đọc/sửa đúng code của nó,
không phụ thuộc branch đang được checkout trong một working directory dùng chung.

## Vấn Đề Hiện Tại

Phase 4 hiện chỉ checkout `planned_branch` khi bắt đầu `Developer.act`. Các bước
sau như Verification Runner, Validator, Developer repair và QA đọc branch đang
được checkout tại thời điểm chạy. Nếu một run khác đổi branch giữa hai lần
`continue-run`, run cũ có thể đọc hoặc sửa nhầm code.

Tên branch hiện cũng chỉ dựa trên Jira issue/use case. Một run mới của cùng
source có thể checkout lại branch cũ thay vì bắt đầu từ base sạch. Vì vậy cơ chế
hiện tại chỉ an toàn khi vận hành tuần tự một run và không phù hợp với Phase 5.

## Kiến Trúc Đã Chốt

```text
canonical target repository
  -> fetch origin/<base_branch>
  -> Run Workspace Manager
       -> run A: worktree A + branch A
       -> run B: worktree B + branch B

Phase 4/5 action(run_id)
  -> resolve run_workspace_v1
  -> acquire per-run lock
  -> validate worktree + branch + remote identity
  -> Developer / Runner / Validator / QA / GitHub publish
  -> refresh workspace artifact
  -> release lock
```

- Canonical repository chỉ dùng để fetch và quản lý Git worktree.
- Developer, Verification Runner, Validator và QA không chạy trực tiếp trong
  canonical working directory.
- Mỗi run có một worktree và branch riêng trong toàn bộ vòng đời review/sửa.
- Cùng run luôn resolve về cùng worktree; không dựa vào current working branch.
- Run mới luôn có branch mới, kể cả source/Jira issue giống run đã kết thúc.
- Branch mới được tạo từ SHA mới nhất của `origin/<base_branch>`.
- Resume không tự rebase, reset hoặc force-push branch đang làm dở.

## Branch Identity

Branch thuộc về run, không chỉ thuộc Jira issue:

```text
<configured-prefix>/<issue-or-source-slug>-<run-suffix>

Ví dụ:
feature/sdlc-1-f661babf
feature/etour-uc1-8f759075
```

`run-suffix` lấy từ phần UUID cuối của `run_id`. Branch name phải được ghi vào
`run_workspace_v1.json` và không được tính lại theo trạng thái Git hiện tại.

Nếu branch dự kiến đã tồn tại nhưng không có workspace artifact chứng minh nó
thuộc cùng run, workflow phải fail với `workspace_branch_conflict`; không tự
checkout hoặc nối run mới vào branch đó.

## Workspace Root

Config mới dự kiến:

```text
SDLC_TARGET_WORKTREE_ROOT=<optional path>
```

Nếu không cấu hình, mặc định dùng thư mục sibling của target repo:

```text
<target-repo-parent>/<target-repo-name>-worktrees/<run_id>
```

Không tạo worktree bên trong target repo hoặc bên trong một worktree khác.

## Base Branch Policy

Khi tạo workspace cho run mới:

1. Xác minh canonical repo và `origin` đúng cấu hình.
2. Xác minh canonical repo không có thay đổi ngoài metadata được cho phép.
3. Chạy `git fetch origin <base_branch>`.
4. Resolve và lưu SHA của `origin/<base_branch>`.
5. Tạo branch mới và worktree từ đúng remote SHA đó.

Nếu fetch thất bại, workflow dừng; không âm thầm dùng local `main` có thể đã cũ.
Với run đang resume, Harness không fetch rồi thay đổi base của branch.

## Locking

Hai lớp lock được dùng:

- `worktree-registry.lock`: chỉ giữ trong lúc create/adopt/cleanup để bảo vệ Git
  worktree registry.
- `workspace-<run_id>.lock`: giữ trong toàn bộ action có thể đọc/sửa code của
  run, ngăn hai lệnh cùng tác động một workspace.

Action cùng run thứ hai phải fail nhanh bằng `run_workspace_locked`. Hai run khác
nhau được phép chạy song song vì dùng worktree riêng.

## Code Review Và Phase 5

```text
code_review_ready
  -> human approve checkpoint
  -> Harness chuyển Draft PR thành Ready for review
  -> code_review_approved
  -> human tự review/merge trên GitHub
```

Harness không được gọi merge hoặc bật auto-merge. Worktree vẫn được giữ sau
`code_review_approved` để human có thể yêu cầu sửa trước khi merge.

Nếu human request change, Phase 5 tiếp tục trên chính worktree, branch và PR của
run đó. Harness phân loại phạm vi thay đổi rồi chạy lại đúng đoạn workflow; nó
không tạo branch/PR khác cho mỗi vòng repair.

## Cleanup

Cleanup chỉ xảy ra qua lệnh explicit và không xóa remote branch:

```text
cleanup-run-workspace --run-id <run_id>
```

Lệnh chỉ được chạy khi:

- PR đã được GitHub xác nhận merged bởi human; hoặc
- run đã ở trạng thái `cancelled`;
- worktree không còn thay đổi chưa commit.

Nếu workspace dirty hoặc trạng thái PR/run không hợp lệ, cleanup fail closed.
Không có `--force` trong V1.

## Ranh Giới V1

- Một target repository cấu hình cho workflow, nhiều worktree theo run.
- Không tự rebase, reset, force-push hoặc xóa remote branch.
- Không tự merge PR.
- Không tự adopt branch cũ khi thiếu bằng chứng run/PR duy nhất.
- Không xử lý nhiều repository trong cùng một run ở slice này.

