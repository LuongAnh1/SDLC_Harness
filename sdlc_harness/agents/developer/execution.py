"""Budget-aware Developer tool loop with durable per-work-item checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from ...core.errors import HarnessError
from ...storage.runtime import RuntimeStore, utc_now_iso
from .actor import DeveloperActAgent


_ACTION_NAMES = {
    "list_files",
    "read_file",
    "search_code",
    "read_diff",
    "write_file",
    "apply_patch",
    "complete",
}
_OBSERVATION_ACTIONS = {"list_files", "read_file", "search_code", "read_diff"}
_MUTATION_ACTIONS = {"write_file", "apply_patch"}
_READ_DENIED_NAMES = {".env", ".git", ".runtime"}
_TEXT_SUFFIXES = {
    ".java", ".kt", ".py", ".js", ".ts", ".tsx", ".jsx", ".xml", ".json",
    ".yaml", ".yml", ".toml", ".gradle", ".properties", ".md", ".txt", ".sh", ".ps1",
}


class DeveloperExecutionError(HarnessError):
    """Tool-loop failure carrying durable artifact/progress references."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        run_id: str,
        retriable: bool,
        manifest_path: str = "",
        journal_path: str = "",
        progress: dict[str, object] | None = None,
    ):
        super().__init__(error_code, message, run_id=run_id)
        self.retriable = retriable
        self.manifest_path = manifest_path
        self.journal_path = journal_path
        self.progress = progress or {}


@dataclass(frozen=True)
class DeveloperExecutionResult:
    summary: str
    files: list[dict[str, object]]
    manifest_path: str
    journal_path: str
    progress: dict[str, object]


class DeveloperToolLoopExecutor:
    """Execute approved code work as bounded JSON actions against one run worktree."""

    def __init__(self, *, settings: Any, store: RuntimeStore, llm_client: Any):
        self.settings = settings
        self.store = store
        self.llm_client = llm_client

    def execute(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        repair_feedback: dict[str, object] | None = None,
        repair_iteration: int = 0,
        repair_scope: str = "implementation",
        self_check: Callable[[], object] | None = None,
        on_progress: Callable[[dict[str, object], str, str], None] | None = None,
    ) -> DeveloperExecutionResult:
        normalized_scope = re.sub(r"[^a-z0-9_]+", "_", repair_scope.strip().lower()).strip("_")
        normalized_scope = normalized_scope or "implementation"
        mode = f"{normalized_scope}_repair" if repair_feedback else "act"
        session_name = f"{mode}_v{repair_iteration}" if repair_feedback else "act_v1"
        session_dir = self.store.run_dir(run_id) / "developer_execution" / session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = session_dir / "manifest.json"
        journal_file = session_dir / "journal.json"
        manifest_path = self.store.relative(manifest_file)
        journal_path = self.store.relative(journal_file)

        selection_policy_migrated = False
        manifest = _build_manifest(
            run_id=run_id,
            work_packet=work_packet,
            target_path=target_path,
            mode=mode,
            repair_feedback=repair_feedback,
            repair_iteration=repair_iteration,
        )
        if manifest_file.exists():
            existing = _read_json(manifest_file, run_id=run_id)
            if existing.get("input_hash") != manifest.get("input_hash"):
                compatible_manifest = None
                if repair_feedback:
                    persisted_paths = {
                        str(item.get("path") or "").strip().replace("\\", "/")
                        for item in _dict_items(existing.get("items"))
                        if str(item.get("path") or "").strip()
                    }
                    compatible_manifest = _build_manifest(
                        run_id=run_id,
                        work_packet=work_packet,
                        target_path=target_path,
                        mode=mode,
                        repair_feedback=repair_feedback,
                        repair_iteration=repair_iteration,
                        selected_paths=persisted_paths,
                    )
                if not compatible_manifest or existing.get("input_hash") != compatible_manifest.get("input_hash"):
                    raise DeveloperExecutionError(
                        "developer_input_drift",
                        "Approved Developer input changed after the execution session was created.",
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                    )
                selection_policy_migrated = True
            manifest = existing
        else:
            _write_json_atomic(manifest_file, manifest)

        journal = (
            _read_json(journal_file, run_id=run_id)
            if journal_file.exists()
            else _new_journal(run_id=run_id, manifest=manifest, mode=mode, repair_iteration=repair_iteration)
        )
        if selection_policy_migrated:
            migrations = journal.get("migrations")
            if not isinstance(migrations, list):
                migrations = []
                journal["migrations"] = migrations
            if not any(
                isinstance(row, dict) and row.get("code") == "repair_selection_policy_migrated"
                for row in migrations
            ):
                migrations.append(
                    {
                        "code": "repair_selection_policy_migrated",
                        "persisted_item_paths": [
                            str(item.get("path") or "") for item in _dict_items(manifest.get("items"))
                        ],
                        "current_item_paths": [
                            str(item.get("path") or "")
                            for item in _dict_items(
                                _build_manifest(
                                    run_id=run_id,
                                    work_packet=work_packet,
                                    target_path=target_path,
                                    mode=mode,
                                    repair_feedback=repair_feedback,
                                    repair_iteration=repair_iteration,
                                ).get("items")
                            )
                        ],
                        "migrated_at": utc_now_iso(),
                    }
                )
        self._recover_interrupted_item(
            run_id=run_id,
            target_path=target_path,
            session_dir=session_dir,
            manifest=manifest,
            journal=journal,
        )
        self._validate_workspace(
            run_id=run_id,
            target_path=target_path,
            manifest=manifest,
            journal=journal,
            manifest_path=manifest_path,
            journal_path=journal_path,
        )
        _write_json_atomic(journal_file, journal)

        if getattr(self.settings, "llm_mode", "") == "fake":
            self._execute_fake(
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_path,
                session_dir=session_dir,
                manifest=manifest,
                journal=journal,
                journal_file=journal_file,
                on_progress=on_progress,
                manifest_path=manifest_path,
                journal_path=journal_path,
            )
        else:
            for item in _dict_items(manifest.get("items")):
                item_id = str(item.get("id") or "")
                entry = _journal_item(journal, item_id)
                if entry.get("status") == "completed":
                    continue
                self._execute_item(
                    run_id=run_id,
                    target_path=target_path,
                    session_dir=session_dir,
                    item=item,
                    manifest=manifest,
                    journal=journal,
                    journal_file=journal_file,
                    on_progress=on_progress,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    initial_history=_resumed_self_check_history(
                        journal,
                        item_path=str(item.get("path") or ""),
                    ),
                )

        if self_check is not None:
            self._run_self_check_loop(
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_path,
                session_dir=session_dir,
                manifest=manifest,
                journal=journal,
                journal_file=journal_file,
                self_check=self_check,
                on_progress=on_progress,
                manifest_path=manifest_path,
                journal_path=journal_path,
            )

        journal["status"] = "completed"
        journal["completed_at"] = utc_now_iso()
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)
        progress = _progress(journal)
        files = [_completed_file(item, _journal_item(journal, str(item.get("id") or ""))) for item in _dict_items(manifest.get("items"))]
        return DeveloperExecutionResult(
            summary=(
                f"Developer tool loop completed {progress['completed_items']}/{progress['total_items']} "
                f"work items in {mode} mode."
            ),
            files=files,
            manifest_path=manifest_path,
            journal_path=journal_path,
            progress=progress,
        )

    def _run_self_check_loop(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        session_dir: Path,
        manifest: dict[str, object],
        journal: dict[str, object],
        journal_file: Path,
        self_check: Callable[[], object],
        on_progress: Callable[[dict[str, object], str, str], None] | None,
        manifest_path: str,
        journal_path: str,
    ) -> None:
        """Run a harness-owned verification gate and reopen diagnosed work items."""

        max_attempts = int(getattr(self.settings, "developer_self_check_limit", 5))
        check_state = journal.get("self_check")
        if not isinstance(check_state, dict):
            check_state = {"status": "pending", "total_attempts": 0, "attempts": []}
            journal["self_check"] = check_state

        for invocation_attempt in range(1, max_attempts + 1):
            try:
                result = _self_check_result(self_check())
            except Exception as exc:
                self._pause_for_self_check(
                    run_id=run_id,
                    journal=journal,
                    journal_file=journal_file,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    message=f"Developer self-check could not run: {exc}",
                )

            total_attempts = int(check_state.get("total_attempts") or 0) + 1
            check_state["total_attempts"] = total_attempts
            attempt = {
                "attempt": total_attempts,
                "invocation_attempt": invocation_attempt,
                "created_at": utc_now_iso(),
                "result": _clip_json_value(result, 8000),
            }
            attempts = check_state.get("attempts")
            if not isinstance(attempts, list):
                attempts = []
                check_state["attempts"] = attempts
            attempts.append(attempt)
            check_state["latest_result"] = _clip_json_value(result, 8000)
            check_state["status"] = str(result.get("status") or "failed")
            check_state["max_attempts_per_invocation"] = max_attempts
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            if on_progress:
                on_progress(_progress(journal), manifest_path, journal_path)

            if result.get("status") == "passed":
                check_state["passed_at"] = utc_now_iso()
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                return

            affected_paths = _self_check_affected_paths(manifest=manifest, result=result)
            check_state["affected_paths"] = affected_paths
            attempt["affected_paths"] = affected_paths
            _write_json_atomic(journal_file, journal)
            if not affected_paths:
                self._pause_for_self_check(
                    run_id=run_id,
                    journal=journal,
                    journal_file=journal_file,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    message=(
                        "Developer self-check failed, but its diagnostics did not identify a file "
                        "inside the approved Developer manifest."
                    ),
                )
            if invocation_attempt >= max_attempts:
                break

            feedback = _self_check_feedback(result)
            for path in affected_paths:
                item = _manifest_item_for_path(manifest, path)
                entry = _journal_item(journal, str(item.get("id") or ""))
                snapshot = session_dir / "snapshots" / f"{str(item.get('id') or '')}.json"
                if snapshot.exists():
                    snapshot.unlink()
                entry["status"] = "pending"
                check_state["status"] = "repairing"
                journal["status"] = "running"
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                self._execute_item(
                    run_id=run_id,
                    target_path=target_path,
                    session_dir=session_dir,
                    item=item,
                    manifest=manifest,
                    journal=journal,
                    journal_file=journal_file,
                    on_progress=on_progress,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    initial_history=[feedback],
                )

        self._pause_for_self_check(
            run_id=run_id,
            journal=journal,
            journal_file=journal_file,
            manifest_path=manifest_path,
            journal_path=journal_path,
            message=f"Developer self-check still failed after {max_attempts} attempts.",
        )

    def _pause_for_self_check(
        self,
        *,
        run_id: str,
        journal: dict[str, object],
        journal_file: Path,
        manifest_path: str,
        journal_path: str,
        message: str,
    ) -> None:
        check_state = _dict_value(journal.get("self_check"))
        check_state["status"] = "failed"
        check_state["last_error"] = message
        journal["status"] = "paused"
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)
        raise DeveloperExecutionError(
            "developer_self_check_failed",
            message,
            run_id=run_id,
            retriable=True,
            manifest_path=manifest_path,
            journal_path=journal_path,
            progress=_progress(journal),
        )

    def _execute_fake(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        session_dir: Path,
        manifest: dict[str, object],
        journal: dict[str, object],
        journal_file: Path,
        on_progress: Callable[[dict[str, object], str, str], None] | None,
        manifest_path: str,
        journal_path: str,
    ) -> None:
        generated = DeveloperActAgent(None).act(work_packet=work_packet)
        by_path = {str(item.get("path") or ""): item for item in generated.files}
        for item in _dict_items(manifest.get("items")):
            item_id = str(item.get("id") or "")
            entry = _journal_item(journal, item_id)
            if entry.get("status") == "completed":
                continue
            path = str(item.get("path") or "")
            generated_file = by_path.get(path)
            if not generated_file:
                raise DeveloperExecutionError(
                    "developer_execution_incomplete",
                    f"Fake Developer did not return planned file {path}.",
                    run_id=run_id,
                    retriable=False,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                )
            self._begin_item(
                target_path=target_path,
                session_dir=session_dir,
                item=item,
                entry=entry,
                journal=journal,
                journal_file=journal_file,
            )
            target = _safe_write_target(target_path, path, path, run_id=run_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(generated_file.get("content") or "").rstrip() + "\n", encoding="utf-8")
            self._complete_item(item=item, entry=entry, target=target, summary=str(generated_file.get("reason") or ""))
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            if on_progress:
                on_progress(_progress(journal), manifest_path, journal_path)

    def _execute_item(
        self,
        *,
        run_id: str,
        target_path: Path,
        session_dir: Path,
        item: dict[str, object],
        manifest: dict[str, object],
        journal: dict[str, object],
        journal_file: Path,
        on_progress: Callable[[dict[str, object], str, str], None] | None,
        manifest_path: str,
        journal_path: str,
        initial_history: list[dict[str, object]] | None = None,
    ) -> None:
        item_id = str(item.get("id") or "")
        entry = _journal_item(journal, item_id)
        self._begin_item(
            target_path=target_path,
            session_dir=session_dir,
            item=item,
            entry=entry,
            journal=journal,
            journal_file=journal_file,
        )
        history: list[dict[str, object]] = list(initial_history or [])
        seen_observations: set[str] = set()
        mutated = False
        repair_mode = str(manifest.get("mode") or "").endswith("_repair")
        length_failures = 0
        invalid_action_failures = 0
        max_actions = int(getattr(self.settings, "developer_max_actions_per_item", 24))
        max_retries = int(getattr(self.settings, "developer_max_action_retries", 2))
        configured_max_tokens = int(getattr(self.settings, "llm_structured_max_tokens", 8192))
        context_window_tokens = int(getattr(self.settings, "llm_context_window_tokens", 128000))
        max_write_chars = max(2000, min(18000, (configured_max_tokens - 1024) * 2))

        try:
            for action_index in range(1, max_actions + 1):
                prompt = _action_prompt(
                    item=item,
                    manifest=manifest,
                    history=history,
                    target_path=target_path,
                    max_write_chars=max_write_chars,
                    force_incremental=length_failures > 0,
                    action_index=action_index,
                    max_actions=max_actions,
                    mutated=mutated,
                    context_window_tokens=context_window_tokens,
                )
                max_tokens = _completion_token_budget(
                    prompt,
                    configured_max_tokens=configured_max_tokens,
                    context_window_tokens=context_window_tokens,
                )
                completion = self.llm_client.complete_with_metadata(
                    system=(
                        "You are Developer Executor in a controlled SDLC worktree. Return exactly one JSON action. "
                        "Use tools to inspect and edit only the current work item. Do not include analysis, "
                        "thinking markers, markdown, or a second action."
                    ),
                    prompt=prompt,
                    json_mode=True,
                    max_tokens=max_tokens,
                )
                raw = str(getattr(completion, "content", ""))
                metadata = {
                    "item_attempt": int(entry.get("attempts") or 1),
                    "action_index": action_index,
                    "finish_reason": str(getattr(completion, "finish_reason", "") or ""),
                    "usage": getattr(completion, "usage", {}) if isinstance(getattr(completion, "usage", {}), dict) else {},
                    "max_tokens": max_tokens,
                    "created_at": utc_now_iso(),
                }
                _write_action_artifacts(
                    session_dir,
                    item_id,
                    int(entry.get("attempts") or 1),
                    action_index,
                    raw,
                    metadata,
                )
                entry.setdefault("actions", []).append(metadata)
                if metadata["finish_reason"] == "length":
                    length_failures += 1
                    history.append({"action": "rejected", "error": "finish_reason=length", "instruction": "Use a smaller write or patch."})
                    if length_failures > max_retries:
                        raise DeveloperExecutionError(
                            "developer_action_truncated",
                            f"Developer action for {item.get('path')} exceeded the output budget after {length_failures} attempts.",
                            run_id=run_id,
                            retriable=True,
                            manifest_path=manifest_path,
                            journal_path=journal_path,
                            progress=_progress(journal),
                        )
                    continue
                try:
                    action = _parse_action(raw)
                    invalid_action_failures = 0
                    if action.get("normalization"):
                        entry["actions"][-1]["parse_normalization"] = action["normalization"]
                except HarnessError as exc:
                    invalid_action_failures += 1
                    history.append(
                        {
                            "action": "rejected",
                            "error": exc.message,
                            "instruction": (
                                "Return exactly one JSON action object with no analysis, thinking markers, "
                                "markdown, or additional action."
                            ),
                        }
                    )
                    if invalid_action_failures > max_retries:
                        raise DeveloperExecutionError(
                            "developer_action_invalid",
                            f"Developer returned invalid tool actions for {item.get('path')}: {exc.message}",
                            run_id=run_id,
                            retriable=True,
                            manifest_path=manifest_path,
                            journal_path=journal_path,
                            progress=_progress(journal),
                        ) from exc
                    continue
                policy_error = _action_policy_error(
                    action=action,
                    history=history,
                    seen_observations=seen_observations,
                    mutated=mutated,
                    action_index=action_index,
                    max_actions=max_actions,
                )
                if not policy_error:
                    policy_error = _current_item_scope_error(
                        action=action,
                        item_path=str(item.get("path") or ""),
                        manifest=manifest,
                    )
                if policy_error:
                    result = {"ok": False, **policy_error}
                else:
                    result = _execute_action(
                        action=action,
                        run_id=run_id,
                        repo_root=target_path,
                        item_path=str(item.get("path") or ""),
                        session_dir=session_dir,
                        max_write_chars=max_write_chars,
                    )
                history.append(
                    {
                        "action": action["action"],
                        "arguments": _action_history_arguments(action),
                        "result": result,
                    }
                )
                entry["actions"][-1]["tool_action"] = action["action"]
                entry["actions"][-1]["tool_result"] = _clip_json_value(result, 4000)
                if action["action"] in _OBSERVATION_ACTIONS and result.get("ok"):
                    seen_observations.add(_action_signature(action))
                if action["action"] in _MUTATION_ACTIONS and result.get("ok"):
                    mutated = True
                    seen_observations.clear()
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if action["action"] != "complete":
                    continue
                target = _safe_write_target(target_path, str(item.get("path") or ""), str(item.get("path") or ""), run_id=run_id)
                no_change_required = _dict_value(action.get("arguments")).get("no_change_required") is True
                inspected_current_target = _has_successful_target_inspection(
                    history,
                    item_path=str(item.get("path") or ""),
                )
                no_change_allowed = (
                    repair_mode
                    and no_change_required
                    and target.is_file()
                    and inspected_current_target
                )
                if not target.is_file() or (not mutated and not no_change_allowed):
                    history.append(
                        {
                            "action": "rejected",
                            "error": (
                                "complete requires a successful mutation and an existing target file; "
                                "repair items may instead use no_change_required=true after reading the current target"
                            ),
                        }
                    )
                    continue
                self._complete_item(
                    item=item,
                    entry=entry,
                    target=target,
                    summary=str(action.get("arguments", {}).get("summary") or ""),
                    no_change_required=no_change_allowed,
                )
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if on_progress:
                    on_progress(_progress(journal), manifest_path, journal_path)
                return
            raise DeveloperExecutionError(
                "developer_action_limit_exceeded",
                f"Developer did not complete {item.get('path')} within {max_actions} tool actions.",
                run_id=run_id,
                retriable=True,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            )
        except DeveloperExecutionError as exc:
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            entry["status"] = "pending"
            entry["last_error_at"] = utc_now_iso()
            journal["status"] = "paused"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            exc.progress = _progress(journal)
            raise
        except HarnessError as exc:
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            entry["status"] = "pending"
            journal["status"] = "blocked"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            raise DeveloperExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=False,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            entry["status"] = "pending"
            journal["status"] = "paused"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            raise DeveloperExecutionError(
                "developer_tool_failed",
                f"Developer workspace tool failed for {item.get('path')}: {exc}",
                run_id=run_id,
                retriable=True,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            ) from exc

    def _begin_item(
        self,
        *,
        target_path: Path,
        session_dir: Path,
        item: dict[str, object],
        entry: dict[str, object],
        journal: dict[str, object],
        journal_file: Path,
    ) -> None:
        item_id = str(item.get("id") or "")
        path = str(item.get("path") or "")
        target = _safe_write_target(target_path, path, path, run_id=str(journal.get("run_id") or ""))
        snapshot = session_dir / "snapshots" / f"{item_id}.json"
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                snapshot,
                {
                    "path": path,
                    "existed": target.is_file(),
                    "content": target.read_text(encoding="utf-8", errors="replace") if target.is_file() else "",
                    "sha256": _file_hash(target),
                },
            )
        entry["status"] = "running"
        entry["started_at"] = utc_now_iso()
        previous_actions = entry.get("actions")
        if isinstance(previous_actions, list) and previous_actions:
            entry.setdefault("attempt_history", []).append(
                {
                    "attempt": int(entry.get("attempts") or 0),
                    "actions": previous_actions,
                    "archived_at": utc_now_iso(),
                }
            )
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["actions"] = []
        journal["status"] = "running"
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)

    def _complete_item(
        self,
        *,
        item: dict[str, object],
        entry: dict[str, object],
        target: Path,
        summary: str,
        no_change_required: bool = False,
    ) -> None:
        entry.update(
            {
                "status": "completed",
                "completed_at": utc_now_iso(),
                "after_sha256": _file_hash(target),
                "summary": summary,
                "no_change_required": no_change_required,
            }
        )

    def _rollback_item(self, *, target_path: Path, session_dir: Path, item: dict[str, object], entry: dict[str, object]) -> None:
        item_id = str(item.get("id") or "")
        snapshot = session_dir / "snapshots" / f"{item_id}.json"
        if not snapshot.exists():
            return
        data = _read_json(snapshot, run_id="")
        path = str(item.get("path") or "")
        target = _safe_write_target(target_path, path, path, run_id="")
        if data.get("existed"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(data.get("content") or ""), encoding="utf-8")
        elif target.exists():
            target.unlink()
        entry["rolled_back_at"] = utc_now_iso()

    def _recover_interrupted_item(
        self,
        *,
        run_id: str,
        target_path: Path,
        session_dir: Path,
        manifest: dict[str, object],
        journal: dict[str, object],
    ) -> None:
        for item in _dict_items(manifest.get("items")):
            entry = _journal_item(journal, str(item.get("id") or ""))
            if entry.get("status") != "running":
                continue
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            entry["status"] = "pending"
            entry["recovered_at"] = utc_now_iso()
            journal["status"] = "paused"

    def _validate_workspace(
        self,
        *,
        run_id: str,
        target_path: Path,
        manifest: dict[str, object],
        journal: dict[str, object],
        manifest_path: str,
        journal_path: str,
    ) -> None:
        allowed = set(_string_items(manifest.get("workspace_allowed_paths")))
        completed = {
            str(item.get("path") or ""): _journal_item(journal, str(item.get("id") or ""))
            for item in _dict_items(manifest.get("items"))
        }
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(target_path),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        dirty = set()
        for line in status.splitlines():
            raw = line[3:].strip() if len(line) > 3 else line.strip()
            path = raw.split(" -> ")[-1].replace("\\", "/")
            if path == ".codegraph" or path.startswith(".codegraph/"):
                continue
            dirty.add(path)
        if dirty - allowed:
            raise DeveloperExecutionError(
                "developer_workspace_drift",
                f"Run worktree contains changes outside the Developer manifest: {sorted(dirty - allowed)}",
                run_id=run_id,
                retriable=False,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            )
        for item in _dict_items(manifest.get("items")):
            path = str(item.get("path") or "")
            entry = completed[path]
            expected = str(entry.get("after_sha256") or item.get("initial_sha256") or "")
            actual = _file_hash(target_path / path)
            if actual != expected:
                raise DeveloperExecutionError(
                    "developer_workspace_drift",
                    f"Run worktree file changed outside the execution journal: {path}",
                    run_id=run_id,
                    retriable=False,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                )


def repair_scope_mismatch_retry_allowed(state: dict[str, object]) -> bool:
    """Allow repair migration after the former cross-item failure or selection-policy drift."""

    if str(state.get("status") or "") != "blocked":
        return False
    if str(_dict_value(state.get("last_error")).get("error_code") or "") not in {
        "developer_act_out_of_scope",
        "developer_input_drift",
    }:
        return False
    execution = _dict_value(state.get("developer_execution"))
    mode = str(execution.get("mode") or "")
    phase = str(state.get("current_phase") or "")
    artifacts = _dict_value(state.get("latest_artifacts"))
    if mode == "implementation_repair" or phase == "implementation_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "implementation_repair_feedback",
                "developer_repair_execution_manifest",
                "developer_repair_execution_journal",
            )
        )
    if mode == "qa_repair" or phase == "qa_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "qa_repair_feedback",
                "developer_qa_repair_execution_manifest",
                "developer_qa_repair_execution_journal",
            )
        )
    return False


def _build_manifest(
    *,
    run_id: str,
    work_packet: dict[str, object],
    target_path: Path,
    mode: str,
    repair_feedback: dict[str, object] | None,
    repair_iteration: int,
    selected_paths: set[str] | None = None,
) -> dict[str, object]:
    output_contract = _dict_value(work_packet.get("output_contract"))
    allowed = [str(value).strip().replace("\\", "/") for value in output_contract.get("allowed_paths", []) if str(value).strip()]
    if not allowed:
        raise HarnessError("developer_work_packet_allowed_paths_missing", "DeveloperWorkPacket has no allowed paths.", run_id=run_id)
    selected = (
        {path.strip().replace("\\", "/") for path in selected_paths}
        if selected_paths is not None
        else _selected_repair_paths(work_packet, repair_feedback) if repair_feedback else set(allowed)
    )
    proposed = {str(item.get("path") or ""): item for item in _dict_items(work_packet.get("proposed_files"))}
    coverage = _dict_value(work_packet.get("requirement_coverage_by_file"))
    mappings = _dict_value(work_packet.get("design_mappings_by_file"))
    canonical = {
        str(item.get("id") or ""): str(item.get("content") or "")
        for item in _dict_items(work_packet.get("canonical_requirements"))
    }
    steps = _dict_items(work_packet.get("implementation_steps"))
    ordered = [path for path in allowed if path in selected and "/src/test/" not in f"/{path}"]
    ordered.extend(path for path in allowed if path in selected and "/src/test/" in f"/{path}")
    items: list[dict[str, object]] = []
    for index, path in enumerate(ordered, start=1):
        req_ids = []
        for row in _dict_items(coverage.get(path)):
            value = str(row.get("requirement_id") or "")
            if value and value not in req_ids:
                req_ids.append(value)
        file_steps = []
        for row in steps:
            files = [str(value).replace("\\", "/") for value in row.get("files", [])] if isinstance(row.get("files"), list) else []
            if path in files:
                file_steps.append(
                    {
                        "order": row.get("order"),
                        "title": row.get("title"),
                        "description": row.get("description"),
                        "design_element_ids": row.get("design_element_ids", []),
                        "requirement_ids": row.get("requirement_ids", []),
                    }
                )
        design_rows = []
        for row in _dict_items(mappings.get(path)):
            design_rows.append(
                {
                    "design_element_id": row.get("design_element_id"),
                    "planned_code_target": row.get("planned_code_target"),
                    "message_type": row.get("message_type"),
                    "trace_policy": row.get("trace_policy"),
                }
            )
        item = proposed.get(path, {})
        items.append(
            {
                "id": f"WI-{index:03d}",
                "path": path,
                "operation": str(item.get("operation") or "create_or_update"),
                "purpose": str(item.get("purpose") or item.get("reason") or ""),
                "requirement_ids": req_ids,
                "requirements": [{"id": value, "content": canonical.get(value, "")} for value in req_ids],
                "design_mappings": design_rows,
                "implementation_steps": file_steps,
                "dependencies": ordered[: index - 1],
                "initial_sha256": _file_hash(target_path / path),
            }
        )
    input_payload = {
        "approved_plan": work_packet.get("approved_plan"),
        "effective_design_contract": work_packet.get("effective_design_contract"),
        "requirement_contract": work_packet.get("requirement_contract"),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "workspace_allowed_paths": allowed,
        "items": [{key: value for key, value in item.items() if key != "initial_sha256"} for item in items],
        "repair_feedback": repair_feedback or {},
    }
    return {
        "version": "developer_execution_manifest_v1",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "created_at": utc_now_iso(),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "input_hash": _json_hash(input_payload),
        "workspace_allowed_paths": allowed,
        "repair_feedback": repair_feedback or {},
        "items": items,
    }


def _new_journal(*, run_id: str, manifest: dict[str, object], mode: str, repair_iteration: int) -> dict[str, object]:
    return {
        "version": "developer_execution_journal_v1",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "manifest_input_hash": manifest.get("input_hash"),
        "status": "pending",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "items": [
            {"id": item.get("id"), "path": item.get("path"), "status": "pending", "attempts": 0, "actions": []}
            for item in _dict_items(manifest.get("items"))
        ],
    }


def _action_prompt(
    *,
    item: dict[str, object],
    manifest: dict[str, object],
    history: list[dict[str, object]],
    target_path: Path,
    max_write_chars: int,
    force_incremental: bool,
    action_index: int,
    max_actions: int,
    mutated: bool,
    context_window_tokens: int,
) -> str:
    path = str(item.get("path") or "")
    target = target_path / path
    phase = _execution_phase(
        history=history,
        mutated=mutated,
        action_index=action_index,
        max_actions=max_actions,
    )
    action_memory = _action_memory(
        history,
        target_item_path=path,
        max_evidence_chars=max(8000, min(48000, context_window_tokens)),
    )
    action_schema = {
        "action": "list_files|read_file|search_code|read_diff|write_file|apply_patch|complete",
        "arguments": {},
        "summary": "short reason",
    }
    context = {
        "work_item": item,
        "execution": {"mode": manifest.get("mode"), "base_sha": manifest.get("base_sha")},
        "repair_feedback": manifest.get("repair_feedback", {}),
        "target_state": {"exists": target.is_file(), "sha256": _file_hash(target)},
        "action_budget": {
            "current_action": action_index,
            "max_actions": max_actions,
            "remaining_including_current": max_actions - action_index + 1,
            "phase": phase,
        },
        "working_memory": action_memory,
    }
    extra = (
        "The previous response exceeded the output budget. Use write_file for a small compilable skeleton, then "
        "add bounded pieces with apply_patch. Do not return a large complete file in one action."
        if force_incremental
        else ""
    )
    repair_mode = str(manifest.get("mode") or "").endswith("_repair")
    phase_instruction = {
        "inspect_or_edit": (
            "Inspect only evidence that is still missing. Do not repeat an observation already recorded in "
            "working_memory.action_ledger. Start editing as soon as the target change is clear."
        ),
        "edit_required": (
            "The inspection budget is exhausted. Your next action MUST be write_file or apply_patch for the "
            "current target path. Use working_memory.inspected_sources; do not read, list, or search again."
        ),
        "edit_or_complete": (
            "At least one mutation succeeded. Make only a necessary bounded follow-up edit, or call complete now."
        ),
        "complete_required": (
            "This is the final available action and a mutation already succeeded. You MUST call complete now."
        ),
    }[phase]
    if repair_mode and not mutated:
        phase_instruction += (
            " This repair manifest is processed one file at a time. Never write a different work-item path. "
            "If reading the current target proves that it needs no edit, call complete with "
            "no_change_required=true and explain why; the executor will move to the relevant item."
        )
    return (
        "Developer Tool Action v1\n\n"
        "Return exactly one JSON object matching this shape:\n"
        f"{json.dumps(action_schema, ensure_ascii=False)}\n\n"
        "Available actions:\n"
        "- list_files: arguments {prefix?, limit?}\n"
        "- read_file: arguments {path, start_line?, end_line?}\n"
        "- search_code: arguments {query, prefix?, limit?}\n"
        "- read_diff: arguments {path}\n"
        "- write_file: arguments {path, expected_sha256, content}; target path only\n"
        "- apply_patch: arguments {path, expected_sha256, patch}; unified diff for target path only\n"
        "- complete: arguments {summary, no_change_required?}; normally only after a successful write/patch. "
        "In repair mode, no_change_required=true is allowed only after reading the existing current target.\n\n"
        f"A write_file content or apply_patch value must stay below {max_write_chars} characters. {extra}\n"
        "Do not output source code outside the JSON action. Do not invent requirement IDs. "
        "When repair_feedback is present, fix its Verification Runner, Validator, or QA findings. "
        "Feedback describes an earlier failure. When it conflicts with a successful fresh read from the "
        "current worktree, treat the current worktree content as authoritative. "
        "Inspect dependencies with read_file/search_code when needed. "
        f"{phase_instruction}\n\n"
        f"Execution context:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _execution_phase(
    *,
    history: list[dict[str, object]],
    mutated: bool,
    action_index: int,
    max_actions: int,
) -> str:
    remaining = max_actions - action_index + 1
    if mutated:
        return "complete_required" if remaining == 1 else "edit_or_complete"
    observation_count = sum(1 for row in history if row.get("action") in _OBSERVATION_ACTIONS)
    inspection_limit = max(4, max_actions // 2)
    return "edit_required" if observation_count >= inspection_limit else "inspect_or_edit"


def _action_policy_error(
    *,
    action: dict[str, object],
    history: list[dict[str, object]],
    seen_observations: set[str],
    mutated: bool,
    action_index: int,
    max_actions: int,
) -> dict[str, object] | None:
    name = str(action.get("action") or "")
    phase = _execution_phase(
        history=history,
        mutated=mutated,
        action_index=action_index,
        max_actions=max_actions,
    )
    if name in _OBSERVATION_ACTIONS and _action_signature(action) in seen_observations:
        return {
            "error": "duplicate_observation",
            "instruction": "Use the cached observation in working_memory and advance the current work item.",
        }
    if name in _OBSERVATION_ACTIONS and phase == "edit_required":
        return {
            "error": "mutation_required",
            "instruction": "The inspection budget is exhausted; write_file or apply_patch the current target now.",
        }
    if phase == "complete_required" and name != "complete":
        return {
            "error": "completion_required",
            "instruction": "A mutation already succeeded and only one action remains; call complete now.",
        }
    return None


def _current_item_scope_error(
    *,
    action: dict[str, object],
    item_path: str,
    manifest: dict[str, object],
) -> dict[str, object] | None:
    """Turn a write aimed at another queued item into recoverable tool feedback.

    Unknown paths still reach ``_safe_write_target`` and remain hard failures.
    """

    if str(action.get("action") or "") not in _MUTATION_ACTIONS:
        return None
    requested = str(_dict_value(action.get("arguments")).get("path") or "").strip().replace("\\", "/").lstrip("/")
    expected = item_path.strip().replace("\\", "/").lstrip("/")
    if requested == expected:
        return None
    queued_paths = {
        str(item.get("path") or "").strip().replace("\\", "/").lstrip("/").casefold()
        for item in _dict_items(manifest.get("items"))
    }
    if requested.casefold() not in queued_paths:
        return None
    return {
        "error": "current_item_mismatch",
        "requested_path": requested,
        "current_item": expected,
        "instruction": (
            "Do not modify another queued work item while processing the current item. "
            "Edit only the current target, or in repair mode inspect it and complete with "
            "no_change_required=true when no edit is needed."
        ),
    }


def _has_successful_target_inspection(history: list[dict[str, object]], *, item_path: str) -> bool:
    expected = item_path.strip().replace("\\", "/").lstrip("/").casefold()
    for row in history:
        if row.get("action") not in {"read_file", "read_diff"}:
            continue
        if not _dict_value(row.get("result")).get("ok"):
            continue
        observed = str(_dict_value(row.get("arguments")).get("path") or "").strip().replace("\\", "/").lstrip("/")
        if observed.casefold() == expected:
            return True
    return False


def _action_signature(action: dict[str, object]) -> str:
    return json.dumps(
        {
            "action": action.get("action"),
            "arguments": _dict_value(action.get("arguments")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _action_history_arguments(action: dict[str, object]) -> dict[str, object]:
    arguments = _dict_value(action.get("arguments"))
    safe: dict[str, object] = {}
    for key, value in arguments.items():
        if key in {"content", "patch"} and isinstance(value, str):
            safe[f"{key}_chars"] = len(value)
        elif key == "expected_sha256" and isinstance(value, str):
            safe[key] = value[:12]
        else:
            safe[key] = value
    return safe


def _action_memory(
    history: list[dict[str, object]],
    *,
    target_item_path: str,
    max_evidence_chars: int,
) -> dict[str, object]:
    ledger: list[dict[str, object]] = []
    for index, row in enumerate(history, start=1):
        arguments = _dict_value(row.get("arguments"))
        result = _dict_value(row.get("result"))
        ledger.append(
            {
                "index": index,
                "action": row.get("action"),
                "path": arguments.get("path") or result.get("path"),
                "query": arguments.get("query"),
                "ok": result.get("ok"),
                "error": row.get("error") or result.get("error"),
            }
        )

    latest_reads: list[dict[str, object]] = []
    seen_ranges: set[tuple[str, object, object]] = set()
    for row in reversed(history):
        if row.get("action") != "read_file":
            continue
        result = _dict_value(row.get("result"))
        if not result.get("ok") or not isinstance(result.get("content"), str):
            continue
        key = (str(result.get("path") or ""), result.get("start_line"), result.get("end_line"))
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        latest_reads.append(
            {
                "path": key[0],
                "start_line": key[1],
                "end_line": key[2],
                "content": result["content"],
            }
        )
    latest_reads.sort(key=lambda row: (str(row.get("path")) != target_item_path))

    inspected_sources: list[dict[str, object]] = []
    evidence_chars = 0
    for row in latest_reads:
        content = str(row.get("content") or "")
        remaining = max_evidence_chars - evidence_chars
        if remaining <= 0:
            break
        if len(content) > remaining:
            if inspected_sources:
                continue
            content = content[:remaining]
        inspected_sources.append({**row, "content": content})
        evidence_chars += len(content)

    recent_results: list[dict[str, object]] = []
    for row in history[-4:]:
        result = dict(_dict_value(row.get("result")))
        if row.get("error") and not result:
            result = {
                "ok": False,
                "error": row.get("error"),
                "instruction": row.get("instruction"),
            }
        if isinstance(result.get("content"), str):
            result["content_chars"] = len(result["content"])
            result.pop("content", None)
        recent_results.append(
            {
                "action": row.get("action"),
                "arguments": _dict_value(row.get("arguments")),
                "result": _clip_json_value(result, 6000),
            }
        )
    return {
        "action_ledger": ledger,
        "inspected_sources": inspected_sources,
        "recent_results": recent_results,
    }


def _parse_action(raw: str) -> dict[str, object]:
    text = raw.strip()
    normalization = ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        payload, duplicate_count = _parse_identical_json_sequence(text)
        if payload is None:
            embedded_actions = _embedded_action_count(text)
            if embedded_actions > 1:
                message = (
                    f"Developer response contained {embedded_actions} JSON actions. "
                    "Return exactly one JSON action and no surrounding text."
                )
            elif embedded_actions == 1:
                message = "Developer response contained text outside its JSON action. Return only the JSON action."
            else:
                message = "Developer action is not valid JSON."
            raise HarnessError("developer_action_invalid", message) from exc
        normalization = f"collapsed_{duplicate_count}_identical_json_objects"
    if not isinstance(payload, dict):
        raise HarnessError("developer_action_invalid", "Developer action must be a JSON object.")
    name = str(payload.get("action") or "").strip()
    arguments = payload.get("arguments")
    if name not in _ACTION_NAMES or not isinstance(arguments, dict):
        raise HarnessError("developer_action_invalid", "Developer action has an unsupported action or arguments shape.")
    return {
        "action": name,
        "arguments": arguments,
        "summary": str(payload.get("summary") or ""),
        "normalization": normalization,
    }


def _parse_identical_json_sequence(text: str) -> tuple[object | None, int]:
    """Recover only an unambiguous sequence such as `{same}{same}`."""

    decoder = json.JSONDecoder()
    values: list[object] = []
    position = 0
    try:
        while position < len(text):
            while position < len(text) and text[position].isspace():
                position += 1
            if position >= len(text):
                break
            value, position = decoder.raw_decode(text, position)
            values.append(value)
    except json.JSONDecodeError:
        return None, 0
    if len(values) < 2 or any(value != values[0] for value in values[1:]):
        return None, len(values)
    return values[0], len(values)


def _embedded_action_count(text: str) -> int:
    """Count complete action-shaped JSON objects embedded in malformed output."""

    decoder = json.JSONDecoder()
    count = 0
    position = 0
    while position < len(text):
        start = text.find("{", position)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            position = start + 1
            continue
        if (
            isinstance(value, dict)
            and str(value.get("action") or "").strip() in _ACTION_NAMES
            and isinstance(value.get("arguments"), dict)
        ):
            count += 1
        position = max(end, start + 1)
    return count


def _completion_token_budget(
    prompt: str,
    *,
    configured_max_tokens: int,
    context_window_tokens: int,
) -> int:
    """Reserve context headroom without relying on provider-specific model metadata."""

    estimated_input_tokens = max(1, len(prompt) // 4)
    available = max(1024, context_window_tokens - estimated_input_tokens - 2048)
    return max(1024, min(configured_max_tokens, available))


def _execute_action(
    *,
    action: dict[str, object],
    run_id: str,
    repo_root: Path,
    item_path: str,
    session_dir: Path,
    max_write_chars: int,
) -> dict[str, object]:
    name = str(action["action"])
    args = _dict_value(action.get("arguments"))
    if name == "list_files":
        prefix = _safe_read_path(repo_root, str(args.get("prefix") or ""), run_id=run_id, allow_root=True)
        limit = max(1, min(int(args.get("limit") or 100), 200))
        files = []
        if prefix.is_file():
            files = [str(prefix.relative_to(repo_root)).replace("\\", "/")]
        elif prefix.exists():
            for path in prefix.rglob("*"):
                if path.is_file() and _read_path_allowed(repo_root, path):
                    files.append(str(path.relative_to(repo_root)).replace("\\", "/"))
                    if len(files) >= limit:
                        break
        return {"ok": True, "files": files, "truncated": len(files) >= limit}
    if name == "read_file":
        path = _safe_read_path(repo_root, str(args.get("path") or ""), run_id=run_id)
        if not path.is_file() or not _read_path_allowed(repo_root, path):
            return {"ok": False, "error": "file_not_readable"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 300)))
        content = "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))
        return {"ok": True, "path": str(args.get("path")), "start_line": start, "end_line": end, "content": content[:12000]}
    if name == "search_code":
        query = str(args.get("query") or "")
        if not query or len(query) > 200:
            return {"ok": False, "error": "invalid_query"}
        prefix = _safe_read_path(repo_root, str(args.get("prefix") or ""), run_id=run_id, allow_root=True)
        limit = max(1, min(int(args.get("limit") or 30), 60))
        hits = []
        candidates = [prefix] if prefix.is_file() else prefix.rglob("*") if prefix.exists() else []
        for path in candidates:
            if not path.is_file() or not _read_path_allowed(repo_root, path):
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if query.casefold() in line.casefold():
                    hits.append({"path": str(path.relative_to(repo_root)).replace("\\", "/"), "line": line_no, "text": line[:300]})
                    if len(hits) >= limit:
                        return {"ok": True, "hits": hits, "truncated": True}
        return {"ok": True, "hits": hits, "truncated": False}
    if name == "read_diff":
        path = str(args.get("path") or "")
        _safe_read_path(repo_root, path, run_id=run_id)
        completed = subprocess.run(
            ["git", "diff", "--", path], cwd=str(repo_root), check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return {"ok": True, "path": path, "diff": completed.stdout[:12000], "truncated": len(completed.stdout) > 12000}
    if name == "write_file":
        path = str(args.get("path") or "")
        target = _safe_write_target(repo_root, path, item_path, run_id=run_id)
        content = args.get("content")
        if not isinstance(content, str) or len(content) > max_write_chars:
            return {"ok": False, "error": "write_content_too_large", "max_chars": max_write_chars}
        expected = str(args.get("expected_sha256") or "")
        if expected != _file_hash(target):
            return {"ok": False, "error": "stale_file_hash", "actual_sha256": _file_hash(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        return {"ok": True, "path": path, "sha256": _file_hash(target), "chars": len(content)}
    if name == "apply_patch":
        path = str(args.get("path") or "")
        target = _safe_write_target(repo_root, path, item_path, run_id=run_id)
        patch_text = args.get("patch")
        if not isinstance(patch_text, str) or len(patch_text) > max_write_chars:
            return {"ok": False, "error": "patch_too_large", "max_chars": max_write_chars}
        expected = str(args.get("expected_sha256") or "")
        if expected != _file_hash(target):
            return {"ok": False, "error": "stale_file_hash", "actual_sha256": _file_hash(target)}
        _validate_patch_paths(patch_text, item_path=item_path, run_id=run_id)
        patch_file = session_dir / "pending.patch"
        patch_file.write_text(patch_text, encoding="utf-8")
        try:
            checked = subprocess.run(
                ["git", "apply", "--check", "--whitespace=nowarn", str(patch_file)],
                cwd=str(repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if checked.returncode != 0:
                return {"ok": False, "error": "patch_check_failed", "detail": (checked.stderr or checked.stdout)[:1000]}
            subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch_file)],
                cwd=str(repo_root), check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        finally:
            if patch_file.exists():
                patch_file.unlink()
        return {"ok": True, "path": path, "sha256": _file_hash(target)}
    if name == "complete":
        return {"ok": True, "summary": str(args.get("summary") or "")}
    return {"ok": False, "error": "unsupported_action"}


def _selected_repair_paths(work_packet: dict[str, object], feedback: dict[str, object] | None) -> set[str]:
    allowed = {
        path.replace("\\", "/")
        for path in _string_items(_dict_value(work_packet.get("output_contract")).get("allowed_paths"))
    }
    if not feedback:
        return allowed
    feedback_source = str(feedback.get("feedback_source") or "")
    path_evidence = _dict_value(feedback.get("verification")) if feedback_source == "verification" else feedback
    text = "\n".join(_walk_strings(path_evidence)).replace("\\", "/").replace("./", "")
    text_folded = text.casefold()
    selected = {path for path in allowed if path.casefold() in text_folded}
    if feedback_source == "verification":
        selected.update(
            path
            for path in allowed
            if Path(path).stem and _compiler_symbol_referenced(text_folded, Path(path).stem.casefold())
        )
    requirement_ids = {
        str(item.get("requirement_id") or item.get("requirement") or item.get("requirement_ref") or "")
        for item in _walk_dicts(feedback)
    } - {""}
    coverage = _dict_value(work_packet.get("requirement_coverage_by_file"))
    for path, rows in coverage.items():
        if any(str(row.get("requirement_id") or "") in requirement_ids for row in _dict_items(rows)):
            selected.add(str(path).replace("\\", "/"))

    if feedback_source == "verification" and len(selected) == 1:
        selected_path = next(iter(selected))
        for row in _dict_items(coverage.get(selected_path)):
            for target in _string_items(row.get("planned_code_targets")):
                dependency_path = target.split("#", 1)[0].replace("\\", "/")
                if dependency_path in allowed:
                    selected.add(dependency_path)
    return selected or allowed


def _self_check_result(value: object) -> dict[str, object]:
    """Normalize a VerificationResult-like object without exposing callback internals."""

    if isinstance(value, dict):
        raw = value
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            raise ValueError("self-check callback did not return a structured result")
        raw = to_dict()
    if not isinstance(raw, dict):
        raise ValueError("self-check result is not an object")
    status = str(raw.get("status") or "").strip().lower()
    if status not in {"passed", "failed", "skipped"}:
        raise ValueError(f"self-check returned unsupported status: {status or '<empty>'}")
    return {
        "status": status,
        "command": str(raw.get("command") or ""),
        "exit_code": raw.get("exit_code"),
        "duration_seconds": raw.get("duration_seconds"),
        "stdout": str(raw.get("stdout") or "")[:12000],
        "stderr": str(raw.get("stderr") or "")[:12000],
        "reason": str(raw.get("reason") or ""),
        "timed_out": bool(raw.get("timed_out")),
    }


def _self_check_affected_paths(
    *,
    manifest: dict[str, object],
    result: dict[str, object],
) -> list[str]:
    """Map diagnostics to manifest items and fail closed when no owner is evident."""

    text = "\n".join(_walk_strings(result)).replace("\\", "/").replace("./", "").casefold()
    selected: list[str] = []
    for item in _dict_items(manifest.get("items")):
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            continue
        symbol = Path(path).stem.casefold()
        if path.casefold() in text or (symbol and _compiler_symbol_referenced(text, symbol)):
            selected.append(path)
    return selected


def _self_check_feedback(result: dict[str, object]) -> dict[str, object]:
    return {
        "action": "developer_self_check",
        "arguments": {},
        "result": {
            "ok": False,
            "error": "developer_self_check_failed",
            "verification": _clip_json_value(result, 8000),
            "instruction": (
                "Repair this self-check diagnostic in the current work item, then call complete. "
                "The diagnostic is historical; freshly read worktree content is authoritative."
            ),
        },
    }


def _resumed_self_check_history(
    journal: dict[str, object],
    *,
    item_path: str,
) -> list[dict[str, object]]:
    check_state = _dict_value(journal.get("self_check"))
    if str(check_state.get("status") or "") not in {"failed", "repairing"}:
        return []
    affected = {
        value.replace("\\", "/").casefold()
        for value in _string_items(check_state.get("affected_paths"))
    }
    if item_path.replace("\\", "/").casefold() not in affected:
        return []
    latest = check_state.get("latest_result")
    if not isinstance(latest, dict):
        return []
    return [_self_check_feedback(latest)]


def _manifest_item_for_path(manifest: dict[str, object], path: str) -> dict[str, object]:
    expected = path.strip().replace("\\", "/").casefold()
    for item in _dict_items(manifest.get("items")):
        candidate = str(item.get("path") or "").strip().replace("\\", "/").casefold()
        if candidate == expected:
            return item
    raise HarnessError("developer_execution_manifest_invalid", f"Manifest is missing diagnosed path: {path}")


def _compiler_symbol_referenced(text_folded: str, symbol_folded: str) -> bool:
    """Match compiler-style type ownership without treating every type mention as a repair target."""

    symbol = re.escape(symbol_folded)
    patterns = (
        rf"\b(?:class|interface|enum|record|extends|implements|final)\s+{symbol}\b",
        rf"\bsymbol\s*:\s*(?:(?:class|interface|enum|record)\s+)?{symbol}\b",
        rf"\bin\s+{symbol}\b",
    )
    return any(re.search(pattern, text_folded) for pattern in patterns)


def _walk_dicts(value: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            result.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk_dicts(child))
    return result


def _walk_strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _walk_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _walk_strings(child)]
    return [str(value)] if isinstance(value, (str, int, float)) else []


def _safe_read_path(repo_root: Path, raw: str, *, run_id: str, allow_root: bool = False) -> Path:
    normalized = raw.strip().replace("\\", "/").lstrip("/")
    if not normalized and allow_root:
        return repo_root.resolve()
    if not normalized or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise HarnessError("developer_tool_unsafe_path", f"Unsafe read path: {raw}", run_id=run_id or None)
    target = (repo_root / normalized).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise HarnessError("developer_tool_unsafe_path", f"Read path escapes worktree: {raw}", run_id=run_id or None) from exc
    return target


def _safe_write_target(repo_root: Path, raw: str, item_path: str, *, run_id: str) -> Path:
    normalized = raw.strip().replace("\\", "/").lstrip("/")
    expected = item_path.strip().replace("\\", "/").lstrip("/")
    if normalized != expected:
        raise HarnessError("developer_act_out_of_scope", f"Tool action tried to modify {raw}; current item is {item_path}.", run_id=run_id or None)
    return _safe_read_path(repo_root, normalized, run_id=run_id)


def _read_path_allowed(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    if any(part in _READ_DENIED_NAMES for part in rel.parts):
        return False
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in {"pom.xml", "build.gradle", "settings.gradle", "gradlew"}


def _validate_patch_paths(patch_text: str, *, item_path: str, run_id: str) -> None:
    paths: set[str] = set()
    deletion = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            if not match:
                raise HarnessError("developer_patch_invalid", "Patch has an invalid diff header.", run_id=run_id)
            paths.update(match.groups())
        elif line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0].strip()
            if value == "/dev/null":
                if line.startswith("+++ "):
                    deletion = True
                continue
            if value.startswith(("a/", "b/")):
                paths.add(value[2:])
    normalized = item_path.replace("\\", "/")
    if deletion or not paths or paths != {normalized}:
        raise HarnessError("developer_patch_out_of_scope", f"Patch must only create/update current item {normalized}.", run_id=run_id)


def _write_action_artifacts(
    session_dir: Path,
    item_id: str,
    attempt: int,
    index: int,
    raw: str,
    metadata: dict[str, object],
) -> None:
    action_dir = session_dir / "actions"
    action_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{item_id.lower()}_a{attempt:03d}_{index:03d}"
    (action_dir / f"{stem}.txt").write_text(raw, encoding="utf-8")
    _write_json_atomic(action_dir / f"{stem}.json", metadata)


def _journal_item(journal: dict[str, object], item_id: str) -> dict[str, object]:
    for item in _dict_items(journal.get("items")):
        if str(item.get("id") or "") == item_id:
            return item
    raise HarnessError("developer_execution_journal_invalid", f"Journal is missing work item {item_id}.")


def _completed_file(item: dict[str, object], entry: dict[str, object]) -> dict[str, object]:
    return {
        "path": item.get("path"),
        "operation": item.get("operation"),
        "reason": entry.get("summary") or item.get("purpose"),
        "covered_design_elements": [
            str(row.get("design_element_id") or "") for row in _dict_items(item.get("design_mappings")) if str(row.get("design_element_id") or "")
        ],
        "covered_requirement_ids": _string_items(item.get("requirement_ids")),
        "work_item_id": item.get("id"),
        "after_sha256": entry.get("after_sha256"),
        "no_change_required": bool(entry.get("no_change_required")),
    }


def _progress(journal: dict[str, object]) -> dict[str, object]:
    items = _dict_items(journal.get("items"))
    completed = sum(1 for item in items if item.get("status") == "completed")
    current = next((str(item.get("id") or "") for item in items if item.get("status") == "running"), "")
    progress: dict[str, object] = {
        "status": journal.get("status"),
        "total_items": len(items),
        "completed_items": completed,
        "remaining_items": len(items) - completed,
        "current_item": current,
    }
    self_check = journal.get("self_check")
    if isinstance(self_check, dict):
        progress["self_check"] = {
            "status": self_check.get("status"),
            "attempts": self_check.get("total_attempts", 0),
            "max_attempts_per_invocation": self_check.get("max_attempts_per_invocation"),
            "affected_paths": self_check.get("affected_paths", []),
        }
    return progress


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_json(path: Path, *, run_id: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("developer_execution_artifact_invalid", f"Invalid execution artifact: {path.name}", run_id=run_id or None) from exc
    if not isinstance(value, dict):
        raise HarnessError("developer_execution_artifact_invalid", f"Execution artifact is not an object: {path.name}", run_id=run_id or None)
    return value


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _clip_json_value(value: object, limit: int) -> object:
    raw = json.dumps(value, ensure_ascii=False)
    if len(raw) <= limit:
        return value
    return {"truncated": True, "preview": raw[:limit]}


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
