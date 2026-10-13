# Sync Design Checkpoint Phase 2

Tài liệu này chốt cách Phase 2 đồng bộ checkpoint thiết kế lên Jira/Confluence.
Mục tiêu không phải tạo thêm một luồng sync riêng cho Niko, mà là để
`SDLC_Harness` sở hữu toàn bộ việc ghi artifact ra tool, còn Niko chỉ đọc
`state.json` và `tool_sync_report_v2.json` để báo lại cho người dùng.

Trạng thái triển khai 2026-10-13: mock sync và real Atlassian sync cho Phase 2
đã có unit test. `sync-run` hiện tự chọn sync requirement/design dựa trên
`pending_checkpoint.type`. Live smoke với Atlassian thật vẫn cần chạy riêng để
khóa vận hành.

## Ranh Giới

Phase 2 chỉ được sync sau khi Validator đã kiểm tra design và workflow có đủ dữ
liệu để tạo checkpoint review.

```text
waiting_requirement_approval
  -> design_drafting
  -> design_validating
  -> design_repairing nếu cần
  -> design_validating
  -> sync design checkpoint
  -> waiting_design_approval
```

Quy tắc:

- Niko không gọi Atlassian API trực tiếp cho luồng SDLC.
- Requirement Refiner không chạy lại chỉ để sync Phase 2.
- Nếu Validator kết luận requirement thiếu nghiêm trọng, workflow quay về
  `waiting_requirement_approval` hoặc `blocked`; không tạo design checkpoint giả.
- Nếu sync lỗi, artifact vẫn được giữ nguyên trong runtime để người dùng/operator
  có thể xem và chạy lại `sync-run`.

## Artifact Được Sync

Checkpoint design phải tóm tắt các artifact mới nhất:

| Artifact | Vai trò trong checkpoint |
| --- | --- |
| `designer_analysis_vN.md` | Lý do chọn hướng thiết kế, pattern, boundary chính. |
| `design_artifact_vN.md` | Class diagram và sequence diagram để người dùng review. |
| `requirement_design_trace_vN.json` | Liên kết requirement -> class/method/lifeline/message. |
| `missing_design_links_vN.json` | Danh sách phần còn thiếu, mong muốn là rỗng khi pass. |
| `design_quality_report_vN.md` | Kết luận Validator và các vòng repair đã chạy. |

Nội dung sync không nên dump toàn bộ JSON trace lên Jira comment. Jira comment
chỉ cần summary, link artifact và trạng thái. Confluence page có thể chứa bản
review đầy đủ hơn vì đó là nơi đọc tài liệu thiết kế.

## Chính Sách Sync Hiện Tại

Sync hiện tại dùng chính sách idempotent theo artifact do `SDLC_Harness` tạo:

- Nếu report cũ đã có `external_id`, adapter cập nhật lại đúng Jira comment hoặc
  Confluence page đó.
- Nếu chưa có `external_id`, adapter tạo comment/page mới và lưu id vào
  `tool_sync_report_v2.json`.
- Harness không chỉnh sửa comment/page do người dùng viết tay; chỉ cập nhật
  artifact mà chính nó đã tạo và theo dõi bằng report.
- Jira/Confluence được xem là bề mặt review/checkpoint mới nhất, không phải nơi
  lưu toàn bộ lịch sử runtime. Lịch sử chi tiết vẫn nằm trong `.runtime/runs`
  theo các version artifact; Confluence cũng có page history ở phía tool.

Cách này tránh spam comment/page mỗi khi chạy `sync-run`, repair design hoặc
retry sau lỗi mạng. Khi cần audit timeline rõ hơn, có thể mở rộng thành policy
lai: giữ một comment/page summary được update in-place, đồng thời tạo thêm Jira
event comment cho các mốc lớn như requirement ready, design ready, sync failed
hoặc cần human approval.

## Jira Sync

Ưu tiên cập nhật cùng comment/run thread đã tạo trước đó. Adapter tìm
`external_id` trong `tool_sync_report_v2.json` trước, rồi fallback sang
`tool_sync_report_v1.json`. Nếu chưa có `external_id`, adapter tạo comment mới.

Comment nên có cấu trúc:

```text
SDLC Harness - Design Checkpoint

Run: <run_id>
Source: <source_ref>
Status: waiting_design_approval

Design summary:
- ...

Traceability:
- Requirements checked: N
- Missing design links: 0 hoặc danh sách rút gọn
- Repair iterations: N

Artifacts:
- design_artifact_vN
- requirement_design_trace_vN
- design_quality_report_vN

Next action:
- Approve design để sang Phase 3
- Request change nếu cần sửa requirement/design
```

Nếu Jira không hỗ trợ update comment trong cấu hình hiện tại, adapter phải ghi rõ
`operation="created"` và lưu `external_id` mới để những lần re-sync sau không tạo
trùng.

## Confluence Sync

Ưu tiên cập nhật page của run đã tạo trước đó. Adapter tìm `external_id` trong
`tool_sync_report_v2.json` trước, rồi fallback sang `tool_sync_report_v1.json`.
Nếu chưa có page, adapter tạo page mới dưới parent page đã cấu hình. Lát hiện
tại update toàn bộ page checkpoint bằng nội dung design review; việc append
section mịn hơn có thể làm sau khi cần format Confluence đẹp hơn.

Page nên có các section:

- `Run Overview`: run id, source ref, trạng thái hiện tại.
- `Approved Requirement`: link/tóm tắt requirement packet đã duyệt.
- `Designer Analysis`: tóm tắt quyết định thiết kế.
- `Design Artifact`: class diagram và sequence diagram ở dạng đọc được.
- `Validator Traceability`: số requirement, số link, missing links.
- `Quality Report`: kết luận Validator và vòng repair.
- `Human Checkpoint`: câu hỏi duyệt/sửa trước khi sang Phase 3.

Confluence là nơi có thể chứa bản đầy đủ hơn Jira, nhưng vẫn phải tránh ghi raw
runtime/private config hoặc API key.

Lát V4.1 hiện tách diagram khỏi phần artifact text:

- Jira comment có section `Diagrams` và đính kèm các block PlantUML cho class /
  sequence diagram.
- Confluence page có section `Design Diagrams`, mỗi class/sequence diagram nằm
  trong code macro PlantUML riêng để reviewer không phải đọc lẫn workflow
  context.
- Đây vẫn là diagram source dạng PlantUML, chưa phải ảnh PNG/SVG. Nếu cần hình
  render thật, bước kế tiếp là tạo SVG/PNG từ `class_diagram_graph_vN.json` và
  `sequence_diagram_graph_vN.json`, upload attachment rồi embed vào Confluence.

Ghi chú về Confluence Cloud: code macro/code snippet mặc định chỉ hiển thị source
code có syntax highlight, không tự render PlantUML thành hình. Muốn reviewer thấy
hình thật có ba hướng:

- cài app/macro diagram trên Confluence, ví dụ PlantUML/Mermaid/draw.io tùy môi
  trường;
- để `SDLC_Harness` render SVG/PNG từ PlantUML/graph, upload attachment lên page,
  rồi embed bằng image attachment;
- giữ code block PlantUML như hiện tại làm fallback/evidence để người đọc vẫn có
  nguồn diagram gốc.

Hướng ưu tiên cho harness là render và upload ảnh ở phía `SDLC_Harness`, vì không
phụ thuộc marketplace app và phù hợp với mục tiêu kiểm soát artifact/version.

## `tool_sync_report_v2.json`

Phase 2 dùng report riêng để không trộn với checkpoint requirement của Phase 1.
Schema dự kiến:

```json
{
  "version": "tool_sync_report_v2",
  "phase": "design_checkpoint",
  "status": "synced",
  "mode": "mock",
  "targets": [
    {
      "tool": "jira",
      "operation": "updated",
      "external_id": "12345",
      "url": "https://example.atlassian.net/browse/SDLC-1?focusedCommentId=12345",
      "status": "synced",
      "artifact_refs": [
        "design_artifact_v2",
        "requirement_design_trace_v2",
        "design_quality_report_v2"
      ],
      "error": null
    },
    {
      "tool": "confluence",
      "operation": "updated",
      "external_id": "294923",
      "url": "https://example.atlassian.net/wiki/x/C4AE",
      "status": "synced",
      "artifact_refs": [
        "designer_analysis_v2",
        "design_artifact_v2",
        "missing_design_links_v2"
      ],
      "error": null
    }
  ],
  "checkpoint": {
    "type": "design_approval",
    "artifact_ref": "design_artifact_v2",
    "artifact_path": "artifacts/design_artifact_v2.md"
  },
  "created_at": "2026-10-13T00:00:00Z",
  "updated_at": "2026-10-13T00:00:00Z"
}
```

`status` tổng thể:

- `synced`: tất cả target đã ghi thành công.
- `partial_failed`: ít nhất một target lỗi, artifact local vẫn hợp lệ.
- `failed`: không target nào ghi được.
- `mock_synced`: mock file đã được tạo cho test/local.

## Mock Sync

Mock sync phải tạo file trong runtime để test không cần Atlassian:

```text
.runtime/runs/<run_id>/sync/design-jira-comment.md
.runtime/runs/<run_id>/sync/design-confluence-page.md
.runtime/runs/<run_id>/sync/tool_sync_report_v2.json
```

Mock content cần giống cấu trúc real sync ở mức heading và summary để test format
không lệch quá xa khi chuyển sang Atlassian thật.

## Re-Sync

`sync-run` ở Phase 2 nên có khả năng sync lại checkpoint mới nhất của run:

```text
sdlc-harness sync-run --run-id <run_id>
```

Quy tắc re-sync:

- Đọc `state.json` để xác định checkpoint hiện tại.
- Nếu checkpoint hiện tại là `requirement_approval`, dùng report Phase 1.
- Nếu checkpoint hiện tại là `design_approval`, dùng `tool_sync_report_v2.json`.
- Nếu report cũ có `external_id`, update target cũ thay vì tạo target mới.
- Nếu report cũ thiếu một target, chỉ tạo target còn thiếu.
- Không xóa artifact local khi sync lỗi.

Config liên quan:

```text
SDLC_SYNC_MODE=mock|real|both
JIRA_SYNC_ENABLED=0|1
CONFLUENCE_SYNC_ENABLED=0|1
```

## Niko Đọc Gì

Niko chỉ cần đọc các trường rút gọn:

```text
status=waiting_design_approval
pending_checkpoint.type=design_approval
latest_artifacts.design_artifact
quality_reports.design_traceability.summary
last_sync.phase=design_checkpoint
last_sync.status=synced|partial_failed|failed|mock_synced
last_sync.targets[].tool/status/url
summary_for_user
```

Nếu `last_sync.status=partial_failed` hoặc `failed`, Niko vẫn có thể báo artifact
đã tạo xong nhưng cần nói rõ sync tool chưa hoàn tất. Không để Niko tự gọi Jira
hoặc Confluence để sửa lỗi sync.

## Kiểm Thử Bắt Buộc

- Mock sync tạo đủ 3 file trong thư mục `sync/`.
- Real Jira sync tạo hoặc update đúng comment.
- Real Confluence sync tạo hoặc update đúng page/section.
- Re-sync không tạo comment/page trùng khi đã có `external_id`.
- Khi Jira lỗi nhưng Confluence pass, report là `partial_failed`.
- Khi sync fail toàn bộ, state vẫn giữ artifact và có thể chạy lại `sync-run`.
