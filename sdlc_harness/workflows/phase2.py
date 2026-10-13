"""Workflow Phase 2: approved requirement -> design checkpoint.

Phase 2 nhận requirement đã duyệt, gọi Designer để sinh PlantUML, parse class/
sequence graph, chạy Validator traceability và repair design khi còn missing
link. Workflow chỉ mở checkpoint design khi traceability pass hoặc chuyển
`blocked` nếu hết vòng repair mà vẫn thiếu link.
"""

from __future__ import annotations

from ..agents.designer import DesignerAgent, parse_design_model
from ..agents.validator import ValidatorAgent
from ..artifacts.writers import write_phase2_design_artifacts
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint

ALLOWED_DESIGN_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseTwoWorkflow:
    """Điều phối Phase 2 từ checkpoint requirement sang checkpoint design."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.designer = DesignerAgent(LlmClient(settings))
        self.validator = ValidatorAgent(LlmClient(settings))

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Duyệt requirement checkpoint hiện tại và tạo design checkpoint."""

        state = self.store.read_state(run_id)
        self._require_requirement_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        requirement_path = artifact_paths.get("requirement_packet", "")
        if not requirement_path:
            raise HarnessError(
                "approved_requirement_not_found",
                f"Run does not have an approved requirement artifact: {run_id}",
                run_id=run_id,
            )
        requirement_abs_path = (self.store.repo_root / requirement_path).resolve()
        if not requirement_abs_path.exists():
            raise HarnessError(
                "approved_requirement_not_found",
                f"Requirement artifact does not exist: {requirement_path}",
                run_id=run_id,
            )

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_drafting",
                "status": "design_drafting",
                "approved_artifacts": {
                    "requirement_packet": requirement_path,
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "summary_for_user": "Đã duyệt requirement checkpoint, đang tạo design draft.",
            }
        )
        self.store.write_state(state)

        requirement_packet = requirement_abs_path.read_text(encoding="utf-8")
        formal_use_case_path = artifact_paths.get("formal_use_case", "")
        formal_use_case = ""
        if formal_use_case_path:
            formal_use_case_abs_path = (self.store.repo_root / formal_use_case_path).resolve()
            if formal_use_case_abs_path.exists():
                formal_use_case = formal_use_case_abs_path.read_text(encoding="utf-8")
                state["approved_artifacts"] = {
                    **_string_dict(state.get("approved_artifacts")),
                    "formal_use_case": formal_use_case_path,
                }
                self.store.write_state(state)

        try:
            draft = self.designer.draft(
                requirement_packet=requirement_packet,
                formal_use_case=formal_use_case,
            )
            design_model = draft.design_model
            versions: list[str] = []
            design_paths: dict[str, str] = {}
            merged_artifacts = dict(artifact_paths)
            traceability = None
            previous_quality_report = ""
            repair_limit = self.settings.design_repair_limit

            for repair_iteration in range(repair_limit + 1):
                version = repair_iteration + 1
                if repair_iteration == 0:
                    state.update(
                        {
                            "updated_at": utc_now_iso(),
                            "current_phase": "design_validating",
                            "status": "design_validating",
                            "summary_for_user": (
                                "Designer đã sinh design draft, đang parse PlantUML "
                                "và chạy traceability."
                            ),
                        }
                    )
                else:
                    state.update(
                        {
                            "updated_at": utc_now_iso(),
                            "current_phase": "design_repairing",
                            "status": "design_repairing",
                            "summary_for_user": (
                                f"Validator còn missing link, Designer đang repair design "
                                f"lần {repair_iteration}/{repair_limit}."
                            ),
                        }
                    )
                self.store.write_state(state)

                parsed = parse_design_model(design_model)
                traceability = self.validator.construct_traceability(
                    requirement_packet=requirement_packet,
                    class_graph=parsed.class_graph,
                    sequence_graph=parsed.sequence_graph,
                )
                design_paths = write_phase2_design_artifacts(
                    store=self.store,
                    run_id=run_id,
                    designer_analysis=_analysis_for_version(
                        base_analysis=draft.analysis_report,
                        version=version,
                        previous_quality_report=previous_quality_report,
                    ),
                    design_model=design_model,
                    requirement_graph=traceability.requirement_graph,
                    class_graph=parsed.class_graph,
                    sequence_graph=parsed.sequence_graph,
                    class_trace=traceability.class_trace,
                    sequence_trace=traceability.sequence_trace,
                    missing_links=traceability.missing_links,
                    requirement_packet=requirement_packet,
                    formal_use_case=formal_use_case,
                    version=version,
                    repair_iterations=repair_iteration,
                )
                versions.append(f"v{version}")
                merged_artifacts = {**artifact_paths, **design_paths}
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "latest_artifacts": merged_artifacts,
                        "quality_reports": {
                            **_dict_value(state.get("quality_reports")),
                            "design_traceability": {
                                "ok": traceability.ok,
                                "mode": "traceability_v1",
                                "versions": versions,
                                "summary": _traceability_summary(traceability.missing_links),
                                "report": design_paths["design_quality_report"],
                            },
                        },
                    }
                )
                self.store.write_state(state)
                if traceability.ok:
                    break
                if repair_iteration >= repair_limit:
                    break

                previous_quality_report = design_paths["design_quality_report"]
                audit_report_path = (self.store.repo_root / previous_quality_report).resolve()
                audit_report = audit_report_path.read_text(encoding="utf-8")
                design_model = self.designer.update_design(
                    requirement_packet=requirement_packet,
                    formal_use_case=formal_use_case,
                    current_design=design_model,
                    audit_report=audit_report,
                )
        except HarnessError as exc:
            requirement_error = _requirement_blocking_error(exc, run_id=run_id)
            if requirement_error is not None:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "design_review",
                        "status": "blocked",
                        "pending_checkpoint": None,
                        "allowed_next_actions": ["get_status", "request_change", "cancel_run"],
                        "last_error": requirement_error.to_response(),
                        "summary_for_user": (
                            "Phase 2 bị chặn vì requirement chưa đủ rõ để tạo traceability graph: "
                            f"{exc.message}"
                        ),
                    }
                )
                self.store.write_state(state)
                return self._status_response(state, created_new_run=False)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": str(state.get("current_phase") or "design_validating"),
                    "status": "failed",
                    "last_error": exc.to_response(),
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "summary_for_user": f"Phase 2 lỗi khi tạo/parse design: {exc.message}",
                }
            )
            self.store.write_state(state)
            if not exc.run_id:
                raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
            raise

        if traceability is None:
            raise HarnessError("designer_failed", "Phase 2 did not produce a design traceability result.", run_id=run_id)

        if not traceability.ok:
            repair_limit = self.settings.design_repair_limit
            blocked_error = HarnessError(
                "design_traceability_blocked",
                (
                    f"Design still has missing requirement links after "
                    f"{repair_limit} repair iteration(s)."
                ),
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": blocked_error.to_response(),
                    "summary_for_user": (
                        f"Phase 2 bị chặn sau {repair_limit} vòng repair. "
                        f"Validator vẫn {_traceability_summary(traceability.missing_links)}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref", "")),
            artifact_paths=merged_artifacts,
        )

        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": _artifact_ref(design_paths["design_artifact"]),
                    "artifact_path": design_paths["design_artifact"],
                },
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_sync": last_sync,
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "design_traceability": {
                        "ok": traceability.ok,
                        "mode": "traceability_v1",
                        "versions": versions,
                        "summary": _traceability_summary(traceability.missing_links),
                        "report": design_paths["design_quality_report"],
                    },
                },
                "summary_for_user": (
                    f"Đã duyệt requirement và tạo design checkpoint {versions[-1].upper()} cho run {run_id}. "
                    f"Validator traceability đã chạy xong, "
                    f"{_traceability_summary(traceability.missing_links)} "
                    f"sync status={last_sync.get('status')}. "
                    "Run đang chờ anh review design trước khi sang Phase 3."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_requirement_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        """Chặn approve sai checkpoint hoặc sai trạng thái để workflow không nhảy cóc."""

        if str(state.get("status")) != "waiting_requirement_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for requirement approval: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "requirement_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not a requirement checkpoint: {run_id}",
                run_id=run_id,
            )
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError(
                "checkpoint_not_current",
                f"Checkpoint is not current: {checkpoint_id}",
                run_id=run_id,
            )

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        """Giữ response shape giống Phase 1 để Niko không phải biết nội bộ workflow."""

        return {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": state.get("allowed_next_actions", []),
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _traceability_summary(missing_links: dict[str, list[dict[str, str]]]) -> str:
    class_missing = len(missing_links.get("class_diagram", []))
    sequence_missing = len(missing_links.get("sequence_diagram", []))
    total = class_missing + sequence_missing
    if total == 0:
        return "không phát hiện missing link class/sequence."
    return f"còn {total} missing link class/sequence cần review."


def _requirement_blocking_error(exc: HarnessError, *, run_id: str) -> HarnessError | None:
    """Phân loại lỗi requirement đầu vào để Phase 2 không bị hiểu nhầm là lỗi hệ thống."""

    message = exc.message.lower()
    if exc.error_code == "validator_traceability_failed" and "traceable requirement" in message:
        return HarnessError(
            "requirement_traceability_blocked",
            exc.message,
            run_id=run_id,
        )
    return None


def _analysis_for_version(*, base_analysis: str, version: int, previous_quality_report: str) -> str:
    """Gắn ngữ cảnh repair vào analysis artifact của version mới."""

    if version == 1:
        return base_analysis
    return (
        f"{base_analysis.rstrip()}\n\n"
        "## Repair Context\n\n"
        f"- Design version: v{version}\n"
        f"- Previous quality report: `{previous_quality_report}`\n"
    )


def _artifact_ref(path: str) -> str:
    """Lấy artifact ref từ relative path trong runtime."""

    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]
