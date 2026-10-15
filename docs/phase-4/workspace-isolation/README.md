# Run Workspace Và Branch Isolation

Tài liệu này chốt kiến trúc quản lý target repository trước khi mở Code Review
approval và Phase 5. Mục tiêu là mỗi workflow run luôn đọc/sửa đúng code của nó,
không phụ thuộc branch đang được checkout trong một working directory dùng chung.

## Vấn Đề Đã Được A3 Khóa Cho Run Mới

Cơ chế cũ checkout `planned_branch` trong canonical repo khiến Runner,
Validator, repair và QA có thể đọc nhầm branch khi nhiều run xen kẽ. Từ Slice
4.9A3, design approval tạo branch/worktree riêng có run suffix; mọi action Phase
3/4 resolve artifact đó và giữ per-run lock. Canonical repo không còn là working
directory của workflow.

Run/PR sinh trước A3 chỉ được tiếp tục qua lệnh adopt explicit của Slice 4.9B,
không tự nối vào branch cũ. Run legacy hiện tại và Draft PR #1 đã được adopt,
giữ nguyên HEAD/PR identity và vẫn ở checkpoint `code_review_ready`.

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

Khi đủ điều kiện, artifact được ghi `cleanup_pending` trước khi có side effect:

```text
verify eligibility + recovery copy
  -> lifecycle=cleanup_pending
  -> remove worktree
  -> git worktree prune
  -> delete local branch bằng expected HEAD
  -> lifecycle=cleaned + audit metadata
```

Nếu tiến trình dừng giữa chừng, chạy lại cùng lệnh sẽ tiếp tục từ Git state thực
tế. `cleaned` là idempotent. Cleanup không đổi workflow phase/status/checkpoint.
PR merged phải có HEAD nằm trong remote base vừa fetch; run cancelled phải còn
remote branch trỏ đúng recorded HEAD.

## Ranh Giới V1

- Một target repository cấu hình cho workflow, nhiều worktree theo run.
- Không tự rebase, reset, force-push hoặc xóa remote branch.
- Không tự merge PR.
- Không tự adopt branch cũ khi thiếu bằng chứng run/PR duy nhất.
- Không xử lý nhiều repository trong cùng một run ở slice này.

## Trạng Thái Triển Khai

Slice 4.9A1 đã hoàn thành phần foundation:

- schema và persistence cho `run_workspace_v1.json`;
- branch naming có source slug và run suffix;
- `SDLC_TARGET_WORKTREE_ROOT` với default sibling;
- global registry lock và per-run lock bằng advisory lock của hệ điều hành;
- static resolver fail closed cho run/source/path/branch/base/remote mismatch.

Slice 4.9A2 đã hoàn thành creation engine độc lập:

- `create_for_run(run_id)` fetch `origin/<base_branch>`, chốt `base_sha`, rồi tạo
  branch/worktree riêng có run suffix;
- creation idempotent khi artifact và Git identity của cùng run còn hợp lệ;
- `resolve()` và `locked_workspace()` xác minh path, branch, remote, HEAD và Git
  worktree registry trước khi trao workspace cho caller;
- runtime state lưu tham chiếu artifact và snapshot identity tối thiểu;
- lỗi trước khi có ownership artifact chỉ rollback khi worktree còn sạch và HEAD
  vẫn ở `base_sha`; trường hợp không chứng minh được thì fail bằng
  `workspace_creation_partial` thay vì force-delete;
- integration test dùng bare remote cục bộ, không chạm GitHub hoặc live run.

Slice 4.9A3 đã nối creation engine vào workflow:

- design approval tạo workspace rồi tự chuẩn bị CodeGraph tại worktree;
- implementation plan và DeveloperWorkPacket ghi identity branch/base/workspace;
- Developer, Runner, Validator, QA, repair, revalidate và publisher cùng dùng
  `locked_workspace(run_id)`;
- publication refresh HEAD cùng PR identity vào artifact/state;
- status và tool sync không phát tán machine-local worktree path;
- canonical repo có thể đổi branch mà run vẫn resume đúng workspace.

Slice 4.9B đã hoàn thành CLI adoption, fail-closed guard, rollback và live
migration run/PR #1. Slice 4.9C đã hoàn thành cleanup CLI, lifecycle/audit,
recovery proof, dirty guard và retry idempotent. Phần còn mở là 4.9D chuyển Draft
PR thành Ready for review.

