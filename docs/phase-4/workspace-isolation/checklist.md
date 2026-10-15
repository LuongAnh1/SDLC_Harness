# Run Workspace Isolation Checklist

Checklist này là điều kiện bắt buộc trước khi mở Code Review approval và Phase 5.
Không tick mục chỉ vì tài liệu đã mô tả; chỉ tick sau khi code và test tương ứng
đã hoàn thành.

## Slice 4.9A - Workspace Identity Và Guard

Mục đích: mọi action của một run luôn thao tác đúng worktree/branch, không phụ
thuộc branch hiện tại của canonical repo.

- [x] Tạo foundation Run Workspace Manager và schema/persistence `run_workspace_v1.json`.
- [x] Thêm `SDLC_TARGET_WORKTREE_ROOT` với default sibling an toàn, không tạo folder khi load config.
- [x] Khóa quy tắc branch identity có issue/source slug và run suffix bằng unit test.
- [x] Nối branch identity mới vào creation engine tạo branch/worktree thật.
- [x] Fetch `origin/<base_branch>` và lưu `base_sha` trước khi tạo branch mới.
- [x] Run mới không tái sử dụng branch của run cũ.
- [x] Thêm global worktree-registry lock và per-run action lock bằng OS advisory lock.
- [x] `create_for_run`, `resolve` và `locked_workspace` kiểm path/branch/remote/HEAD
  cùng Git worktree registry theo hướng fail closed.
- [x] Developer, Runner, Validator, QA, revalidate và publisher đều resolve
  workspace theo `run_id`.
- [x] Branch/path/remote mismatch fail closed trước khi đọc hoặc sửa code.
- [x] Không còn bước Phase 3/4 nào dùng current branch của canonical repo làm
  nguồn sự thật.

## Slice 4.9B - Adopt Legacy Run

Mục đích: đưa run/PR hiện tại vào kiến trúc mới mà không sinh lại code hoặc làm
thay đổi commit đã review.

- [x] Có CLI `adopt-run-workspace --run-id` trả JSON ổn định.
- [x] Xác minh runtime, branch, remote, HEAD và PR head cùng một identity.
- [x] Adopt run `run_20261013_160421_f661babf` và Draft PR #1.
- [x] HEAD SHA trước/sau adoption không đổi.
- [x] `get-status` vẫn là `code_review_ready` sau adoption.
- [x] Adoption lặp lại idempotent, không tạo worktree thứ hai.
- [x] Ambiguous/missing identity trả `workspace_adoption_failed` và không mutate.

## Slice 4.9C - Lifecycle Và Cleanup

Mục đích: worktree được giữ đủ lâu cho review/Phase 5 nhưng có đường dọn an toàn,
không tích tụ vô hạn hoặc xóa nhầm code.

- [x] Có CLI `cleanup-run-workspace --run-id` trả JSON ổn định.
- [x] Chỉ cleanup khi PR đã merged bởi human hoặc run đã cancelled.
- [x] Dirty worktree bị chặn; V1 không có force cleanup.
- [x] Remove worktree và prune registry trước khi xóa local branch.
- [x] Không xóa remote branch.
- [x] Ghi lifecycle `cleaned` và giữ audit metadata trong runtime.

## Slice 4.9D - Code Review Approval

Mục đích: human approval đóng Phase 4 mà không trao quyền merge cho Harness.

- [x] Approve chỉ hợp lệ tại `pending_checkpoint.type=code_review`.
- [x] Approve chuyển Draft PR thành Ready for review bằng GitHub API/CLI.
- [x] State chuyển `code_review_ready -> code_review_approved`.
- [x] Jira/Confluence được sync trạng thái approved/ready.
- [x] Worktree tiếp tục được giữ sau approval.
- [x] Không có code path merge hoặc bật auto-merge.
- [x] Merge luôn do human thao tác trực tiếp trên GitHub.

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

- [x] Resume cùng run sau khi canonical repo đổi branch vẫn dùng đúng worktree.
- [x] Hai use case chạy xen kẽ không đổi branch/file của nhau.
- [x] Run mới của cùng source có branch và worktree khác run cũ.
- [x] Branch mới có `base_sha` đúng `origin/main` vừa fetch.
- [x] Dirty workspace run A không khiến run B đọc hoặc sửa nhầm code.
- [x] Hai action đồng thời của cùng run bị per-run lock chặn.
- [x] Mismatch workspace artifact/work packet và path/branch/remote bị chặn
  trước các entrypoint Phase 4 chạm code.
- [x] Legacy adoption giữ nguyên commit và PR #1.
- [ ] Phase 5 repair cập nhật cùng branch/PR.
- [x] Approve gọi Draft -> Ready nhưng không merge.
- [x] Cleanup từ chối run chưa merged/cancelled và từ chối dirty worktree.
- [x] Full regression Phase 1-4 pass sau migration.

## Nhật Ký Slice 4.9B

### 2026-10-15 - Adopt Legacy Run Và Draft PR #1

Mục đích: đưa run cũ vào worktree-per-run mà không sinh lại artifact/code, không
đổi commit đã review và không sửa trạng thái Draft PR.

- [x] CLI kiểm tra runtime publication, local/remote branch và GitHub PR identity
  trước khi tạo worktree.
- [x] Checkout canonical sạch được chuyển từ legacy branch về `main`.
- [x] Branch `feature/sdlc-1-implementation` được gắn vào worktree riêng của run.
- [x] HEAD trước/sau giữ nguyên `088416465cc7139255aafa120a0b5bb8f17cb971`.
- [x] PR #1 vẫn `OPEN`, `Draft` và giữ cùng head/base OID.
- [x] Run vẫn `code_review_ready`; checkpoint, `last_sync` và các artifact cũ
  không bị thay đổi ngoài việc bổ sung workspace reference.
- [x] Verification script chạy ngay trong adopted worktree báo `All tests passed`;
  worktree vẫn sạch và HEAD không đổi.
- [x] Chạy lại lệnh trả `adopted=false` và không tạo worktree lần hai.
- [x] Test mismatch và persistence failure chứng minh fail closed/rollback an toàn.

Kết quả kiểm thử: test adoption/workspace/GitHub pass; full regression `174 passed`.

## Nhật Ký Slice 4.9A

### 2026-10-15 - A3 Phase 3/4 Workspace Integration

Mục đích: chuyển toàn bộ đường chạy code sang workspace theo run trong một lát
cắt, tránh trạng thái nửa canonical repo, nửa worktree.

- [x] Design approval tạo workspace trước khi Developer.plan đọc repository.
- [x] CodeGraph được tự chuẩn bị và collect trong run worktree.
- [x] `developer_work_packet_v{n}` snapshot workspace mode/ref/branch/base SHA/remote.
- [x] Developer, Runner, Validator, QA, repair, revalidate, publisher và re-sync
  dùng cùng workspace dưới per-run lock.
- [x] Publisher refresh `head_sha`, PR number/URL vào artifact và state sau publish.
- [x] Jira/Confluence chỉ nhận artifact ref, branch/commit/PR; không nhận local path.
- [x] Test dùng bare remote cục bộ; không gọi LLM thật, không sync tool thật và
  không đụng live run/Draft PR hiện có.
- [x] Legacy adoption đã hoàn thành trong Slice 4.9B.
- [x] Cleanup đã hoàn thành trong Slice 4.9C.

Kết quả kiểm thử: workspace suite `15 passed`; full regression `167 passed`.

### 2026-10-15 - A2 Git Worktree Creation Engine

Mục đích: chứng minh engine có thể tạo và xác minh workspace riêng một cách an
toàn trước khi thay đường dẫn repo của workflow đang chạy.

- [x] Thêm creation engine và state reconciliation.
- [x] Thêm rollback bảo thủ cho lỗi trước khi ownership artifact được ghi.
- [x] Test bằng bare remote cục bộ cho fetch mới nhất, idempotency, isolation giữa
  hai run, dirty canonical repo, `.codegraph`, conflict, identity drift và lỗi
  persistence.
- [x] Nối engine vào Phase 3/4 trong Slice 4.9A3.
- [x] Adopt live run/Draft PR cũ đã hoàn thành riêng trong Slice 4.9B.

## Nhật Ký Slice 4.9C

### 2026-10-15 - Lifecycle Và Cleanup An Toàn

Mục đích: dọn worktree/local branch sau khi run kết thúc mà không làm mất commit
chưa có bản sao phục hồi và không trao quyền xóa remote cho Harness.

- [x] `cleanup-run-workspace --run-id` dùng registry lock rồi per-run lock.
- [x] PR path yêu cầu live PR ở `MERGED`, đúng number/URL/branch/head/base và có
  `mergedAt` cùng merge commit; cancelled path không gọi GitHub.
- [x] Trước cleanup, merged HEAD phải là ancestor của remote base; cancelled HEAD
  phải được remote branch giữ đúng SHA.
- [x] Artifact chuyển `active -> cleanup_pending -> cleaned`; retry từ trạng thái
  pending đối chiếu worktree/ref thực tế và không hỏi GitHub lần hai.
- [x] Worktree dirty bị chặn; `.codegraph/` chỉ được dọn khi là dữ liệu generated,
  không phải tracked path hay symlink.
- [x] Worktree được remove/prune trước khi local ref bị xóa bằng expected SHA.
- [x] Remote branch không bị xóa; workflow phase/checkpoint không bị thay đổi.
- [x] PR #1 live được kiểm tra read-only vẫn `OPEN/Draft`, `mergedAt=null`; worktree
  vẫn sạch và nguyên vẹn nên chưa đủ điều kiện cleanup.

Kết quả kiểm thử tại mốc 4.9C: workspace suite `42 passed`; full regression
`182 passed`. Slice 4.9D bên dưới đã hoàn tất Draft -> Ready mà không merge và
vẫn giữ workspace cho Phase 5.

## Nhật Ký Slice 4.9D

### 2026-10-15 - Draft PR Thành Ready For Review

- [x] GitHub approver đọc PR trước/sau, xác minh OPEN/unmerged và đúng
  number/branch/head SHA/base branch.
- [x] PR đã Ready được xử lý idempotent; identity mismatch không gọi `gh pr ready`.
- [x] Workflow ghi `github_code_review_approval_v1.json` riêng và chuyển state
  sang `code_review_approved` sau khi sync thành công.
- [x] Lỗi Jira/Confluence giữ approval, chuyển sang trạng thái retryable và
  `sync-run` không chạy lại GitHub operation.
- [x] Test kiểm worktree vẫn active và không có command merge/auto-merge.

Kết quả kiểm thử: GitHub/Code Review suite `16 passed`; full regression
ban đầu `188 passed`. Controlled live approval sau đó đã chạy qua Niko: PR #1
chuyển sang Ready for review, vẫn OPEN/chưa merge; run đạt
`code_review_approved`. Post-live full regression: SDLC Harness `192 passed`,
Niko Agent `299 passed`.

