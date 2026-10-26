"""Workflow L2 khóa workspace và phát hành repo_landscape_v1 trước Design."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..core.config import Settings
from ..core.errors import HarnessError
from ..profiles import ProfileSnapshotV1
from ..repo_context import CodeGraphContextProvider, build_repo_landscape, validate_repo_landscape
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..workspaces import RunWorkspaceManager


REPO_DISCOVERY_CONTRACT_STATE_KEY = "repo_discovery_contract_version"
REPO_DISCOVERY_CONTRACT_VERSION = "repo_landscape_v1"
REPO_LANDSCAPE_FILENAME = "repo_landscape_v1.json"
_RETRYABLE_ERRORS = {
    "run_workspace_locked",
    "workspace_fetch_failed",
    "workspace_creation_failed",
    "workspace_lock_failed",
    "target_repo_codegraph_failed",
}


class RepoDiscoveryWorkflow:
    """Owner của state/artifact L2; không gọi LLM và không sửa source target."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.workspace_manager = RunWorkspaceManager(settings)
        self.codegraph = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")

    def run(
        self,
        *,
        run_id: str,
        requirement_model: dict[str, object],
    ) -> dict[str, object] | None:
        """Tạo/reuse workspace và landscape; trả None khi state đã pause/block."""

        state = self.store.read_state(run_id)
        if str(state.get(REPO_DISCOVERY_CONTRACT_STATE_KEY) or "") != REPO_DISCOVERY_CONTRACT_VERSION:
            raise HarnessError(
                "repo_discovery_contract_missing",
                "Run does not opt into the L2 repo discovery contract.",
                run_id=run_id,
            )
        existing = _dict_value(state.get("latest_artifacts")).get("repo_landscape")
        if isinstance(existing, str) and existing.strip():
            path = self._artifact_path(existing, run_id=run_id)
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessError(
                    "repo_landscape_invalid",
                    "Repo landscape artifact is not valid JSON.",
                    run_id=run_id,
                ) from exc
            workspace = self.workspace_manager.resolve(run_id)
            snapshot = ProfileSnapshotV1.from_dict(state.get("profile_snapshot"), run_id=run_id)
            payload = validate_repo_landscape(
                stored,
                expected_run_id=run_id,
                expected_snapshot={
                    "workspace_ref": "run_workspace_v1",
                    "base_sha": workspace.base_sha,
                    "head_sha": workspace.head_sha,
                    "project_profile_id": snapshot.project_profile_id,
                    "project_profile_schema": snapshot.project_profile_schema,
                    "language_profile_id": snapshot.language_profile_id,
                    "language_profile_schema": snapshot.language_profile_schema,
                    "target_manifest_digest": snapshot.target_manifest_digest,
                },
            )
            return payload

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "repo_discovery",
                "status": "repo_discovery",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status"],
                "last_error": None,
                "summary_for_user": "Đã duyệt Requirement, đang khóa workspace và khảo sát codebase AS-IS.",
            }
        )
        self.store.write_state(state)
        try:
            workspace = self.workspace_manager.create_for_run(run_id)
            state = self.store.read_state(run_id)
            snapshot = ProfileSnapshotV1.from_dict(state.get("profile_snapshot"), run_id=run_id)
            query: Callable[[Path, str], str] | None = None
            if (Path(workspace.worktree_path) / ".codegraph").is_dir():
                query = self.codegraph.query_readonly
            payload = build_repo_landscape(
                run_id=run_id,
                target_path=Path(workspace.worktree_path),
                workspace=workspace,
                snapshot=snapshot,
                requirement_model=requirement_model,
                codegraph_query=query,
            )
            path = self.store.write_json_artifact(run_id, REPO_LANDSCAPE_FILENAME, payload)
            state = self.store.read_state(run_id)
            artifacts = _dict_value(state.get("latest_artifacts"))
            quality = _dict_value(state.get("quality_reports"))
            summary = _dict_value(payload.get("summary"))
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": {**artifacts, "repo_landscape": self.store.relative(path)},
                    "quality_reports": {
                        **quality,
                        "repo_discovery": {
                            "status": "passed" if int(summary.get("blocking_count") or 0) == 0 else "blocked",
                            "artifact": self.store.relative(path),
                            "mode": _dict_value(payload.get("discovery")).get("mode"),
                            **summary,
                        },
                    },
                }
            )
            if int(summary.get("blocking_count") or 0):
                error = HarnessError(
                    "repo_discovery_conformance_blocked",
                    "Repo landscape contains profile conformance blockers.",
                    run_id=run_id,
                )
                state.update(
                    {
                        "status": "repo_discovery_blocked",
                        "allowed_next_actions": ["get_status"],
                        "last_error": error.to_response(),
                        "summary_for_user": "Repo Discovery bị chặn bởi profile conformance findings.",
                    }
                )
                self.store.write_state(state)
                return None
            self.store.write_state(state)
            return payload
        except HarnessError as exc:
            state = self.store.read_state(run_id)
            retryable = exc.error_code in _RETRYABLE_ERRORS
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "repo_discovery",
                    "status": "repo_discovery_paused" if retryable else "repo_discovery_blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["continue_run", "get_status"] if retryable else ["get_status"],
                    "last_error": exc.to_response(),
                    "summary_for_user": (
                        f"Repo Discovery tạm dừng và có thể tiếp tục: {exc.message}"
                        if retryable
                        else f"Repo Discovery bị chặn: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return None

    def _artifact_path(self, reference: str, *, run_id: str) -> Path:
        path = Path(reference)
        resolved = path.resolve() if path.is_absolute() else (self.store.repo_root / path).resolve()
        run_root = self.store.run_dir(run_id).resolve()
        try:
            resolved.relative_to(run_root)
        except ValueError as exc:
            raise HarnessError(
                "repo_landscape_invalid",
                "Repo landscape artifact escapes its run directory.",
                run_id=run_id,
            ) from exc
        if not resolved.is_file():
            raise HarnessError("repo_landscape_invalid", "Repo landscape artifact is missing.", run_id=run_id)
        return resolved


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}
