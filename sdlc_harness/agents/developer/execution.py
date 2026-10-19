"""Vòng tool Developer có giới hạn ngân sách và checkpoint execution unit bền vững."""

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
from .repair_context import build_repair_closure


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
        persisted_required_paths_for_migration: set[str] = set()
        legacy_file_manifest_migrated = False
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
            if (
                existing.get("version") == "developer_execution_manifest_v1"
                and _manifest_paths(existing) == _manifest_paths(manifest)
            ):
                legacy_manifest_file = session_dir / "manifest_file_items_v1.json"
                legacy_journal_file = session_dir / "journal_file_items_v1.json"
                if not legacy_manifest_file.exists():
                    _write_json_atomic(legacy_manifest_file, existing)
                if journal_file.exists() and not legacy_journal_file.exists():
                    _write_json_atomic(legacy_journal_file, _read_json(journal_file, run_id=run_id))
                _write_json_atomic(manifest_file, manifest)
                legacy_file_manifest_migrated = True
                existing = manifest
            if existing.get("input_hash") != manifest.get("input_hash"):
                compatible_manifest = None
                persisted_paths: set[str] = set()
                legacy_context_schema = (
                    bool(repair_feedback)
                    and existing.get("version") in {
                        "developer_execution_manifest_v3",
                        "developer_execution_manifest_v4",
                    }
                    and manifest.get("version") == "developer_execution_manifest_v5"
                    and existing.get("mode") == manifest.get("mode")
                    and existing.get("repair_iteration") == manifest.get("repair_iteration")
                    and existing.get("repair_feedback") == manifest.get("repair_feedback")
                    and _manifest_paths(existing) == _manifest_paths(manifest)
                )
                if repair_feedback:
                    persisted_paths = _manifest_context_paths(existing)
                    compatible_manifest = _build_manifest(
                        run_id=run_id,
                        work_packet=work_packet,
                        target_path=target_path,
                        mode=mode,
                        repair_feedback=repair_feedback,
                        repair_iteration=repair_iteration,
                        selected_paths=persisted_paths,
                    )
                if legacy_context_schema:
                    existing = compatible_manifest or manifest
                elif not compatible_manifest or existing.get("input_hash") != compatible_manifest.get("input_hash"):
                    raise DeveloperExecutionError(
                        "developer_input_drift",
                        "Approved Developer input changed after the execution session was created.",
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                    )
                else:
                    existing = manifest
                selection_policy_migrated = True
                persisted_required_paths_for_migration = persisted_paths
                _write_json_atomic(manifest_file, existing)
            manifest = existing
        else:
            _write_json_atomic(manifest_file, manifest)

        journal = (
            _read_json(journal_file, run_id=run_id)
            if journal_file.exists() and not legacy_file_manifest_migrated
            else _new_journal(run_id=run_id, manifest=manifest, mode=mode, repair_iteration=repair_iteration)
        )
        if legacy_file_manifest_migrated:
            journal["migrations"] = [
                {
                    "code": "file_items_replaced_by_execution_unit",
                    "legacy_manifest": self.store.relative(session_dir / "manifest_file_items_v1.json"),
                    "legacy_journal": self.store.relative(session_dir / "journal_file_items_v1.json"),
                    "migrated_at": utc_now_iso(),
                }
            ]
        if selection_policy_migrated:
            journal["manifest_input_hash"] = manifest.get("input_hash")
            for current_item in _dict_items(manifest.get("items")):
                current_entry = _journal_item(journal, str(current_item.get("id") or ""))
                context_paths = _item_context_paths(current_item)
                current_entry["paths"] = _item_paths(current_item)
                current_entry["context_paths"] = context_paths
                current_entry["direct_repair_paths"] = _item_direct_repair_paths(current_item)
                current_entry["diagnostic_paths"] = _item_diagnostic_paths(current_item)
                current_entry["contract_anchor_paths"] = _item_contract_anchor_paths(current_item)
                current_entry.pop("required_paths", None)
                if current_entry.get("status") != "completed":
                    current_entry["handled_paths"] = []
                    current_entry["inspected_paths"] = []
                    current_entry["unchanged_paths"] = []
                    current_entry["unresolved_paths"] = []
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
                        "persisted_item_paths": sorted(persisted_required_paths_for_migration),
                        "current_item_paths": sorted(
                            _manifest_required_paths(
                                _build_manifest(
                                    run_id=run_id,
                                    work_packet=work_packet,
                                    target_path=target_path,
                                    mode=mode,
                                    repair_feedback=repair_feedback,
                                    repair_iteration=repair_iteration,
                                )
                            )
                        ),
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
                )

        journal["status"] = "completed"
        journal["completed_at"] = utc_now_iso()
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)
        progress = _progress(journal)
        files = [
            completed_file
            for item in _dict_items(manifest.get("items"))
            for completed_file in _completed_files(
                item,
                _journal_item(journal, str(item.get("id") or "")),
            )
        ]
        return DeveloperExecutionResult(
            summary=(
                f"Developer tool loop completed {progress['completed_items']}/{progress['total_items']} "
                f"execution units in {mode} mode."
            ),
            files=files,
            manifest_path=manifest_path,
            journal_path=journal_path,
            progress=progress,
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
            self._begin_item(
                target_path=target_path,
                session_dir=session_dir,
                item=item,
                entry=entry,
                journal=journal,
                journal_file=journal_file,
            )
            mutated_paths: set[str] = set()
            reasons: list[str] = []
            for path in _item_paths(item):
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
                target = _safe_write_target(target_path, path, _item_paths(item), run_id=run_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(generated_file.get("content") or "").rstrip() + "\n", encoding="utf-8")
                mutated_paths.add(path)
                reason = str(generated_file.get("reason") or "")
                if reason:
                    reasons.append(reason)
            self._complete_item(
                item=item,
                entry=entry,
                target_path=target_path,
                summary="; ".join(reasons),
                mutated_paths=mutated_paths,
            )
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
        item_paths = _item_paths(item)
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
        mutated_paths: set[str] = set()
        repair_mode = str(manifest.get("mode") or "").endswith("_repair")
        context_repair = str(manifest.get("mode") or "") == "implementation_repair"
        context_paths = _item_context_paths(item) if context_repair else item_paths
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
                    mutated=bool(mutated_paths),
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
                        "Use tools to inspect and edit only the approved execution unit. Do not include analysis, "
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
                            f"Developer action for execution unit {item_id} exceeded the output budget after {length_failures} attempts.",
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
                            f"Developer returned invalid tool actions for execution unit {item_id}: {exc.message}",
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
                    mutated=bool(mutated_paths),
                    action_index=action_index,
                    max_actions=max_actions,
                    closure_resolved=True,
                    handled_paths=set(),
                    unresolved_paths=set(),
                )
                if policy_error:
                    result = {"ok": False, **policy_error}
                else:
                    result = _execute_action(
                        action=action,
                        run_id=run_id,
                        repo_root=target_path,
                        item_paths=item_paths,
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
                    mutated_path = str(result.get("path") or "").replace("\\", "/")
                    if mutated_path:
                        mutated_paths.add(mutated_path)
                    seen_observations.clear()
                inspected_paths = {
                    path for path in context_paths if _has_successful_target_inspection(history, item_path=path)
                }
                handled_paths = mutated_paths & set(context_paths)
                entry["handled_paths"] = sorted(handled_paths)
                entry["inspected_paths"] = sorted(inspected_paths)
                entry["unresolved_paths"] = []
                if result.get("error") == "no_effect_mutation":
                    entry["no_effect_actions"] = int(entry.get("no_effect_actions") or 0) + 1
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if action["action"] != "complete":
                    continue
                complete_arguments = _dict_value(action.get("arguments"))
                no_change_required = complete_arguments.get("no_change_required") is True
                declared_unchanged: set[str] = set()
                invalid_unchanged: list[str] = []
                for row in _dict_items(complete_arguments.get("unchanged_paths")):
                    path = str(row.get("path") or "").replace("\\", "/")
                    reason = str(row.get("reason") or "").strip()
                    if path not in context_paths or path not in inspected_paths or not reason:
                        invalid_unchanged.append(path or "<missing>")
                        continue
                    declared_unchanged.add(path)
                handled_paths = (mutated_paths & set(context_paths)) | declared_unchanged
                entry["handled_paths"] = sorted(handled_paths)
                entry["unchanged_paths"] = sorted(declared_unchanged)
                entry["unresolved_paths"] = []
                existing_paths = {path for path in item_paths if (target_path / path).is_file()}
                no_change_allowed = (
                    repair_mode
                    and no_change_required
                    and bool(declared_unchanged)
                    and not invalid_unchanged
                )
                completed_unit = bool(mutated_paths) and mutated_paths <= existing_paths
                if not completed_unit and not no_change_allowed:
                    history.append(
                        {
                            "action": "rejected",
                            "error": (
                                "complete requires at least one successful mutation. Repair feedback may instead use "
                                "no_change_required=true with unchanged_paths=[{path, reason}] after fresh inspection. "
                                f"invalid_unchanged_paths={invalid_unchanged}."
                            ),
                        }
                    )
                    continue
                self._complete_item(
                    item=item,
                    entry=entry,
                    target_path=target_path,
                    summary=str(action.get("arguments", {}).get("summary") or ""),
                    mutated_paths=mutated_paths,
                    no_change_required=no_change_allowed,
                )
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if on_progress:
                    on_progress(_progress(journal), manifest_path, journal_path)
                return
            raise DeveloperExecutionError(
                "developer_action_limit_exceeded",
                f"Developer did not complete execution unit {item_id} within {max_actions} tool actions.",
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
                f"Developer workspace tool failed for execution unit {item_id}: {exc}",
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
        snapshot = session_dir / "snapshots" / f"{item_id}.json"
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            files = []
            for path in _item_paths(item):
                target = _safe_write_target(
                    target_path,
                    path,
                    _item_paths(item),
                    run_id=str(journal.get("run_id") or ""),
                )
                files.append(
                    {
                        "path": path,
                        "existed": target.is_file(),
                        "content": target.read_text(encoding="utf-8", errors="replace") if target.is_file() else "",
                        "sha256": _file_hash(target),
                    }
                )
            _write_json_atomic(
                snapshot,
                {"files": files},
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
        target_path: Path,
        summary: str,
        mutated_paths: set[str],
        no_change_required: bool = False,
    ) -> None:
        entry.update(
            {
                "status": "completed",
                "completed_at": utc_now_iso(),
                "after_sha256_by_path": {
                    path: _file_hash(target_path / path) for path in _item_paths(item)
                },
                "mutated_paths": sorted(mutated_paths),
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
        snapshot_files = _dict_items(data.get("files"))
        if not snapshot_files and data.get("path"):
            snapshot_files = [data]
        for snapshot_file in snapshot_files:
            path = str(snapshot_file.get("path") or "")
            target = _safe_write_target(target_path, path, _item_paths(item), run_id="")
            if snapshot_file.get("existed"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(snapshot_file.get("content") or ""), encoding="utf-8")
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
            entry = _journal_item(journal, str(item.get("id") or ""))
            after_by_path = _dict_value(entry.get("after_sha256_by_path"))
            file_by_path = {str(row.get("path") or ""): row for row in _item_files(item)}
            for path in _item_paths(item):
                expected = str(after_by_path.get(path) or file_by_path.get(path, {}).get("initial_sha256") or "")
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
        "developer_patch_out_of_scope",
        "developer_patch_invalid",
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
    closure = build_repair_closure(work_packet=work_packet, feedback=repair_feedback, target_path=target_path)
    if repair_feedback and mode == "implementation_repair":
        context_selected = set(allowed)
    else:
        context_selected = (
            {path.strip().replace("\\", "/") for path in selected_paths}
            if selected_paths is not None
            else set(_string_items(closure.get("context_paths"))) if repair_feedback else set(allowed)
        )
    direct_selected = set(_string_items(closure.get("direct_repair_paths")))
    diagnostic_selected = set(
        _string_items(closure.get("diagnostic_paths"))
        or _string_items(closure.get("direct_repair_paths"))
    )
    anchor_selected = set(_string_items(closure.get("contract_anchor_paths")))
    proposed = {str(item.get("path") or ""): item for item in _dict_items(work_packet.get("proposed_files"))}
    coverage = _dict_value(work_packet.get("requirement_coverage_by_file"))
    mappings = _dict_value(work_packet.get("design_mappings_by_file"))
    canonical = {
        str(item.get("id") or ""): str(item.get("content") or "")
        for item in _dict_items(work_packet.get("canonical_requirements"))
    }
    steps = _dict_items(work_packet.get("implementation_steps"))
    execution_paths = set(allowed) if not repair_feedback or mode == "implementation_repair" else context_selected
    ordered = [path for path in allowed if path in execution_paths and "/src/test/" not in f"/{path}"]
    ordered.extend(path for path in allowed if path in execution_paths and "/src/test/" in f"/{path}")
    context_ordered = [path for path in ordered if path in context_selected]
    direct_ordered = [path for path in ordered if path in direct_selected]
    diagnostic_ordered = [path for path in ordered if path in diagnostic_selected]
    anchor_ordered = [path for path in ordered if path in anchor_selected]
    if repair_feedback and not context_ordered:
        raise HarnessError(
            "developer_repair_scope_empty",
            "Developer repair context has no approved paths.",
            run_id=run_id,
        )
    files: list[dict[str, object]] = []
    for path in ordered:
        req_ids = []
        for row in _dict_items(coverage.get(path)):
            value = str(row.get("requirement_id") or "")
            if value and value not in req_ids:
                req_ids.append(value)
        file_steps = []
        for row in steps:
            step_paths = [str(value).replace("\\", "/") for value in row.get("files", [])] if isinstance(row.get("files"), list) else []
            if path in step_paths:
                file_steps.append(
                    {
                        "order": row.get("order"),
                        "title": row.get("title"),
                        "description": row.get("description"),
                        "design_element_ids": row.get("design_element_ids", []),
                        "requirement_ids": row.get("requirement_ids", []),
                    }
                )
        design_rows = [
            {
                "design_element_id": row.get("design_element_id"),
                "planned_code_target": row.get("planned_code_target"),
                "message_type": row.get("message_type"),
                "trace_policy": row.get("trace_policy"),
            }
            for row in _dict_items(mappings.get(path))
        ]
        proposed_item = proposed.get(path, {})
        files.append(
            {
                "path": path,
                "operation": str(proposed_item.get("operation") or "create_or_update"),
                "purpose": str(proposed_item.get("purpose") or proposed_item.get("reason") or ""),
                "requirement_ids": req_ids,
                "requirements": [{"id": value, "content": canonical.get(value, "")} for value in req_ids],
                "design_mappings": design_rows,
                "implementation_steps": file_steps,
                "initial_sha256": _file_hash(target_path / path),
            }
        )
    if not files:
        raise HarnessError(
            "developer_repair_scope_empty",
            "Developer execution unit has no approved paths selected from the work packet.",
            run_id=run_id,
        )
    requirement_ids = list(
        dict.fromkeys(
            requirement_id
            for file_item in files
            for requirement_id in _string_items(file_item.get("requirement_ids"))
        )
    )
    item = {
        "id": "TASK-001",
        "title": "Repair approved implementation" if repair_feedback else "Implement approved plan",
        "paths": ordered,
        "context_paths": context_ordered,
        "direct_repair_paths": direct_ordered,
        "diagnostic_paths": diagnostic_ordered,
        "contract_anchor_paths": anchor_ordered,
        "files": files,
        "requirement_ids": requirement_ids,
        "requirements": [{"id": value, "content": canonical.get(value, "")} for value in requirement_ids],
        "implementation_steps": steps,
        "repair_closure": {
            **closure,
            "context_paths": context_ordered,
            "direct_repair_paths": direct_ordered,
            "diagnostic_paths": diagnostic_ordered,
            "contract_anchor_paths": anchor_ordered,
        } if repair_feedback else {},
    }
    input_payload = {
        "approved_plan": work_packet.get("approved_plan"),
        "effective_design_contract": work_packet.get("effective_design_contract"),
        "requirement_contract": work_packet.get("requirement_contract"),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "workspace_allowed_paths": allowed,
        "items": [
            {
                **item,
                "files": [{key: value for key, value in file_item.items() if key != "initial_sha256"} for file_item in files],
            }
        ],
        "repair_feedback": repair_feedback or {},
    }
    return {
        "version": "developer_execution_manifest_v5",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "created_at": utc_now_iso(),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "input_hash": _json_hash(input_payload),
        "workspace_allowed_paths": allowed,
        "repair_feedback": repair_feedback or {},
        "items": [item],
    }


def _new_journal(*, run_id: str, manifest: dict[str, object], mode: str, repair_iteration: int) -> dict[str, object]:
    return {
        "version": "developer_execution_journal_v3",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "manifest_input_hash": manifest.get("input_hash"),
        "status": "pending",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "items": [
            {
                "id": item.get("id"),
                "paths": _item_paths(item),
                "context_paths": _item_context_paths(item),
                "direct_repair_paths": _item_direct_repair_paths(item),
                "diagnostic_paths": _item_diagnostic_paths(item),
                "contract_anchor_paths": _item_contract_anchor_paths(item),
                "status": "pending",
                "attempts": 0,
                "actions": [],
                "handled_paths": [],
                "inspected_paths": [],
                "unchanged_paths": [],
                "unresolved_paths": [],
                "no_effect_actions": 0,
            }
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
    repair_mode = str(manifest.get("mode") or "").endswith("_repair")
    context_repair = str(manifest.get("mode") or "") == "implementation_repair"
    paths = _item_context_paths(item) if context_repair else _item_paths(item)
    diagnostic_paths = _item_diagnostic_paths(item) if context_repair else paths
    anchor_paths = _item_contract_anchor_paths(item) if context_repair else []
    handled_paths = _handled_paths(history, required_paths=paths)
    inspected_paths = _inspected_paths(history, required_paths=paths)
    phase = _execution_phase(history=history, mutated=mutated, action_index=action_index, max_actions=max_actions)
    action_memory = _action_memory(
        history,
        target_item_paths=paths,
        max_evidence_chars=max(8000, min(48000, context_window_tokens)),
    )
    action_schema = {
        "action": "list_files|read_file|search_code|read_diff|write_file|apply_patch|complete",
        "arguments": {},
        "summary": "short reason",
    }
    context = {
        "execution_task": _prompt_execution_task(item, context_paths=paths),
        "execution": {"mode": manifest.get("mode"), "base_sha": manifest.get("base_sha")},
        "repair_feedback": _developer_repair_context(manifest.get("repair_feedback")),
        "repair_progress": {
            "workspace_allowed_paths": _item_paths(item),
            "diagnostic_paths": diagnostic_paths,
            "contract_anchor_paths": anchor_paths,
            "repair_hints": _dict_value(item.get("repair_closure")).get("repair_hints", {}),
            "mutated_context_paths": sorted(handled_paths),
            "inspected_context_paths": sorted(inspected_paths),
            "completion_rule": (
                "Apply one coherent real mutation, including every related approved file needed to keep the worktree "
                "internally consistent, then call complete so Verification can rerun. Diagnostic paths are hints, "
                "not a mutation boundary. A no-change completion requires inspected evidence and a specific reason."
                if context_repair
                else "Complete after one coherent mutation; the owning quality gate will rerun."
            ),
        },
        "target_state": {
            path: {"exists": (target_path / path).is_file(), "sha256": _file_hash(target_path / path)}
            for path in paths
        },
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
    feedback_source = str(_dict_value(manifest.get("repair_feedback")).get("feedback_source") or "")
    phase_instruction = {
        "inspect_or_edit": (
            "Inspect only evidence that is still missing. Do not repeat an observation already recorded in "
            "working_memory.action_ledger. Start editing as soon as the target change is clear."
        ),
        "edit_required": (
            "The inspection budget is exhausted. Your next action MUST be write_file or apply_patch for an "
            "approved execution-unit path. Use working_memory.inspected_sources; do not read, list, or search again."
        ),
        "edit_or_complete": (
            "At least one mutation succeeded. Continue only when another change is necessary for the same coherent "
            "fix; otherwise call complete and let Verification run."
        ),
        "complete_required": "A mutation succeeded and only one action remains. You MUST call complete now.",
    }[phase]
    if not repair_mode:
        work_instruction = (
            "Choose the next coherent implementation slice yourself from the approved plan. Size it to fit the "
            "remaining tool-action and JSON output budget. Keep the worktree internally consistent and buildable. "
            "Finish that slice, then call complete; Verification and Validator will assess the accumulated worktree."
        )
    elif feedback_source == "verification":
        work_instruction = (
            "This is a Verification repair pass. The approved requirement, design, and work packet are the contract; "
            "the latest verification field is the current diagnostic and worktree files are the current state. Find "
            "the root cause and make the smallest coherent fix across any workspace_allowed_paths that must change. "
            "Diagnostic and contract-anchor paths are inspection hints only. Keep production code, callers, and tests "
            "consistent; do not regenerate unrelated files. Then call complete for Verification to rerun."
        )
    elif feedback_source == "validator_code_trace":
        work_instruction = (
            "Verification already passed. Implement one coherent bounded slice from Validator's repair_objective. "
            "Make each affected contract match its expected_signature and update only the dependent callers, "
            "implementations, or tests needed for that slice. Context paths may remain unchanged. Then call complete."
        )
    else:
        work_instruction = (
            "Repair the reported quality feedback from the current worktree as one coherent bounded slice. Context "
            "paths may be inspected without modification. Call complete when the mutation is ready for its quality gate."
        )
    if repair_mode and not mutated:
        work_instruction += (
            " If fresh inspection proves the reported failure is already resolved, call complete with "
            "no_change_required=true and list the inspected path plus a specific reason in unchanged_paths."
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
        "- write_file: arguments {path, expected_sha256, content}; any approved execution-unit path\n"
        "- apply_patch: arguments {path, expected_sha256, patch}; unified diff for an approved path. A headerless "
        "patch MUST begin with a complete hunk header such as @@ -10,2 +10,3 @@. Bare @@ is invalid.\n"
        "- complete: arguments {summary, no_change_required?, unchanged_paths?: [{path, reason}]}; normally after "
        "at least one successful mutation. no_change_required=true requires fresh inspected evidence.\n\n"
        f"A write_file content or apply_patch value must stay below {max_write_chars} characters. {extra}\n"
        "Return one bounded JSON action per response. Do not output source code outside the JSON action. "
        f"{work_instruction} {phase_instruction}\n\n"
        f"Execution context:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _developer_repair_context(value: object) -> dict[str, object]:
    """Keep Validator observations out of the action context sent to Developer."""

    feedback = _dict_value(value)
    if str(feedback.get("feedback_source") or "") != "validator_code_trace":
        return feedback

    actionable_links = [
        dict(item)
        for item in _dict_items(feedback.get("missing_links"))
        if item.get("blocking") is not False
        and str(item.get("owner") or "developer").strip().lower() == "developer"
        and str(item.get("recommended_action") or "developer_repair").strip().lower()
        == "developer_repair"
    ]
    messages = [str(item.get("message") or "").strip() for item in actionable_links]
    messages = [message for message in messages if message]
    summary = f"Validator reported {len(actionable_links)} actionable blocking link(s)."
    if messages:
        summary += " " + " | ".join(messages)

    allowed_keys = {
        "version",
        "run_id",
        "source_ref",
        "issue_key",
        "status",
        "repair_iteration",
        "repair_scope",
        "feedback_source",
        "severity",
        "required_action",
        "target_repo",
        "branch",
        "trace_artifact",
        "developer_constraints",
        "artifact_ref",
        "repair_objective",
        "repair_lineage",
    }
    return {
        **{key: item for key, item in feedback.items() if key in allowed_keys},
        "summary": summary,
        "missing_links": actionable_links,
    }


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
    closure_resolved: bool = True,
    handled_paths: set[str] | None = None,
    unresolved_paths: set[str] | None = None,
) -> dict[str, object] | None:
    name = str(action.get("action") or "")
    phase = _execution_phase(
        history=history,
        mutated=mutated,
        action_index=action_index,
        max_actions=max_actions,
    )
    handled_paths = handled_paths or set()
    unresolved_paths = unresolved_paths or set()
    action_path = str(_dict_value(action.get("arguments")).get("path") or "").replace("\\", "/")
    if name in _MUTATION_ACTIONS and unresolved_paths and action_path in handled_paths:
        return {
            "error": "repair_path_already_handled",
            "instruction": (
                "Advance to one of the unresolved repair paths before revisiting this file: "
                f"{sorted(unresolved_paths)}"
            ),
            "unresolved_paths": sorted(unresolved_paths),
        }
    if name in _OBSERVATION_ACTIONS and _action_signature(action) in seen_observations:
        return {
            "error": "duplicate_observation",
            "instruction": "Use the cached observation in working_memory and advance the execution unit.",
        }
    if name in _OBSERVATION_ACTIONS and phase == "edit_required":
        return {
            "error": "mutation_required",
            "instruction": "The inspection budget is exhausted; write_file or apply_patch an approved unit path now.",
        }
    if phase == "complete_required" and closure_resolved and name != "complete":
        return {
            "error": "completion_required",
            "instruction": "A mutation already succeeded and only one action remains; call complete now.",
        }
    return None


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
    target_item_paths: list[str],
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
    target_order = {path: index for index, path in enumerate(target_item_paths)}
    latest_reads.sort(key=lambda row: target_order.get(str(row.get("path")), len(target_order)))

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
            embedded_actions = _embedded_actions(text)
            if len(embedded_actions) == 1:
                payload = embedded_actions[0]
                normalization = "extracted_single_embedded_json_action"
            elif len(embedded_actions) > 1:
                message = (
                    f"Developer response contained {len(embedded_actions)} JSON actions. "
                    "Return exactly one JSON action and no surrounding text."
                )
            else:
                message = "Developer action is not valid JSON."
            if payload is None:
                raise HarnessError("developer_action_invalid", message) from exc
        else:
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


def _embedded_actions(text: str) -> list[dict[str, object]]:
    """Extract action objects without executing prose or a second action."""

    decoder = json.JSONDecoder()
    actions: list[dict[str, object]] = []
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
            actions.append(value)
        position = max(end, start + 1)
    return actions


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
    item_paths: list[str],
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
        target = _safe_write_target(repo_root, path, item_paths, run_id=run_id)
        content = args.get("content")
        if not isinstance(content, str) or len(content) > max_write_chars:
            return {"ok": False, "error": "write_content_too_large", "max_chars": max_write_chars}
        expected = str(args.get("expected_sha256") or "")
        before_sha256 = _file_hash(target)
        if expected != before_sha256:
            return {"ok": False, "error": "stale_file_hash", "actual_sha256": before_sha256}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        after_sha256 = _file_hash(target)
        if after_sha256 == before_sha256:
            return {
                "ok": False,
                "error": "no_effect_mutation",
                "path": path,
                "sha256": after_sha256,
                "instruction": "This content is already present. Advance to an unresolved repair path or complete.",
            }
        return {"ok": True, "path": path, "sha256": after_sha256, "chars": len(content)}
    if name == "apply_patch":
        path = str(args.get("path") or "")
        target = _safe_write_target(repo_root, path, item_paths, run_id=run_id)
        patch_text = args.get("patch")
        if not isinstance(patch_text, str) or len(patch_text) > max_write_chars:
            return {"ok": False, "error": "patch_too_large", "max_chars": max_write_chars}
        expected = str(args.get("expected_sha256") or "")
        before_sha256 = _file_hash(target)
        if expected != before_sha256:
            return {"ok": False, "error": "stale_file_hash", "actual_sha256": before_sha256}
        try:
            patch_text = _prepare_patch_text(patch_text, item_path=path, run_id=run_id)
        except HarnessError as exc:
            return {
                "ok": False,
                "error": exc.error_code,
                "detail": exc.message,
                "instruction": (
                    "Retry with a valid unified diff. A headerless patch must begin with a complete hunk "
                    "header such as @@ -10,2 +10,3 @@; bare @@ is invalid. Use write_file when valid hunk "
                    "ranges cannot be provided."
                ),
            }
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
        after_sha256 = _file_hash(target)
        if after_sha256 == before_sha256:
            return {
                "ok": False,
                "error": "no_effect_mutation",
                "path": path,
                "sha256": after_sha256,
                "instruction": "The patch produced no file change. Advance to an unresolved repair path.",
            }
        return {"ok": True, "path": path, "sha256": after_sha256}
    if name == "complete":
        return {"ok": True, "summary": str(args.get("summary") or "")}
    return {"ok": False, "error": "unsupported_action"}


def _selected_repair_paths(
    work_packet: dict[str, object],
    feedback: dict[str, object] | None,
    *,
    target_path: Path | None = None,
) -> set[str]:
    """Compatibility wrapper returning diagnostic hints, not the write boundary."""

    closure = build_repair_closure(
        work_packet=work_packet,
        feedback=feedback,
        target_path=target_path,
    )
    return set(
        _string_items(closure.get("diagnostic_paths"))
        or _string_items(closure.get("direct_repair_paths"))
    )


def _looks_like_test_path(path: str) -> bool:
    normalized = "/" + path.strip().replace("\\", "/").casefold().lstrip("/")
    name = Path(normalized).name
    return (
        "/src/test/" in normalized
        or normalized.startswith("/test/")
        or normalized.startswith("/tests/")
        or "/__tests__/" in normalized
        or name.startswith("test_")
        or name.endswith("test.java")
        or ".test." in name
        or ".spec." in name
    )


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


def _safe_write_target(
    repo_root: Path,
    raw: str,
    item_paths: list[str] | set[str] | tuple[str, ...] | str,
    *,
    run_id: str,
) -> Path:
    normalized = raw.strip().replace("\\", "/").lstrip("/")
    raw_paths = [item_paths] if isinstance(item_paths, str) else list(item_paths)
    allowed = {
        path.strip().replace("\\", "/").lstrip("/")
        for path in raw_paths
        if path.strip()
    }
    if normalized not in allowed:
        raise HarnessError(
            "developer_act_out_of_scope",
            f"Tool action tried to modify {raw}; approved execution-unit paths are {sorted(allowed)}.",
            run_id=run_id or None,
        )
    return _safe_read_path(repo_root, normalized, run_id=run_id)


def _read_path_allowed(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    if any(part in _READ_DENIED_NAMES for part in rel.parts):
        return False
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in {"pom.xml", "build.gradle", "settings.gradle", "gradlew"}


def _prepare_patch_text(patch_text: str, *, item_path: str, run_id: str) -> str:
    """Validate one bounded patch and add headers when arguments.path is authoritative."""

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
    if deletion:
        raise HarnessError(
            "developer_patch_out_of_scope",
            f"Patch actions may create or update approved path {normalized}, but may not delete it.",
            run_id=run_id,
        )
    if paths and paths != {normalized}:
        raise HarnessError(
            "developer_patch_out_of_scope",
            f"Patch headers must target only the approved arguments.path {normalized}.",
            run_id=run_id,
        )
    if paths:
        return patch_text

    stripped = patch_text.lstrip()
    if not stripped.startswith("@@ "):
        raise HarnessError(
            "developer_patch_invalid",
            "Headerless patch must start with a unified-diff @@ hunk.",
            run_id=run_id,
        )
    return f"--- a/{normalized}\n+++ b/{normalized}\n{stripped}"


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
    raise HarnessError("developer_execution_journal_invalid", f"Journal is missing execution unit {item_id}.")


def _item_files(item: dict[str, object]) -> list[dict[str, Any]]:
    files = _dict_items(item.get("files"))
    if files:
        return files
    if str(item.get("path") or ""):
        return [item]
    return []


def _item_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("paths"))]
    if declared:
        return list(dict.fromkeys(declared))
    return list(
        dict.fromkeys(
            str(file_item.get("path") or "").replace("\\", "/")
            for file_item in _item_files(item)
            if str(file_item.get("path") or "")
        )
    )


def _item_context_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("context_paths"))]
    if not declared:
        declared = [path.replace("\\", "/") for path in _string_items(item.get("required_paths"))]
    return list(dict.fromkeys(declared)) if declared else _item_paths(item)


def _item_direct_repair_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("direct_repair_paths"))]
    return list(dict.fromkeys(declared))


def _item_diagnostic_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("diagnostic_paths"))]
    if not declared:
        declared = _item_direct_repair_paths(item)
    return list(dict.fromkeys(declared))


def _item_contract_anchor_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("contract_anchor_paths"))]
    return list(dict.fromkeys(declared))


def _item_required_paths(item: dict[str, object]) -> list[str]:
    """Compatibility alias: legacy required paths are context, not a mutation checklist."""

    return _item_context_paths(item)


def _prompt_execution_task(item: dict[str, object], *, context_paths: list[str]) -> dict[str, object]:
    context = set(context_paths)
    steps = [
        row
        for row in _dict_items(item.get("implementation_steps"))
        if context.intersection(path.replace("\\", "/") for path in _string_items(row.get("files")))
    ]
    visible_item = {key: value for key, value in item.items() if key != "required_paths"}
    return {
        **visible_item,
        "paths": context_paths,
        "allowed_paths": _item_paths(item),
        "files": [row for row in _item_files(item) if str(row.get("path") or "") in context],
        "implementation_steps": steps,
    }


def _handled_paths(history: list[dict[str, object]], *, required_paths: list[str]) -> set[str]:
    handled: set[str] = set()
    required_folded = {path.casefold(): path for path in required_paths}
    for row in history:
        if row.get("action") not in _MUTATION_ACTIONS or not _dict_value(row.get("result")).get("ok"):
            continue
        path = str(_dict_value(row.get("result")).get("path") or "").replace("\\", "/")
        canonical = required_folded.get(path.casefold())
        if canonical:
            handled.add(canonical)
    return handled


def _inspected_paths(history: list[dict[str, object]], *, required_paths: list[str]) -> set[str]:
    return {path for path in required_paths if _has_successful_target_inspection(history, item_path=path)}


def _manifest_paths(manifest: dict[str, object]) -> set[str]:
    return {path for item in _dict_items(manifest.get("items")) for path in _item_paths(item)}


def _manifest_context_paths(manifest: dict[str, object]) -> set[str]:
    return {path for item in _dict_items(manifest.get("items")) for path in _item_context_paths(item)}


def _manifest_required_paths(manifest: dict[str, object]) -> set[str]:
    return _manifest_context_paths(manifest)


def _completed_files(item: dict[str, object], entry: dict[str, object]) -> list[dict[str, object]]:
    after_by_path = _dict_value(entry.get("after_sha256_by_path"))
    mutated_paths = set(_string_items(entry.get("mutated_paths")))
    result: list[dict[str, object]] = []
    for file_item in _item_files(item):
        path = str(file_item.get("path") or "")
        if path not in mutated_paths:
            continue
        result.append(
            {
                "path": path,
                "operation": file_item.get("operation"),
                "reason": file_item.get("purpose") or "Developer tool mutation recorded for this approved path.",
                "execution_summary": entry.get("summary"),
                "covered_design_elements": [
                    str(row.get("design_element_id") or "")
                    for row in _dict_items(file_item.get("design_mappings"))
                    if str(row.get("design_element_id") or "")
                ],
                "covered_requirement_ids": _string_items(file_item.get("requirement_ids")),
                "work_item_id": item.get("id"),
                "after_sha256": after_by_path.get(path),
                "no_change_required": False,
            }
        )
    return result


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
