"""Lưu trạng thái workflow Phase 1 bằng file JSON.

Runtime store cố tình đơn giản: mỗi run có folder riêng, state nằm trong
`state.json`, còn index active run dùng để `start-or-get-run` idempotent theo
source_ref. Khi cần scale mới chuyển sang SQLite/API service.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from ..core.errors import HarnessError

TERMINAL_STATUSES = {"done", "cancelled"}


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

    def write_state(self, state: dict[str, object]) -> None:
        run_id = str(state["run_id"])
        run_dir = self.run_dir(run_id)
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError("runtime_write_failed", str(exc), run_id=run_id) from exc
        self._write_json(self.state_path(run_id), state)

    def _write_json(self, path: Path, data: dict[str, object]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise HarnessError("runtime_write_failed", str(exc)) from exc


def utc_now_iso() -> str:
    """Timestamp UTC ổn định cho state và trace."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

