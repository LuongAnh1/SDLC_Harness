"""Tài liệu có phiên bản cho bố cục runtime cục bộ."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
import time
from uuid import uuid4

from ..core.errors import HarnessError


RUNTIME_LAYOUT_VERSION = "runtime_layout_v4"
_README_LOCK = RLock()
RUNTIME_README = """# Dữ liệu runtime của SDLC Harness

Phiên bản bố cục: `runtime_layout_v4`

Thư mục `.runtime/` là kho trạng thái vận hành cục bộ của SDLC Harness. Nó chứa
trạng thái workflow, artifact trung gian và cuối cùng, nhật ký thực thi, kết quả
đồng bộ Jira/Confluence/GitHub và dữ liệu phục vụ retry. Đây không phải mã nguồn
của Harness và không phải nơi lưu mã nguồn mà Developer Agent sửa.

> **Cảnh báo:** không commit hoặc chia sẻ nguyên thư mục này. `config.json`,
> artifact nguồn, câu trả lời làm rõ, đầu ra LLM, stdout/stderr và nội dung mã
> nguồn có thể chứa bí mật hoặc dữ liệu riêng tư.

## 1. Cách đọc tài liệu này

- `<run_id>` có dạng `run_YYYYMMDD_HHMMSS_<suffix>` và định danh một lần chạy.
- `v<N>` là phiên bản artifact của một phase; phiên bản mới không ghi đè bản cũ.
- `r<N>` là revision của Requirement Model sau mỗi lần làm rõ hoặc thay đổi.
- `a<N>` là attempt của Requirement tool loop hoặc một lần gọi tool/LLM.
- Một số file chỉ xuất hiện khi nhánh tương ứng thực sự chạy, ví dụ repair,
  revalidation, mock sync, GitHub publish hoặc lỗi parse đầu ra LLM.
- Đường dẫn artifact mới nhất được ghi trong `state.json -> latest_artifacts`.
  Các phiên bản cũ vẫn được giữ để audit nhưng không mặc nhiên là đầu vào hiện tại.

## 2. Cấu trúc tổng quát

```text
.runtime/
  README.md
  config.json
  index/
    active_runs.json
  locks/
    run-<run_id>.lock
    event-<run_id>.lock
    source-<sha256>.lock
    workspace-<run_id>.lock
    worktree-registry.lock
  operations/
    op_<timestamp>_<suffix>.json
  idempotency/
    <sha256>.json
  runs/
    <run_id>/
      state.json
      events.jsonl
      artifacts/
      sync/
      developer_execution/
```

Thư mục được tạo theo nhu cầu. Một run dừng ở Requirement sẽ chưa có artifact
Design, Plan, Code hoặc QA.

## 3. Nguồn sự thật và thứ tự ưu tiên

| Vị trí | Nguồn sinh | Lưu gì | Được dùng làm đầu vào cho |
| --- | --- | --- | --- |
| `config.json` | Config API/Ops Dashboard qua `RuntimeConfigStore` | Các giá trị cấu hình runtime được phép sửa; có thể gồm secret | Lần dựng `Settings` kế tiếp và operation kế tiếp |
| `index/active_runs.json` | `RuntimeStore.register_active_run` khi tạo run | Ánh xạ `source_ref -> run_id` đang hoạt động | `start_or_get_run`, chống tạo hai run chưa kết thúc cho cùng nguồn |
| `runs/<run_id>/state.json` | Tất cả workflow qua `RuntimeStore.write_state` | Phase, status, checkpoint, action hợp lệ, artifact mới nhất, quality report, lỗi và sync gần nhất | Mọi action tiếp theo, Read API, Ops Dashboard và Niko |
| `runs/<run_id>/events.jsonl` | Event Store từ operation, workflow, LLM, validator, runner, QA và sync | Timeline quan sát đã lọc dữ liệu nhạy cảm | Events API và Ops Dashboard; **không** dùng để dựng lại hay cấp quyền thay cho `state.json` |
| `operations/*.json` | HTTP write API/Operation Manager | Trạng thái bền vững của một request ghi bất đồng bộ | Poll operation, phục hồi sau restart và trả kết quả/lỗi cho client |
| `idempotency/*.json` | Operation Store khi nhận idempotency key | Hash key, hash request và `operation_id` tương ứng | Nhận diện retry cùng request, ngăn workflow chạy hai lần và rebuild index sau crash |
| `locks/*.lock` | Runtime Store, Event Store và Workspace Manager | Điểm neo cho khóa hệ điều hành | Tuần tự hóa mutation theo source, run, event hoặc worktree |

`state.json` là nguồn sự thật về trạng thái workflow hiện tại. Artifact là bằng
chứng và dữ liệu phase. Operation chỉ mô tả một API request. Event chỉ là dòng
thời gian quan sát. Sự tồn tại của file `.lock` không có nghĩa khóa đang bị giữ;
quyền khóa nằm ở handle của tiến trình.

## 4. Cấu hình và lớp vận hành HTTP

### `README.md`

- **Nguồn sinh:** hằng `RUNTIME_README` trong
  `sdlc_harness/storage/runtime_readme.py`; `ensure_runtime_readme` tự đồng bộ.
- **Mục đích:** giải thích bố cục, không tham gia quyết định workflow.
- **Đầu vào cho:** người vận hành và lập trình viên.

### `config.json`

- **Nguồn sinh:** Config API đã xác thực hoặc tab Config của Ops Dashboard.
- **Mục đích:** lưu override cục bộ theo allowlist. Thứ tự ưu tiên hiệu lực là:
  code default -> `.env` gốc -> file này -> biến môi trường OS -> override của
  đúng invocation/action.
- **Đầu vào cho:** `load_settings` khi tạo Application Service/operation mới.
- **Lưu ý:** host, port, API token và vị trí file config là bootstrap field nên
  không sửa qua Dashboard. Thay đổi queue/shutdown cần restart API; cấu hình còn
  lại áp dụng từ operation kế tiếp.

### `operations/op_<timestamp>_<suffix>.json`

- **Nguồn sinh:** HTTP write endpoint tạo một `operation_v1`, sau đó Operation
  Manager cập nhật `queued -> running -> succeeded|failed|interrupted`.
- **Mục đích:** lưu command, `run_id`, `sync_mode`, hash request, thời điểm chạy,
  kết quả hoặc lỗi; không lưu raw request hay idempotency key dạng rõ.
- **Đầu vào cho:** endpoint đọc/poll operation, cơ chế phát hiện operation đang
  chạy, và phục hồi khi API restart. Operation dở dang sau restart được đánh dấu
  `interrupted`, không tự chạy lại.

### `idempotency/<sha256>.json`

- **Nguồn sinh:** Operation Store sau khi nhận một write request có idempotency key.
- **Mục đích:** ánh xạ hash của key và request sang đúng một operation.
- **Đầu vào cho:** lần retry HTTP kế tiếp; cùng key và cùng request trả lại
  operation cũ, còn cùng key nhưng khác request bị từ chối.

### Các file `locks/*.lock`

| Pattern | Chủ sở hữu | Bảo vệ thao tác nào |
| --- | --- | --- |
| `source-<sha256>.lock` | Runtime Store | Tạo/intake run cho cùng một `source_ref` |
| `run-<run_id>.lock` | Runtime Store/Application Service | Mutation state và artifact của một run |
| `event-<run_id>.lock` | Event Store | Ghi nối tiếp `events.jsonl` và cấp `sequence` |
| `workspace-<run_id>.lock` | Workspace Manager | Thao tác trên worktree/branch của đúng run |
| `worktree-registry.lock` | Workspace Manager | Thay đổi registry Git worktree dùng chung |

## 5. File gốc của mỗi run

### `runs/<run_id>/state.json`

- **Nguồn sinh:** Phase 1 tạo lần đầu; mọi phase cập nhật atomically sau mỗi
  transition hoặc khi có artifact mới.
- **Lưu gì:** `current_phase`, `status`, `pending_checkpoint`,
  `allowed_next_actions`, `latest_artifacts`, `approved_artifacts`,
  `quality_reports`, `last_error`, `last_sync`, workspace identity và bộ đếm repair.
- **Đầu vào cho:** tất cả action như `get_status`, `continue_run`,
  `approve_checkpoint`, `answer_clarification`, `revalidate_*`, `sync_run`;
  đồng thời là dữ liệu Niko dùng để báo trạng thái cho người dùng.

### `runs/<run_id>/events.jsonl`

- **Nguồn sinh:** Event Store ghi nối tiếp sau operation/state transition, gọi
  LLM, validator, verification, QA và sync.
- **Lưu gì:** mỗi dòng là một `event_v1` với `sequence` tăng liên tục. Chỉ metadata
  trong allowlist được giữ; prompt, model output, câu trả lời, command,
  stdout/stderr, credential và local path bị loại.
- **Đầu vào cho:**
  `GET /api/v1/runs/{run_id}/events?after=<sequence>&limit=<n>` và Dashboard.
- **Phục hồi:** dòng cuối bị ghi dở do crash sẽ bị cắt trước lần append kế tiếp;
  dòng hoàn chỉnh sai cấu trúc gây lỗi `event_log_corrupted`.

## 6. Artifact Phase 1 - Requirement

Các file dưới đây nằm trong `runs/<run_id>/artifacts/`.

| File/pattern | Nguồn sinh | Nội dung và mục đích | Đầu vào cho bước sau |
| --- | --- | --- | --- |
| `use_case_packet_v1.md` | Source loader + `write_use_case_packet` | Bản đóng gói dễ đọc của Jira issue/fixture/dataset thô cùng định danh nguồn | Audit, checkpoint và đối chiếu nguồn; canonical processing dùng snapshot bất biến bên dưới |
| `source_snapshot_<source_id>_v2.json` | Phase 1 ngay sau khi load nguồn | Bản chụp nguồn bất biến gồm text, metadata, revision và fingerprint | Requirement tool loop, Validator, clarification, revalidation và frozen Requirement input cho Design |
| `requirement_source_manifest_v1.json` | Requirement Refiner/Source Tools | Danh mục các nguồn được phép dùng, artifact ref và fingerprint kỳ vọng | Mọi lần đọc nguồn có kiểm soát, kiểm tra drift và Gap Report |
| `requirement_source_journal_v1.json` | Requirement source tool loop | Nhật ký lần đọc nguồn, kết quả hoặc lỗi và evidence đã quan sát | Resume tool loop, Gap Report và audit nguồn |
| `requirement_gap_report_v1.json` | Deterministic Gap Report builder | Phần thông tin còn thiếu, nguồn lỗi và gap cần xử lý | Requirement loop quyết định đọc thêm nguồn, hỏi người dùng hoặc dừng |
| `requirement_loop_session_v1.json` | Requirement Loop Session Store | Budget, call ID, retry và tiến độ của vòng tool-loop | Resume/retry an toàn mà không lặp lại action đã hoàn tất |
| `requirement_action_intent_v1_<call_id>.json` | Mỗi lần Requirement Agent đề xuất tool action | Intent máy đọc được, tham số, kết quả kiểm tra và trạng thái action | Executor của tool-loop và audit từng quyết định gọi tool |
| `requirement_working_candidate_v2_a<N>.json` | Requirement Refiner sau mỗi attempt | Candidate Requirement Model đang được xây | Attempt kế tiếp, deterministic validation và finalization |
| `requirement_delta_v1_a<N>.json` | DeepSeek đề xuất, Harness kiểm tra rồi áp dụng | Các phép thêm/sửa/xóa có giới hạn trên candidate cùng base hash | Delta applier tạo working candidate tiếp theo; không dùng trực tiếp làm requirement đã duyệt |
| `requirement_loop_final_model_v1.json` | Requirement Refiner khi tool loop kết thúc | Candidate cuối cùng cùng lý do dừng và audit loop | Tạo bundle Requirement revision bất biến |
| `requirement_model_v2_r<N>.json` | Requirement artifact writer | Mô hình chuẩn máy đọc được: actor, requirement, acceptance, scenario, surface, open question, assumption, decision và provenance | Clarification/revalidation; frozen input của Design, Plan, Code Trace và QA |
| `requirement_packet_v2_r<N>.md` | Renderer từ `requirement_model_v2_r<N>.json` | Bản trình bày cho người review | Requirement checkpoint, Confluence, Designer và semantic Validator |
| `formal_use_case_v2_r<N>.txt` | Renderer từ cùng Requirement Model | Use case theo grammar ổn định kiểu TraceDev | Designer và traceability compatibility |
| `requirement_validation_v2_r<N>.json` | Requirement Validator + projection validator | Kết quả rule validation, coverage và kiểm tra các projection MD/TXT khớp model | Mở/chặn Requirement checkpoint và audit revision |
| `requirement_decisions_v2_r<N>.json` | Requirement artifact writer | Projection riêng của human decision, assumption và quyết định làm rõ | Audit, render/sync và kiểm tra lịch sử quyết định |
| `requirement_clarification_analysis_v1_r<N>.json` | Clarification analyzer/DeepSeek | Các câu hỏi đề xuất, phân loại, lựa chọn gợi ý và metadata để Harness xét policy | Harness chọn `human_required`, `agent_default` hoặc `deferred_to_design` |
| `requirement_quality_checklist_v1_r<N>.json` | Deterministic Requirement quality gate | Checklist completeness/consistency sau draft, answer, default, challenge hoặc revalidation | Quyết định có được mở Requirement Approval Checkpoint hay phải tiếp tục làm rõ |
| `requirement_clarification_policy_shadow_v1_r<N>.json` | Clarification policy shadow evaluator khi được bật | So sánh/quan sát policy phân loại mà không thay đổi quyết định chính | Chẩn đoán và đánh giá policy; không phải nguồn cấp quyền workflow |
| `requirement_reasonable_defaults_v1_r<N>.json` | Harness guardrail sau đề xuất `agent_default` của DeepSeek | Default rủi ro thấp đã chấp nhận/từ chối và assumption `ASM-*` được tạo | Requirement revision, Requirement Approval và khả năng `challenge_assumption` |
| `requirement_assumption_challenge_v1_r<N>.json` | Action `challenge_assumption` | Audit việc rút assumption, mở lại câu hỏi và buộc người dùng trả lời | Revision Requirement mới và vòng `answer_clarification` tiếp theo |
| `requirement_revalidation_v1_r<N>.json` | Action deterministic-only `revalidate_requirement` | Kết quả kiểm tra lại model/snapshot/projection hiện hành mà không gọi LLM | Giữ hoặc chặn Requirement checkpoint |

Artifact `requirement_policy_defaults_v1_r<N>.json` nếu còn trong run cũ là dữ
liệu lịch sử của cơ chế registry V2 đã bị loại bỏ. Code hiện tại không sinh và
không dùng nó làm đầu vào.

## 7. Artifact L2/Phase 2 - Repo Discovery và Design

| File/pattern | Nguồn sinh | Nội dung và mục đích | Đầu vào cho bước sau |
| --- | --- | --- | --- |
| `run_workspace_v1.json` | Workspace Manager ngay sau Requirement Approval của run L2 mới | Identity worktree/branch và base/head SHA đã freeze; bind target profile snapshot trước khi đọc code | Repo Discovery, Design, Plan và toàn bộ execution; các run cũ không có marker L2 vẫn giữ flow cũ |
| `repo_landscape_v1.json` | Repo Discovery từ worktree đã freeze, profile roots và CodeGraph có sẵn hoặc bounded fallback | Inventory component/file, AS-IS anchor, candidate mapping theo scenario surface, conformance finding, uncertainty và digest; không chứa raw source hoặc absolute path | Designer hiện hành, Read API, human audit và Phase 3 planning |
| `designer_execution/design_v<N>/designer_loop_session_v1.json` | L3 Designer executor | Frozen authority, cursor, pending call, budgets và canonical graph đang chạy | Private resume/reconciliation; không mở qua Read API |
| `designer_execution/design_v<N>/designer_loop_manifest_v1.json` | L3 Designer executor | Input digest, tool policy và workspace binding của session | Input-drift guard; private runtime evidence |
| `designer_execution/design_v<N>/designer_loop_journal_v1.jsonl` | L3 Designer executor | Sanitized tool result/evidence refs theo call ID | Private audit/idempotency; không phải public artifact |
| `designer_execution/design_v<N>/designer_delta_designer-<call>.json` | `design.propose_delta` | Delta input, apply result và graph revision đã persist | Reconcile ambiguous mutation khi resume; private runtime evidence |
| `vertical_design_graph_v<N>.json` | L3 Designer graph engine | Canonical scenario/component graph bind frozen Requirement/landscape/workspace | Design checkpoint, deterministic projections và L4/B1 handoff |
| `design_component_view_v<N>.json`, `design_surface_view_v<N>.json` | Deterministic projection renderer | Read-only views nhóm canonical node theo component/surface | Human review và public Read API |
| `design_projection_manifest_v<N>.json` | Deterministic projection renderer | Graph digest/revision và refs của compatibility projections | Checkpoint/sync, integrity audit và downstream handoff |
| `design_finding_ledger_v<N>.json` | L3 validation workflow | Blocker/warning/disputed findings đã sanitize theo Design version | Human review, revalidation và L4 finding interface |
| `designer_analysis_v<N>.md` | Designer Agent | Phân tích thiết kế và bối cảnh của version | Repair vòng sau, human review và audit |
| `design_artifact_v<N>.md` | Designer Agent | TO-BE design hiện hành, gồm PlantUML/class/sequence theo phạm vi use case | Parser, Design Validator, Design checkpoint và Implementation Plan Agent |
| `class_diagram_graph_v<N>.json` | Deterministic PlantUML parser | Graph class/interface/entity và quan hệ đã parse | Traceability Validator, Plan Validator và Code Trace Validator |
| `sequence_diagram_graph_v<N>.json` | Deterministic PlantUML parser | Graph actor/lifeline/message/fragment của luồng tương tác | Traceability Validator, Plan Validator và Code Trace Validator |
| `requirement_graph_v<N>.json` | Traceability builder từ Requirement Model/packet | Graph requirement ổn định dùng để nối sang design | Requirement-to-design trace, Plan review và Code Trace |
| `requirement_design_trace_v<N>.json` | Traceability Validator | Các cạnh Requirement -> Class/Sequence cùng input refs | Design gate, Implementation Plan và mở rộng thành Requirement-Design-Code trace |
| `missing_design_links_v<N>.json` | Traceability Validator | Danh sách requirement chưa có class/sequence link | Designer repair và Design quality report |
| `design_validation_report_v<N>.json` | Hybrid Design Validator | Rule findings, semantic findings/consensus, blocker, warning và repair count | Quyết định repair hoặc mở Design Approval Checkpoint; Plan chỉ nhận design đã pass |
| `design_quality_report_v<N>.md` | Artifact writer từ kết quả validator | Bản tóm tắt dễ đọc cho người review | Design checkpoint và sync Jira/Confluence |
| `traceability_<class|sequence>_v<N>_attempt<M>_raw.txt` | Mỗi lần gọi LLM traceability | Raw response trước parse | Chỉ debug khi parse/retry có vấn đề |
| `traceability_<class|sequence>_v<N>_attempt<M>_metadata.json` | Traceability attempt recorder | finish reason, token usage, parse status, error và ref đến raw response | Retry diagnostics và audit; normalized graph/trace mới là đầu vào nghiệp vụ |

`design_consensus_experiment.json/.md`, `experiment_report.json/.md`,
`validation_cycle_v<N>.json`, `class_graph_v<N>.json`,
`sequence_graph_v<N>.json` và `traceability_v<N>.json` chỉ xuất hiện ở luồng
thử nghiệm Design Validator. Chúng không thay thế artifact production ở bảng trên.

Run có `repo_discovery_contract_version=repo_landscape_v1` phải hoàn tất L2 trước
khi Designer chạy. L2 chỉ dùng index `.codegraph/` đã tồn tại và không tự khởi tạo
hay sửa target repo; khi CodeGraph không sẵn sàng, bounded profile scan vẫn tạo
artifact và ghi uncertainty. Lỗi môi trường retry được dừng ở
`repo_discovery_paused`; lỗi identity, path, artifact hoặc conformance dừng đóng ở
`repo_discovery_blocked`.

## 8. Artifact Phase 3 - Implementation Plan

| File/pattern | Nguồn sinh | Nội dung và mục đích | Đầu vào cho bước sau |
| --- | --- | --- | --- |
| `implementation_plan_v<N>.md` | Developer Planner Agent từ Requirement đã duyệt, Design đã pass và repo context | Kế hoạch thay đổi file, thứ tự triển khai và test ở dạng người đọc | Implementation Plan checkpoint, Jira/Confluence và Developer Phase 4 |
| `implementation_plan_structured_v<N>.json` | Planner parser/normalizer | Plan máy đọc được: execution unit, file mục tiêu, mapping design/requirement và test intent | Plan Validator; sau khi duyệt dùng để tạo Developer Work Packet |
| `implementation_plan_review_v<N>.json` | Hybrid Plan Validator | Rule/LLM review, blocker, warning, coverage và repair history | Quyết định Planner repair hoặc mở Implementation Plan Approval Checkpoint |

Repo discovery ở phase này đọc worktree đã freeze, profile snapshot gắn với
workspace và CodeGraph/source files. Kết quả cần thiết được đóng vào structured
plan; không coi canonical checkout bên ngoài worktree là nguồn sự thật.

## 9. Artifact Phase 4 - Code, Verification và QA

| File/pattern | Nguồn sinh | Nội dung và mục đích | Đầu vào cho bước sau |
| --- | --- | --- | --- |
| `run_workspace_v1.json` | Workspace Manager; run L2 mới tạo ngay sau Requirement Approval, run legacy có thể tạo muộn hơn | Identity worktree, branch, base/head, target profile snapshot và lifecycle cleanup | Mọi thao tác Repo Discovery, Developer, Verification, Validator, QA và GitHub; local path không công khai qua API |
| `effective_design_contract_v<N>.json` | Harness sau khi plan được duyệt | Design contract hiệu lực sau các design adjustment hợp lệ trong plan | Developer Work Packet và Code Trace Validator |
| `developer_work_packet_v<N>.json` | Harness từ structured plan đã duyệt + effective design + workspace/profile | Phạm vi file, execution unit, test intent, mapping và policy mà Developer được phép thực hiện | Developer tool-loop, Verification Runner, Code Trace và QA context |
| `developer_<action>_raw_response_v<N>.txt` | Developer Agent call | Raw structured response cho `act` hoặc repair | Debug parse/convergence; không phải artifact checkpoint |
| `developer_<action>_llm_metadata_v<N>.json` | Developer Agent call | model, finish reason, usage và metadata lần gọi | Chẩn đoán budget/retry và audit |
| `code_change_report_v<N>.md` | Harness sau Developer tool-loop | File đã đổi, hash/scope và tóm tắt implementation | Verification, Code Trace, QA, PR body và external sync |
| `test_report_v<N>.md` | Verification Runner | Kết quả các stage setup/build/unit test, command, exit code, stdout/stderr và file test đã xác minh | Code Trace Validator, repair decision, QA và PR evidence |
| `requirement_design_code_trace_v<N>.json` | Code Trace Validator | Mapping Requirement -> Design -> code/test cùng missing links/blocker | Implementation repair gate; chỉ khi pass mới sang QA |
| `implementation_validation_ledger_v<N>.json` | Code contract/trace validation | Ledger ổn định qua nhiều vòng validation để theo dõi finding và evidence | Lần revalidate/repair kế tiếp và quyết định hội tụ |
| `implementation_repair_feedback_v<N>.json` | Harness từ test failure hoặc Code Trace blocker | Feedback máy đọc được, phạm vi liên quan và yêu cầu sửa | Developer implementation repair tool-loop |
| `qa_generated_tests_v<N>.json` | QA/Tester Agent | Danh sách acceptance/system test được sinh, nội dung và requirement refs | Harness áp test vào worktree và Verification Runner chạy đúng các file đó |
| `qa_test_execution_v<N>.md` | Verification Runner sau khi áp QA tests | Bằng chứng test QA thực sự được chạy: selected/verified files và kết quả stage | QA/Tester đánh giá cuối, repair decision và PR evidence |
| `qa_test_report_v<N>.md` | QA/Tester Agent | Kết luận từng acceptance scenario, evidence, risk và coverage | QA gate; khi pass dùng để publish Draft PR và sync |
| `qa_tester_raw_response_v<N>.txt` | QA/Tester khi cần lưu raw output | Nội dung LLM gốc, đặc biệt hữu ích khi parse lỗi/cắt output | Chỉ debug và retry diagnostics |
| `qa_repair_feedback_v<N>.json` | Harness khi QA fail | Scenario thất bại, test liên quan và repair scope | Developer QA-repair tool-loop rồi chạy lại verification/QA |

Mã nguồn và test thật mà Developer/QA tạo **không nằm trong `.runtime`**. Chúng
nằm trong Git worktree riêng của run; runtime chỉ giữ identity, packet, journal,
report và evidence để điều phối an toàn.

## 10. `developer_execution/` - checkpoint của Developer tool-loop

```text
runs/<run_id>/developer_execution/
  act_v1/
  implementation_repair_v<N>/
  qa_repair_v<N>/
    manifest.json
    journal.json
    snapshots/<execution_unit_id>.json
    actions/<item>_a<attempt>_<index>.txt
    actions/<item>_a<attempt>_<index>.json
```

| File | Nguồn sinh | Mục đích | Đầu vào cho |
| --- | --- | --- | --- |
| `manifest.json` | Developer Tool Loop Executor từ Work Packet và repair feedback | Khóa input hash, execution unit, path được phép, context path và scope | Mọi tool action, kiểm tra input drift và resume session |
| `journal.json` | Executor cập nhật sau từng unit/action | Trạng thái pending/running/completed, attempt history, hash trước/sau và mutated paths | Resume/retry, phát hiện phần đã hoàn tất và tạo Code Change Report |
| `snapshots/<id>.json` | Executor trước khi sửa một execution unit | Nội dung/hash file trước thay đổi | Rollback unit nếu action lỗi hoặc tiến trình bị ngắt |
| `actions/*.txt` | Executor sau mỗi LLM turn | Raw response của Developer cho đúng item/attempt/index | Debug tool-loop |
| `actions/*.json` | Executor cùng lúc với file `.txt` | Metadata parse/tool action của turn tương ứng | Audit và chẩn đoán retry |

`manifest_file_items_v1.json` và `journal_file_items_v1.json` chỉ là bản sao
migration khi đọc session theo schema cũ; session mới dùng `manifest.json` và
`journal.json`.

## 11. `sync/` - đồng bộ công cụ ngoài và GitHub

| File/pattern | Nguồn sinh | Mục đích | Đầu vào cho |
| --- | --- | --- | --- |
| `tool_sync_report_v1.json` | Requirement sync adapter | Kết quả đồng bộ Requirement checkpoint | `last_sync`, retry `sync_run` và status cho Niko |
| `tool_sync_report_v2.json` | Design sync adapter | Kết quả đồng bộ Design checkpoint | `last_sync`, retry và Design status |
| `tool_sync_report_v3.json` | Implementation Plan sync adapter | Kết quả đồng bộ Plan checkpoint | `last_sync`, retry và Plan status |
| `tool_sync_report_v4.json` | Code Review sync adapter | Kết quả đồng bộ PR/test/trace/QA evidence | `last_sync`, retry và Code Review status |
| `jira-comment.md`, `confluence-page.md` | Mock Requirement sync | Nội dung sẽ gửi Jira/Confluence nhưng chỉ ghi local | Test/demo mock sync; không phải bằng chứng remote |
| `design-jira-comment.md`, `design-confluence-page.md` | Mock Design sync | Payload dễ đọc của Design checkpoint | Test/demo mock sync |
| `implementation-plan-jira-comment.md`, `implementation-plan-confluence-page.md` | Mock Plan sync | Payload dễ đọc của Plan checkpoint | Test/demo mock sync |
| `code-review-jira-comment.md`, `code-review-confluence-page.md` | Mock Code Review sync | Payload PR/test/trace/QA | Test/demo mock sync |
| `github_pr_body_v<N>.md` | Harness từ các artifact Phase 4 đã pass | Nội dung truyền cho `gh pr create/edit --body-file` | GitHub publisher |
| `github_code_review_v<N>.json` | GitHub publisher | Evidence commit, push, Draft PR, branch/base và publication paths | Code Review checkpoint, retry publish, sync Jira/Confluence và workspace lifecycle |
| `github_code_review_approval_v<N>.json` | Code Review approval action | Audit Draft -> Ready for review; không merge | Sync trạng thái approval và `get_status` |

Ở `sync_mode=real`, nội dung được gửi qua adapter thật và report JSON giữ kết
quả/remote identity. Các file Markdown payload chủ yếu xuất hiện ở `mock` mode.
Nếu approval sync lỗi, retry `sync_run`; không gọi lại approval GitHub để tránh
lặp side effect.

## 12. File tạm và file ngoài `.runtime`

- File dạng `.<tên>.<uuid>.tmp` có thể tồn tại rất ngắn trong lúc ghi atomic.
  Sau khi replace thành công hoặc gặp lỗi, writer cố xóa file tạm.
- `.codegraph/` nằm trong run worktree/target repo, không thuộc `.runtime`.
- `node_modules/.sdlc-harness-dependency-setup.json` và
  `node_modules/.sdlc-harness-generated-source-setup.json` là marker của
  Verification Runner trong worktree, không phải runtime artifact.
- Mã nguồn, test, Git commit và branch nằm trong run worktree/Git; `.runtime`
  chỉ giữ tham chiếu và bằng chứng điều phối.

## 13. Dọn dẹp và phục hồi

- Giữ run đang có checkpoint chờ người duyệt, operation `queued/running`, Draft
  PR chưa xử lý hoặc workspace chưa cleanup.
- Không xóa riêng `state.json` hay artifact khỏi một run còn hoạt động vì các
  action sau dựa vào ref và hash trong đó.
- Khi xóa một run hoàn chỉnh, cần xóa cả entry tương ứng trong
  `index/active_runs.json` và lock theo run. Việc dọn worktree phải đi qua action
  `cleanup-run-workspace`, không xóa thư mục worktree bằng tay.
- Operation chưa hoàn tất khi API restart trở thành `interrupted`; client phải
  kiểm tra operation/run trước khi quyết định retry với cùng idempotency key.
"""


def ensure_runtime_readme(runtime_root: Path) -> None:
    """Create or update README atomically, without rewriting identical content."""

    path = runtime_root / "README.md"
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with _README_LOCK:
            runtime_root.mkdir(parents=True, exist_ok=True)
            if _readme_is_current(path):
                return
            temp_path.write_text(RUNTIME_README, encoding="utf-8")
            for attempt in range(20):
                try:
                    temp_path.replace(path)
                    break
                except PermissionError:
                    if _readme_is_current(path):
                        return
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
    except OSError as exc:
        raise HarnessError("runtime_write_failed", "Runtime README could not be written.") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _readme_is_current(path: Path) -> bool:
    try:
        return path.is_file() and path.read_text(encoding="utf-8") == RUNTIME_README
    except OSError:
        return False
