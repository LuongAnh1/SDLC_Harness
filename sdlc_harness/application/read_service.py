"""Các read model an toàn cho HTTP API và Ops Dashboard hiện tại."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from .commands import ApplicationCommand
from .service import ApplicationService
from .action_specs import public_dashboard_schema
from ..core.config import Settings, load_settings, repo_root as default_repo_root
from ..core.errors import HarnessError
from ..events import DEFAULT_EVENT_LIMIT, EventStore
from ..profiles import profile_status_metadata, public_allowed_actions, public_run_summary
from ..storage.runtime import RuntimeStore, utc_now_iso


DEFAULT_RUN_LIMIT = 50
MAX_RUN_LIMIT = 200
MAX_ARTIFACT_BYTES = 1024 * 1024
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/][^\s`\"']+)")
_SENSITIVE_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "password",
    "secret",
    "token",
    "worktree_path",
    "target_repo",
    "target_repo_path",
    "runtime_root",
    "repo_root",
}

# Fail closed: adding a new runtime artifact does not publish it automatically.
PUBLIC_ARTIFACT_KEYS = frozenset(
    {
        "use_case_packet",
        "requirement_model",
        "requirement_packet",
        "formal_use_case",
        "requirement_validation",
        "requirement_decisions",
        "requirement_clarification_policy_shadow",
        "requirement_reasonable_defaults",
        "requirement_assumption_challenge",
        "repo_landscape",
        "designer_analysis",
        "design_artifact",
        "requirement_graph",
        "class_diagram_graph",
        "sequence_diagram_graph",
        "requirement_design_trace",
        "missing_design_links",
        "design_validation_report",
        "design_quality_report",
        "implementation_plan",
        "implementation_plan_structured",
        "implementation_plan_review",
        "effective_design_contract",
        "code_change_report",
        "test_report",
        "requirement_design_code_trace",
        "implementation_validation_ledger",
        "implementation_repair_feedback",
        "qa_generated_tests",
        "qa_execution_report",
        "qa_test_report",
        "qa_repair_feedback",
        "github_pr_body",
        "github_code_review",
        "github_code_review_approval",
        "tool_sync_report",
    }
)


class ApplicationReadService:
    """Build public, path-free read projections from persisted workflow state."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        runtime_root: Path | None = None,
        application_service: ApplicationService | None = None,
    ):
        self.root = (root or default_repo_root()).resolve()
        self.settings = load_settings(root=self.root, runtime_root=runtime_root)
        self.store = RuntimeStore(repo_root=self.root, runtime_root=self.settings.runtime_root)
        self.event_store = EventStore(runtime_root=self.settings.runtime_root)
        self.application_service = application_service or ApplicationService(root=self.root)

    def list_runs(self, *, limit: int = DEFAULT_RUN_LIMIT) -> dict[str, object]:
        limit = _validated_limit(limit)
        states = self.store.list_states(limit=limit)
        return {
            "ok": True,
            "runs": [self._run_summary(state) for state in states],
            "count": len(states),
            "limit": limit,
        }

    def get_run(self, *, run_id: str) -> dict[str, object]:
        run_id = _validated_identifier(run_id, kind="run id")
        payload = self.application_service.execute(
            ApplicationCommand(
                name="get-status",
                arguments={"run_id": run_id},
                runtime_root=self.settings.runtime_root,
            )
        )
        state = self.store.read_state(run_id)
        for key in (
            "created_at",
            "updated_at",
            "implementation_repair_iterations",
            "qa_repair_iterations",
        ):
            if key not in payload and key in state:
                payload[key] = state[key]
        return self._public_run(payload)

    def list_events(
        self,
        *,
        run_id: str,
        after: int = 0,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        run_id = _validated_identifier(run_id, kind="run id")
        return self._sanitize(self.event_store.read(run_id=run_id, after=after, limit=limit))

    def dashboard_snapshot(
        self,
        *,
        limit: int = DEFAULT_RUN_LIMIT,
        operations: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        runs = self.list_runs(limit=limit)
        return {
            "ok": True,
            "generated_at": utc_now_iso(),
            "runs": runs["runs"],
            "run_count": runs["count"],
            "operations": operations or [],
            "config": {
                "api_host": self.settings.api_host,
                "api_port": self.settings.api_port,
                "auth_enabled": bool(self.settings.api_token),
            },
            "capabilities": api_capabilities(),
        }

    def dashboard_schema(self) -> dict[str, object]:
        """Return UI metadata without exposing secrets or filesystem settings."""

        settings = load_settings(root=self.root, runtime_root=self.settings.runtime_root)
        return public_dashboard_schema(settings)

    def read_artifact(self, *, run_id: str, artifact_key: str) -> dict[str, object]:
        run_id = _validated_identifier(run_id, kind="run id")
        artifact_key = _validated_identifier(artifact_key, kind="artifact key")
        state = self.store.read_state(run_id)
        artifact_path = self._resolve_public_artifact(
            state=state,
            run_id=run_id,
            artifact_key=artifact_key,
        )
        size_bytes = artifact_path.stat().st_size
        if size_bytes > MAX_ARTIFACT_BYTES:
            raise HarnessError(
                "artifact_too_large",
                f"Artifact preview exceeds the {MAX_ARTIFACT_BYTES}-byte limit.",
                run_id=run_id,
            )
        media_type = _artifact_media_type(artifact_path)
        try:
            text = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HarnessError(
                "artifact_read_failed",
                "Artifact could not be read as UTF-8 text.",
                run_id=run_id,
            ) from exc
        if media_type == "application/json":
            try:
                content: object = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HarnessError(
                    "artifact_corrupted",
                    "Published JSON artifact is not valid JSON.",
                    run_id=run_id,
                ) from exc
        else:
            content = text
        return {
            "ok": True,
            "run_id": run_id,
            "artifact_key": artifact_key,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "content": self._sanitize(content),
        }

    def public_value(self, value: Any) -> Any:
        """Sanitize API-visible operation/status data with the same read boundary."""

        if isinstance(value, dict) and "run_id" in value and "latest_artifacts" in value:
            return self._public_run(value)
        return self._sanitize(deepcopy(value))

    def _public_run(self, payload: dict[str, object]) -> dict[str, object]:
        projected = deepcopy(payload)
        run_id = str(projected.get("run_id") or "")
        try:
            state = self.store.read_state(run_id)
        except HarnessError:
            state = projected
        if "allowed_next_actions" in projected:
            projected["allowed_next_actions"] = public_allowed_actions(
                state,
                projected["allowed_next_actions"],
            )
        if "summary_for_user" in projected:
            projected["summary_for_user"] = public_run_summary(
                state,
                projected["summary_for_user"],
            )
        projected["latest_artifacts"] = self._artifact_metadata(
            run_id=run_id,
            references=projected.get("latest_artifacts"),
        )
        return self._sanitize(projected)

    def _run_summary(self, state: dict[str, object]) -> dict[str, object]:
        run_id = str(state.get("run_id") or "")
        checkpoint = state.get("pending_checkpoint")
        last_error = state.get("last_error")
        summary = {
            "run_id": run_id,
            "source_ref": state.get("source_ref"),
            "execution_profile": profile_status_metadata(state),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "pending_checkpoint": checkpoint,
            "allowed_next_actions": public_allowed_actions(
                state,
                state.get("allowed_next_actions", []),
            ),
            "last_error": last_error,
            "summary_for_user": public_run_summary(state),
            "latest_artifacts": self._artifact_metadata(
                run_id=run_id,
                references=state.get("latest_artifacts"),
            ),
        }
        return self._sanitize(summary)

    def _artifact_metadata(self, *, run_id: str, references: object) -> dict[str, object]:
        if not run_id or not isinstance(references, dict):
            return {}
        metadata: dict[str, object] = {}
        state = {"latest_artifacts": references}
        for raw_key in sorted(references):
            key = str(raw_key)
            if key not in PUBLIC_ARTIFACT_KEYS:
                continue
            try:
                path = self._resolve_public_artifact(state=state, run_id=run_id, artifact_key=key)
            except HarnessError:
                continue
            size_bytes = path.stat().st_size
            if size_bytes > MAX_ARTIFACT_BYTES:
                continue
            metadata[key] = {
                "url": f"/api/v1/runs/{quote(run_id)}/artifacts/{quote(key)}",
                "media_type": _artifact_media_type(path),
                "size_bytes": size_bytes,
            }
        return metadata

    def _resolve_public_artifact(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        artifact_key: str,
    ) -> Path:
        references = state.get("latest_artifacts")
        reference = references.get(artifact_key) if isinstance(references, dict) else None
        if artifact_key not in PUBLIC_ARTIFACT_KEYS or not isinstance(reference, str) or not reference.strip():
            raise HarnessError("artifact_not_found", "Artifact is not publicly available.", run_id=run_id)
        path = Path(reference)
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        run_dir = self.store.run_dir(run_id).resolve()
        try:
            resolved.relative_to(run_dir)
        except ValueError as exc:
            raise HarnessError("artifact_not_found", "Artifact is not publicly available.", run_id=run_id) from exc
        if not resolved.is_file():
            raise HarnessError("artifact_not_found", "Artifact is not publicly available.", run_id=run_id)
        if _artifact_media_type(resolved) not in {"application/json", "text/markdown", "text/plain"}:
            raise HarnessError("artifact_not_found", "Artifact is not publicly available.", run_id=run_id)
        return resolved

    def _sanitize(self, value: Any) -> Any:
        settings = load_settings(root=self.root, runtime_root=self.settings.runtime_root)
        secrets = tuple(
            secret
            for secret in (
                settings.api_token,
                settings.llm_api_key,
                settings.atlassian_api_token,
            )
            if secret
        )
        roots = tuple(
            str(path)
            for path in (
                self.root,
                settings.runtime_root,
                settings.target_repo_path,
                settings.target_worktree_root,
            )
        )
        return _sanitize_value(value, secrets=secrets, roots=roots)


def api_capabilities() -> dict[str, bool]:
    return {
        "read_api": True,
        "write_api": True,
        "operation_queue": True,
        "events": True,
        "dashboard": True,
        "config_api": True,
    }


def _validated_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RUN_LIMIT:
        raise HarnessError("invalid_query", f"limit must be an integer from 1 to {MAX_RUN_LIMIT}.")
    return value


def _validated_identifier(value: str, *, kind: str) -> str:
    text = str(value).strip()
    if not text or not _SAFE_IDENTIFIER.fullmatch(text):
        raise HarnessError("invalid_path_parameter", f"Invalid {kind}.")
    return text


def _artifact_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    return "application/octet-stream"


def _sanitize_value(value: Any, *, secrets: tuple[str, ...], roots: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_KEYS:
                result[key] = "********" if any(word in key.lower() for word in ("token", "key", "secret", "password", "authorization")) else "<local-path>"
            else:
                result[key] = _sanitize_value(item, secrets=secrets, roots=roots)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item, secrets=secrets, roots=roots) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, secrets=secrets, roots=roots) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in secrets:
        text = text.replace(secret, "********")
    for root in sorted(set(roots), key=len, reverse=True):
        if not root:
            continue
        text = text.replace(root, "<local-path>")
        text = text.replace(root.replace("\\", "/"), "<local-path>")
    return _WINDOWS_PATH.sub("<local-path>", text)
