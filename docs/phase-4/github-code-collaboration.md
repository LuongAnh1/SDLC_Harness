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

Repo hiện có Java skeleton tối thiểu và `scripts/test.ps1`. Trên máy hiện tại
chưa có `javac`, nên script sẽ chạy static validation và báo rõ compile/runtime
tests đang skipped; khi cài JDK, cùng script sẽ compile và chạy test harness.

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

