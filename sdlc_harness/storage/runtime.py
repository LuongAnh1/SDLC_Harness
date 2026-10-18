"""Lưu trạng thái và JSON artifact của workflow bằng file local.

Runtime store cố tình đơn giản: mỗi run có folder riêng, state nằm trong
`state.json`, artifact nằm trong `artifacts/`, còn index active run dùng để
`start-or-get-run` idempotent theo source_ref. Các lần ghi JSON dùng temp file và
atomic replace; khi cần scale mới chuyển sang SQLite/API service.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..core.errors import HarnessError
from .locks import RuntimeActionLock
from .runtime_readme import ensure_runtime_readme

TERMINAL_STATUSES = {"done", "cancelled", "failed", "code_review_approved"}


class RuntimeStore:
    """Quản lý `.runtime` cho workflow state và active-run index."""

    def __init__(self, *, repo_root: Path, runtime_root: Path):
        self.repo_root = repo_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.runs_dir = self.runtime_root / "runs"
        self.index_dir = self.runtime_root / "index"
        self.active_runs_path = self.index_dir / "active_runs.json"

    def ensure_layout(self) -> None:
        """Tạo folder runtime tối thiểu."""

        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            self.index_dir.mkdir(parents=True, exist_ok=True)
            ensure_runtime_readme(self.runtime_root)
        except OSError as exc:
            raise HarnessError("runtime_write_failed", str(exc)) from exc

    def new_run_id(self) -> str:
        """Tạo run id đọc được bằng mắt và đủ tránh đụng nhau."""

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"run_{stamp}_{uuid4().hex[:8]}"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state.json"

    def run_lock(self, run_id: str) -> RuntimeActionLock:
        """Serialize state/artifact mutations belonging to one workflow run."""

        return RuntimeActionLock(self.runtime_root / "locks" / f"run-{run_id}.lock", run_id=run_id)

    def source_lock(self, source_ref: str) -> RuntimeActionLock:
        """Serialize creation/intake for one canonical source across CLI and API."""

        digest = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()
        return RuntimeActionLock(
            self.runtime_root / "locks" / f"source-{digest}.lock",
            run_id=None,
            error_code="source_action_locked",
            locked_message=f"Another action is already creating a run for source {source_ref}.",
        )

    def artifact_path(self, run_id: str, filename: str) -> Path:
        """Trả đường dẫn artifact cố định và chặn filename thoát thư mục run."""

        if not filename or Path(filename).name != filename:
            raise HarnessError("runtime_write_failed", f"Invalid artifact filename: {filename}", run_id=run_id)
        return self.run_dir(run_id) / "artifacts" / filename

    def relative(self, path: Path) -> str:
        """Đường dẫn relative để JSON không lộ path tuyệt đối của máy anh."""

        try:
            return str(path.resolve().relative_to(self.repo_root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def read_active_runs(self) -> dict[str, str]:
        self.ensure_layout()
        if not self.active_runs_path.exists():
            return {}
        try:
            return json.loads(self.active_runs_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HarnessError("state_corrupted", "active_runs.json is not valid JSON") from exc

    def write_active_runs(self, active_runs: dict[str, str]) -> None:
        self.ensure_layout()
        self._write_json(self.active_runs_path, active_runs)

    def find_active_run(self, source_ref: str) -> dict[str, object] | None:
        """Tìm run chưa terminal cho source_ref, nếu có."""

        run_id = self.read_active_runs().get(source_ref)
        if not run_id:
            return None
        state_path = self.state_path(run_id)
        if not state_path.exists():
            return None
        state = self.read_state(run_id)
        if str(state.get("status")) in TERMINAL_STATUSES:
            return None
        return state

    def register_active_run(self, source_ref: str, run_id: str) -> None:
        active_runs = self.read_active_runs()
        active_runs[source_ref] = run_id
        self.write_active_runs(active_runs)

    def read_state(self, run_id: str) -> dict[str, object]:
        path = self.state_path(run_id)
        if not path.exists():
            raise HarnessError("run_not_found", f"Run not found: {run_id}", run_id=run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HarnessError("state_corrupted", f"State file is not valid JSON: {run_id}", run_id=run_id) from exc

    def list_states(self, *, limit: int) -> list[dict[str, object]]:
        """Read recent run states without creating or mutating runtime storage."""

        if limit < 1:
            raise HarnessError("invalid_query", "Run list limit must be at least 1.")
        if not self.runs_dir.exists():
            return []
        states: list[dict[str, object]] = []
        for entry in self.runs_dir.iterdir():
            if not entry.is_dir() or not (entry / "state.json").is_file():
                continue
            states.append(self.read_state(entry.name))
        states.sort(
            key=lambda state: (str(state.get("updated_at") or ""), str(state.get("run_id") or "")),
            reverse=True,
        )
        return states[:limit]

    def write_state(self, state: dict[str, object]) -> None:
        run_id = str(state["run_id"])
        run_dir = self.run_dir(run_id)
        previous: dict[str, object] | None = None
        if self.state_path(run_id).is_file():
            previous = self.read_state(run_id)
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError("runtime_write_failed", str(exc), run_id=run_id) from exc
        self._write_json(self.state_path(run_id), state)
        self._record_state_events(state=state, previous=previous)

    def _record_state_events(
        self,
        *,
        state: dict[str, object],
        previous: dict[str, object] | None,
    ) -> None:
        """Best-effort observability after state is durable."""

        from ..events import bind_current_run, current_event_context, emit_current, emit_for_run, update_current_state

        run_id = str(state["run_id"])
        phase = str(state.get("current_phase") or "") or None
        status = str(state.get("status") or "") or None
        bind_current_run(run_id)
        update_current_state(phase=phase, status=status)

        def emit(
            event_type: str,
            message: str,
            data: dict[str, object],
            *,
            category: str = "workflow",
        ) -> None:
            if current_event_context() is not None:
                emit_current(
                    category=category,
                    event_type=event_type,
                    message=message,
                    data=data,
                    phase=phase,
                    status=status,
                )
            else:
                emit_for_run(
                    runtime_root=self.runtime_root,
                    run_id=run_id,
                    category=category,
                    event_type=event_type,
                    message=message,
                    data=data,
                    phase=phase,
                    status=status,
                )

        if previous is None:
            emit(
                "workflow_created",
                "Workflow run created.",
                {"current_phase": phase or "", "current_status": status or ""},
            )
        else:
            previous_phase = str(previous.get("current_phase") or "") or None
            previous_status = str(previous.get("status") or "") or None
            if (previous_phase, previous_status) != (phase, status):
                emit(
                    "workflow_state_changed",
                    "Workflow state changed.",
                    {
                        "previous_phase": previous_phase or "",
                        "current_phase": phase or "",
                        "previous_status": previous_status or "",
                        "current_status": status or "",
                    },
                )
        previous_checkpoint = previous.get("pending_checkpoint") if previous else None
        current_checkpoint = state.get("pending_checkpoint")
        if not previous_checkpoint and isinstance(current_checkpoint, dict):
            emit(
                "checkpoint_opened",
                "Checkpoint opened.",
                {
                    "checkpoint_id": str(current_checkpoint.get("checkpoint_id") or current_checkpoint.get("id") or ""),
                    "checkpoint_type": str(current_checkpoint.get("type") or current_checkpoint.get("name") or phase or ""),
                },
            )
        elif isinstance(previous_checkpoint, dict) and not current_checkpoint:
            emit(
                "checkpoint_approved",
                "Checkpoint approved.",
                {
                    "checkpoint_id": str(previous_checkpoint.get("checkpoint_id") or previous_checkpoint.get("id") or ""),
                    "checkpoint_type": str(previous_checkpoint.get("type") or previous_checkpoint.get("name") or previous.get("current_phase") or ""),
                },
            )

        previous_status = str(previous.get("status") or "") if previous else ""
        repair_states = {
            "design_repair_needed": ("design", "design_repair_iterations"),
            "implementation_plan_repair_needed": ("implementation_plan", "implementation_plan_repair_iterations"),
            "implementation_repair_needed": ("implementation", "implementation_repair_iterations"),
            "qa_repair_needed": ("qa", "qa_repair_iterations"),
        }
        if previous_status in repair_states and previous_status != status:
            repair_phase, counter_key = repair_states[previous_status]
            iteration = int(previous.get(counter_key) or 0) + 1 if previous else 1
            emit(
                "repair_iteration_started",
                "Repair iteration started.",
                {"repair_phase": repair_phase, "iteration": iteration},
                category="repair",
            )
        for repair_phase, counter_key in {
            "design": "design_repair_iterations",
            "implementation_plan": "implementation_plan_repair_iterations",
            "implementation": "implementation_repair_iterations",
            "qa": "qa_repair_iterations",
        }.items():
            before = int(previous.get(counter_key) or 0) if previous else 0
            after = int(state.get(counter_key) or 0)
            if after > before:
                emit(
                    "repair_iteration_completed",
                    "Repair iteration completed.",
                    {"repair_phase": repair_phase, "iteration": after, "result": status or "unknown"},
                    category="repair",
                )

        previous_quality = previous.get("quality_reports") if previous else {}
        current_quality = state.get("quality_reports")
        previous_quality = previous_quality if isinstance(previous_quality, dict) else {}
        current_quality = current_quality if isinstance(current_quality, dict) else {}
        validation_keys = {
            "design_validation": "design",
            "implementation_plan_review": "implementation_plan",
            "implementation_validation": "code_trace",
            "code_trace": "code_trace",
        }
        for quality_key, validator_name in validation_keys.items():
            report = current_quality.get(quality_key)
            if not isinstance(report, dict) or report == previous_quality.get(quality_key):
                continue
            blockers_value = report.get("blocking_count", report.get("issue_count", report.get("missing_count", 0)))
            warnings_value = report.get("warning_count", 0)
            emit(
                "validator_completed",
                "Validator completed.",
                {
                    "validator": validator_name,
                    "status": str(report.get("status") or ("passed" if report.get("ok") else "failed")),
                    "blockers": int(blockers_value or 0),
                    "warnings": int(warnings_value or 0),
                    "repair_iterations": int(report.get("repair_iterations") or 0),
                },
                category="validation",
            )
        qa_report = current_quality.get("qa_acceptance")
        if isinstance(qa_report, dict) and qa_report != previous_quality.get("qa_acceptance"):
            scenarios = qa_report.get("scenarios")
            scenario_items = scenarios if isinstance(scenarios, list) else []
            passed = sum(1 for item in scenario_items if isinstance(item, dict) and str(item.get("result") or "").lower() == "pass")
            failed = sum(1 for item in scenario_items if isinstance(item, dict) and str(item.get("result") or "").lower() == "fail")
            emit(
                "qa_completed",
                "QA completed.",
                {
                    "status": str(qa_report.get("status") or qa_report.get("result") or "unknown"),
                    "scenario_count": int(qa_report.get("scenario_count") or len(scenario_items)),
                    "passed_count": passed,
                    "failed_count": failed,
                    "repair_iterations": int(state.get("qa_repair_iterations") or 0),
                },
                category="qa",
            )

    def write_json_artifact(self, run_id: str, filename: str, data: dict[str, object]) -> Path:
        """Ghi JSON artifact bằng cùng cơ chế atomic với workflow state."""

        path = self.artifact_path(run_id, filename)
        self._write_json(path, data)
        return path

    def _write_json(self, path: Path, data: dict[str, object]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            raise HarnessError("runtime_write_failed", str(exc)) from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def utc_now_iso() -> str:
    """Timestamp UTC ổn định cho state và trace."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

