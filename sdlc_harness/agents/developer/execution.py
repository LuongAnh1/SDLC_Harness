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
from ...llm.json_utils import final_response_text
from ...llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ...profiles import (
    VerificationStagePolicy,
    build_code_trace_policy,
    build_planning_path_policy,
    get_language_profile,
    get_project_profile,
    implementation_signature_contract_digest,
)
from ...repo_context import CodeGraphContextProvider
from ...storage.runtime import RuntimeStore, utc_now_iso
from ...verification import VerificationRunner
from ..validator.code_contract import deterministic_typescript_contract_findings
from ..validator.typescript_ast import TypeScriptAstExtractor
from .fake_generation import fake_developer_files
from .profile_context import (
    developer_generation_context,
    developer_path_is_test,
    developer_prompt_context,
)
from .repair_context import build_repair_closure


_LEGACY_ACTION_NAMES = {
    "list_files",
    "read_file",
    "search_code",
    "read_diff",
    "write_file",
    "apply_patch",
    "complete",
}
_UNIT_ACTION_NAMES = {
    "repo.list",
    "repo.read",
    "repo.search",
    "repo.read_diff",
    "codegraph.query_readonly",
    "repo.write_file",
    "repo.apply_patch",
    "verification.run_stage",
    "execution.complete_unit",
}
_ACTION_NAMES = _LEGACY_ACTION_NAMES | _UNIT_ACTION_NAMES
_ACTION_TO_LEGACY = {
    "repo.list": "list_files",
    "repo.read": "read_file",
    "repo.search": "search_code",
    "repo.read_diff": "read_diff",
    "repo.write_file": "write_file",
    "repo.apply_patch": "apply_patch",
    "execution.complete_unit": "complete",
}
_OBSERVATION_ACTIONS = {
    "list_files", "read_file", "search_code", "read_diff",
    "repo.list", "repo.read", "repo.search", "repo.read_diff", "codegraph.query_readonly",
}
_MUTATION_ACTIONS = {"write_file", "apply_patch", "repo.write_file", "repo.apply_patch"}
_RECOVERABLE_ACTION_POLICY_ERRORS = {
    "developer_act_out_of_scope",
    "developer_patch_out_of_scope",
    "developer_patch_invalid",
    "developer_test_context_required",
}
_MAX_IDENTICAL_DIAGNOSTIC_RESULTS = 3
_MAX_IDENTICAL_NO_EFFECT_MUTATIONS = 3
_WORKING_MEMORY_LEDGER_SOFT_LIMIT = 16
_WORKING_MEMORY_RECENT_RESULT_LIMIT = 4
_WORKING_MEMORY_BREADCRUMB_LIMIT = 8
_TIMEOUT_DIAGNOSTIC_SOURCE_MARKER = "SDLC_DIAGNOSTIC_INSTRUMENTATION"
_TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX = "SDLC_DIAGNOSTIC_TIMEOUT:"
_READ_DENIED_NAMES = {".env", ".git", ".runtime"}
_TEXT_SUFFIXES = {
    ".java", ".kt", ".py", ".js", ".ts", ".tsx", ".jsx", ".xml", ".json",
    ".yaml", ".yml", ".toml", ".gradle", ".properties", ".md", ".txt", ".sh", ".ps1",
    ".prisma", ".sql",
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
        session_path: str = "",
        failure_path: str = "",
        progress: dict[str, object] | None = None,
    ):
        super().__init__(error_code, message, run_id=run_id)
        self.retriable = retriable
        self.manifest_path = manifest_path
        self.journal_path = journal_path
        self.session_path = session_path
        self.failure_path = failure_path
        self.progress = progress or {}
        self.failure_classification = _failure_classification(error_code, retriable=retriable)

    def to_response(self) -> dict[str, object]:
        return {
            **super().to_response(),
            "failure_classification": self.failure_classification,
            **({"failure_artifact": self.failure_path} if self.failure_path else {}),
        }


@dataclass(frozen=True)
class DeveloperExecutionResult:
    summary: str
    files: list[dict[str, object]]
    manifest_path: str
    journal_path: str
    progress: dict[str, object]
    session_path: str = ""


class DeveloperToolLoopExecutor:
    """Execute approved code work as bounded JSON actions against one run worktree."""

    def __init__(
        self,
        *,
        settings: Any,
        store: RuntimeStore,
        llm_client: Any,
        repo_context_provider: CodeGraphContextProvider | None = None,
        verification_runner: VerificationRunner | None = None,
        step_review: dict[str, object] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.llm_client = llm_client
        self.repo_context_provider = repo_context_provider or CodeGraphContextProvider(
            fake_mode=getattr(settings, "llm_mode", "") == "fake"
        )
        self.verification_runner = verification_runner or VerificationRunner()
        self.step_review = dict(step_review or {})

    def execute(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        repair_feedback: dict[str, object] | None = None,
        repair_iteration: int = 0,
        session_iteration: int | None = None,
        repair_scope: str = "implementation",
        expected_dirty_paths: list[str] | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        on_progress: Callable[[dict[str, object], str, str], None] | None = None,
    ) -> DeveloperExecutionResult:
        normalized_scope = re.sub(r"[^a-z0-9_]+", "_", repair_scope.strip().lower()).strip("_")
        normalized_scope = normalized_scope or "implementation"
        mode = f"{normalized_scope}_repair" if repair_feedback else "act"
        session_ordinal = repair_iteration if session_iteration is None else max(0, session_iteration)
        session_name = (
            f"{mode}_v{session_ordinal}"
            if repair_feedback
            else f"act_v{max(1, session_ordinal)}"
        )
        session_dir = self.store.run_dir(run_id) / "developer_execution" / session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = session_dir / "manifest.json"
        journal_file = session_dir / "journal.json"
        session_file = session_dir / "session.json"
        manifest_path = self.store.relative(manifest_file)
        journal_path = self.store.relative(journal_file)
        session_path = self.store.relative(session_file)

        selection_policy_migrated = False
        context_contract_migrated = False
        entrypoint_scope_migrated = False
        scope_reopened_item_ids: list[str] = []
        persisted_required_paths_for_migration: set[str] = set()
        legacy_file_manifest_migrated = False
        manifest = _build_manifest(
            run_id=run_id,
            work_packet=work_packet,
            target_path=target_path,
            mode=mode,
            repair_feedback=repair_feedback,
            repair_iteration=repair_iteration,
            expected_dirty_paths=expected_dirty_paths,
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
                context_upgrade = _merge_context_only_manifest(existing, manifest)
                entrypoint_scope_upgrade = _merge_database_entrypoint_scope_manifest(
                    existing,
                    manifest,
                )
                legacy_context_schema = (
                    bool(repair_feedback)
                    and existing.get("version") in {
                        "developer_execution_manifest_v3",
                        "developer_execution_manifest_v4",
                        "developer_execution_manifest_v5",
                        "developer_execution_manifest_v6",
                    }
                    and manifest.get("version") == "developer_execution_manifest_v7"
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
                        expected_dirty_paths=expected_dirty_paths,
                    )
                if context_upgrade is not None:
                    existing = context_upgrade
                    context_contract_migrated = True
                elif entrypoint_scope_upgrade is not None:
                    existing, scope_reopened_item_ids = entrypoint_scope_upgrade
                    entrypoint_scope_migrated = True
                elif legacy_context_schema:
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
            migration_code = (
                "database_entrypoint_scope_migrated"
                if entrypoint_scope_migrated
                else "execution_context_contract_migrated"
                if context_contract_migrated
                else "repair_selection_policy_migrated"
            )
            if not any(
                isinstance(row, dict) and row.get("code") == migration_code
                for row in migrations
            ):
                migrations.append(
                    {
                        "code": migration_code,
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
                                    expected_dirty_paths=expected_dirty_paths,
                                )
                            )
                        ),
                        **(
                            {"reopened_item_ids": scope_reopened_item_ids}
                            if entrypoint_scope_migrated
                            else {}
                        ),
                        "migrated_at": utc_now_iso(),
                    }
                )
        if not scope_reopened_item_ids:
            scope_reopened_item_ids = _database_entrypoint_journal_reopen_ids(
                manifest=manifest,
                journal=journal,
            )
        if scope_reopened_item_ids:
            _reopen_database_entrypoint_items(
                target_path=target_path,
                session_dir=session_dir,
                manifest=manifest,
                journal=journal,
                item_ids=scope_reopened_item_ids,
            )
            migrations = journal.setdefault("migrations", [])
            if not any(
                isinstance(row, dict) and row.get("code") == "database_entrypoint_scope_migrated"
                for row in migrations
            ):
                migrations.append(
                    {
                        "code": "database_entrypoint_scope_migrated",
                        "reopened_item_ids": scope_reopened_item_ids,
                        "migrated_at": utc_now_iso(),
                    }
                )
            _write_json_atomic(journal_file, journal)
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
        _write_session(
            session_file,
            run_id=run_id,
            manifest=manifest,
            journal=journal,
            status="running",
        )

        try:
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
                    repair_feedback=repair_feedback,
                    repair_iteration=repair_iteration,
                )
            else:
                for item in _dict_items(manifest.get("items")):
                    item_id = str(item.get("id") or "")
                    entry = _journal_item(journal, item_id)
                    if entry.get("status") == "completed":
                        continue
                    incomplete_dependencies = [
                        dependency
                        for dependency in _string_items(item.get("depends_on"))
                        if _journal_item(journal, dependency).get("status") != "completed"
                    ]
                    if incomplete_dependencies:
                        raise DeveloperExecutionError(
                            "developer_unit_dependency_deadlock",
                            f"Execution unit {item_id} has incomplete dependencies: {incomplete_dependencies}",
                            run_id=run_id,
                            retriable=False,
                            manifest_path=manifest_path,
                            journal_path=journal_path,
                            session_path=session_path,
                            progress=_progress(journal),
                        )
                    self._execute_item(
                        run_id=run_id,
                        work_packet=work_packet,
                        target_path=target_path,
                        session_dir=session_dir,
                        item=item,
                        manifest=manifest,
                        journal=journal,
                        journal_file=journal_file,
                        on_progress=on_progress,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        verification_policy=verification_policy,
                    )
                    _write_session(
                        session_file,
                        run_id=run_id,
                        manifest=manifest,
                        journal=journal,
                        status="running",
                    )
        except DeveloperExecutionError as exc:
            exc.session_path = session_path
            failure_file = session_dir / "developer_failure_classification_v1.json"
            _write_failure_classification(
                failure_file,
                run_id=run_id,
                error=exc,
                manifest=manifest,
                journal=journal,
            )
            exc.failure_path = self.store.relative(failure_file)
            _write_session(
                session_file,
                run_id=run_id,
                manifest=manifest,
                journal=journal,
                status="paused" if exc.retriable else "blocked",
            )
            raise

        journal["status"] = "completed"
        journal["completed_at"] = utc_now_iso()
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)
        _write_session(
            session_file,
            run_id=run_id,
            manifest=manifest,
            journal=journal,
            status="completed",
        )
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
            session_path=session_path,
        )

    def recover_approved_step_review_state(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        item_id: str,
        pending_call_id: str,
        session_name: str = "act_v1",
    ) -> dict[str, object]:
        """Restore a rolled-back unit only from approved exchanges and matching journal hashes."""

        session_dir = self.store.run_dir(run_id) / "developer_execution" / session_name
        manifest = _read_json(session_dir / "manifest.json", run_id=run_id)
        journal_file = session_dir / "journal.json"
        journal = _read_json(journal_file, run_id=run_id)
        item = next(
            (row for row in _dict_items(manifest.get("items")) if str(row.get("id") or "") == item_id),
            None,
        )
        if item is None:
            raise HarnessError(
                "developer_step_review_recovery_invalid",
                f"Execution unit {item_id} is not present in the durable manifest.",
                run_id=run_id,
            )
        entry = _journal_item(journal, item_id)
        action_groups = [
            *[_dict_items(row.get("actions")) for row in _dict_items(entry.get("attempt_history"))],
            _dict_items(entry.get("actions")),
        ]
        source_by_index: dict[int, dict[str, object]] = {}
        for action_group in action_groups:
            for metadata in action_group:
                action_index = int(metadata.get("action_index") or 0)
                if action_index:
                    source_by_index[action_index] = metadata
        source_actions = [source_by_index[index] for index in sorted(source_by_index)]
        if not source_actions:
            raise HarnessError(
                "developer_step_review_recovery_invalid",
                f"Execution unit {item_id} has no durable tool history to recover.",
                run_id=run_id,
            )

        safe_item_id = re.sub(r"[^a-z0-9_-]+", "-", item_id.casefold()).strip("-") or "unit"
        review_root = session_dir / "step_review" / safe_item_id
        review_session = _read_json(review_root / "step_review_session_v1.json", run_id=run_id)
        review_calls = _dict_items(review_session.get("calls"))
        pending = next(
            (
                row
                for row in review_calls
                if str(row.get("call_id") or "") == pending_call_id
                and str(row.get("status") or "") == "pending"
            ),
            None,
        )
        if pending is None:
            raise HarnessError(
                "developer_step_review_recovery_invalid",
                f"Pending exchange {pending_call_id} is not available for recovery.",
                run_id=run_id,
            )
        approved_by_ordinal = {
            int(row.get("ordinal") or 0): row
            for row in review_calls
            if str(row.get("status") or "") == "approved"
        }

        history: list[dict[str, object]] = []
        mutated_paths: set[str] = set()
        seen_observations: set[str] = set()
        restored_paths: list[str] = []
        max_action_index = 0
        max_write_chars = max(
            2000,
            min(18000, (int(getattr(self.settings, "llm_structured_max_tokens", 8192)) - 1024) * 2),
        )
        item_paths = _item_paths(item)
        for metadata in sorted(source_actions, key=lambda row: int(row.get("action_index") or 0)):
            action_index = int(metadata.get("action_index") or 0)
            action_name = str(metadata.get("tool_action") or "")
            tool_call = _dict_value(metadata.get("tool_call"))
            arguments = _dict_value(tool_call.get("arguments"))
            result = _dict_value(metadata.get("tool_result"))
            if not action_index or not action_name or not tool_call or not result:
                continue
            max_action_index = max(max_action_index, action_index)
            history.append(
                {
                    "action": action_name,
                    "arguments": arguments,
                    "result": result,
                    "summary": str(metadata.get("action_summary") or ""),
                    "tool_call": tool_call,
                    "tool_result": {
                        "version": "role_tool_result_v1",
                        "call_id": tool_call.get("call_id"),
                        "tool_id": action_name,
                        "result": result,
                    },
                }
            )
            if result.get("ok") is not True:
                continue
            if action_name in _OBSERVATION_ACTIONS:
                seen_observations.add(_action_signature({"action": action_name, "arguments": arguments}))
                continue
            if action_name not in _MUTATION_ACTIONS:
                continue
            approved = approved_by_ordinal.get(action_index)
            parsed = _dict_value(approved.get("parsed_output")) if approved else {}
            parsed_arguments = _dict_value(parsed.get("arguments"))
            path = str(parsed_arguments.get("path") or "").replace("\\", "/")
            if parsed.get("action") != action_name or path not in set(item_paths):
                raise HarnessError(
                    "developer_step_review_recovery_invalid",
                    f"Approved exchange for action {action_index} does not match journaled mutation scope.",
                    run_id=run_id,
                )
            payload_key = "patch" if action_name.endswith("apply_patch") else "content"
            payload = parsed_arguments.get(payload_key)
            expected_payload_hash = str(arguments.get(f"{payload_key}_sha256") or "")
            if not isinstance(payload, str) or hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_payload_hash:
                raise HarnessError(
                    "developer_step_review_recovery_invalid",
                    f"Approved exchange payload hash does not match journaled action {action_index}.",
                    run_id=run_id,
                )
            expected_after = str(result.get("sha256") or "")
            if _file_hash(target_path / path) != expected_after:
                replayed = _execute_action(
                    action=parsed,
                    run_id=run_id,
                    work_packet=work_packet,
                    repo_root=target_path,
                    item_paths=item_paths,
                    item=item,
                    session_dir=session_dir,
                    max_write_chars=max_write_chars,
                    repo_context_provider=self.repo_context_provider,
                    verification_runner=self.verification_runner,
                    verification_policy=None,
                )
                if replayed.get("ok") is not True or str(replayed.get("sha256") or "") != expected_after:
                    raise HarnessError(
                        "developer_step_review_recovery_invalid",
                        f"Recovery hash check failed for {path}: {replayed}.",
                        run_id=run_id,
                    )
                restored_paths.append(path)
            mutated_paths.add(path)
            seen_observations.clear()

        if not history or max_action_index >= int(pending.get("ordinal") or 0):
            raise HarnessError(
                "developer_step_review_recovery_invalid",
                "Durable history does not end immediately before the pending exchange.",
                run_id=run_id,
            )
        working_state_file = session_dir / "units" / item_id / "working_state.json"
        _write_json_atomic(
            working_state_file,
            {
                "version": "developer_working_state_v1",
                "unit_id": item_id,
                "history": history,
                "seen_observations": sorted(seen_observations),
                "mutated_paths": sorted(mutated_paths),
                "length_failures": 0,
                "invalid_action_failures": 0,
                "next_action_index": max_action_index + 1,
                "review_cursor": max_action_index,
                "updated_at": utc_now_iso(),
            },
        )
        entry["status"] = "review_required"
        entry["working_state_path"] = self.store.relative(working_state_file)
        entry["paused_sha256_by_path"] = {
            path: _file_hash(target_path / path) for path in item_paths
        }
        entry["step_review_recovery"] = {
            "version": "developer_step_review_recovery_v1",
            "pending_call_id": pending_call_id,
            "restored_paths": restored_paths,
            "source_action_count": len(history),
            "recovered_at": utc_now_iso(),
        }
        journal["status"] = "paused"
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(journal_file, journal)
        return dict(entry["step_review_recovery"])

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
        repair_feedback: dict[str, object] | None,
        repair_iteration: int,
    ) -> None:
        del repair_iteration
        generated_files = fake_developer_files(work_packet)
        by_path = {str(item.get("path") or ""): item for item in generated_files}
        for item in _dict_items(manifest.get("items")):
            item_id = str(item.get("id") or "")
            entry = _journal_item(journal, item_id)
            if entry.get("status") == "completed":
                continue
            incomplete_dependencies = [
                dependency
                for dependency in _string_items(item.get("depends_on"))
                if _journal_item(journal, dependency).get("status") != "completed"
            ]
            if incomplete_dependencies:
                raise DeveloperExecutionError(
                    "developer_unit_dependency_deadlock",
                    f"Execution unit {item_id} has incomplete dependencies: {incomplete_dependencies}",
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
            mutated_paths: set[str] = set()
            inspected_paths: set[str] = set()
            unchanged_paths: set[str] = set()
            reasons: list[str] = []
            for path in _item_paths(item):
                generated_file = by_path.get(path)
                if not generated_file:
                    if repair_feedback:
                        inspected_paths.add(path)
                        unchanged_paths.add(path)
                    continue
                target = _safe_write_target(target_path, path, _item_paths(item), run_id=run_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                before_sha256 = _file_hash(target)
                target.write_text(str(generated_file.get("content") or "").rstrip() + "\n", encoding="utf-8")
                inspected_paths.add(path)
                if _file_hash(target) == before_sha256:
                    unchanged_paths.add(path)
                else:
                    mutated_paths.add(path)
                reason = str(generated_file.get("reason") or "")
                if reason:
                    reasons.append(reason)
            unit_aware = manifest.get("version") == "developer_execution_manifest_v8"
            if not mutated_paths and not repair_feedback and not unit_aware:
                raise DeveloperExecutionError(
                    "developer_execution_incomplete",
                    "Fake Developer did not mutate any approved implementation path.",
                    run_id=run_id,
                    retriable=False,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                )
            entry["inspected_paths"] = sorted(inspected_paths)
            entry["unchanged_paths"] = sorted(unchanged_paths)
            fake_evidence = [
                {
                    "stage_id": stage_id,
                    "status": "passed",
                    "unit_id": item_id,
                    "input_hashes": {
                        path: _file_hash(target_path / path) for path in _item_paths(item)
                    },
                    "mode": "fake",
                }
                for stage_id in _string_items(item.get("required_stage_ids"))
            ]
            self._complete_item(
                item=item,
                entry=entry,
                target_path=target_path,
                session_dir=session_dir,
                summary="; ".join(reasons) or "Deterministic fake Developer completed the execution unit.",
                mutated_paths=mutated_paths,
                no_change_required=bool(not mutated_paths and (repair_feedback or unit_aware)),
                verification_evidence=fake_evidence,
            )
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            if on_progress:
                on_progress(_progress(journal), manifest_path, journal_path)

    def _execute_item(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        session_dir: Path,
        item: dict[str, object],
        manifest: dict[str, object],
        journal: dict[str, object],
        journal_file: Path,
        on_progress: Callable[[dict[str, object], str, str], None] | None,
        manifest_path: str,
        journal_path: str,
        verification_policy: VerificationStagePolicy | None,
        initial_history: list[dict[str, object]] | None = None,
    ) -> None:
        item_id = str(item.get("id") or "")
        entry = _journal_item(journal, item_id)
        item_paths = _item_paths(item)
        working_state_file = session_dir / "units" / item_id / "working_state.json"
        resuming_step_review = entry.get("status") == "review_required" and working_state_file.is_file()
        if resuming_step_review:
            working_state = _read_json(working_state_file, run_id=run_id)
            entry["status"] = "running"
            journal["status"] = "running"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
        else:
            self._begin_item(
                target_path=target_path,
                session_dir=session_dir,
                item=item,
                entry=entry,
                journal=journal,
                journal_file=journal_file,
            )
            history_seed: list[dict[str, object]] = list(initial_history or [])
            prior_feedback = _prior_attempt_feedback(entry)
            if prior_feedback:
                history_seed.append(prior_feedback)
            working_state = {
                "history": history_seed,
                "seen_observations": [],
                "mutated_paths": [],
                "length_failures": 0,
                "invalid_action_failures": 0,
                "next_action_index": 1,
                "review_cursor": 0,
            }
        history = _dict_items(working_state.get("history"))
        refreshed_dependency_ids = set(_string_items(working_state.get("refreshed_dependency_ids")))
        refreshed_now: list[str] = []
        for dependency_id in _string_items(item.get("depends_on")):
            dependency_entry = _journal_item(journal, dependency_id)
            if (
                dependency_id in refreshed_dependency_ids
                or dependency_entry.get("status") != "completed"
                or not dependency_entry.get("scope_reopened_at")
            ):
                continue
            stale_stage_ids = sorted(_verification_repair_state(history))
            history.append(
                {
                    "action": "upstream_dependency_completed",
                    "result": {
                        "ok": True,
                        "dependency_id": dependency_id,
                        "changed_paths": _string_items(dependency_entry.get("mutated_paths")),
                        "stale_stage_ids": stale_stage_ids,
                    },
                    "instruction": (
                        "An upstream dependency completed after this unit's previous diagnostics. "
                        "Treat the listed failures as stale and rerun those verification stages before "
                        "making another mutation."
                    ),
                }
            )
            refreshed_dependency_ids.add(dependency_id)
            refreshed_now.append(dependency_id)
        if refreshed_now and resuming_step_review:
            _supersede_pending_step_review(
                session_dir=session_dir,
                journal=journal,
                reason="upstream_dependency_completed",
            )
        seen_observations = _current_observation_signatures(history)
        mutated_paths = set(_string_items(working_state.get("mutated_paths")))
        repair_mode = str(manifest.get("mode") or "").endswith("_repair")
        context_paths = _item_context_paths(item)
        length_failures = int(working_state.get("length_failures") or 0)
        invalid_action_failures = int(working_state.get("invalid_action_failures") or 0)
        next_action_index = max(1, int(working_state.get("next_action_index") or 1))
        review_cursor = max(0, int(working_state.get("review_cursor") or 0))
        configured_max_actions = int(getattr(self.settings, "developer_max_actions_per_item", 24))
        timeout_action_reserve = 2 * max(1, len(_string_items(item.get("required_stage_ids")))) + 2
        maximum_action_limit = configured_max_actions + timeout_action_reserve
        max_retries = int(getattr(self.settings, "developer_max_action_retries", 2))
        configured_max_tokens = int(getattr(self.settings, "llm_structured_max_tokens", 8192))
        context_window_tokens = int(getattr(self.settings, "llm_context_window_tokens", 128000))
        max_write_chars = max(2000, min(18000, (configured_max_tokens - 1024) * 2))
        llm_client = self.llm_client
        if bool(self.step_review.get("enabled")):
            relative_session = session_dir.relative_to(self.store.run_dir(run_id))
            safe_item_id = re.sub(r"[^a-z0-9_-]+", "-", item_id.casefold()).strip("-") or "unit"
            review_root = session_dir / "step_review" / safe_item_id
            llm_client = ReviewableLlmClient(
                self.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.step_review,
                call_prefix=f"developer-{safe_item_id}",
                execution_dir=(relative_session / "step_review" / safe_item_id).as_posix(),
                error_prefix="developer",
                label=f"Developer {item_id}",
                start_ordinal=_step_review_start_ordinal(
                    session_path=review_root / "step_review_session_v1.json",
                    requested_cursor=review_cursor,
                    run_id=run_id,
                ),
            )

        def persist_working_state(next_index: int, *, cursor: int | None = None) -> None:
            current_cursor = int(getattr(llm_client, "cursor", 0)) if cursor is None else cursor
            payload = {
                "version": "developer_working_state_v1",
                "unit_id": item_id,
                "history": history,
                "seen_observations": sorted(seen_observations),
                "mutated_paths": sorted(mutated_paths),
                "length_failures": length_failures,
                "invalid_action_failures": invalid_action_failures,
                "next_action_index": next_index,
                "review_cursor": max(0, current_cursor),
                "refreshed_dependency_ids": sorted(refreshed_dependency_ids),
                "updated_at": utc_now_iso(),
            }
            _write_json_atomic(working_state_file, payload)
            entry["working_state_path"] = self.store.relative(working_state_file)
            entry["paused_sha256_by_path"] = {
                path: _file_hash(target_path / path) for path in item_paths
            }

        try:
            for action_index in range(next_action_index, maximum_action_limit + 1):
                max_actions = _timeout_diagnostic_action_limit(
                    configured_max_actions=configured_max_actions,
                    timeout_action_reserve=timeout_action_reserve,
                    history=history,
                )
                if action_index > max_actions:
                    break
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
                completion = llm_client.complete_with_metadata(
                    system=(
                        "You are Developer Executor in a controlled SDLC worktree. One response proposes exactly "
                        "one JSON action and then stops. The action is not executed until the Harness returns its "
                        "tool result in a later turn. Never narrate reasoning, emit or repeat a second action, or "
                        "claim an action ran without a Harness tool result. Use tools only within the approved "
                        "execution unit; do not include analysis, thinking markers, or markdown."
                    ),
                    prompt=prompt,
                    json_mode=True,
                    max_tokens=max_tokens,
                )
                journal.pop("pending_step_review", None)
                raw = str(getattr(completion, "content", ""))
                call_id = f"{item_id}:a{int(entry.get('attempts') or 1)}:c{action_index}"
                metadata = {
                    "call_id": call_id,
                    "item_attempt": int(entry.get("attempts") or 1),
                    "action_index": action_index,
                    "finish_reason": str(getattr(completion, "finish_reason", "") or ""),
                    "usage": getattr(completion, "usage", {}) if isinstance(getattr(completion, "usage", {}), dict) else {},
                    "max_tokens": max_tokens,
                    "created_at": utc_now_iso(),
                    "replayed": getattr(llm_client, "last_replay_kind", "") == "approved",
                }
                if metadata["replayed"] is not True:
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
                    persist_working_state(action_index + 1)
                    _write_json_atomic(journal_file, journal)
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
                    persist_working_state(action_index + 1)
                    _write_json_atomic(journal_file, journal)
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
                if (
                    manifest.get("version") == "developer_execution_manifest_v8"
                    and action.get("action") not in _UNIT_ACTION_NAMES
                ):
                    history.append(
                        {
                            "action": "rejected",
                            "error": "developer_unit_legacy_tool_not_allowed",
                            "instruction": "Use one canonical Developer Tool Action v2 tool ID.",
                        }
                    )
                    persist_working_state(action_index + 1)
                    _write_json_atomic(journal_file, journal)
                    continue
                current_hashes = {path: _file_hash(target_path / path) for path in item_paths}
                repair_state = _verification_repair_state(history)
                failed_stage_ids = sorted(repair_state)
                policy_error = _new_test_context_policy_error(
                    action=action,
                    history=history,
                    manifest=manifest,
                    target_path=target_path,
                    item_paths=item_paths,
                ) or _action_policy_error(
                    action=action,
                    history=history,
                    seen_observations=seen_observations,
                    mutated=bool(mutated_paths),
                    action_index=action_index,
                    max_actions=max_actions,
                    pending_stage_ids=sorted(
                        set(_string_items(item.get("required_stage_ids")))
                        - _current_passed_stages(
                            history,
                            current_hashes=current_hashes,
                        )
                    ),
                    failed_stage_ids=failed_stage_ids,
                    repair_mutated_stage_ids=sorted(
                        stage_id
                        for stage_id, episode in repair_state.items()
                        if episode.get("mutated_since_failure") is True
                    ),
                    repair_verification_ready_stage_ids=sorted(
                        stage_id
                        for stage_id, episode in repair_state.items()
                        if episode.get("verification_ready") is True
                    ),
                    repair_verification_forced_stage_ids=sorted(
                        stage_id
                        for stage_id, episode in repair_state.items()
                        if episode.get("verification_forced") is True
                    ),
                    closure_resolved=True,
                    handled_paths=set(),
                    unresolved_paths=set(),
                )
                if policy_error:
                    result = {"ok": False, **policy_error}
                else:
                    try:
                        result = _execute_action(
                            action=action,
                            run_id=run_id,
                            work_packet=work_packet,
                            repo_root=target_path,
                            item_paths=item_paths,
                            item=item,
                            session_dir=session_dir,
                            max_write_chars=max_write_chars,
                            repo_context_provider=self.repo_context_provider,
                            verification_runner=self.verification_runner,
                            verification_policy=verification_policy,
                        )
                    except HarnessError as exc:
                        if exc.error_code in _RECOVERABLE_ACTION_POLICY_ERRORS:
                            repeat_count = 1 + sum(
                                1
                                for row in history
                                if _dict_value(row.get("result")).get("error") == exc.error_code
                            )
                            result = {
                                "ok": False,
                                "error": exc.error_code,
                                "reason": exc.message,
                                "instruction": (
                                    "Do not create or modify that path. Rework the implementation and tests "
                                    "using only the approved execution-unit paths."
                                ),
                                "approved_paths": item_paths,
                                "policy_repeat_count": repeat_count,
                            }
                        else:
                            raise DeveloperExecutionError(
                                exc.error_code,
                                exc.message,
                                run_id=run_id,
                                retriable=(
                                    exc.error_code == "llm_transport_failed"
                                    or exc.error_code.startswith("target_repo_codegraph")
                                ),
                                manifest_path=manifest_path,
                                journal_path=journal_path,
                                progress=_progress(journal),
                            ) from exc
                if (
                    action["action"] == "verification.run_stage"
                    and result.get("status") == "failed"
                    and result.get("failure_kind") == "implementation"
                ):
                    diagnostic_fingerprint = _verification_diagnostic_fingerprint(result)
                    result["diagnostic_fingerprint"] = diagnostic_fingerprint
                    result["diagnostic_repeat_count"] = 1 + _diagnostic_repeat_count(
                        history,
                        fingerprint=diagnostic_fingerprint,
                    )
                timeout_diagnostic = _timeout_diagnostic_state(history)
                if (
                    result.get("ok") is True
                    and _is_timeout_diagnostic_instrumentation_action(
                        action,
                        timeout_diagnostic=timeout_diagnostic,
                    )
                ):
                    result["timeout_diagnostic_instrumentation"] = True
                history.append(
                    {
                        "action": action["action"],
                        "arguments": _action_history_arguments(action),
                        "result": result,
                        "summary": str(action.get("summary") or "").strip()[:500],
                        "tool_call": {
                            "version": "role_tool_call_v1",
                            "call_id": call_id,
                            "tool_id": action["action"],
                            "arguments": _action_history_arguments(action),
                        },
                        "tool_result": {
                            "version": "role_tool_result_v1",
                            "call_id": call_id,
                            "tool_id": action["action"],
                            "result": _clip_json_value(result, 4000),
                        },
                    }
                )
                entry["actions"][-1]["tool_action"] = action["action"]
                entry["actions"][-1]["action_summary"] = history[-1]["summary"]
                entry["actions"][-1]["tool_call"] = history[-1]["tool_call"]
                entry["actions"][-1]["tool_result"] = _clip_json_value(result, 4000)
                if (
                    action["action"] == "verification.run_stage"
                    and result.get("ok") is not True
                    and result.get("failure_kind") == "environment"
                ):
                    raise DeveloperExecutionError(
                        "developer_verification_environment_failed",
                        str(result.get("reason") or "Developer unit verification environment failed."),
                        run_id=run_id,
                        retriable=True,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        progress=_progress(journal),
                    )
                if action["action"] in _OBSERVATION_ACTIONS and result.get("ok"):
                    seen_observations.update(_observation_signatures(action, result=result))
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
                persist_working_state(action_index + 1)
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if (
                    result.get("error") in _RECOVERABLE_ACTION_POLICY_ERRORS
                    and int(result.get("policy_repeat_count") or 0) > max_retries
                ):
                    raise DeveloperExecutionError(
                        str(result.get("error") or "developer_act_out_of_scope"),
                        str(result.get("reason") or "Developer action remained outside the approved unit scope."),
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        progress=_progress(journal),
                    )
                if (
                    result.get("error") in {"duplicate_no_effect_mutation", "verification_repair_ready"}
                    and int(result.get("repeat_count") or 0) >= _MAX_IDENTICAL_NO_EFFECT_MUTATIONS
                ):
                    raise DeveloperExecutionError(
                        "developer_repair_stalled",
                        (
                            "Developer repeated the same no-effect mutation "
                            f"{_MAX_IDENTICAL_NO_EFFECT_MUTATIONS} times for "
                            f"{_dict_value(action.get('arguments')).get('path') or '<unknown>'}."
                        ),
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        progress=_progress(journal),
                    )
                if (
                    result.get("error") == "duplicate_observation"
                    and int(result.get("repeat_count") or 0) >= _MAX_IDENTICAL_DIAGNOSTIC_RESULTS
                ):
                    raise DeveloperExecutionError(
                        "developer_repair_stalled",
                        (
                            "Developer repeated the same cached observation "
                            f"{_MAX_IDENTICAL_DIAGNOSTIC_RESULTS} times instead of advancing the repair."
                        ),
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        progress=_progress(journal),
                    )
                if (
                    action["action"] == "verification.run_stage"
                    and int(result.get("diagnostic_repeat_count") or 0)
                    >= _MAX_IDENTICAL_DIAGNOSTIC_RESULTS
                ):
                    raise DeveloperExecutionError(
                        "developer_repair_stalled",
                        (
                            f"Developer verification stage {result.get('stage_id') or '<unknown>'} returned the "
                            f"same diagnostics {_MAX_IDENTICAL_DIAGNOSTIC_RESULTS} times."
                        ),
                        run_id=run_id,
                        retriable=False,
                        manifest_path=manifest_path,
                        journal_path=journal_path,
                        progress=_progress(journal),
                    )
                if action["action"] not in {"complete", "execution.complete_unit"}:
                    continue
                diagnostic_completion_error = _timeout_diagnostic_completion_error(
                    target_path=target_path,
                    item_paths=item_paths,
                )
                if diagnostic_completion_error:
                    entry["actions"][-1]["completion_gate"] = {
                        "status": "rejected",
                        **diagnostic_completion_error,
                    }
                    history.append({"action": "rejected", **diagnostic_completion_error})
                    _write_json_atomic(journal_file, journal)
                    continue
                complete_arguments = _dict_value(action.get("arguments"))
                no_change_required = complete_arguments.get("no_change_required") is True
                declared_unchanged: set[str] = set()
                declared_dispositions: dict[str, dict[str, str]] = {}
                invalid_unchanged: list[str] = []
                for row in _dict_items(complete_arguments.get("unchanged_paths")):
                    path = str(row.get("path") or "").replace("\\", "/")
                    reason = str(row.get("reason") or "").strip()
                    if path not in context_paths or path not in inspected_paths or not reason:
                        invalid_unchanged.append(path or "<missing>")
                        continue
                    declared_unchanged.add(path)
                    declared_dispositions[path] = {"status": "verified_unchanged", "reason": reason}
                if manifest.get("version") == "developer_execution_manifest_v8":
                    for row in _dict_items(complete_arguments.get("path_dispositions")):
                        path = str(row.get("path") or "").replace("\\", "/")
                        status = str(row.get("status") or "").strip()
                        reason = str(row.get("reason") or "").strip()
                        valid = (
                            path in item_paths
                            and status in {"mutated", "verified_unchanged", "not_required"}
                            and bool(reason)
                            and (
                                (status == "mutated" and path in mutated_paths)
                                or (status != "mutated" and path in inspected_paths)
                            )
                        )
                        if not valid:
                            invalid_unchanged.append(path or "<missing>")
                            continue
                        declared_dispositions[path] = {"status": status, "reason": reason}
                        if status != "mutated":
                            declared_unchanged.add(path)
                handled_paths = (mutated_paths & set(context_paths)) | declared_unchanged
                entry["handled_paths"] = sorted(handled_paths)
                entry["unchanged_paths"] = sorted(declared_unchanged)
                covered_paths = set(mutated_paths) | set(declared_dispositions)
                unresolved_paths = set(item_paths) - covered_paths
                entry["unresolved_paths"] = (
                    sorted(unresolved_paths)
                    if manifest.get("version") == "developer_execution_manifest_v8"
                    else []
                )
                entry["path_dispositions"] = {
                    path: declared_dispositions.get(
                        path,
                        {"status": "mutated", "reason": "Successful bounded mutation."},
                    )
                    for path in sorted(covered_paths)
                }
                existing_paths = {path for path in item_paths if (target_path / path).is_file()}
                verified_unchanged_completion = (
                    manifest.get("version") == "developer_execution_manifest_v8"
                    and bool(item_paths)
                    and set(item_paths) <= existing_paths
                    and not invalid_unchanged
                    and not unresolved_paths
                    and all(
                        declared_dispositions.get(path, {}).get("status") == "verified_unchanged"
                        for path in item_paths
                    )
                )
                no_change_allowed = verified_unchanged_completion or (
                    repair_mode
                    and no_change_required
                    and bool(declared_unchanged)
                    and not invalid_unchanged
                )
                completed_unit = (
                    bool(mutated_paths)
                    and mutated_paths <= existing_paths
                    and (
                        not unresolved_paths
                        if manifest.get("version") == "developer_execution_manifest_v8"
                        else True
                    )
                )
                if not completed_unit and not no_change_allowed:
                    entry["actions"][-1]["completion_gate"] = {
                        "status": "rejected",
                        "reason": "path_disposition_or_mutation_missing",
                        "unresolved_paths": sorted(unresolved_paths),
                    }
                    history.append(
                        {
                            "action": "rejected",
                            "error": (
                                "complete requires at least one successful mutation and a disposition for every unit "
                                "path. Repair feedback may instead use "
                                "no_change_required=true with unchanged_paths=[{path, reason}] after fresh inspection. "
                                f"invalid_paths={invalid_unchanged}; unresolved_paths={sorted(unresolved_paths)}."
                            ),
                        }
                    )
                    _write_json_atomic(journal_file, journal)
                    continue
                required_stages = set(_string_items(item.get("required_stage_ids")))
                current_hashes = {path: _file_hash(target_path / path) for path in item_paths}
                missing_stages = sorted(
                    required_stages - _current_passed_stages(history, current_hashes=current_hashes)
                )
                if missing_stages:
                    entry["actions"][-1]["completion_gate"] = {
                        "status": "rejected",
                        "reason": "required_verification_missing",
                        "missing_stage_ids": missing_stages,
                    }
                    history.append(
                        {
                            "action": "rejected",
                            "error": "developer_unit_verification_missing",
                            "instruction": f"Run required verification stages on current hashes: {missing_stages}",
                        }
                    )
                    _write_json_atomic(journal_file, journal)
                    continue
                signature_evidence = _unit_signature_evidence(
                    run_id=run_id,
                    target_path=target_path,
                    work_packet=work_packet,
                    item=item,
                )
                if signature_evidence.get("status") == "failed":
                    findings = _dict_items(signature_evidence.get("findings"))
                    entry["actions"][-1]["completion_gate"] = {
                        "status": "rejected",
                        "reason": "approved_signature_mismatch",
                        "findings": _clip_json_value(findings, 4000),
                    }
                    history.append(
                        {
                            "action": "rejected",
                            "error": "developer_unit_signature_mismatch",
                            "instruction": "Repair the approved signatures before completing this unit.",
                            "findings": findings,
                        }
                    )
                    _write_json_atomic(journal_file, journal)
                    continue
                entry["signature_evidence"] = signature_evidence
                entry["actions"][-1]["completion_gate"] = {"status": "passed"}
                self._complete_item(
                    item=item,
                    entry=entry,
                    target_path=target_path,
                    session_dir=session_dir,
                    summary=str(action.get("arguments", {}).get("summary") or ""),
                    mutated_paths=mutated_paths,
                    no_change_required=no_change_allowed,
                    verification_evidence=_stage_evidence(history),
                )
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                if on_progress:
                    on_progress(_progress(journal), manifest_path, journal_path)
                return
            active_repair = _verification_repair_state(history)
            error_code = "developer_repair_budget_exhausted" if active_repair else "developer_action_limit_exceeded"
            message = (
                f"Developer did not finish repair for execution unit {item_id} within {max_actions} tool actions."
                if active_repair
                else f"Developer did not complete execution unit {item_id} within {max_actions} tool actions."
            )
            raise DeveloperExecutionError(
                error_code,
                message,
                run_id=run_id,
                retriable=manifest.get("version") != "developer_execution_manifest_v8",
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            )
        except LlmStepReviewRequired as exc:
            pending_action_index = locals().get("action_index", next_action_index)
            persist_working_state(
                int(pending_action_index),
                cursor=max(0, int(getattr(llm_client, "cursor", 1)) - 1),
            )
            journal["last_failed_unit_id"] = item_id
            journal["pending_step_review"] = dict(exc.progress)
            entry["status"] = "review_required"
            entry["last_error_at"] = utc_now_iso()
            journal["status"] = "paused"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            raise DeveloperExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=True,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            ) from exc
        except DeveloperExecutionError as exc:
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            journal["last_failed_unit_id"] = item_id
            entry["status"] = "pending"
            entry["last_error_at"] = utc_now_iso()
            journal["status"] = "paused"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            exc.manifest_path = exc.manifest_path or manifest_path
            exc.journal_path = exc.journal_path or journal_path
            exc.progress = _progress(journal)
            raise
        except HarnessError as exc:
            if exc.error_code == "llm_transport_failed":
                pending_action_index = locals().get("action_index", next_action_index)
                persist_working_state(
                    int(pending_action_index),
                    cursor=max(0, int(getattr(llm_client, "cursor", 1)) - 1),
                )
                journal.pop("pending_step_review", None)
                journal["last_failed_unit_id"] = item_id
                entry["status"] = "review_required"
                entry["last_error_at"] = utc_now_iso()
                journal["status"] = "paused"
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                raise DeveloperExecutionError(
                    exc.error_code,
                    exc.message,
                    run_id=run_id,
                    retriable=True,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                ) from exc
            if exc.error_code == "developer_step_review_drift":
                pending_action_index = locals().get("action_index", next_action_index)
                persist_working_state(
                    int(pending_action_index),
                    cursor=max(0, int(getattr(llm_client, "cursor", 1)) - 1),
                )
                journal["last_failed_unit_id"] = item_id
                entry["status"] = "review_required"
                entry["last_error_at"] = utc_now_iso()
                journal["status"] = "paused"
                journal["updated_at"] = utc_now_iso()
                _write_json_atomic(journal_file, journal)
                raise DeveloperExecutionError(
                    exc.error_code,
                    exc.message,
                    run_id=run_id,
                    retriable=True,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                ) from exc
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            journal["last_failed_unit_id"] = item_id
            entry["status"] = "pending"
            journal["status"] = "blocked"
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            raise DeveloperExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=exc.error_code in {
                    "typescript_ast_extractor_unavailable",
                    "typescript_ast_extraction_failed",
                },
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self._rollback_item(target_path=target_path, session_dir=session_dir, item=item, entry=entry)
            journal["last_failed_unit_id"] = item_id
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
        session_dir: Path | None = None,
        summary: str,
        mutated_paths: set[str],
        no_change_required: bool = False,
        verification_evidence: list[dict[str, object]] | None = None,
    ) -> None:
        path_dispositions = _dict_value(entry.get("path_dispositions")) or {
            path: {
                "status": "mutated" if path in mutated_paths else "verified_unchanged",
                "reason": (
                    "Successful bounded mutation."
                    if path in mutated_paths
                    else "Inspected deterministic fake/repair output was already current."
                ),
            }
            for path in _item_paths(item)
        }
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
                "verification_evidence": verification_evidence or [],
                "path_dispositions": path_dispositions,
            }
        )
        if session_dir is not None and str(item.get("id") or ""):
            completion_file = session_dir / "units" / str(item.get("id")) / "completion.json"
            completion_file.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                completion_file,
                {
                    "version": "developer_unit_completion_v1",
                    "unit_id": item.get("id"),
                    "requirement_refs": item.get("requirement_refs", item.get("requirement_ids", [])),
                    "acceptance_refs": item.get("acceptance_refs", []),
                    "design_node_ids": item.get("design_node_ids", []),
                    "write_paths": _item_paths(item),
                    "mutated_paths": sorted(mutated_paths),
                    "path_dispositions": path_dispositions,
                    "after_sha256_by_path": entry.get("after_sha256_by_path", {}),
                    "verification_evidence": verification_evidence or [],
                    "signature_evidence": entry.get("signature_evidence", {}),
                    "summary": summary,
                    "completed_at": entry.get("completed_at"),
                },
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
            working_state_file = session_dir / "units" / str(item.get("id") or "") / "working_state.json"
            paused_hashes = _dict_value(entry.get("paused_sha256_by_path"))
            current_hashes = {path: _file_hash(target_path / path) for path in _item_paths(item)}
            if working_state_file.is_file() and paused_hashes and current_hashes == paused_hashes:
                entry["status"] = "review_required"
                entry["recovered_at"] = utc_now_iso()
                entry["recovery_mode"] = "durable_working_state"
                journal["status"] = "paused"
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
        expected_dirty_files = _dict_items(manifest.get("expected_dirty_files"))
        allowed.update(str(item.get("path") or "") for item in expected_dirty_files)
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
        for item in expected_dirty_files:
            path = str(item.get("path") or "")
            expected = str(item.get("initial_sha256") or "")
            if not path or _file_hash(target_path / path) != expected:
                raise DeveloperExecutionError(
                    "developer_workspace_drift",
                    f"Expected pre-existing workspace change drifted outside the Developer journal: {path}",
                    run_id=run_id,
                    retriable=False,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                )
        expected_by_path: dict[str, str] = {}
        for item in _dict_items(manifest.get("items")):
            entry = _journal_item(journal, str(item.get("id") or ""))
            after_by_path = _dict_value(entry.get("after_sha256_by_path"))
            paused_by_path = _dict_value(entry.get("paused_sha256_by_path"))
            file_by_path = {str(row.get("path") or ""): row for row in _item_files(item)}
            for path in _item_paths(item):
                expected_by_path.setdefault(
                    path,
                    str(file_by_path.get(path, {}).get("initial_sha256") or ""),
                )
                if entry.get("status") == "completed" and path in after_by_path:
                    expected_by_path[path] = str(after_by_path[path])
                elif entry.get("status") == "review_required" and path in paused_by_path:
                    expected_by_path[path] = str(paused_by_path[path])
                elif entry.get("scope_reopened_at"):
                    expected_by_path[path] = str(file_by_path.get(path, {}).get("initial_sha256") or "")
        for path, expected in expected_by_path.items():
            if _file_hash(target_path / path) != expected:
                raise DeveloperExecutionError(
                    "developer_workspace_drift",
                    f"Run worktree file changed outside the execution journal: {path}",
                    run_id=run_id,
                    retriable=False,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                    progress=_progress(journal),
                )


def developer_execution_retry_allowed(state: dict[str, object]) -> bool:
    """Allow same-session retry after a recoverable Developer execution failure."""

    if str(state.get("status") or "") != "blocked":
        return False
    if str(_dict_value(state.get("last_error")).get("error_code") or "") not in {
        "developer_act_out_of_scope",
        "developer_patch_out_of_scope",
        "developer_patch_invalid",
        "developer_input_drift",
        "developer_step_review_drift",
        "developer_action_limit_exceeded",
        "developer_repair_budget_exhausted",
        "developer_repair_stalled",
    }:
        return False
    execution = _dict_value(state.get("developer_execution"))
    mode = str(execution.get("mode") or "")
    phase = str(state.get("current_phase") or "")
    artifacts = _dict_value(state.get("latest_artifacts"))
    if mode == "developer_act" or phase == "implementation_writing":
        return all(
            bool(artifacts.get(key))
            for key in (
                "developer_work_packet",
                "developer_execution_manifest",
                "developer_execution_journal",
            )
        )
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


def developer_transport_retry_allowed(state: dict[str, object]) -> bool:
    """Cho resume tool-loop sau lỗi transport LLM, kể cả state cũ chưa có mã riêng."""

    if str(state.get("status") or "") != "blocked":
        return False
    error = _dict_value(state.get("last_error"))
    error_code = str(error.get("error_code") or "")
    message = str(error.get("message") or "")
    if error_code != "llm_transport_failed" and not (
        error_code == "requirement_refiner_failed"
        and message.startswith("LLM request failed:")
    ):
        return False
    execution = _dict_value(state.get("developer_execution"))
    if execution.get("executor") != "tool_loop" or execution.get("status") not in {"blocked", "paused"}:
        return False
    mode = str(execution.get("mode") or "")
    artifacts = _dict_value(state.get("latest_artifacts"))
    required_by_mode = {
        "developer_act": ("developer_work_packet",),
        "implementation_repair": (
            "developer_work_packet",
            "implementation_repair_feedback",
            "developer_repair_execution_manifest",
            "developer_repair_execution_journal",
        ),
        "qa_repair": (
            "developer_work_packet",
            "qa_repair_feedback",
            "developer_qa_repair_execution_manifest",
            "developer_qa_repair_execution_journal",
        ),
    }
    required = required_by_mode.get(mode)
    return bool(required) and all(bool(artifacts.get(key)) for key in required)


def _build_manifest(
    *,
    run_id: str,
    work_packet: dict[str, object],
    target_path: Path,
    mode: str,
    repair_feedback: dict[str, object] | None,
    repair_iteration: int,
    selected_paths: set[str] | None = None,
    expected_dirty_paths: list[str] | None = None,
) -> dict[str, object]:
    output_contract = _dict_value(work_packet.get("output_contract"))
    allowed = [str(value).strip().replace("\\", "/") for value in output_contract.get("allowed_paths", []) if str(value).strip()]
    if not allowed:
        raise HarnessError("developer_work_packet_allowed_paths_missing", "DeveloperWorkPacket has no allowed paths.", run_id=run_id)
    closure = build_repair_closure(work_packet=work_packet, feedback=repair_feedback, target_path=target_path)
    execution_units = _dict_items(work_packet.get("execution_units"))
    if repair_feedback and execution_units:
        repair_seed = (
            {path.strip().replace("\\", "/") for path in selected_paths}
            if selected_paths is not None
            else set(_string_items(closure.get("context_paths")))
            or set(_string_items(closure.get("direct_repair_paths")))
        )
        affected_units = [
            unit
            for unit in execution_units
            if set(_string_items(unit.get("write_paths"))) & repair_seed
        ]
        context_selected = {
            path
            for unit in affected_units
            for path in (
                _string_items(unit.get("context_paths"))
                or _string_items(unit.get("write_paths"))
            )
        }
    elif repair_feedback and mode == "implementation_repair":
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
    resolved_generation_context = developer_generation_context(work_packet)
    generation_context = developer_prompt_context(work_packet)
    signatures_by_file = _dict_value(resolved_generation_context.get("signatures_by_file"))
    canonical = {
        str(item.get("id") or ""): str(item.get("content") or "")
        for item in _dict_items(work_packet.get("canonical_requirements"))
    }
    steps = _dict_items(work_packet.get("implementation_steps"))
    execution_paths = set(allowed) if not repair_feedback else context_selected
    ordered = [path for path in allowed if path in execution_paths and not developer_path_is_test(path)]
    ordered.extend(path for path in allowed if path in execution_paths and developer_path_is_test(path))
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
                "approved_signatures": _dict_items(signatures_by_file.get(path)),
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
    expected_dirty_files = []
    for raw_path in sorted(set(expected_dirty_paths or [])):
        normalized_dirty_path = raw_path.strip().replace("\\", "/")
        if (
            not normalized_dirty_path
            or normalized_dirty_path.startswith("/")
            or Path(normalized_dirty_path).is_absolute()
            or normalized_dirty_path == ".."
            or normalized_dirty_path.startswith("../")
            or "/../" in normalized_dirty_path
        ):
            raise HarnessError(
                "developer_tool_unsafe_path",
                f"Unsafe expected dirty path: {raw_path}",
                run_id=run_id,
            )
        target = _safe_read_path(target_path, normalized_dirty_path, run_id=run_id)
        relative = str(target.relative_to(target_path.resolve())).replace("\\", "/")
        if relative in allowed:
            continue
        expected_dirty_files.append(
            {
                "path": relative,
                "initial_sha256": _file_hash(target),
            }
        )
    file_by_path = {str(item.get("path") or ""): item for item in files}
    items: list[dict[str, object]] = []
    if execution_units:
        selected_unit_ids = {
            str(unit.get("unit_id") or "")
            for unit in execution_units
            if set(_string_items(unit.get("write_paths"))) & execution_paths
        }
        for unit in execution_units:
            unit_paths = [
                path for path in _string_items(unit.get("write_paths"))
                if path in execution_paths and path in file_by_path
            ]
            if not unit_paths:
                continue
            unit_context_candidates = _string_items(unit.get("context_paths")) or unit_paths
            unit_context = [
                path for path in unit_context_candidates
                if path in context_selected and path in allowed
            ]
            unit_contract_anchors = set(_string_items(unit.get("contract_anchor_paths")))
            unit_signature_ids = _string_items(unit.get("signature_entry_ids"))
            unit_signature_id_set = set(unit_signature_ids)
            items.append(
                {
                    "id": str(unit.get("unit_id") or ""),
                    "title": str(unit.get("title") or ""),
                    "objective": str(unit.get("objective") or ""),
                    "order": unit.get("order"),
                    "scenario_id": unit.get("scenario_id"),
                    "component_id": unit.get("component_id"),
                    "surfaces": _string_items(unit.get("surfaces")),
                    "depends_on": [
                        dependency
                        for dependency in _string_items(unit.get("depends_on"))
                        if dependency in selected_unit_ids
                    ],
                    "paths": unit_paths,
                    "context_paths": unit_context,
                    "direct_repair_paths": [path for path in unit_paths if path in direct_selected],
                    "diagnostic_paths": [path for path in unit_context if path in diagnostic_selected],
                    "contract_anchor_paths": [
                        path for path in unit_context
                        if path in unit_contract_anchors or path in anchor_selected
                    ],
                    "files": [
                        _project_file_to_execution_unit(
                            file_item=file_by_path[path],
                            unit=unit,
                            signature_ids=unit_signature_id_set,
                        )
                        for path in unit_paths
                    ],
                    "requirement_ids": _string_items(unit.get("requirement_refs")),
                    "requirement_refs": _string_items(unit.get("requirement_refs")),
                    "acceptance_refs": _string_items(unit.get("acceptance_refs")),
                    "design_node_ids": _string_items(unit.get("design_node_ids")),
                    "design_edge_ids": _string_items(unit.get("design_edge_ids")),
                    "planned_code_targets": _dict_items(unit.get("planned_code_targets")),
                    "migration_impact": _dict_value(unit.get("migration_impact")),
                    "runtime_bindings": _dict_items(unit.get("runtime_bindings")),
                    "boundary_contracts": _dict_items(unit.get("boundary_contracts")),
                    "surface_contracts": _dict_items(unit.get("surface_contracts")),
                    "required_stage_ids": _string_items(unit.get("required_stage_ids")),
                    "test_target_paths": _string_items(unit.get("test_target_paths")),
                    "signature_entry_ids": unit_signature_ids,
                    "input_digest": str(unit.get("input_digest") or ""),
                    "requirements": [
                        {"id": value, "content": canonical.get(value, "")}
                        for value in _string_items(unit.get("requirement_refs"))
                    ],
                    "test_intents": _dict_items(unit.get("test_intents")),
                    "repair_closure": {
                        **closure,
                        "context_paths": unit_context,
                    } if repair_feedback else {},
                }
            )
    else:
        requirement_ids = list(
            dict.fromkeys(
                requirement_id
                for file_item in files
                for requirement_id in _string_items(file_item.get("requirement_ids"))
            )
        )
        items = [
            {
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
        ]
    if not items:
        raise HarnessError(
            "developer_repair_scope_empty",
            "Developer execution has no unit inside the approved repair scope.",
            run_id=run_id,
        )
    input_payload = {
        "approved_plan": work_packet.get("approved_plan"),
        "effective_design_contract": work_packet.get("effective_design_contract"),
        "requirement_contract": work_packet.get("requirement_contract"),
        "developer_generation_context": generation_context,
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "workspace_allowed_paths": allowed,
        "items": [
            {
                **item,
                "files": [
                    {key: value for key, value in file_item.items() if key != "initial_sha256"}
                    for file_item in _dict_items(item.get("files"))
                ],
            }
            for item in items
        ],
        "repair_feedback": repair_feedback or {},
        **({"expected_dirty_paths": [item["path"] for item in expected_dirty_files]} if expected_dirty_files else {}),
    }
    return {
        "version": "developer_execution_manifest_v8" if execution_units else "developer_execution_manifest_v7",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "created_at": utc_now_iso(),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "input_hash": _json_hash(input_payload),
        "workspace_allowed_paths": allowed,
        "expected_dirty_files": expected_dirty_files,
        "repair_feedback": repair_feedback or {},
        "developer_generation_context": generation_context,
        "items": items,
    }


def _project_file_to_execution_unit(
    *,
    file_item: dict[str, object],
    unit: dict[str, object],
    signature_ids: set[str],
) -> dict[str, object]:
    requirement_ids = set(_string_items(unit.get("requirement_refs")))
    design_ids = set(_string_items(unit.get("design_node_ids"))) | set(
        _string_items(unit.get("design_edge_ids"))
    )
    target_refs = set()
    for target in _dict_items(unit.get("planned_code_targets")):
        path = str(target.get("path") or "").strip().replace("\\", "/")
        symbol = str(target.get("symbol") or "").strip()
        if path:
            target_refs.add(f"{path}#{symbol}" if symbol else path)
    unit_order = int(unit.get("order") or 0)
    unit_title = str(unit.get("title") or "").strip()
    return {
        **file_item,
        "requirement_ids": [
            value for value in _string_items(file_item.get("requirement_ids")) if value in requirement_ids
        ],
        "requirements": [
            row
            for row in _dict_items(file_item.get("requirements"))
            if str(row.get("id") or "") in requirement_ids
        ],
        "design_mappings": [
            row
            for row in _dict_items(file_item.get("design_mappings"))
            if str(row.get("design_element_id") or "") in design_ids
            or str(row.get("planned_code_target") or "").replace("\\", "/") in target_refs
        ],
        "approved_signatures": [
            signature
            for signature in _dict_items(file_item.get("approved_signatures"))
            if str(signature.get("contract_id") or "") in signature_ids
        ],
        "implementation_steps": [
            row
            for row in _dict_items(file_item.get("implementation_steps"))
            if int(row.get("order") or 0) == unit_order
            or (unit_title and str(row.get("title") or "").strip() == unit_title)
        ],
    }


def _new_journal(*, run_id: str, manifest: dict[str, object], mode: str, repair_iteration: int) -> dict[str, object]:
    return {
        "version": (
            "developer_execution_journal_v4"
            if manifest.get("version") == "developer_execution_manifest_v8"
            else "developer_execution_journal_v3"
        ),
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
    unit_aware = manifest.get("version") == "developer_execution_manifest_v8"
    repair_mode = str(manifest.get("mode") or "").endswith("_repair")
    context_repair = str(manifest.get("mode") or "") == "implementation_repair"
    paths = _item_context_paths(item)
    diagnostic_paths = _item_diagnostic_paths(item)
    anchor_paths = _item_contract_anchor_paths(item)
    handled_paths = _handled_paths(history, required_paths=paths)
    inspected_paths = _inspected_paths(history, required_paths=paths)
    current_hashes = {path: _file_hash(target_path / path) for path in _item_paths(item)}
    pending_stage_ids = sorted(
        set(_string_items(item.get("required_stage_ids")))
        - _current_passed_stages(history, current_hashes=current_hashes)
    )
    repair_state = _verification_repair_state(history)
    timeout_diagnostic = _timeout_diagnostic_state(history)
    timeout_cleanup_paths = _timeout_diagnostic_cleanup_paths(
        target_path=target_path,
        item_paths=_item_paths(item),
    )
    failed_stage_ids = sorted(repair_state)
    verification_ready_stage_ids = sorted(
        stage_id
        for stage_id, episode in repair_state.items()
        if episode.get("verification_ready") is True
    )
    verification_forced_stage_ids = sorted(
        stage_id
        for stage_id, episode in repair_state.items()
        if episode.get("verification_forced") is True
    )
    phase = _execution_phase(
        history=history,
        mutated=mutated,
        action_index=action_index,
        max_actions=max_actions,
        pending_stage_ids=pending_stage_ids,
        failed_stage_ids=failed_stage_ids,
    )
    action_memory = _action_memory(
        history,
        target_item_paths=paths,
        max_evidence_chars=max(8000, min(24000, context_window_tokens // 2)),
    )
    current_sources = _current_mutated_sources(
        history,
        target_path=target_path,
        target_item_paths=paths,
        max_evidence_chars=max(8000, min(24000, context_window_tokens // 4)),
    )
    action_memory["authoritative_sources"] = _authoritative_sources(
        inspected_sources=_dict_items(action_memory.pop("inspected_sources", [])),
        current_sources=current_sources,
        target_path=target_path,
    )
    if failed_stage_ids:
        action_memory["recent_results"] = [
            row
            for row in _dict_items(action_memory.get("recent_results"))
            if not (
                row.get("action") == "verification.run_stage"
                and _dict_value(row.get("result")).get("status") == "failed"
            )
        ]
    new_test_targets = [
        path
        for path in _item_paths(item)
        if _is_typescript_test_path(path) and not (target_path / path).is_file()
    ]
    test_context_ready = not new_test_targets or _has_test_authoring_context(
        history,
        target_test_path=new_test_targets[0],
    )
    action_schema = {
        "action": (
            "repo.list|repo.read|repo.search|repo.read_diff|codegraph.query_readonly|"
            "repo.write_file|repo.apply_patch|verification.run_stage|execution.complete_unit"
            if unit_aware
            else "list_files|read_file|search_code|read_diff|write_file|apply_patch|complete"
        ),
        "arguments": {},
        "summary": "short evidence conclusion and next-action reason",
    }
    if unit_aware and verification_forced_stage_ids:
        action_schema["action"] = "verification.run_stage"
    active_failures = [
        _compact_repair_episode(repair_state[stage_id]) for stage_id in failed_stage_ids
    ]
    test_authoring_policy: dict[str, object] = {
        "new_test_targets": new_test_targets,
        "repository_evidence_required": bool(new_test_targets),
        "repository_evidence_satisfied": test_context_ready,
    }
    if new_test_targets:
        test_authoring_policy["rules"] = [
            "Inspect an existing repository test or search/CodeGraph evidence for its test and mocking conventions before creating a new TypeScript test.",
            "Test doubles must satisfy the exact production runtime predicates, including instanceof checks.",
            "Hoisted module-mock factories may reference only values initialized through the test framework's supported hoisting mechanism.",
        ]
    test_authoring_instruction = (
        "Before creating a new TypeScript test, satisfy test_authoring_policy with repository evidence. "
        "Test doubles must satisfy exact production predicates such as instanceof; never replace a nominal runtime "
        "check with a shape-only fake. Respect the test framework's module-mock hoisting rules. "
        if new_test_targets
        else ""
    )
    context = {
        "active_focus": {
            "unit_id": item.get("id"),
            "goal": item.get("objective") or item.get("title"),
            "phase": phase,
            "failed_stage_ids": failed_stage_ids,
            "known_facts": (
                [
                    "Every source in working_memory.authoritative_sources is verified against the current worktree.",
                    "Do not read an authoritative source again unless a successful mutation changed it.",
                ]
                if action_memory["authoritative_sources"]
                else []
            ),
            "next_decision": (
                "Use the active diagnostic and authoritative sources to choose a different diagnostic observation "
                "or the smallest coherent mutation."
                if failed_stage_ids
                else "Inspect only missing evidence, then implement or verify the current unit."
            ),
        },
        "execution_task": _focused_execution_task(
            item,
            context_paths=paths,
            active_repair=bool(failed_stage_ids),
        ),
        "execution": {"mode": manifest.get("mode"), "base_sha": manifest.get("base_sha")},
        "dependency_receipts": [
            {"unit_id": dependency, "status": "completed"}
            for dependency in _string_items(item.get("depends_on"))
        ],
        "developer_generation_context": manifest.get("developer_generation_context"),
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
            "pending_stage_ids": pending_stage_ids,
            "failed_stage_ids": failed_stage_ids,
            "verification_ready_stage_ids": verification_ready_stage_ids,
            "verification_forced_stage_ids": verification_forced_stage_ids,
        },
        "active_failures": active_failures,
        "test_authoring_policy": test_authoring_policy,
        "working_memory": action_memory,
    }
    if timeout_diagnostic or timeout_cleanup_paths:
        context["timeout_diagnostic_protocol"] = {
            "version": "timeout_diagnostic_v1",
            "status": timeout_diagnostic.get("status") if timeout_diagnostic else "cleanup_required",
            "stage_id": timeout_diagnostic.get("stage_id") if timeout_diagnostic else "",
            "test_paths": timeout_diagnostic.get("test_paths") if timeout_diagnostic else timeout_cleanup_paths,
            "source_marker": _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER,
            "failure_prefix": _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX,
            "cleanup_required_paths": timeout_cleanup_paths,
            "rules": [
                "Treat an unlocated timeout as an evidence gap, not a confirmed root cause.",
                "Instrument bounded suspected await points in the selected test before changing production behavior.",
                "After instrumentation succeeds, immediately rerun the failed verification stage.",
                "Remove all diagnostic instrumentation after the failing await is identified; completion is blocked while markers remain.",
            ],
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
            "approved execution-unit path. Use working_memory.authoritative_sources; do not read, list, or search again."
        ),
        "edit_or_complete": (
            "At least one mutation succeeded. Continue only when another change is necessary for the same coherent "
            "fix; otherwise call complete and let Verification run."
        ),
        "edit_or_verify": (
            "A mutation succeeded. Finish only the edits needed for this unit, then run every pending verification "
            "stage before proposing completion. Do not call completion while pending_stage_ids is non-empty."
        ),
        "verification_required": (
            "The remaining budget is reserved for pending verification stages plus completion. Your next action "
            "MUST be verification.run_stage for one pending_stage_id."
        ),
        "verification_repair_required": (
            "A verification repair episode is active. Review active_failures plus "
            "working_memory.authoritative_sources, then make all coherent production, caller, and test changes needed. "
            "Use a read/search action only for a different dependency whose evidence is absent from authoritative_sources. "
            "Call verification.run_stage only when the repair batch is "
            "internally consistent and at least one mutation followed the latest failure; do not complete the unit. "
            "When verification_ready_stage_ids is non-empty, rerun one of those stages unless you have a materially "
            "different repair; never resend a payload that already had no effect."
        ),
        "complete_required": "A mutation succeeded and only one action remains. You MUST call complete now.",
    }[phase]
    if failed_stage_ids:
        work_instruction = (
            "This is an active verification repair, not a fresh implementation pass. The current unit sources have "
            "already been inspected and authoritative_sources is the verified current worktree view. Diagnose the "
            "active failure from that evidence, then make the smallest coherent production or test mutation. Do not "
            "restart inspection of a source already present in authoritative_sources."
        )
    elif not repair_mode:
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
    if timeout_diagnostic:
        if timeout_diagnostic.get("status") == "instrumentation_required":
            work_instruction = (
                "The active timeout is not localized. Do not guess at a production fix. Your next mutation must "
                f"instrument one selected test path with {_TIMEOUT_DIAGNOSTIC_SOURCE_MARKER} and bounded guards "
                f"that raise {_TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX}<step>."
            )
        elif timeout_diagnostic.get("status") == "verification_required":
            work_instruction = (
                "Timeout instrumentation is present. Rerun the failed verification stage now and use its labeled "
                "failure as evidence before any further mutation."
            )
    elif timeout_cleanup_paths:
        work_instruction = (
            f"Remove timeout diagnostic instrumentation from {timeout_cleanup_paths} before completion, then rerun "
            "the required verification stages against the cleaned source."
        )
    if unit_aware:
        work_instruction = work_instruction.replace("call complete", "call execution.complete_unit")
        phase_instruction = (
            phase_instruction
            .replace("write_file or apply_patch", "repo.write_file or repo.apply_patch")
            .replace("call complete", "run required verification stages, then call execution.complete_unit")
        )
        available_actions = (
            "- repo.list: arguments {prefix?, limit?}\n"
            "- repo.read: arguments {path, start_line?, end_line?}\n"
            "- repo.search: arguments {query, prefix?, limit?}\n"
            "- repo.read_diff: arguments {path}\n"
            "- codegraph.query_readonly: arguments {query}; read-only symbol and call-path context\n"
            "- repo.write_file: arguments {path, expected_sha256, content}; approved unit write path only\n"
            "- repo.apply_patch: arguments {path, expected_sha256, patch}; unified diff for an approved unit path\n"
            "- verification.run_stage: arguments {stage_id}; stage_id must be listed in required_stage_ids\n"
            "- execution.complete_unit: arguments {summary, path_dispositions: [{path, status, reason}]}; status is "
            "mutated|verified_unchanged|not_required. Every unit path needs a disposition, non-mutated paths need "
            "fresh repo.read/repo.read_diff evidence, and every required stage must pass against current hashes\n"
        )
        context["execution_unit_contract"] = {
            "unit_id": item.get("id"),
            "depends_on": item.get("depends_on", []),
            "write_paths": _item_paths(item),
            "read_context_paths": _item_context_paths(item),
            "contract_anchor_paths": _item_contract_anchor_paths(item),
            "requirement_refs": item.get("requirement_refs", []),
            "acceptance_refs": item.get("acceptance_refs", []),
            "design_node_ids": item.get("design_node_ids", []),
            "planned_code_targets": _dict_items(item.get("planned_code_targets")),
            "migration_impact": _dict_value(item.get("migration_impact")),
            "runtime_bindings": _dict_items(item.get("runtime_bindings")),
            "boundary_contracts": _dict_items(item.get("boundary_contracts")),
            "surface_contracts": _dict_items(item.get("surface_contracts")),
            "required_stage_ids": item.get("required_stage_ids", []),
            "test_target_paths": item.get("test_target_paths", []),
            "approved_signatures": _item_approved_signatures(item),
            "hard_constraints": [
                "Approved signatures are immutable and override conflicting prose objectives.",
                "Preserve each qualified_name, parameter list, async flag, and return_type exactly.",
                "Treat planned code targets, migration impact, runtime bindings, boundary mappings, and surface contracts as approved execution constraints.",
                "Preserve each boundary input/output/error mapping and runtime attachment; inspect related approved paths instead of inventing a different contract.",
                "For UI work, implement the approved experience contract including structure, feedback, accessibility, responsive behavior, and focus flow.",
                "Before mutating a boundary consumer, inspect its unread contract_anchor_paths; they are read-only context and never expand write_paths.",
                "Represent conflicting behavior through compatible control flow or domain errors; do not change an approved contract.",
            ],
        }
        if verification_forced_stage_ids:
            available_actions = (
                "- verification.run_stage: arguments {stage_id}; stage_id MUST be one of "
                f"{verification_forced_stage_ids}\n"
            )
            phase_instruction = (
                "Harness rejected an exact no-effect mutation. Your next and only allowed action MUST be "
                f"verification.run_stage for one of {verification_forced_stage_ids}."
            )
    else:
        available_actions = (
            "- list_files: arguments {prefix?, limit?}\n"
            "- read_file: arguments {path, start_line?, end_line?}\n"
            "- search_code: arguments {query, prefix?, limit?}\n"
            "- read_diff: arguments {path}\n"
            "- write_file: arguments {path, expected_sha256, content}; any approved execution-unit path\n"
            "- apply_patch: arguments {path, expected_sha256, patch}; unified diff for an approved path. A "
            "headerless patch MUST begin with a complete hunk header such as @@ -10,2 +10,3 @@. Bare @@ is invalid.\n"
            "- complete: arguments {summary, no_change_required?, unchanged_paths?: [{path, reason}]}; normally "
            "after at least one successful mutation. no_change_required=true requires fresh inspected evidence.\n"
        )
    return (
        f"Developer Tool Action {'v2' if unit_aware else 'v1'}\n\n"
        "Return exactly one JSON object matching this shape:\n"
        f"{json.dumps(action_schema, ensure_ascii=False)}\n\n"
        "Available actions:\n"
        f"{available_actions}\n"
        f"A write_file content or apply_patch value must stay below {max_write_chars} characters. {extra}\n"
        "Treat execution_unit_contract.approved_signatures as immutable hard constraints. They override conflicting "
        "prose objectives; never change an approved qualified name, parameters, async flag, or return type. "
        "Choose exactly one next action, emit its JSON object, and end the response immediately. Only Harness "
        "tool results in working_memory prove execution. Treat authoritative_sources as the single verified current "
        "worktree view; each entry includes its freshness and verification action. Do not read the same "
        "path/range again unless a successful mutation changed that file. Do not output source code "
        "outside the JSON action. In summary, state the conclusion established by the latest relevant evidence "
        "and why the chosen action follows. Keep it brief; do not include chain-of-thought. "
        f"{test_authoring_instruction}"
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
    pending_stage_ids: list[str] | None = None,
    failed_stage_ids: list[str] | None = None,
) -> str:
    remaining = max_actions - action_index + 1
    if failed_stage_ids:
        return "verification_repair_required"
    if mutated:
        pending_count = len(pending_stage_ids or [])
        if pending_count:
            return "verification_required" if remaining <= pending_count + 1 else "edit_or_verify"
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
    pending_stage_ids: list[str] | None = None,
    failed_stage_ids: list[str] | None = None,
    repair_mutated_stage_ids: list[str] | None = None,
    repair_verification_ready_stage_ids: list[str] | None = None,
    repair_verification_forced_stage_ids: list[str] | None = None,
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
        pending_stage_ids=pending_stage_ids,
        failed_stage_ids=failed_stage_ids,
    )
    handled_paths = handled_paths or set()
    unresolved_paths = unresolved_paths or set()
    repair_mutated_stage_ids = repair_mutated_stage_ids or []
    repair_verification_ready_stage_ids = repair_verification_ready_stage_ids or []
    repair_verification_forced_stage_ids = repair_verification_forced_stage_ids or []
    action_path = str(_dict_value(action.get("arguments")).get("path") or "").replace("\\", "/")
    timeout_diagnostic = _timeout_diagnostic_state(history)
    timeout_instrumentation_action = _is_timeout_diagnostic_instrumentation_action(
        action,
        timeout_diagnostic=timeout_diagnostic,
    )
    if (
        name in _MUTATION_ACTIONS
        and unresolved_paths
        and action_path in handled_paths
        and not timeout_instrumentation_action
    ):
        return {
            "error": "repair_path_already_handled",
            "instruction": (
                "Advance to one of the unresolved repair paths before revisiting this file: "
                f"{sorted(unresolved_paths)}"
            ),
            "unresolved_paths": sorted(unresolved_paths),
        }
    if name in _OBSERVATION_ACTIONS and _observation_signatures(action) & seen_observations:
        repeat_count = 1 + _duplicate_observation_repeat_count(action, history)
        return {
            "error": "duplicate_observation",
            "instruction": (
                "This exact observation is already present in working_memory.authoritative_sources. "
                "Do not read it again. Choose a different dependency/search observation or make the smallest "
                "coherent mutation supported by the cached source and active diagnostic."
            ),
            "repeat_count": repeat_count,
        }
    if timeout_diagnostic:
        diagnostic_status = str(timeout_diagnostic.get("status") or "")
        diagnostic_stage_id = str(timeout_diagnostic.get("stage_id") or "")
        diagnostic_test_paths = set(_string_items(timeout_diagnostic.get("test_paths")))
        if diagnostic_status == "instrumentation_required":
            if timeout_instrumentation_action:
                return None
            if name in _OBSERVATION_ACTIONS and action_path in diagnostic_test_paths:
                return None
            return {
                "error": "timeout_diagnostic_required",
                "instruction": (
                    "The active test timeout has no source location. Before changing production behavior, "
                    f"instrument one of {sorted(diagnostic_test_paths)} with marker "
                    f"{_TIMEOUT_DIAGNOSTIC_SOURCE_MARKER} and raise an error prefixed "
                    f"{_TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX} from a bounded guard around each suspected await."
                ),
                "stage_id": diagnostic_stage_id,
                "test_paths": sorted(diagnostic_test_paths),
            }
        if diagnostic_status == "verification_required":
            stage_id = str(_dict_value(action.get("arguments")).get("stage_id") or "")
            if name == "verification.run_stage" and stage_id == diagnostic_stage_id:
                return None
            return {
                "error": "timeout_diagnostic_verification_required",
                "instruction": (
                    "Timeout instrumentation was added successfully. Run verification.run_stage for "
                    f"{diagnostic_stage_id} now; do not make another mutation before collecting its evidence."
                ),
                "stage_id": diagnostic_stage_id,
            }
    if name in _OBSERVATION_ACTIONS and phase == "edit_required":
        return {
            "error": "mutation_required",
            "instruction": "The inspection budget is exhausted; write_file or apply_patch an approved unit path now.",
        }
    if phase == "verification_required" and name != "verification.run_stage":
        return {
            "error": "verification_required",
            "instruction": (
                "Run one pending verification stage now; the remaining action budget is reserved for "
                f"{pending_stage_ids or []} plus completion."
            ),
        }
    if phase == "verification_repair_required":
        if repair_verification_forced_stage_ids:
            if name == "verification.run_stage":
                stage_id = str(_dict_value(action.get("arguments")).get("stage_id") or "")
                if stage_id in set(repair_verification_forced_stage_ids):
                    return None
            repeated = (
                _no_effect_mutation_repeat_count(action, history) + 1
                if name in _MUTATION_ACTIONS and _repeats_no_effect_mutation(action, history)
                else 0
            )
            return {
                "error": "verification_repair_ready",
                "instruction": (
                    "Harness already rejected an exact no-effect mutation. The next and only allowed action is "
                    f"verification.run_stage for one of {repair_verification_forced_stage_ids}."
                ),
                **({"repeat_count": repeated} if repeated else {}),
            }
        if repair_verification_ready_stage_ids:
            if name == "verification.run_stage":
                stage_id = str(_dict_value(action.get("arguments")).get("stage_id") or "")
                if stage_id in set(repair_verification_ready_stage_ids):
                    return None
            if name in _MUTATION_ACTIONS:
                if _repeats_no_effect_mutation(action, history):
                    return {
                        "error": "verification_repair_ready",
                        "instruction": (
                            "This exact payload already had no effect after a successful repair mutation. "
                            "Do not rewrite it; your next action MUST rerun the repaired failed stage: "
                            f"{repair_verification_ready_stage_ids}."
                        ),
                        "repeat_count": _no_effect_mutation_repeat_count(action, history) + 1,
                    }
                return None
            return {
                "error": "verification_repair_ready",
                "instruction": (
                    "The repair batch already contains a successful mutation and the latest write had no effect. "
                    "Do not rewrite the same content or patch again; rerun the repaired failed stage now: "
                    f"{repair_verification_ready_stage_ids}."
                ),
            }
        if name in _MUTATION_ACTIONS and _repeats_no_effect_mutation(action, history):
            return {
                "error": "duplicate_no_effect_mutation",
                "instruction": (
                    "This exact file content or patch already produced no change. Do not resend it. "
                    "Submit a materially different repair using working_memory.authoritative_sources."
                ),
                "repeat_count": _no_effect_mutation_repeat_count(action, history) + 1,
            }
        if name in _MUTATION_ACTIONS or name in _OBSERVATION_ACTIONS:
            return None
        if name == "verification.run_stage":
            stage_id = str(_dict_value(action.get("arguments")).get("stage_id") or "")
            if stage_id not in set(failed_stage_ids or []):
                return {
                    "error": "verification_repair_stage_required",
                    "instruction": f"Repair and rerun the active failed stage first: {failed_stage_ids or []}.",
                }
            if stage_id not in set(repair_mutated_stage_ids):
                return {
                    "error": "verification_repair_required",
                    "instruction": (
                        "Make at least one coherent mutation after the latest failure before rerunning "
                        f"{stage_id}."
                    ),
                }
            return None
        return {
            "error": "verification_repair_required",
            "instruction": (
                "Finish the active repair batch and rerun its failed stage before completing the unit: "
                f"{failed_stage_ids or []}."
            ),
        }
    if name in _MUTATION_ACTIONS and _repeats_no_effect_mutation(action, history):
        return {
            "error": "duplicate_no_effect_mutation",
            "instruction": (
                "This exact file content or patch already produced no change. Do not resend it. "
                "Submit a materially different repair using working_memory.authoritative_sources."
            ),
            "repeat_count": _no_effect_mutation_repeat_count(action, history) + 1,
        }
    if phase == "complete_required" and closure_resolved and name not in {"complete", "execution.complete_unit"}:
        return {
            "error": "completion_required",
            "instruction": "A mutation already succeeded and only one action remains; call complete now.",
        }
    return None


def _new_test_context_policy_error(
    *,
    action: dict[str, object],
    history: list[dict[str, object]],
    manifest: dict[str, object],
    target_path: Path,
    item_paths: list[str],
) -> dict[str, object] | None:
    """Require repository evidence before the first write of a new TypeScript test."""

    if manifest.get("version") != "developer_execution_manifest_v8":
        return None
    if str(action.get("action") or "") not in _MUTATION_ACTIONS:
        return None
    action_path = str(_dict_value(action.get("arguments")).get("path") or "").replace("\\", "/")
    if action_path not in set(item_paths) or not _is_typescript_test_path(action_path):
        return None
    if (target_path / action_path).is_file():
        return None
    if _has_test_authoring_context(history, target_test_path=action_path):
        return None
    repeat_count = 1 + sum(
        1
        for row in history
        if _dict_value(row.get("result")).get("error") == "developer_test_context_required"
    )
    return {
        "error": "developer_test_context_required",
        "reason": f"Repository test conventions were not inspected before creating {action_path}.",
        "instruction": (
            "Inspect an existing TypeScript test with repo.read, or use repo.search/codegraph.query_readonly for "
            "the repository's test and mocking conventions. Inspect unfamiliar runtime predicates or dependency "
            "types used by the production branch. Then create the test so its doubles satisfy exact checks such "
            "as instanceof and its module mocks obey framework hoisting rules."
        ),
        "policy_repeat_count": repeat_count,
    }


def _has_test_authoring_context(
    history: list[dict[str, object]],
    *,
    target_test_path: str,
) -> bool:
    target = target_test_path.replace("\\", "/").casefold()
    query_markers = ("test", "spec", "mock", "vitest", "jest", "instanceof")
    for row in history:
        result = _dict_value(row.get("result"))
        if not result.get("ok"):
            continue
        action_name = str(row.get("action") or "")
        arguments = _dict_value(row.get("arguments"))
        if action_name in {"read_file", "read_diff", "repo.read", "repo.read_diff"}:
            observed = str(arguments.get("path") or "").replace("\\", "/")
            if observed.casefold() != target and _is_typescript_test_path(observed):
                return True
        if action_name in {"search_code", "repo.search"}:
            query = str(arguments.get("query") or "").casefold()
            if _dict_items(result.get("hits")) and any(marker in query for marker in query_markers):
                return True
        if action_name == "codegraph.query_readonly":
            query = str(arguments.get("query") or "").casefold()
            if str(result.get("evidence") or "").strip() and any(
                marker in query for marker in query_markers
            ):
                return True
    return False


def _is_typescript_test_path(path: str) -> bool:
    return path.casefold().endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))


def _has_successful_target_inspection(history: list[dict[str, object]], *, item_path: str) -> bool:
    expected = item_path.strip().replace("\\", "/").lstrip("/").casefold()
    for row in history:
        if row.get("action") not in {"read_file", "read_diff", "repo.read", "repo.read_diff"}:
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


def _observation_signatures(
    action: dict[str, object],
    *,
    result: dict[str, object] | None = None,
) -> set[str]:
    """Normalize read ranges so an explicit full range matches an earlier implicit full read."""

    name = str(action.get("action") or "")
    arguments = _dict_value(action.get("arguments"))
    if name not in {"read_file", "repo.read"}:
        return {_action_signature(action)}
    path = str(arguments.get("path") or "").replace("\\", "/")
    ranges = {(arguments.get("start_line"), arguments.get("end_line"))}
    if result:
        ranges.add((result.get("start_line"), result.get("end_line")))
    return {
        json.dumps(
            {"action": name, "path": path, "start_line": start, "end_line": end},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for start, end in ranges
    }


def _current_observation_signatures(history: list[dict[str, object]]) -> set[str]:
    last_mutation = max(
        (
            index
            for index, row in enumerate(history)
            if row.get("action") in _MUTATION_ACTIONS and _dict_value(row.get("result")).get("ok") is True
        ),
        default=-1,
    )
    signatures: set[str] = set()
    for row in history[last_mutation + 1 :]:
        if row.get("action") not in _OBSERVATION_ACTIONS:
            continue
        result = _dict_value(row.get("result"))
        if result.get("ok") is not True:
            continue
        signatures.update(
            _observation_signatures(
                {"action": row.get("action"), "arguments": _dict_value(row.get("arguments"))},
                result=result,
            )
        )
    return signatures


def _duplicate_observation_repeat_count(
    action: dict[str, object],
    history: list[dict[str, object]],
) -> int:
    candidates = _observation_signatures(action)
    count = 0
    for row in history:
        result = _dict_value(row.get("result"))
        if result.get("error") != "duplicate_observation":
            continue
        previous = {"action": row.get("action"), "arguments": _dict_value(row.get("arguments"))}
        if candidates & _observation_signatures(previous):
            count += 1
    return count


def _action_history_arguments(action: dict[str, object]) -> dict[str, object]:
    arguments = _dict_value(action.get("arguments"))
    safe: dict[str, object] = {}
    for key, value in arguments.items():
        if key in {"content", "patch"} and isinstance(value, str):
            safe[f"{key}_chars"] = len(value)
            safe[f"{key}_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        elif key == "expected_sha256" and isinstance(value, str):
            safe[key] = value[:12]
        else:
            safe[key] = value
    return safe


def _repeats_no_effect_mutation(
    action: dict[str, object],
    history: list[dict[str, object]],
) -> bool:
    """Reject an exact mutation payload that already proved to have no effect."""

    return _no_effect_mutation_repeat_count(action, history) > 0


def _no_effect_mutation_repeat_count(
    action: dict[str, object],
    history: list[dict[str, object]],
) -> int:
    """Count identical mutation payloads already known to have no effect."""

    current = _action_history_arguments(action)
    content_key = "patch_sha256" if str(action.get("action") or "").endswith("apply_patch") else "content_sha256"
    fingerprint = str(current.get(content_key) or "")
    path = str(current.get("path") or "").replace("\\", "/")
    if not fingerprint or not path:
        return 0
    count = 0
    for row in history:
        if str(row.get("action") or "") != str(action.get("action") or ""):
            continue
        result = _dict_value(row.get("result"))
        if result.get("error") not in {
            "no_effect_mutation",
            "duplicate_no_effect_mutation",
            "verification_repair_ready",
        }:
            continue
        previous = _dict_value(row.get("arguments"))
        if (
            str(previous.get("path") or "").replace("\\", "/") == path
            and str(previous.get(content_key) or "") == fingerprint
        ):
            count += 1
    return count


def _action_memory(
    history: list[dict[str, object]],
    *,
    target_item_paths: list[str],
    max_evidence_chars: int,
) -> dict[str, object]:
    ledger_by_key: dict[str, dict[str, object]] = {}
    evidence_conclusions_by_key: dict[str, dict[str, object]] = {}
    mutation_state_by_path: dict[str, dict[str, object]] = {}
    verification_state_by_stage: dict[str, dict[str, object]] = {}
    for index, row in enumerate(history, start=1):
        arguments = _dict_value(row.get("arguments"))
        result = _dict_value(row.get("result"))
        action = str(row.get("action") or "")
        path = str(arguments.get("path") or result.get("path") or "").replace("\\", "/")
        summary = str(row.get("summary") or "").strip()
        key = json.dumps(
            {
                "action": action,
                "path": path,
                "query": arguments.get("query"),
                "stage_id": arguments.get("stage_id") or result.get("stage_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        previous = ledger_by_key.get(key, {})
        ledger_by_key[key] = {
            "action": action,
            "path": path,
            "query": arguments.get("query"),
            "stage_id": arguments.get("stage_id") or result.get("stage_id"),
            "count": int(previous.get("count") or 0) + 1,
            "last_index": index,
            "ok": result.get("ok"),
            "error": row.get("error") or result.get("error"),
        }
        if action in _MUTATION_ACTIONS and result.get("ok") is True and path:
            for evidence_key, evidence in list(evidence_conclusions_by_key.items()):
                if evidence.get("path") == path:
                    evidence_conclusions_by_key.pop(evidence_key, None)
            mutation_state_by_path[path] = {
                key: value
                for key, value in {
                    "path": path,
                    "action": action,
                    "last_index": index,
                    "sha256": result.get("sha256"),
                    "summary": summary[:500],
                }.items()
                if value not in {None, ""}
            }
        if action in _OBSERVATION_ACTIONS and summary:
            evidence_key = json.dumps(
                {
                    "action": action,
                    "path": path,
                    "query": arguments.get("query"),
                    "start_line": result.get("start_line"),
                    "end_line": result.get("end_line"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            evidence_conclusions_by_key[evidence_key] = {
                key: value
                for key, value in {
                    "action": action,
                    "path": path,
                    "query": arguments.get("query"),
                    "start_line": result.get("start_line"),
                    "end_line": result.get("end_line"),
                    "verified_at_action": index,
                    "summary": summary[:500],
                    "outcome": (
                        "ok"
                        if result.get("ok") is True
                        else str(row.get("error") or result.get("error") or "unknown")
                    ),
                }.items()
                if value not in {None, ""}
            }
        if action == "verification.run_stage":
            stage_id = str(arguments.get("stage_id") or result.get("stage_id") or "")
            if stage_id:
                verification_state_by_stage[stage_id] = {
                    "stage_id": stage_id,
                    "last_index": index,
                    "result": _compact_working_result(result),
                }
    complete_ledger = sorted(ledger_by_key.values(), key=lambda row: int(row.get("last_index") or 0))
    sticky_ledger = [
        row
        for row in complete_ledger
        if row.get("action") in {*_MUTATION_ACTIONS, "verification.run_stage"} or row.get("error")
    ]
    remaining_capacity = max(0, _WORKING_MEMORY_LEDGER_SOFT_LIMIT - len(sticky_ledger))
    sticky_ids = {id(row) for row in sticky_ledger}
    non_sticky = [row for row in complete_ledger if id(row) not in sticky_ids]
    recent_non_sticky = non_sticky[-remaining_capacity:] if remaining_capacity else []
    retained_ids = {id(row) for row in [*sticky_ledger, *recent_non_sticky]}
    ledger = [row for row in complete_ledger if id(row) in retained_ids]

    diagnostic_breadcrumbs: list[dict[str, object]] = []
    for index, row in enumerate(history, start=1):
        summary = str(row.get("summary") or "").strip()
        arguments = _dict_value(row.get("arguments"))
        result = _dict_value(row.get("result"))
        action = str(row.get("action") or "")
        path = str(arguments.get("path") or result.get("path") or "").replace("\\", "/")
        if action in _MUTATION_ACTIONS and result.get("ok") is True and path:
            diagnostic_breadcrumbs = [
                breadcrumb
                for breadcrumb in diagnostic_breadcrumbs
                if not (
                    breadcrumb.get("path") == path
                    and breadcrumb.get("action") in _OBSERVATION_ACTIONS
                )
            ]
        if not summary:
            continue
        outcome = (
            "ok"
            if result.get("ok") is True
            else str(row.get("error") or result.get("error") or result.get("status") or "unknown")
        )
        diagnostic_breadcrumbs.append(
            {
                key: value
                for key, value in {
                    "action_index": index,
                    "action": action,
                    "path": path,
                    "stage_id": arguments.get("stage_id") or result.get("stage_id"),
                    "summary": summary[:500],
                    "outcome": outcome,
                }.items()
                if value is not None and value != ""
            }
        )

    latest_reads: list[dict[str, object]] = []
    seen_ranges: set[tuple[str, object, object]] = set()
    for history_index in range(len(history) - 1, -1, -1):
        row = history[history_index]
        if row.get("action") not in {"read_file", "repo.read"}:
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
                "verified_at_action": history_index + 1,
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
    for row in history[-_WORKING_MEMORY_RECENT_RESULT_LIMIT:]:
        result = dict(_dict_value(row.get("result")))
        if row.get("error") and not result:
            result = {
                "ok": False,
                "error": row.get("error"),
                "instruction": row.get("instruction"),
            }
            if row.get("findings"):
                result["findings"] = row.get("findings")
        if isinstance(result.get("content"), str):
            result["content_chars"] = len(result["content"])
            result.pop("content", None)
        recent_results.append(
            {
                "action": row.get("action"),
                "arguments": _dict_value(row.get("arguments")),
                "result": _compact_working_result(result),
            }
        )
    memory = {
        "projection": {
            "version": "developer_working_memory_projection_v2",
            "history_count": len(history),
            "action_ledger_total": len(complete_ledger),
            "action_ledger_retained": len(ledger),
            "raw_completion_included": False,
        },
        "action_ledger": ledger,
        "inspected_sources": inspected_sources,
        "recent_results": recent_results,
    }
    if evidence_conclusions_by_key:
        memory["evidence_conclusions"] = list(evidence_conclusions_by_key.values())
    if mutation_state_by_path:
        memory["mutation_state"] = list(mutation_state_by_path.values())
    if verification_state_by_stage:
        memory["verification_state"] = list(verification_state_by_stage.values())
    if diagnostic_breadcrumbs:
        memory["diagnostic_breadcrumbs"] = diagnostic_breadcrumbs[-_WORKING_MEMORY_BREADCRUMB_LIMIT:]
    return memory


def _compact_working_result(result: dict[str, object]) -> dict[str, object]:
    """Keep the next decision visible without duplicating full durable reports."""

    if result.get("failure_kind") or result.get("status") == "failed":
        reason = str(result.get("reason") or "")
        return {
            key: value
            for key, value in {
                "ok": result.get("ok"),
                "stage_id": result.get("stage_id"),
                "status": result.get("status"),
                "failure_kind": result.get("failure_kind"),
                "failing_test": _failing_test_name(reason),
                "reason": _compact_diagnostic_reason(reason),
                "diagnostic_fingerprint": result.get("diagnostic_fingerprint"),
                "diagnostic_repeat_count": result.get("diagnostic_repeat_count"),
                "report_ref": result.get("report_ref"),
            }.items()
            if value not in {None, ""}
        }
    return _dict_value(_clip_json_value(result, 2000))


def _failing_test_name(reason: str) -> str:
    marker = "Vitest test failure: "
    if marker not in reason:
        return ""
    value = reason.split(marker, 1)[1]
    return value.split(": Error:", 1)[0].strip()


def _compact_diagnostic_reason(reason: str) -> str:
    compact = reason.strip()
    for marker in (" at task (file://", " at Object.<anonymous> (file://", "\n    at "):
        if marker in compact:
            compact = compact.split(marker, 1)[0].rstrip()
    return compact[:800]


def _compact_repair_episode(episode: dict[str, object]) -> dict[str, object]:
    diagnostics = _dict_value(episode.get("diagnostics"))
    reason = str(diagnostics.get("reason") or "")
    return {
        key: value
        for key, value in {
            "stage_id": episode.get("stage_id"),
            "failure_kind": diagnostics.get("failure_kind"),
            "failing_test": _failing_test_name(reason),
            "reason": _compact_diagnostic_reason(reason),
            "diagnostic_excerpt": episode.get("diagnostic_excerpt"),
            "diagnostic_fingerprint": episode.get("diagnostic_fingerprint"),
            "diagnostic_repeat_count": episode.get("diagnostic_repeat_count"),
            "mutated_since_failure": episode.get("mutated_since_failure"),
            "verification_ready": episode.get("verification_ready"),
            "changed_paths": episode.get("changed_paths"),
            "selected_test_files": diagnostics.get("selected_test_files"),
            "report_ref": diagnostics.get("report_ref"),
        }.items()
        if value is not None and value != "" and value != []
    }


def _verification_diagnostic_excerpt(result: dict[str, object], *, limit: int = 4000) -> str:
    parts: list[str] = []
    for stage_result in _dict_items(result.get("stage_results")):
        if stage_result.get("status") != "failed":
            continue
        stage_id = str(stage_result.get("stage_id") or result.get("stage_id") or "verification")
        for stream in ("stdout", "stderr"):
            output = str(stage_result.get(stream) or "").strip()
            if output:
                parts.append(f"{stage_id} {stream}:\n{output}")
    excerpt = "\n\n".join(parts)
    if len(excerpt) <= limit:
        return excerpt
    marker = "\n...[truncated]"
    return excerpt[: max(0, limit - len(marker))] + marker


def _authoritative_sources(
    *,
    inspected_sources: list[dict[str, object]],
    current_sources: list[dict[str, object]],
    target_path: Path,
) -> list[dict[str, object]]:
    """Expose one source view; successful mutations replace cached observations."""

    current_by_path = {str(row.get("path") or ""): row for row in current_sources}
    sources: list[dict[str, object]] = []
    emitted: set[str] = set()
    for inspected in inspected_sources:
        path = str(inspected.get("path") or "")
        current = current_by_path.get(path)
        if current:
            sources.append(
                {
                    **current,
                    "origin": "worktree_after_mutation",
                    "freshness": "current_worktree_verified",
                    "changed": True,
                }
            )
        else:
            source = (target_path / path).resolve()
            sha256 = _file_hash(source) if source.is_file() else ""
            sources.append(
                {
                    **inspected,
                    "sha256": sha256,
                    "origin": "verified_current_read",
                    "freshness": "current_worktree_verified",
                    "changed": False,
                }
            )
        emitted.add(path)
    for path, current in current_by_path.items():
        if path not in emitted:
            sources.append(
                {
                    **current,
                    "origin": "worktree_after_mutation",
                    "freshness": "current_worktree_verified",
                    "changed": True,
                }
            )
    return sources


def _current_mutated_sources(
    history: list[dict[str, object]],
    *,
    target_path: Path,
    target_item_paths: list[str],
    max_evidence_chars: int,
) -> list[dict[str, object]]:
    """Read current worktree content for paths changed during this attempt."""

    mutated: set[str] = set()
    for row in history:
        if row.get("action") not in _MUTATION_ACTIONS:
            continue
        result = _dict_value(row.get("result"))
        path = str(result.get("path") or "").replace("\\", "/")
        if result.get("ok") is True and path in target_item_paths:
            mutated.add(path)

    sources: list[dict[str, object]] = []
    used = 0
    for path in target_item_paths:
        if path not in mutated:
            continue
        source = (target_path / path).resolve()
        try:
            source.relative_to(target_path.resolve())
        except ValueError:
            continue
        if not source.is_file() or not _read_path_allowed(target_path, source):
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        remaining = max_evidence_chars - used
        if remaining <= 0:
            break
        truncated = len(content) > remaining
        if truncated:
            if sources:
                continue
            content = content[:remaining]
        sources.append(
            {
                "path": path,
                "sha256": _file_hash(source),
                "content": content,
                "truncated": truncated,
            }
        )
        used += len(content)
    return sources


def _prior_attempt_feedback(entry: dict[str, object]) -> dict[str, object] | None:
    """Carry the latest rejected completion diagnostics into a resumed attempt."""

    action_sets: list[list[dict[str, object]]] = []
    current_actions = _dict_items(entry.get("actions"))
    if current_actions:
        action_sets.append(current_actions)
    for attempt in reversed(_dict_items(entry.get("attempt_history"))):
        archived_actions = _dict_items(attempt.get("actions"))
        if archived_actions:
            action_sets.append(archived_actions)

    for actions in action_sets:
        for action in reversed(actions):
            gate = _dict_value(action.get("completion_gate"))
            findings = _dict_items(gate.get("findings"))
            if gate.get("status") == "rejected" and findings:
                return {
                    "action": "prior_attempt_feedback",
                    "error": "developer_unit_signature_mismatch",
                    "instruction": "Repair these approved signature findings before completing the unit.",
                    "findings": findings,
                }

        failed_verification = next(
            (
                _dict_value(action.get("tool_result") or action.get("result"))
                for action in reversed(actions)
                if str(action.get("tool_action") or action.get("action") or "")
                == "verification.run_stage"
                and _dict_value(action.get("tool_result") or action.get("result")).get("status") == "failed"
                and _dict_value(action.get("tool_result") or action.get("result")).get("failure_kind")
                == "implementation"
            ),
            None,
        )
        if failed_verification:
            no_effect_count = sum(
                1
                for action in actions
                if _dict_value(action.get("tool_result") or action.get("result")).get("error")
                == "no_effect_mutation"
            )
            patch_failure_count = sum(
                1
                for action in actions
                if _dict_value(action.get("tool_result") or action.get("result")).get("error")
                == "patch_check_failed"
            )
            return {
                "action": "prior_attempt_feedback",
                "error": "previous_verification_repair_stalled",
                "instruction": (
                    "The previous attempt reached this implementation test failure but did not close the repair. "
                    "Re-read the current source, make each coherent mutation once, then rerun the failed stage. "
                    "Never resend identical file content after no_effect_mutation."
                ),
                "failed_stage_id": str(failed_verification.get("stage_id") or ""),
                "diagnostic": str(failed_verification.get("reason") or "")[:1600],
                "previous_no_effect_mutations": no_effect_count,
                "previous_patch_failures": patch_failure_count,
            }
    return None


def _parse_action(raw: str) -> dict[str, object]:
    text = final_response_text(raw)
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
    """Recover only a sequence with one unambiguous executable action."""

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
    if len(values) < 2:
        return None, len(values)
    first = values[0]
    if not isinstance(first, dict) or not isinstance(first.get("arguments"), dict):
        return None, len(values)
    executable = (first.get("action"), first.get("arguments"))
    if any(
        not isinstance(value, dict)
        or not isinstance(value.get("arguments"), dict)
        or (value.get("action"), value.get("arguments")) != executable
        for value in values[1:]
    ):
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
    work_packet: dict[str, object],
    repo_root: Path,
    item_paths: list[str],
    item: dict[str, object],
    session_dir: Path,
    max_write_chars: int,
    repo_context_provider: CodeGraphContextProvider,
    verification_runner: VerificationRunner,
    verification_policy: VerificationStagePolicy | None,
) -> dict[str, object]:
    raw_name = str(action["action"])
    name = _ACTION_TO_LEGACY.get(raw_name, raw_name)
    args = _dict_value(action.get("arguments"))
    if raw_name == "codegraph.query_readonly":
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 500:
            return {"ok": False, "error": "invalid_codegraph_query"}
        evidence = repo_context_provider.query_readonly(repo_root, query)
        return {
            "ok": True,
            "query_sha256": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "evidence": evidence[:12000],
            "truncated": len(evidence) > 12000,
        }
    if raw_name == "verification.run_stage":
        stage_id = str(args.get("stage_id") or "").strip()
        required = set(_string_items(item.get("required_stage_ids")))
        if stage_id not in required:
            return {
                "ok": False,
                "error": "developer_unit_stage_not_approved",
                "approved_stage_ids": sorted(required),
            }
        if verification_policy is None:
            return {"ok": False, "error": "verification_stage_policy_invalid"}
        result = verification_runner.run_stage(
            target_path=repo_root,
            work_packet=work_packet,
            stage_id=stage_id,
            required_test_files=(
                _string_items(item.get("test_target_paths")) if stage_id == "unit" else []
            ),
            verification_policy=verification_policy,
        )
        evidence = {
            "ok": result.status == "passed",
            "unit_id": item.get("id"),
            "stage_id": stage_id,
            "status": result.status,
            "failure_kind": result.failure_kind,
            "reason": result.reason,
            "input_hashes": {path: _file_hash(repo_root / path) for path in item_paths},
            "selected_test_files": list(result.selected_test_files),
            "verified_test_files": list(result.verified_test_files),
            "stage_results": [row.to_dict() for row in result.stage_results],
        }
        report_file = (
            session_dir
            / "units"
            / str(item.get("id") or "unknown")
            / "verification"
            / f"{stage_id}.json"
        )
        report_payload = {
            "version": "developer_unit_verification_v1",
            **evidence,
            "created_at": utc_now_iso(),
        }
        _write_json_atomic(report_file, report_payload)
        return {
            **evidence,
            "report_ref": str(report_file.relative_to(session_dir)).replace("\\", "/"),
            "report_digest": _json_hash(report_payload),
        }
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
                "instruction": (
                    "This exact content is already present and cannot repair the latest diagnostic. "
                    "Do not resend it; make a materially different change, re-read the source, or rerun verification "
                    "only if a prior mutation in this repair batch already succeeded."
                ),
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
    return developer_path_is_test(path)


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
        _validate_patch_hunks(patch_text, run_id=run_id)
        return patch_text

    stripped = patch_text.lstrip()
    if not stripped.startswith("@@ "):
        raise HarnessError(
            "developer_patch_invalid",
            "Headerless patch must start with a unified-diff @@ hunk.",
            run_id=run_id,
        )
    _validate_patch_hunks(stripped, run_id=run_id)
    return f"--- a/{normalized}\n+++ b/{normalized}\n{stripped}"


def _validate_patch_hunks(patch_text: str, *, run_id: str) -> None:
    lines = patch_text.splitlines()
    headers: list[tuple[int, re.Match[str]]] = []
    pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)?$")
    for index, line in enumerate(lines):
        if not line.startswith("@@"):
            continue
        match = pattern.match(line)
        if not match:
            raise HarnessError(
                "developer_patch_invalid",
                f"Patch has an invalid hunk header: {line}",
                run_id=run_id,
            )
        headers.append((index, match))
    if not headers:
        raise HarnessError(
            "developer_patch_invalid",
            "Patch must contain at least one complete unified-diff hunk.",
            run_id=run_id,
        )
    for position, (start, match) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        old_actual = 0
        new_actual = 0
        for line in lines[start + 1 : end]:
            if line.startswith(" "):
                old_actual += 1
                new_actual += 1
            elif line.startswith("-"):
                old_actual += 1
            elif line.startswith("+"):
                new_actual += 1
            elif line == r"\ No newline at end of file":
                continue
            else:
                raise HarnessError(
                    "developer_patch_invalid",
                    "Patch hunk contains a line without a unified-diff prefix.",
                    run_id=run_id,
                )
        old_expected = int(match.group(2) or 1)
        new_expected = int(match.group(4) or 1)
        if old_actual != old_expected or new_actual != new_expected:
            raise HarnessError(
                "developer_patch_invalid",
                (
                    "Patch hunk line counts do not match its header "
                    f"(old {old_actual}/{old_expected}, new {new_actual}/{new_expected})."
                ),
                run_id=run_id,
            )


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


def _item_approved_signatures(item: dict[str, object]) -> list[dict[str, object]]:
    signatures: list[dict[str, object]] = []
    seen: set[str] = set()
    for file_item in _item_files(item):
        for signature in _dict_items(file_item.get("approved_signatures")):
            identity = str(signature.get("contract_id") or "") or _json_hash(signature)
            if identity in seen:
                continue
            seen.add(identity)
            signatures.append(signature)
    return signatures


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


def _focused_execution_task(
    item: dict[str, object],
    *,
    context_paths: list[str],
    active_repair: bool,
) -> dict[str, object]:
    task = _prompt_execution_task(item, context_paths=context_paths)
    if not active_repair:
        return task
    keep = {
        "id",
        "title",
        "objective",
        "order",
        "scenario_id",
        "component_id",
        "surfaces",
        "depends_on",
        "paths",
        "allowed_paths",
        "requirement_ids",
        "requirement_refs",
        "acceptance_refs",
        "requirements",
        "test_intents",
        "required_stage_ids",
        "test_target_paths",
        "implementation_steps",
    }
    return {key: value for key, value in task.items() if key in keep}


def _prompt_execution_task(item: dict[str, object], *, context_paths: list[str]) -> dict[str, object]:
    context = set(context_paths)
    steps = [
        row
        for row in _dict_items(item.get("implementation_steps"))
        if context.intersection(path.replace("\\", "/") for path in _string_items(row.get("files")))
    ]
    contract_fields = {
        "boundary_contracts",
        "migration_impact",
        "planned_code_targets",
        "runtime_bindings",
        "surface_contracts",
    }
    visible_item = {
        key: value
        for key, value in item.items()
        if key != "required_paths" and key not in contract_fields
    }
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


def _merge_context_only_manifest(
    existing: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object] | None:
    """Accept only a read-context expansion while preserving durable write baselines."""

    if existing.get("mode") != "act" or candidate.get("mode") != "act":
        return None
    stable_manifest_fields = (
        "version",
        "run_id",
        "mode",
        "repair_iteration",
        "base_sha",
        "workspace_allowed_paths",
        "expected_dirty_files",
        "repair_feedback",
        "developer_generation_context",
    )
    if any(existing.get(field) != candidate.get(field) for field in stable_manifest_fields):
        return None
    existing_items = _dict_items(existing.get("items"))
    candidate_items = _dict_items(candidate.get("items"))
    if [item.get("id") for item in existing_items] != [item.get("id") for item in candidate_items]:
        return None

    expanded = False
    for old_item, new_item in zip(existing_items, candidate_items):
        if _context_stable_item(old_item) != _context_stable_item(new_item):
            return None
        old_context = set(_item_context_paths(old_item))
        new_context = set(_item_context_paths(new_item))
        old_anchors = set(_item_contract_anchor_paths(old_item))
        new_anchors = set(_item_contract_anchor_paths(new_item))
        if not old_context <= new_context or not old_anchors <= new_anchors:
            return None
        expanded = expanded or old_context != new_context or old_anchors != new_anchors
    if not expanded:
        return None

    merged = json.loads(json.dumps(existing))
    merged["input_hash"] = candidate.get("input_hash")
    for old_item, new_item in zip(_dict_items(merged.get("items")), candidate_items):
        old_item["context_paths"] = _item_context_paths(new_item)
        old_item["contract_anchor_paths"] = _item_contract_anchor_paths(new_item)
        old_item["diagnostic_paths"] = _item_diagnostic_paths(new_item)
        old_item["input_digest"] = new_item.get("input_digest")
    return merged


def _merge_database_entrypoint_scope_manifest(
    existing: dict[str, object],
    candidate: dict[str, object],
) -> tuple[dict[str, object], list[str]] | None:
    """Allow only the deterministic DB barrel addition inside an active act session."""

    barrel = "packages/db/src/index.ts"
    if existing.get("mode") != "act" or candidate.get("mode") != "act":
        return None
    stable_manifest_fields = (
        "version",
        "run_id",
        "mode",
        "repair_iteration",
        "base_sha",
        "workspace_allowed_paths",
        "expected_dirty_files",
        "repair_feedback",
        "developer_generation_context",
    )
    if any(existing.get(field) != candidate.get(field) for field in stable_manifest_fields):
        return None
    existing_items = _dict_items(existing.get("items"))
    candidate_items = _dict_items(candidate.get("items"))
    if [item.get("id") for item in existing_items] != [item.get("id") for item in candidate_items]:
        return None

    reopened: list[str] = []
    for old_item, new_item in zip(existing_items, candidate_items):
        old_paths = set(_item_paths(old_item))
        new_paths = set(_item_paths(new_item))
        added_paths = new_paths - old_paths
        if old_paths - new_paths or added_paths - {barrel}:
            return None
        if _entrypoint_scope_stable_item(old_item) != _entrypoint_scope_stable_item(new_item):
            return None
        if added_paths:
            if str(new_item.get("component_id") or "") != "database" and "data" not in set(
                _string_items(new_item.get("surfaces"))
            ):
                return None
            reopened.append(str(new_item.get("id") or ""))

        old_targets = _dict_items(old_item.get("planned_code_targets"))
        new_targets = _dict_items(new_item.get("planned_code_targets"))
        old_target_hashes = {_json_hash(target) for target in old_targets}
        added_targets = [target for target in new_targets if _json_hash(target) not in old_target_hashes]
        if any(str(target.get("path") or "").replace("\\", "/") != barrel for target in added_targets):
            return None
        old_files = {
            str(file_item.get("path") or ""): _entrypoint_scope_stable_file(file_item)
            for file_item in _item_files(old_item)
        }
        new_files = {
            str(file_item.get("path") or ""): _entrypoint_scope_stable_file(file_item)
            for file_item in _item_files(new_item)
        }
        if any(new_files.get(path) != value for path, value in old_files.items()):
            return None
        if set(new_files) - set(old_files) != added_paths:
            return None

    if not reopened:
        return None
    merged = json.loads(json.dumps(existing))
    merged["input_hash"] = candidate.get("input_hash")
    candidate_by_id = {
        str(item.get("id") or ""): item for item in candidate_items
    }
    for index, old_item in enumerate(_dict_items(merged.get("items"))):
        item_id = str(old_item.get("id") or "")
        if item_id in reopened:
            merged["items"][index] = json.loads(json.dumps(candidate_by_id[item_id]))
    return merged, reopened


def _entrypoint_scope_stable_item(item: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in item.items()
        if key not in {
            "paths",
            "context_paths",
            "direct_repair_paths",
            "diagnostic_paths",
            "contract_anchor_paths",
            "files",
            "planned_code_targets",
            "input_digest",
        }
    }


def _entrypoint_scope_stable_file(file_item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in file_item.items() if key != "initial_sha256"}


def _database_entrypoint_journal_reopen_ids(
    *,
    manifest: dict[str, object],
    journal: dict[str, object],
) -> list[str]:
    """Recover a manifest-first migration interrupted before its journal write."""

    barrel = "packages/db/src/index.ts"
    result: list[str] = []
    for item in _dict_items(manifest.get("items")):
        if barrel not in set(_item_paths(item)):
            continue
        if str(item.get("component_id") or "") != "database" and "data" not in set(
            _string_items(item.get("surfaces"))
        ):
            continue
        item_id = str(item.get("id") or "")
        entry = _journal_item(journal, item_id)
        if barrel not in set(_string_items(entry.get("paths"))):
            result.append(item_id)
    return result


def _reopen_database_entrypoint_items(
    *,
    target_path: Path,
    session_dir: Path,
    manifest: dict[str, object],
    journal: dict[str, object],
    item_ids: list[str],
) -> None:
    """Reopen only completed DB providers and preserve their previous audit artifacts."""

    _supersede_pending_step_review(
        session_dir=session_dir,
        journal=journal,
        reason="database_entrypoint_scope_reopened",
    )

    item_by_id = {
        str(item.get("id") or ""): item for item in _dict_items(manifest.get("items"))
    }
    reopened_at = utc_now_iso()
    for item_id in item_ids:
        item = item_by_id.get(item_id)
        if item is None:
            raise HarnessError(
                "developer_execution_manifest_invalid",
                f"Database entrypoint migration is missing execution unit {item_id}.",
            )
        entry = _journal_item(journal, item_id)
        entry["paths"] = _item_paths(item)
        entry["context_paths"] = _item_context_paths(item)
        entry["direct_repair_paths"] = _item_direct_repair_paths(item)
        entry["diagnostic_paths"] = _item_diagnostic_paths(item)
        entry["contract_anchor_paths"] = _item_contract_anchor_paths(item)
        if entry.get("status") != "completed":
            continue

        unit_dir = session_dir / "units" / item_id
        completion_file = unit_dir / "completion.json"
        if completion_file.is_file():
            completion_history = unit_dir / f"completion_before_scope_reopen_{len(_dict_items(entry.get('scope_reopen_history'))) + 1}.json"
            _write_json_atomic(completion_history, _read_json(completion_file, run_id=""))
        snapshot_file = session_dir / "snapshots" / f"{item_id}.json"
        if snapshot_file.is_file():
            snapshot_history = session_dir / "snapshots" / f"{item_id}.before_scope_reopen_{len(_dict_items(entry.get('scope_reopen_history'))) + 1}.json"
            _write_json_atomic(snapshot_history, _read_json(snapshot_file, run_id=""))
        _write_json_atomic(
            snapshot_file,
            {
                "files": [
                    {
                        "path": path,
                        "existed": (target_path / path).is_file(),
                        "content": (target_path / path).read_text(encoding="utf-8", errors="replace")
                        if (target_path / path).is_file()
                        else "",
                        "sha256": _file_hash(target_path / path),
                    }
                    for path in _item_paths(item)
                ]
            },
        )
        entry.setdefault("scope_reopen_history", []).append(
            {
                "reason": "database_entrypoint_scope_added",
                "reopened_at": reopened_at,
                "completed_at": entry.get("completed_at"),
                "summary": entry.get("summary"),
                "after_sha256_by_path": entry.get("after_sha256_by_path", {}),
                "mutated_paths": entry.get("mutated_paths", []),
            }
        )
        for key in (
            "completed_at",
            "after_sha256_by_path",
            "mutated_paths",
            "summary",
            "no_change_required",
            "verification_evidence",
            "path_dispositions",
            "signature_evidence",
            "paused_sha256_by_path",
        ):
            entry.pop(key, None)
        entry["status"] = "pending"
        entry["scope_reopened_at"] = reopened_at
        entry["handled_paths"] = []
        entry["inspected_paths"] = []
        entry["unchanged_paths"] = []
        entry["unresolved_paths"] = []
    journal.pop("pending_step_review", None)
    journal["status"] = "paused"
    journal["updated_at"] = reopened_at


def _supersede_pending_step_review(
    *,
    session_dir: Path,
    journal: dict[str, object],
    reason: str,
) -> None:
    pending_review = _dict_value(journal.get("pending_step_review"))
    pending_call_id = str(pending_review.get("call_id") or "")
    if not pending_call_id:
        return
    for review_session_file in (session_dir / "step_review").glob("*/step_review_session_v1.json"):
        review_session = _read_json(review_session_file, run_id="")
        calls = _dict_items(review_session.get("calls"))
        changed = False
        for call in calls:
            if (
                str(call.get("call_id") or "") == pending_call_id
                and str(call.get("status") or "") == "pending"
            ):
                call["status"] = "superseded"
                call["superseded_at"] = utc_now_iso()
                call["superseded_reason"] = reason
                _write_json_atomic(review_session_file.parent / f"exchange_{pending_call_id}.json", call)
                changed = True
        if changed:
            review_session["calls"] = calls
            review_session["updated_at"] = utc_now_iso()
            _write_json_atomic(review_session_file, review_session)
            journal.pop("pending_step_review", None)
            return


def _context_stable_item(item: dict[str, object]) -> dict[str, object]:
    stable = {
        key: value
        for key, value in item.items()
        if key not in {
            "context_paths",
            "contract_anchor_paths",
            "diagnostic_paths",
            "input_digest",
        }
    }
    stable["files"] = [
        {key: value for key, value in file_item.items() if key != "initial_sha256"}
        for file_item in _dict_items(stable.get("files"))
    ]
    return stable


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
                "before_sha256": file_item.get("initial_sha256"),
                "after_sha256": after_by_path.get(path),
                "no_change_required": False,
            }
        )
    return result


def _progress(journal: dict[str, object]) -> dict[str, object]:
    items = _dict_items(journal.get("items"))
    completed_ids = [str(item.get("id") or "") for item in items if item.get("status") == "completed"]
    pending_ids = [str(item.get("id") or "") for item in items if item.get("status") != "completed"]
    completed = len(completed_ids)
    tool_calls_used = sum(len(_dict_items(item.get("actions"))) for item in items)
    current = next(
        (str(item.get("id") or "") for item in items if item.get("status") == "running"),
        str(journal.get("last_failed_unit_id") or ""),
    )
    progress: dict[str, object] = {
        "status": journal.get("status"),
        "total_items": len(items),
        "completed_items": completed,
        "remaining_items": len(items) - completed,
        "current_item": current,
        "current_unit_id": current,
        "completed_unit_ids": completed_ids,
        "pending_unit_ids": pending_ids,
        "total_units": len(items),
        "completed_units": completed,
        "tool_calls_used": tool_calls_used,
    }
    pending_step_review = _dict_value(journal.get("pending_step_review"))
    if pending_step_review:
        progress["pending_step_review"] = pending_step_review
    return progress


def _stage_evidence(history: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        dict(_dict_value(row.get("result")))
        for row in history
        if row.get("action") == "verification.run_stage"
        and _dict_value(row.get("result")).get("ok") is True
    ]


def _current_passed_stages(
    history: list[dict[str, object]],
    *,
    current_hashes: dict[str, str],
) -> set[str]:
    return {
        str(result.get("stage_id") or "")
        for result in _stage_evidence(history)
        if result.get("status") == "passed"
        and _dict_value(result.get("input_hashes")) == current_hashes
        and str(result.get("stage_id") or "")
    }


def _current_failed_stages(
    history: list[dict[str, object]],
    *,
    current_hashes: dict[str, str],
) -> set[str]:
    del current_hashes
    return set(_verification_repair_state(history))


def _timeout_diagnostic_state(history: list[dict[str, object]]) -> dict[str, object]:
    """Require evidence-producing instrumentation for a unit-test timeout with no source location."""

    repair_state = _verification_repair_state(history)
    for stage_id in sorted(repair_state):
        episode = repair_state[stage_id]
        diagnostics = _dict_value(episode.get("diagnostics"))
        reason = str(diagnostics.get("reason") or "")
        lowered = reason.casefold()
        test_paths = _string_items(diagnostics.get("selected_test_files"))
        if (
            stage_id != "unit"
            or not test_paths
            or ("timed out" not in lowered and "timeout" not in lowered)
            or _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX.casefold() in lowered
        ):
            continue

        latest_failure_index = -1
        for index in range(len(history) - 1, -1, -1):
            row = history[index]
            result = _dict_value(row.get("result"))
            if (
                row.get("action") == "verification.run_stage"
                and str(result.get("stage_id") or "") == stage_id
                and result.get("status") == "failed"
            ):
                latest_failure_index = index
                break
        instrumented = any(
            _dict_value(row.get("result")).get("timeout_diagnostic_instrumentation") is True
            for row in history[latest_failure_index + 1 :]
        )
        return {
            "status": "verification_required" if instrumented else "instrumentation_required",
            "stage_id": stage_id,
            "test_paths": test_paths,
            "failing_test": _failing_test_name(reason),
            "reason": _compact_diagnostic_reason(reason),
        }
    return {}


def _timeout_diagnostic_action_limit(
    *,
    configured_max_actions: int,
    timeout_action_reserve: int,
    history: list[dict[str, object]],
) -> int:
    """Reserve cleanup, clean verification, and completion only after instrumentation succeeds."""

    instrumented = any(
        _dict_value(row.get("result")).get("timeout_diagnostic_instrumentation") is True
        for row in history
    )
    return configured_max_actions + timeout_action_reserve if instrumented else configured_max_actions


def _step_review_start_ordinal(
    *,
    session_path: Path,
    requested_cursor: int,
    run_id: str,
) -> int:
    """Resume a pending exchange, otherwise continue after prior attempt history."""

    cursor = max(0, requested_cursor)
    if not session_path.is_file():
        return cursor
    calls = _dict_items(_read_json(session_path, run_id=run_id).get("calls"))
    if any(str(row.get("status") or "") == "pending" for row in calls):
        return cursor
    return max(cursor, *(int(row.get("ordinal") or 0) for row in calls))


def _is_timeout_diagnostic_instrumentation_action(
    action: dict[str, object],
    *,
    timeout_diagnostic: dict[str, object],
) -> bool:
    if timeout_diagnostic.get("status") != "instrumentation_required":
        return False
    if action.get("action") not in _MUTATION_ACTIONS:
        return False
    arguments = _dict_value(action.get("arguments"))
    path = str(arguments.get("path") or "").replace("\\", "/")
    payload = str(arguments.get("content") or arguments.get("patch") or "")
    return (
        path in set(_string_items(timeout_diagnostic.get("test_paths")))
        and _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER in payload
        and _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX in payload
    )


def _timeout_diagnostic_cleanup_paths(
    *,
    target_path: Path,
    item_paths: list[str],
) -> list[str]:
    remaining: list[str] = []
    for path in item_paths:
        candidate = target_path / path
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if (
            _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER in content
            or _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX in content
        ):
            remaining.append(path)
    return remaining


def _timeout_diagnostic_completion_error(
    *,
    target_path: Path,
    item_paths: list[str],
) -> dict[str, object] | None:
    remaining = _timeout_diagnostic_cleanup_paths(
        target_path=target_path,
        item_paths=item_paths,
    )
    if not remaining:
        return None
    return {
        "error": "timeout_diagnostic_cleanup_required",
        "paths": remaining,
        "instruction": (
            "Remove temporary timeout diagnostic instrumentation and rerun required verification stages before "
            f"completing the unit: {remaining}."
        ),
    }


def _verification_repair_state(history: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Keep a failed stage active until that same stage passes after a repair batch."""

    active: dict[str, dict[str, object]] = {}
    for row in history:
        action = str(row.get("action") or "")
        result = _dict_value(row.get("result"))
        if action == "verification.run_stage":
            stage_id = str(result.get("stage_id") or "")
            if not stage_id:
                continue
            if result.get("status") == "passed":
                active.pop(stage_id, None)
                continue
            if result.get("status") != "failed" or result.get("failure_kind") != "implementation":
                continue
            fingerprint = str(result.get("diagnostic_fingerprint") or "")
            fingerprint = fingerprint or _verification_diagnostic_fingerprint(result)
            active[stage_id] = {
                "stage_id": stage_id,
                "diagnostic_fingerprint": fingerprint,
                "diagnostic_repeat_count": int(result.get("diagnostic_repeat_count") or 1),
                "mutated_since_failure": False,
                "verification_ready": False,
                "verification_forced": False,
                "changed_paths": [],
                "diagnostic_excerpt": _verification_diagnostic_excerpt(result),
                "diagnostics": _clip_json_value(result, 8000),
            }
            continue
        if action == "upstream_dependency_completed":
            stale_stage_ids = set(_string_items(result.get("stale_stage_ids")))
            for stage_id, episode in active.items():
                if stage_id not in stale_stage_ids:
                    continue
                episode["verification_ready"] = True
                episode["verification_forced"] = True
                episode["upstream_dependency_id"] = str(result.get("dependency_id") or "")
                episode["upstream_changed_paths"] = _string_items(result.get("changed_paths"))
            continue
        if action in _MUTATION_ACTIONS and result.get("error") == "no_effect_mutation":
            for episode in active.values():
                if episode.get("mutated_since_failure") is True:
                    episode["verification_ready"] = True
            continue
        if action in _MUTATION_ACTIONS and result.get("error") == "verification_repair_ready":
            for episode in active.values():
                if episode.get("verification_ready") is True:
                    episode["verification_forced"] = True
            continue
        if action not in _MUTATION_ACTIONS or result.get("ok") is not True:
            continue
        path = str(result.get("path") or "").replace("\\", "/")
        for episode in active.values():
            episode["mutated_since_failure"] = True
            episode["verification_ready"] = False
            episode["verification_forced"] = False
            changed = _string_items(episode.get("changed_paths"))
            if path and path not in changed:
                changed.append(path)
            episode["changed_paths"] = changed
    return active


def _verification_diagnostic_fingerprint(result: dict[str, object]) -> str:
    """Hash stable diagnostics only; ignore timing, report paths and input hashes."""

    def normalized(value: object) -> str:
        text = str(value or "")
        text = re.sub(r"[A-Za-z]:[\\/][^\r\n\s]+", "<absolute-path>", text)
        return re.sub(r"\s+", " ", text).strip()

    stage_results = []
    for row in _dict_items(result.get("stage_results")):
        if row.get("status") != "failed":
            continue
        stage_results.append(
            {
                "stage_id": str(row.get("stage_id") or ""),
                "failure_kind": str(row.get("failure_kind") or ""),
                "reason": normalized(row.get("reason")),
                "stdout": normalized(row.get("stdout")),
                "stderr": normalized(row.get("stderr")),
            }
        )
    payload = {
        "stage_id": str(result.get("stage_id") or ""),
        "failure_kind": str(result.get("failure_kind") or ""),
        "reason": normalized(result.get("reason")),
        "stage_results": stage_results,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _diagnostic_repeat_count(history: list[dict[str, object]], *, fingerprint: str) -> int:
    count = 0
    for row in history:
        if row.get("action") != "verification.run_stage":
            continue
        result = _dict_value(row.get("result"))
        if result.get("status") != "failed" or result.get("failure_kind") != "implementation":
            continue
        observed = str(result.get("diagnostic_fingerprint") or "")
        observed = observed or _verification_diagnostic_fingerprint(result)
        if observed == fingerprint:
            count += 1
    return count


def _unit_signature_evidence(
    *,
    run_id: str,
    target_path: Path,
    work_packet: dict[str, object],
    item: dict[str, object],
) -> dict[str, object]:
    required_ids = set(_string_items(item.get("signature_entry_ids")))
    if not required_ids:
        return {"status": "not_required", "signature_entry_ids": []}
    contract = _dict_value(work_packet.get("implementation_signature_contract"))
    entries = [
        dict(entry)
        for entry in _dict_items(contract.get("entries"))
        if str(entry.get("contract_id") or "") in required_ids
    ]
    if {str(entry.get("contract_id") or "") for entry in entries} != required_ids:
        raise DeveloperExecutionError(
            "implementation_signature_contract_invalid",
            "Developer unit signature IDs do not resolve to the approved contract.",
            run_id=run_id,
            retriable=False,
        )
    project_profile_id = str(
        _dict_value(work_packet.get("planning_profile")).get("project_profile_id")
        or "typescript_fullstack_v1"
    )
    project = get_project_profile(project_profile_id)
    language = get_language_profile(project.language_profile_id)
    planning_policy = build_planning_path_policy(project=project, language=language)
    code_trace_policy = build_code_trace_policy(
        planning_policy=planning_policy,
        symbol_extractor_id=language.symbol_extractor_id,
    )
    ast_evidence = TypeScriptAstExtractor().extract(
        target_path=target_path,
        source_paths=list(dict.fromkeys(str(entry.get("source_path") or "") for entry in entries)),
        policy=code_trace_policy,
        run_id=run_id,
    )
    subset_contract = {"version": contract.get("version"), "entries": entries}
    subset_contract["digest"] = implementation_signature_contract_digest(subset_contract)
    findings = deterministic_typescript_contract_findings(
        work_packet={**work_packet, "implementation_signature_contract": subset_contract},
        ast_evidence=ast_evidence,
    )
    return {
        "status": "failed" if findings else "passed",
        "signature_entry_ids": sorted(required_ids),
        "source_paths": sorted({str(entry.get("source_path") or "") for entry in entries}),
        "findings": [
            {
                "type": finding.get("type"),
                "code_ref": finding.get("code_ref"),
                "message": finding.get("message"),
            }
            for finding in findings
        ],
    }


def _write_session(
    path: Path,
    *,
    run_id: str,
    manifest: dict[str, object],
    journal: dict[str, object],
    status: str,
) -> None:
    progress = _progress(journal)
    _write_json_atomic(
        path,
        {
            "version": "developer_loop_session_v1",
            "run_id": run_id,
            "session_id": path.parent.name,
            "contract_version": (
                "developer_execution_units_v1"
                if manifest.get("version") == "developer_execution_manifest_v8"
                else "developer_execution_legacy_v1"
            ),
            "manifest_input_hash": manifest.get("input_hash"),
            "mode": manifest.get("mode"),
            "status": status,
            **progress,
            "updated_at": utc_now_iso(),
        },
    )


def _failure_classification(error_code: str, *, retriable: bool) -> dict[str, object]:
    if error_code in {"developer_step_review_required", "developer_step_review_drift"}:
        failure_class, owner, route = "operator_review", "harness_operator", "pause_and_resume"
    elif error_code in {"llm_transport_failed", "requirement_refiner_failed"}:
        failure_class, owner, route = "transport_failure", "llm_transport", "pause_and_resume"
    elif (
        "environment" in error_code
        or error_code.startswith("target_repo_codegraph")
        or error_code.startswith("typescript_ast_extract")
    ):
        failure_class, owner, route = "environment_failure", "harness_operator", "pause_and_resume"
    elif error_code in {
        "developer_unit_dependency_deadlock",
        "developer_execution_units_missing",
        "developer_execution_unit_dependency_invalid",
        "developer_unit_write_ownership_ambiguous",
    }:
        failure_class, owner, route = "upstream_plan_conflict", "plan_human", "block_plan_review"
    elif error_code.startswith("developer_work_packet") or error_code == "developer_input_drift":
        failure_class, owner, route = "upstream_plan_conflict", "plan_human", "block_plan_review"
    elif any(token in error_code for token in ("unsafe", "out_of_scope", "policy", "drift")):
        failure_class, owner, route = "policy_violation", "harness", "block"
    else:
        failure_class, owner, route = "implementation_defect", "developer", "block_with_evidence"
    return {
        "class": failure_class,
        "owner": owner,
        "repairable": bool(retriable),
        "route": route,
    }


def _write_failure_classification(
    path: Path,
    *,
    run_id: str,
    error: DeveloperExecutionError,
    manifest: dict[str, object],
    journal: dict[str, object],
) -> None:
    _write_json_atomic(
        path,
        {
            "version": "developer_failure_classification_v1",
            "run_id": run_id,
            "session_id": path.parent.name,
            "unit_id": _progress(journal).get("current_unit_id") or journal.get("last_failed_unit_id"),
            "error_code": error.error_code,
            "message": error.message,
            **error.failure_classification,
            "manifest_input_hash": manifest.get("input_hash"),
            "progress": _progress(journal),
            "created_at": utc_now_iso(),
        },
    )


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
