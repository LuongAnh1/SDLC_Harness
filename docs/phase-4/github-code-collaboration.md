# GitHub Code Collaboration Lane Cho Phase 4

Phase 4 cần một target repo riêng để chứng minh context đi từ Jira/Confluence
sang code collaboration lane. `SDLC_Harness` là repo workflow; target repo là nơi
code sản phẩm được tạo/sửa và review.

## Ranh Giới Tool

| Tool | Vai trò trong Phase 4 |
| --- | --- |
| Jira | Issue nguồn, trạng thái công việc, comment tóm tắt PR. |
| Confluence | Requirement/design/code-review checkpoint đầy đủ. |
| GitHub | Branch, commit, Draft PR, code review và test/CI signal. |
| Niko | Gọi action, đọc status, báo người dùng. |

Không tạo GitHub Issue trong Phase 4. Jira `SDLC-1` đã là issue nguồn; tạo thêm
GitHub Issue sẽ làm phân tán source of truth.

## Target Repo

Phase 4 dùng target test repo trên GitHub. Repo này tách khỏi `SDLC_Harness` để
không trộn code workflow với code sản phẩm.

Yêu cầu tối thiểu với target repo:

- có remote GitHub để push branch và mở PR;
- có cấu trúc build/test tối thiểu hoặc ít nhất có chỗ ghi rõ test skipped;
- phù hợp Java/eTour ban đầu để bám TraceDev và dataset;
- có thể xóa/reset branch demo khi cần chạy lại.

Target repo đã chốt cho Phase 4:

```text
local: D:\Dự án cá nhân\Do_an_II\SDLC_Target_eTour
remote: https://github.com/LuongAnh1/SDLC_Target_eTour
visibility: private
default branch: main
```

Repo hiện có Java skeleton tối thiểu và `scripts/test.ps1`. Verification Runner
tự thêm JDK phổ biến vào `PATH` nếu shell chưa nhận `javac`; khi JDK khả dụng,
script compile và chạy test harness thật. Nếu máy thiếu JDK hoặc test harness
không đủ điều kiện, report phải ghi `skipped`/`failed` rõ ràng, không được coi là
pass.

Tính đến Slice 4.7B, target repo có thể đang dirty trên branch feature vì
Developer.act/repair và QA/Tester đã apply code/test để live test. Workflow chỉ
được vào lát GitHub sync khi state là `qa_tested`, nghĩa là Verification Runner,
Validator code trace và QA acceptance/system test đều đã pass. Commit và Draft PR
chưa được mở tự động trước mốc này.

## Branch, Commit Và PR

Quy ước mặc định:

```text
branch: feature/<issue-key>-<short-slug>
commit: <issue-key>: implement <short summary>
PR title: <issue-key>: <short summary>
```

Ví dụ:

```text
feature/SDLC-1-cultural-heritage-delete
SDLC-1: implement cultural heritage delete workflow
```

PR mở ở trạng thái Draft để thể hiện đây là output cần review, chưa phải code
được merge.

## PR Description Bắt Buộc

PR description phải có:

- Jira issue link;
- Confluence requirement checkpoint link;
- Confluence design checkpoint link;
- `run_id`;
- tóm tắt requirement đã duyệt;
- tóm tắt design đã duyệt;
- danh sách file thay đổi;
- traceability summary: requirement nào map tới file/class/function/test nào;
- test/validation đã chạy;
- QA/acceptance/system behavior summary đã chạy;
- known limitations hoặc skipped tests.

Nếu GitHub API/CLI không tạo được PR, Phase 4 có thể ghi local git report ở lát
mở đường nhưng không được coi là khóa Phase 4 thật. Điều kiện khóa Phase 4 là có
Draft PR thật hoặc quyết định rõ ràng thay thế GitHub bằng GitLab.

## Sync Ngược Về Jira Và Confluence

Sau khi có PR:

- Jira nhận comment ngắn gồm PR link, branch, test summary và trạng thái review.
- Confluence nhận checkpoint đầy đủ gồm implementation plan, code change report,
  traceability summary, test report và QA report.
- `tool_sync_report_v4.json` ghi target GitHub/Jira/Confluence và URL tương ứng.

Sync phải idempotent như Phase 1/2: nếu đã có external id do harness tạo, re-sync
cập nhật artifact cũ thay vì tạo trùng comment/page.

## Slice 4.8A Đã Triển Khai

Publication không stage toàn bộ working tree. Harness dựng manifest từ:

- `DeveloperWorkPacket.output_contract.allowed_paths`;
- các file acceptance/system test do QA sinh;
- script hỗ trợ verification được work packet gọi trực tiếp.

Nếu target repo có file dirty ngoài manifest, publication dừng với
`github_publish_unexpected_changes`. Khi manifest hợp lệ, Harness stage từng path,
tạo commit có trailer `SDLC-Run: <run_id>`, push planned branch và tạo/cập nhật
Draft PR bằng GitHub CLI đã đăng nhập.

Hai artifact local là `github_pr_body_v1.md` và
`github_code_review_v1.json`. Retry publication từ `code_review_publish_failed`
không tạo commit/PR thứ hai: commit được tái sử dụng theo trailer và Draft PR đang
mở được cập nhật. Khi đã ở `code_review_published`, bước kế tiếp thuộc Slice 4.8B:
sync Jira/Confluence rồi mở checkpoint `code_review_ready`, không publish GitHub
lần nữa.

## Slice 4.8B Đã Triển Khai Ở Mức Code/Test

- Jira và Confluence giữ một checkpoint trên mỗi `source_ref + phase`; `run_id`
  nằm trong nội dung và version history.
- Mất report local không buộc tạo target mới: adapter tìm marker remote trước.
- Legacy target chỉ được dùng khi có đúng một ứng viên; nhiều ứng viên trả
  `remote_sync_ambiguous`, không tự xóa dữ liệu cũ.
- `tool_sync_report_v4.json` ghi GitHub ở mode `reference`, còn Jira/Confluence
  là target sync thật.
- CLI mặc định mock; live sync cần `--sync-mode real` rõ ràng.

Live gate đã pass trên run `run_20261013_160421_f661babf`: Jira comment `10104`,
Confluence page `2129921`, Draft PR `#1`; lần `sync-run --sync-mode real` thứ hai
cập nhật đúng hai target cũ và giữ nguyên external ID.

## Slice 4.9 - Workspace Và Branch Isolation Dự Kiến

Mỗi run sẽ sở hữu một Git worktree và branch có run suffix. Canonical target repo
chỉ dùng để fetch và quản lý worktree; Developer, Runner, Validator và QA không
được chạy trực tiếp trong canonical working directory.

Run mới tạo branch từ SHA mới nhất của `origin/main`. Resume cùng run dùng lại
đúng worktree/branch đã ghi trong `run_workspace_v1.json`; run mới của cùng Jira
issue vẫn tạo branch mới. Run/PR #1 hiện tại sẽ được adopt một lần, giữ nguyên
HEAD và PR identity.

Approve Code Review chỉ chuyển Draft PR thành Ready for review. Harness không
merge, không bật auto-merge và không xóa remote branch. Xem contract và checklist
tại `workspace-isolation/`.

