"""Vòng tool Developer có giới hạn ngân sách và checkpoint execution unit bền vững."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable

from ...core.config import phase4_llm_call_options
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
from ..designer.uiux import UiUxGuidanceProvider
from .fake_generation import fake_developer_files
from .package_dependency import ensure_workspace_dependency
from .profile_context import (
    developer_generation_context,
    developer_path_is_test,
    developer_prompt_context,
)
from .repair_context import build_repair_closure
from .runtime_config import runtime_config_source_issues
from .visual_profile import (
    app_visual_scope_id,
    normalize_visual_candidate,
    read_visual_profiles,
    visual_profile_required,
)
from ...core.coerce import dict_items as _dict_items, dict_value as _dict_value, string_items as _string_items
from .unit_facts import (
    _MAX_BATCH_OBSERVATIONS,
    _MUTATION_ACTIONS,
    _OBSERVATION_ACTIONS,
    _READ_DENIED_NAMES,
    _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX,
    _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER,
    _clip_json_value,
    _current_passed_stages,
    _execution_phase,
    _file_hash,
    _has_successful_target_inspection,
    _has_test_authoring_context,
    _is_typescript_test_path,
    _item_context_paths,
    _item_contract_anchor_paths,
    _item_dependency_paths,
    _item_diagnostic_paths,
    _item_direct_repair_paths,
    _item_execution_hash_paths,
    _item_files,
    _item_paths,
    _json_hash,
    _package_dependency_statuses,
    _read_path_allowed,
    _required_test_mutation_paths,
    _stage_evidence,
    _timeout_diagnostic_cleanup_paths,
    _timeout_diagnostic_state,
    _verification_diagnostic_fingerprint,
    _verification_repair_state,
)
from .prompting import (
    _action_memory,
    _action_prompt,
    _current_mutated_sources,
    _unit_mandatory_obligations,
)


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
    "repo.read_batch",
    "repo.search",
    "repo.search_batch",
    "repo.read_diff",
    "codegraph.query",
    "codegraph.query_readonly",
    "uiux.search_guidance",
    "uiux.search_visual_profile",
    "uiux.commit_visual_profile",
    "repo.write_file",
    "repo.apply_patch",
    "repo.replace_text",
    "package.ensure_workspace_dependency",
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
    "repo.replace_text": "replace_text",
    "execution.complete_unit": "complete",
}
_RECOVERABLE_ACTION_POLICY_ERRORS = {
    "developer_act_out_of_scope",
    "developer_patch_out_of_scope",
    "developer_patch_invalid",
    "developer_test_context_required",
}
_MAX_IDENTICAL_DIAGNOSTIC_RESULTS = 3
_MAX_IDENTICAL_NO_EFFECT_MUTATIONS = 3
_SEARCH_PRUNED_DIR_NAMES = {"node_modules", ".codegraph", "dist", "build", "coverage"}
_SEARCH_MAX_SCANNED_FILES = 5000
_SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
_SEARCH_TIMEOUT_SECONDS = 5.0
_BATCH_READ_CHARS_PER_ITEM = 6000


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


@dataclass
class _UnitLoop:
    """One execution unit's Developer tool loop: fixed inputs and budgets, plus the state working_state.json keeps."""

    store: RuntimeStore
    run_id: str
    work_packet: dict[str, object]
    target_path: Path
    session_dir: Path
    item: dict[str, object]
    manifest: dict[str, object]
    journal: dict[str, object]
    journal_file: Path
    on_progress: Callable[[dict[str, object], str, str], None] | None
    manifest_path: str
    journal_path: str
    verification_policy: VerificationStagePolicy | None
    item_id: str
    entry: dict[str, object]
    item_paths: list[str]
    execution_hash_paths: list[str]
    context_paths: list[str]
    repair_mode: bool
    working_state_file: Path
    llm_client: Any
    configured_max_actions: int
    timeout_action_reserve: int
    verification_repair_action_reserve: int
    visual_profile_action_reserve: int
    maximum_action_limit: int
    max_retries: int
    fallback_max_tokens: int
    normal_options: dict[str, object]
    repair_options: dict[str, object]
    context_window_tokens: int
    working_memory_evidence_chars: int
    max_write_chars: int
    visual_profile_file: Path
    visual_app_scope_id: str
    visual_required: bool
    history: list[dict[str, object]]
    seen_observations: set[str]
    mutated_paths: set[str]
    refreshed_dependency_ids: set[str]
    length_failures: int
    invalid_action_failures: int
    next_action_index: int
    durable_next_action_index: int
    action_index: int  # the action in progress: an interrupted unit resumes from it

    def fail(self, error_code: str, message: str, *, retriable: bool) -> DeveloperExecutionError:
        """The loop's error, carrying its manifest, journal and current progress."""
        return DeveloperExecutionError(
            error_code,
            message,
            run_id=self.run_id,
            retriable=retriable,
            manifest_path=self.manifest_path,
            journal_path=self.journal_path,
            progress=_progress(self.journal),
        )

    def reject_completion(self, gate: dict[str, object], feedback: dict[str, object]) -> None:
        """Record why execution.complete_unit was refused; the Developer reads the feedback on its next action."""
        self.entry["actions"][-1]["completion_gate"] = {"status": "rejected", **gate}
        self.history.append({"action": "rejected", **feedback})
        _write_json_atomic(self.journal_file, self.journal)

    def persist(self, next_index: int, *, cursor: int | None = None) -> None:
        """Write working_state.json so a paused unit resumes at next_index with the same history."""
        self.durable_next_action_index = next_index
        current_cursor = int(getattr(self.llm_client, "cursor", 0)) if cursor is None else cursor
        payload = {
            "version": "developer_working_state_v1",
            "unit_id": self.item_id,
            "history": self.history,
            "seen_observations": sorted(self.seen_observations),
            "mutated_paths": sorted(self.mutated_paths),
            "length_failures": self.length_failures,
            "invalid_action_failures": self.invalid_action_failures,
            "next_action_index": next_index,
            "review_cursor": max(0, current_cursor),
            "refreshed_dependency_ids": sorted(self.refreshed_dependency_ids),
            "visual_profile_action_reserve": self.visual_profile_action_reserve,
            "updated_at": utc_now_iso(),
        }
        _write_json_atomic(self.working_state_file, payload)
        self.entry["working_state_path"] = self.store.relative(self.working_state_file)
        self.entry["paused_sha256_by_path"] = {
            path: _file_hash(self.target_path / path) for path in self.execution_hash_paths
        }


@dataclass(frozen=True)
class _ActionTurn:
    """One Developer action request: the facts its prompt showed (the action policy reuses them) and the reply."""

    max_actions: int
    uiux_available: bool
    app_visual_profile: dict[str, object]
    visual_candidates: list[dict[str, object]]
    call_id: str
    raw: str
    finish_reason: str


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
        self.uiux_provider = (
            UiUxGuidanceProvider(
                skill_root=getattr(settings, "uiux_skill_root", None),
                timeout_seconds=int(getattr(settings, "uiux_skill_timeout_seconds", 30)),
                fake_mode=getattr(settings, "llm_mode", "") == "fake",
            )
        )

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
        session_scope: str = "",
    ) -> DeveloperExecutionResult:
        normalized_scope = re.sub(r"[^a-z0-9_]+", "_", repair_scope.strip().lower()).strip("_")
        normalized_scope = normalized_scope or "implementation"
        act_scope = re.sub(r"[^a-z0-9_]+", "_", session_scope.strip().lower()).strip("_")
        # A scoped act (e.g. runtime_closure) gets its own mode and session dir, never `act_v<n>`.
        mode = f"{normalized_scope}_repair" if repair_feedback else (act_scope or "act")
        session_name = developer_session_name(
            repair_feedback=bool(repair_feedback),
            repair_scope=normalized_scope,
            session_scope=act_scope,
            repair_iteration=repair_iteration,
            session_iteration=session_iteration,
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
        dependency_scope_migrated = False
        scope_reopen_reason = ""
        scope_reopened_item_ids: list[str] = []
        persisted_required_paths_for_migration: set[str] = set()
        manifest = _build_manifest(
            run_id=run_id,
            work_packet=work_packet,
            target_path=target_path,
            mode=mode,
            repair_feedback=repair_feedback,
            repair_iteration=repair_iteration,
            expected_dirty_paths=expected_dirty_paths,
        )
        manifest["llm_call_profiles"] = _developer_llm_call_profiles(self.settings)
        if not manifest_file.exists() and self.uiux_provider is not None and any(
            "ui" in {value.strip().lower() for value in _string_items(item.get("surfaces"))}
            for item in _dict_items(manifest.get("items"))
        ):
            try:
                capability = self.uiux_provider.preflight()
            except HarnessError as exc:
                capability = {
                    "status": "unavailable",
                    "error": exc.error_code,
                    "reason": exc.message,
                }
            if capability.get("status") == "available":
                manifest["uiux_capability"] = {
                    **capability,
                    "optional": True,
                    "max_successful_searches_per_screen": 2,
                    "visual_profile_mode": "enforce",
                    "max_visual_profile_candidates_per_app": 2,
                }
            else:
                manifest["uiux_capability"] = {
                    **capability,
                    "optional": False,
                    "visual_profile_mode": "enforce",
                    "max_visual_profile_candidates_per_app": 2,
                }
        if manifest_file.exists():
            existing = _read_json(manifest_file, run_id=run_id)
            if existing.get("input_hash") != manifest.get("input_hash"):
                compatible_manifest = None
                persisted_paths: set[str] = set()
                context_upgrade = _merge_context_only_manifest(existing, manifest)
                runtime_authority_upgrade = _merge_runtime_authority_manifest(existing, manifest)
                entrypoint_scope_upgrade = _merge_database_entrypoint_scope_manifest(
                    existing,
                    manifest,
                )
                dependency_scope_upgrade = _merge_dependency_manifest_scope_manifest(
                    existing,
                    manifest,
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
                elif runtime_authority_upgrade is not None:
                    existing = runtime_authority_upgrade
                    context_contract_migrated = True
                elif entrypoint_scope_upgrade is not None:
                    existing, scope_reopened_item_ids = entrypoint_scope_upgrade
                    entrypoint_scope_migrated = True
                    scope_reopen_reason = "database_entrypoint_scope"
                elif dependency_scope_upgrade is not None:
                    existing, scope_reopened_item_ids = dependency_scope_upgrade
                    dependency_scope_migrated = True
                    scope_reopen_reason = "package_dependency_scope"
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
            persisted_capability = _dict_value(manifest.get("uiux_capability"))
            if (
                self.uiux_provider is not None
                and persisted_capability.get("visual_profile_mode") == "enforce"
                and persisted_capability.get("status") != "available"
            ):
                try:
                    refreshed_capability = self.uiux_provider.preflight()
                except HarnessError:
                    refreshed_capability = {}
                if refreshed_capability.get("status") == "available":
                    manifest["uiux_capability"] = {
                        **refreshed_capability,
                        "optional": True,
                        "max_successful_searches_per_screen": 2,
                        "visual_profile_mode": "enforce",
                        "max_visual_profile_candidates_per_app": 2,
                    }
                    _write_json_atomic(manifest_file, manifest)
        else:
            _write_json_atomic(manifest_file, manifest)

        journal = (
            _read_json(journal_file, run_id=run_id)
            if journal_file.exists()
            else _new_journal(run_id=run_id, manifest=manifest, mode=mode, repair_iteration=repair_iteration)
        )
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
                else "package_dependency_scope_migrated"
                if dependency_scope_migrated
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
            _reopen_scope_expanded_items(
                target_path=target_path,
                session_dir=session_dir,
                manifest=manifest,
                journal=journal,
                item_ids=scope_reopened_item_ids,
                reason=scope_reopen_reason or "database_entrypoint_scope",
            )
            migrations = journal.setdefault("migrations", [])
            scope_migration_code = (
                "package_dependency_scope_migrated"
                if (scope_reopen_reason or "") == "package_dependency_scope"
                else "database_entrypoint_scope_migrated"
            )
            if not any(
                isinstance(row, dict) and row.get("code") == scope_migration_code
                for row in migrations
            ):
                migrations.append(
                    {
                        "code": scope_migration_code,
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
                    capability = _dict_value(manifest.get("uiux_capability"))
                    if visual_profile_required(item, capability) and capability.get("status") != "available":
                        raise DeveloperExecutionError(
                            "developer_visual_profile_unavailable",
                            str(capability.get("reason") or "Required UI/UX visual-profile skill is unavailable."),
                            run_id=run_id,
                            retriable=True,
                            manifest_path=manifest_path,
                            journal_path=journal_path,
                            session_path=session_path,
                            progress=_progress(journal),
                        )
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
            min(30000, (int(getattr(self.settings, "llm_structured_max_tokens", 8192)) - 1024) * 2),
        )
        item_paths = _item_paths(item)
        execution_hash_paths = _item_execution_hash_paths(item)
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
            completion_gate = _dict_value(metadata.get("completion_gate"))
            if completion_gate.get("status") == "rejected":
                history.append(
                    {
                        "action": "rejected",
                        "error": str(completion_gate.get("reason") or "completion_rejected"),
                        "instruction": (
                            "The previous completion was rejected by Harness. Address the structured "
                            "completion_gate evidence before completing this unit again."
                        ),
                        "completion_gate": completion_gate,
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
            for payload_key in _mutation_payload_keys(action_name):
                payload = parsed_arguments.get(payload_key)
                expected_payload_hash = str(arguments.get(f"{payload_key}_sha256") or "")
                if (
                    not isinstance(payload, str)
                    or hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_payload_hash
                ):
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
                    uiux_provider=self.uiux_provider,
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
            path: _file_hash(target_path / path) for path in execution_hash_paths
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
        # Fake Polish is intentionally a no-op: overwriting real UI source with synthetic
        # declarations would make the later regression check test the fixture, not the workflow.
        generated_files = [] if work_packet.get("mode") == "ui_polish" else fake_developer_files(work_packet)
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
                no_change_required=bool(not mutated_paths),
                verification_evidence=fake_evidence,
            )
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            if on_progress:
                on_progress(_progress(journal), manifest_path, journal_path)

    def _open_unit_loop(
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
    ) -> _UnitLoop | None:
        """Begin or resume one unit; None when a resumed unit completed without another action."""
        item_id = str(item.get("id") or "")
        entry = _journal_item(journal, item_id)
        # D11 (b): new files this item already created under its new_file_policy stay writable.
        item_paths = list(dict.fromkeys([*_item_paths(item), *_journal_scope_extension_paths(journal, item_id)]))
        execution_hash_paths = _item_execution_hash_paths(item)
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
        if resuming_step_review and self._revalidate_signature_rejected_completion(
            run_id=run_id,
            work_packet=work_packet,
            target_path=target_path,
            session_dir=session_dir,
            item=item,
            entry=entry,
            history=history,
            mutated_paths=mutated_paths,
        ):
            _supersede_pending_step_review(
                session_dir=session_dir,
                journal=journal,
                reason="completion_signature_revalidated",
            )
            journal["updated_at"] = utc_now_iso()
            _write_json_atomic(journal_file, journal)
            if on_progress:
                on_progress(_progress(journal), manifest_path, journal_path)
            return None
        length_failures = int(working_state.get("length_failures") or 0)
        invalid_action_failures = int(working_state.get("invalid_action_failures") or 0)
        next_action_index = max(1, int(working_state.get("next_action_index") or 1))
        durable_next_action_index = next_action_index
        review_cursor = max(0, int(working_state.get("review_cursor") or 0))
        configured_max_actions = int(getattr(self.settings, "developer_max_actions_per_item", 24))
        verification_repair_action_reserve = int(
            getattr(self.settings, "developer_verification_repair_action_reserve", 6)
        )
        timeout_action_reserve = 2 * max(1, len(_string_items(item.get("required_stage_ids")))) + 2
        maximum_action_limit = (
            configured_max_actions
            + timeout_action_reserve
            + verification_repair_action_reserve
        )
        visual_profile_file = session_dir.parent / "ui_visual_profiles_v1.json"
        visual_app_scope_id = app_visual_scope_id(item)
        visual_required = visual_profile_required(item, manifest.get("uiux_capability"))
        try:
            visual_store = read_visual_profiles(visual_profile_file)
        except HarnessError as exc:
            raise DeveloperExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=True,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(journal),
            ) from exc
        app_visual_profile = _dict_value(_dict_value(visual_store.get("profiles")).get(visual_app_scope_id))
        visual_profile_action_reserve = _pinned_visual_profile_action_reserve(
            working_state=working_state,
            history=history,
            visual_required=visual_required,
            app_visual_profile=app_visual_profile,
        )
        configured_max_actions += visual_profile_action_reserve
        maximum_action_limit += visual_profile_action_reserve
        max_retries = int(getattr(self.settings, "developer_max_action_retries", 2))
        fallback_max_tokens = int(getattr(self.settings, "llm_structured_max_tokens", 8192))
        pinned_profiles = _dict_value(manifest.get("llm_call_profiles"))
        normal_options = _dict_value(pinned_profiles.get("developer_action")) or {
            "max_tokens": fallback_max_tokens
        }
        repair_options = _dict_value(pinned_profiles.get("developer_repair")) or {
            "max_tokens": fallback_max_tokens
        }
        context_window_tokens = int(getattr(self.settings, "llm_context_window_tokens", 128000))
        working_memory_evidence_chars = int(
            getattr(self.settings, "developer_working_memory_evidence_chars", 96000)
        )
        profile_max_tokens = max(
            int(normal_options.get("max_tokens") or fallback_max_tokens),
            int(repair_options.get("max_tokens") or fallback_max_tokens),
        )
        max_write_chars = max(2000, min(30000, (profile_max_tokens - 1024) * 2))
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
        return _UnitLoop(
            store=self.store,
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
            item_id=item_id,
            entry=entry,
            item_paths=item_paths,
            execution_hash_paths=execution_hash_paths,
            context_paths=context_paths,
            repair_mode=repair_mode,
            working_state_file=working_state_file,
            llm_client=llm_client,
            configured_max_actions=configured_max_actions,
            timeout_action_reserve=timeout_action_reserve,
            verification_repair_action_reserve=verification_repair_action_reserve,
            visual_profile_action_reserve=visual_profile_action_reserve,
            maximum_action_limit=maximum_action_limit,
            max_retries=max_retries,
            fallback_max_tokens=fallback_max_tokens,
            normal_options=normal_options,
            repair_options=repair_options,
            context_window_tokens=context_window_tokens,
            working_memory_evidence_chars=working_memory_evidence_chars,
            max_write_chars=max_write_chars,
            visual_profile_file=visual_profile_file,
            visual_app_scope_id=visual_app_scope_id,
            visual_required=visual_required,
            history=history,
            seen_observations=seen_observations,
            mutated_paths=mutated_paths,
            refreshed_dependency_ids=refreshed_dependency_ids,
            length_failures=length_failures,
            invalid_action_failures=invalid_action_failures,
            next_action_index=next_action_index,
            durable_next_action_index=durable_next_action_index,
            action_index=next_action_index,
        )

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
        loop = self._open_unit_loop(
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
            initial_history=initial_history,
        )
        if loop is None:
            return
        try:
            if not loop.history:
                seed_context = _codegraph_seed_context(
                    target_path=target_path,
                    item=item,
                    repo_context_provider=self.repo_context_provider,
                )
                if seed_context:
                    loop.history.append(seed_context)
            for action_index in range(loop.next_action_index, loop.maximum_action_limit + 1):
                loop.action_index = action_index
                max_actions = _developer_action_limit(
                    configured_max_actions=loop.configured_max_actions,
                    timeout_action_reserve=loop.timeout_action_reserve,
                    verification_repair_action_reserve=loop.verification_repair_action_reserve,
                    history=loop.history,
                )
                if action_index > max_actions:
                    break
                turn = self._request_action(loop, max_actions)
                action = self._accept_action(loop, turn)
                if action is None:
                    continue
                result = self._action_result(loop, turn, action)
                self._record_action(loop, turn, action, result)
                self._enforce_repeat_limits(loop, action, result)
                if action["action"] not in {"complete", "execution.complete_unit"}:
                    continue
                if self._complete_unit(loop, action):
                    return
            active_repair = _verification_repair_state(loop.history)
            error_code = "developer_repair_budget_exhausted" if active_repair else "developer_action_limit_exceeded"
            message = (
                f"Developer did not finish repair for execution unit {loop.item_id} within {max_actions} tool actions."
                if active_repair
                else f"Developer did not complete execution unit {loop.item_id} within {max_actions} tool actions."
            )
            raise loop.fail(error_code, message, retriable=False)
        except LlmStepReviewRequired as exc:
            self._pause_unit_for_resume(loop, pending_step_review=dict(exc.progress))
            raise loop.fail(exc.error_code, exc.message, retriable=True) from exc
        except DeveloperExecutionError as exc:
            if exc.error_code == "developer_action_limit_exceeded":
                self._pause_unit_at_budget(loop)
            else:
                self._roll_back_unit(loop, journal_status="paused", mark_error_time=True)
            exc.manifest_path = exc.manifest_path or manifest_path
            exc.journal_path = exc.journal_path or journal_path
            exc.progress = _progress(journal)
            raise
        except HarnessError as exc:
            if exc.error_code == "llm_transport_failed":
                self._pause_unit_for_resume(loop)
                raise loop.fail(exc.error_code, exc.message, retriable=True) from exc
            self._roll_back_unit(loop, journal_status="blocked")
            retriable = exc.error_code in {
                "typescript_ast_extractor_unavailable",
                "typescript_ast_extraction_failed",
            } or exc.error_code.startswith("target_repo_codegraph")
            raise loop.fail(exc.error_code, exc.message, retriable=retriable) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self._roll_back_unit(loop, journal_status="paused")
            raise loop.fail(
                "developer_tool_failed",
                f"Developer workspace tool failed for execution unit {loop.item_id}: {exc}",
                retriable=True,
            ) from exc

    def _request_action(self, loop: _UnitLoop, max_actions: int) -> _ActionTurn:
        """Ask the Developer for the next action and record the call in the journal."""
        uiux_available = _developer_uiux_available(
            manifest=loop.manifest,
            journal=loop.journal,
            item=loop.item,
        )
        uiux_guidance = _developer_uiux_guidance(
            journal=loop.journal,
            item=loop.item,
        )
        visual_store = read_visual_profiles(loop.visual_profile_file)
        app_visual_profile = _dict_value(
            _dict_value(visual_store.get("profiles")).get(loop.visual_app_scope_id)
        )
        visual_candidates = _developer_visual_candidates(
            journal=loop.journal,
            app_scope_id=loop.visual_app_scope_id,
        )
        prompt = _action_prompt(
            item=loop.item,
            manifest=loop.manifest,
            history=loop.history,
            target_path=loop.target_path,
            max_write_chars=loop.max_write_chars,
            force_incremental=loop.length_failures > 0,
            action_index=loop.action_index,
            max_actions=max_actions,
            mutated=bool(loop.mutated_paths),
            context_window_tokens=loop.context_window_tokens,
            working_memory_evidence_chars=loop.working_memory_evidence_chars,
            uiux_available=uiux_available,
            uiux_guidance=uiux_guidance,
            visual_profile_required=loop.visual_required,
            app_visual_profile=app_visual_profile,
            visual_profile_candidates=visual_candidates,
            visual_app_scope_id=loop.visual_app_scope_id,
            journal=loop.journal,
        )
        active_options = (
            loop.repair_options
            if loop.repair_mode or bool(_verification_repair_state(loop.history))
            else loop.normal_options
        )
        configured_max_tokens = int(
            active_options.get("max_tokens") or loop.fallback_max_tokens
        )
        max_tokens = _completion_token_budget(
            prompt,
            configured_max_tokens=configured_max_tokens,
            context_window_tokens=loop.context_window_tokens,
        )
        completion = loop.llm_client.complete_with_metadata(
            system=(
                "You are Developer Executor in a controlled SDLC worktree. One response proposes exactly "
                "one JSON action and then stops. The action is not executed until the Harness returns its "
                "tool result in a later turn. Reason only far enough to choose the current action: do not "
                "plan, draft, or evaluate later actions or files. Outcomes owned by a required Verification "
                "stage are not preconditions for the current action; follow the typed contract and let the "
                "Harness stage judge them instead of speculating about toolchain behavior. Never narrate "
                "reasoning, emit or repeat a second action, or claim an action ran without a Harness tool "
                "result. Use tools only within the approved execution unit; do not include analysis, thinking "
                "markers, or markdown."
            ),
            prompt=prompt,
            json_mode=True,
            model=str(active_options.get("model") or "") or None,
            reasoning_effort=str(active_options.get("reasoning_effort") or "") or None,
            max_tokens=max_tokens,
            timeout_seconds=int(active_options.get("timeout_seconds") or 0) or None,
        )
        loop.journal.pop("pending_step_review", None)
        raw = str(getattr(completion, "content", ""))
        call_id = f"{loop.item_id}:a{int(loop.entry.get('attempts') or 1)}:c{loop.action_index}"
        metadata = {
            "call_id": call_id,
            "item_attempt": int(loop.entry.get("attempts") or 1),
            "action_index": loop.action_index,
            "finish_reason": str(getattr(completion, "finish_reason", "") or ""),
            "usage": getattr(completion, "usage", {}) if isinstance(getattr(completion, "usage", {}), dict) else {},
            "max_tokens": max_tokens,
            "request_options": {
                "model": str(active_options.get("model") or ""),
                "reasoning_effort": str(active_options.get("reasoning_effort") or ""),
                "max_tokens": max_tokens,
                "timeout_seconds": int(active_options.get("timeout_seconds") or 0),
            },
            "created_at": utc_now_iso(),
            "replayed": getattr(loop.llm_client, "last_replay_kind", "") == "approved",
        }
        if metadata["replayed"] is not True:
            _write_action_artifacts(
                loop.session_dir,
                loop.item_id,
                int(loop.entry.get("attempts") or 1),
                loop.action_index,
                raw,
                metadata,
            )
        loop.entry.setdefault("actions", []).append(metadata)
        return _ActionTurn(
            max_actions=max_actions,
            uiux_available=uiux_available,
            app_visual_profile=app_visual_profile,
            visual_candidates=visual_candidates,
            call_id=call_id,
            raw=raw,
            finish_reason=metadata["finish_reason"],
        )

    def _accept_action(self, loop: _UnitLoop, turn: _ActionTurn) -> dict[str, object] | None:
        """The parsed action, or None after recording why the reply is rejected (truncated, invalid, legacy)."""
        if turn.finish_reason == "length":
            loop.length_failures += 1
            loop.history.append({"action": "rejected", "error": "finish_reason=length", "instruction": "Use a smaller write or patch."})
            loop.persist(loop.action_index + 1)
            _write_json_atomic(loop.journal_file, loop.journal)
            if loop.length_failures > loop.max_retries:
                raise loop.fail(
                    "developer_action_truncated",
                    f"Developer action for execution unit {loop.item_id} exceeded the output budget after {loop.length_failures} attempts.",
                    retriable=True,
                )
            return None
        try:
            action = _parse_action(turn.raw)
            loop.invalid_action_failures = 0
            if action.get("normalization"):
                loop.entry["actions"][-1]["parse_normalization"] = action["normalization"]
        except HarnessError as exc:
            loop.invalid_action_failures += 1
            loop.history.append(
                {
                    "action": "rejected",
                    "error": exc.message,
                    "instruction": (
                        "Return exactly one JSON action object with no analysis, thinking markers, "
                        "markdown, or additional action."
                    ),
                }
            )
            loop.persist(loop.action_index + 1)
            _write_json_atomic(loop.journal_file, loop.journal)
            if loop.invalid_action_failures > loop.max_retries:
                raise loop.fail(
                    "developer_action_invalid",
                    f"Developer returned invalid tool actions for execution unit {loop.item_id}: {exc.message}",
                    retriable=True,
                ) from exc
            return None
        if action.get("action") not in _UNIT_ACTION_NAMES:
            loop.history.append(
                {
                    "action": "rejected",
                    "error": "developer_unit_legacy_tool_not_allowed",
                    "instruction": "Use one canonical Developer Tool Action v2 tool ID.",
                }
            )
            loop.persist(loop.action_index + 1)
            _write_json_atomic(loop.journal_file, loop.journal)
            return None
        return action

    def _action_result(
        self,
        loop: _UnitLoop,
        turn: _ActionTurn,
        action: dict[str, object],
    ) -> dict[str, object]:
        """Run the action unless the action policy rejects it; either way return its tool result."""
        current_hashes = {
            path: _file_hash(loop.target_path / path) for path in loop.execution_hash_paths
        }
        dependency_statuses = _package_dependency_statuses(
            target_path=loop.target_path,
            item=loop.item,
            run_id=loop.run_id,
        )
        pending_dependency_ids = [
            str(status.get("dependency_id") or "")
            for status in dependency_statuses
            if status.get("satisfied") is not True
        ]
        repair_state = _verification_repair_state(loop.history)
        failed_stage_ids = sorted(repair_state)
        policy_error = _new_test_context_policy_error(
            action=action,
            history=loop.history,
            manifest=loop.manifest,
            target_path=loop.target_path,
            item_paths=loop.item_paths,
        ) or _action_policy_error(
            action=action,
            history=loop.history,
            seen_observations=loop.seen_observations,
            mutated=bool(loop.mutated_paths),
            action_index=loop.action_index,
            max_actions=turn.max_actions,
            pending_stage_ids=sorted(
                set(_string_items(loop.item.get("required_stage_ids")))
                - _current_passed_stages(
                    loop.history,
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
            pending_package_dependency_ids=pending_dependency_ids,
            uiux_available=turn.uiux_available,
            uiux_success_count=_developer_uiux_success_count(journal=loop.journal, item=loop.item),
            visual_profile_required=loop.visual_required,
            visual_profile_committed=bool(turn.app_visual_profile),
            visual_profile_candidate_count=len(turn.visual_candidates),
        )
        if policy_error:
            result = {"ok": False, **policy_error}
        else:
            try:
                result = _execute_action(
                    action=action,
                    run_id=loop.run_id,
                    work_packet=loop.work_packet,
                    repo_root=loop.target_path,
                    item_paths=loop.item_paths,
                    item=loop.item,
                    session_dir=loop.session_dir,
                    max_write_chars=loop.max_write_chars,
                    repo_context_provider=self.repo_context_provider,
                    verification_runner=self.verification_runner,
                    verification_policy=loop.verification_policy,
                    uiux_provider=self.uiux_provider,
                    journal=loop.journal,
                    visual_profile_file=loop.visual_profile_file,
                    visual_app_scope_id=loop.visual_app_scope_id,
                    package_tool_timeout_seconds=int(
                        getattr(self.settings, "developer_package_tool_timeout_seconds", 120)
                    ),
                )
            except HarnessError as exc:
                if exc.error_code in _RECOVERABLE_ACTION_POLICY_ERRORS:
                    repeat_count = 1 + sum(
                        1
                        for row in loop.history
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
                        "approved_paths": loop.item_paths,
                        "policy_repeat_count": repeat_count,
                    }
                else:
                    raise loop.fail(
                        exc.error_code,
                        exc.message,
                        retriable=(
                            exc.error_code == "llm_transport_failed"
                            or exc.error_code.startswith("target_repo_codegraph")
                        ),
                    ) from exc
        return result

    def _record_action(
        self,
        loop: _UnitLoop,
        turn: _ActionTurn,
        action: dict[str, object],
        result: dict[str, object],
    ) -> None:
        """Add the action and its result to the history and journal, then persist the working state."""
        if (
            action["action"] == "verification.run_stage"
            and result.get("status") == "failed"
            and result.get("failure_kind") == "implementation"
        ):
            diagnostic_fingerprint = _verification_diagnostic_fingerprint(result)
            result["diagnostic_fingerprint"] = diagnostic_fingerprint
            result["diagnostic_repeat_count"] = 1 + _diagnostic_repeat_count(
                loop.history,
                fingerprint=diagnostic_fingerprint,
            )
        if action["action"] == "uiux.search_guidance":
            if result.get("ok") is True:
                _remember_developer_uiux_guidance(
                    journal=loop.journal,
                    item=loop.item,
                    result=result,
                )
            elif str(result.get("error") or "") in {
                "developer_uiux_unavailable",
                "designer_uiux_unavailable",
                "designer_uiux_failed",
            }:
                loop.journal["uiux_disabled"] = {
                    "reason": str(result.get("error") or "developer_uiux_unavailable"),
                    "disabled_at": utc_now_iso(),
                }
        if action["action"] == "uiux.search_visual_profile" and result.get("candidate"):
            _remember_developer_visual_candidate(
                journal=loop.journal,
                app_scope_id=loop.visual_app_scope_id,
                candidate=_dict_value(result.get("candidate")),
            )
            current_candidates = _developer_visual_candidates(
                journal=loop.journal,
                app_scope_id=loop.visual_app_scope_id,
            )
            if len(current_candidates) >= 2 and not any(
                row.get("valid") is True for row in current_candidates
            ):
                invalid_attempts = loop.journal.setdefault("uiux_visual_candidate_failures", [])
                if isinstance(invalid_attempts, list):
                    invalid_attempts.append(
                        {
                            "app_scope_id": loop.visual_app_scope_id,
                            "candidate_ids": [
                                str(row.get("candidate_id") or "") for row in current_candidates
                            ],
                            "failed_at": utc_now_iso(),
                        }
                    )
                candidate_cache = _dict_value(loop.journal.get("uiux_visual_candidates_by_app"))
                candidate_cache[loop.visual_app_scope_id] = []
                loop.journal["uiux_visual_candidates_by_app"] = candidate_cache
                loop.persist(loop.action_index + 1)
                _write_json_atomic(loop.journal_file, loop.journal)
                raise loop.fail(
                    "developer_visual_profile_invalid",
                    "Two UI/UX visual-profile candidates failed deterministic validation.",
                    retriable=True,
                )
        if str(result.get("error") or "") == "developer_visual_profile_unavailable":
            loop.persist(loop.action_index + 1)
            _write_json_atomic(loop.journal_file, loop.journal)
            raise loop.fail(
                "developer_visual_profile_unavailable",
                str(result.get("reason") or "Required UI/UX visual-profile skill is unavailable."),
                retriable=True,
            )
        timeout_diagnostic = _timeout_diagnostic_state(loop.history)
        if (
            result.get("ok") is True
            and _is_timeout_diagnostic_instrumentation_action(
                action,
                timeout_diagnostic=timeout_diagnostic,
            )
        ):
            result["timeout_diagnostic_instrumentation"] = True
        loop.history.append(
            {
                "action": action["action"],
                "arguments": _action_history_arguments(action),
                "result": result,
                "summary": str(action.get("summary") or "").strip()[:500],
                "tool_call": {
                    "version": "role_tool_call_v1",
                    "call_id": turn.call_id,
                    "tool_id": action["action"],
                    "arguments": _action_history_arguments(action),
                },
                "tool_result": {
                    "version": "role_tool_result_v1",
                    "call_id": turn.call_id,
                    "tool_id": action["action"],
                    "result": _clip_json_value(result, 4000),
                },
            }
        )
        loop.entry["actions"][-1]["tool_action"] = action["action"]
        loop.entry["actions"][-1]["action_summary"] = loop.history[-1]["summary"]
        loop.entry["actions"][-1]["tool_call"] = loop.history[-1]["tool_call"]
        loop.entry["actions"][-1]["tool_result"] = _clip_json_value(result, 4000)
        if (
            action["action"] == "verification.run_stage"
            and result.get("ok") is not True
            and result.get("failure_kind") == "environment"
        ):
            raise loop.fail(
                "developer_verification_environment_failed",
                str(result.get("reason") or "Developer unit verification environment failed."),
                retriable=True,
            )
        if action["action"] in _OBSERVATION_ACTIONS and result.get("ok"):
            loop.seen_observations.update(_observation_signatures(action, result=result))
        if action["action"] in _MUTATION_ACTIONS and result.get("ok"):
            mutated_path = str(result.get("path") or "").replace("\\", "/")
            if mutated_path:
                loop.mutated_paths.add(mutated_path)
            loop.seen_observations.clear()
        inspected_paths = {
            path for path in loop.context_paths if _has_successful_target_inspection(loop.history, item_path=path)
        }
        handled_paths = loop.mutated_paths & set(loop.context_paths)
        loop.entry["handled_paths"] = sorted(handled_paths)
        loop.entry["inspected_paths"] = sorted(inspected_paths)
        loop.entry["unresolved_paths"] = []
        if result.get("error") == "no_effect_mutation":
            loop.entry["no_effect_actions"] = int(loop.entry.get("no_effect_actions") or 0) + 1
        loop.persist(loop.action_index + 1)
        loop.journal["updated_at"] = utc_now_iso()
        _write_json_atomic(loop.journal_file, loop.journal)

    def _enforce_repeat_limits(self, loop: _UnitLoop, action: dict[str, object], result: dict[str, object]) -> None:
        """Stop the unit when the Developer keeps repeating an out-of-scope, no-effect or diagnostic action."""
        if (
            result.get("error") in _RECOVERABLE_ACTION_POLICY_ERRORS
            and int(result.get("policy_repeat_count") or 0) > loop.max_retries
        ):
            raise loop.fail(
                str(result.get("error") or "developer_act_out_of_scope"),
                str(result.get("reason") or "Developer action remained outside the approved unit scope."),
                retriable=False,
            )
        if (
            result.get("error") in {"duplicate_no_effect_mutation", "verification_repair_ready"}
            and int(result.get("repeat_count") or 0) >= _MAX_IDENTICAL_NO_EFFECT_MUTATIONS
        ):
            raise loop.fail(
                "developer_repair_stalled",
                (
                    "Developer repeated the same no-effect mutation "
                    f"{_MAX_IDENTICAL_NO_EFFECT_MUTATIONS} times for "
                    f"{_dict_value(action.get('arguments')).get('path') or '<unknown>'}."
                ),
                retriable=False,
            )
        if (
            result.get("error") == "duplicate_observation"
            and int(result.get("repeat_count") or 0) >= _MAX_IDENTICAL_DIAGNOSTIC_RESULTS
        ):
            raise loop.fail(
                "developer_repair_stalled",
                (
                    "Developer repeated the same cached observation "
                    f"{_MAX_IDENTICAL_DIAGNOSTIC_RESULTS} times instead of advancing the repair."
                ),
                retriable=False,
            )
        if (
            action["action"] == "verification.run_stage"
            and int(result.get("diagnostic_repeat_count") or 0)
            >= _MAX_IDENTICAL_DIAGNOSTIC_RESULTS
        ):
            raise loop.fail(
                "developer_repair_stalled",
                (
                    f"Developer verification stage {result.get('stage_id') or '<unknown>'} returned the "
                    f"same diagnostics {_MAX_IDENTICAL_DIAGNOSTIC_RESULTS} times."
                ),
                retriable=False,
            )

    def _complete_unit(self, loop: _UnitLoop, action: dict[str, object]) -> bool:
        """Gate execution.complete_unit: True when the unit completed, False after recording the rejection."""
        inspected_paths = {
            path for path in loop.context_paths if _has_successful_target_inspection(loop.history, item_path=path)
        }
        diagnostic_completion_error = _timeout_diagnostic_completion_error(
            target_path=loop.target_path,
            item_paths=loop.item_paths,
        )
        if diagnostic_completion_error:
            loop.reject_completion(diagnostic_completion_error, diagnostic_completion_error)
            return False
        complete_arguments = _dict_value(action.get("arguments"))
        no_change_required = complete_arguments.get("no_change_required") is True
        declared_unchanged: set[str] = set()
        declared_dispositions: dict[str, dict[str, str]] = {}
        invalid_unchanged: list[str] = []
        for row in _dict_items(complete_arguments.get("unchanged_paths")):
            path = str(row.get("path") or "").replace("\\", "/")
            reason = str(row.get("reason") or "").strip()
            if path not in loop.context_paths or path not in inspected_paths or not reason:
                invalid_unchanged.append(path or "<missing>")
                continue
            declared_unchanged.add(path)
            declared_dispositions[path] = {"status": "verified_unchanged", "reason": reason}
        for row in _dict_items(complete_arguments.get("path_dispositions")):
            path = str(row.get("path") or "").replace("\\", "/")
            status = str(row.get("status") or "").strip()
            reason = str(row.get("reason") or "").strip()
            valid = (
                path in loop.item_paths
                and status in {"mutated", "verified_unchanged", "not_required"}
                and bool(reason)
                and (
                    (status == "mutated" and path in loop.mutated_paths)
                    or (status != "mutated" and path in inspected_paths)
                )
            )
            if not valid:
                invalid_unchanged.append(path or "<missing>")
                continue
            declared_dispositions[path] = {"status": status, "reason": reason}
            if status != "mutated":
                declared_unchanged.add(path)
        handled_paths = (loop.mutated_paths & set(loop.context_paths)) | declared_unchanged
        loop.entry["handled_paths"] = sorted(handled_paths)
        loop.entry["unchanged_paths"] = sorted(declared_unchanged)
        covered_paths = set(loop.mutated_paths) | set(declared_dispositions)
        unresolved_paths = set(loop.item_paths) - covered_paths
        loop.entry["unresolved_paths"] = (
            sorted(unresolved_paths)
        )
        loop.entry["path_dispositions"] = {
            path: declared_dispositions.get(
                path,
                {"status": "mutated", "reason": "Successful bounded mutation."},
            )
            for path in sorted(covered_paths)
        }
        existing_paths = {path for path in loop.item_paths if (loop.target_path / path).is_file()}
        verified_unchanged_completion = (
            bool(loop.item_paths)
            and set(loop.item_paths) <= existing_paths
            and not invalid_unchanged
            and not unresolved_paths
            and all(
                declared_dispositions.get(path, {}).get("status") == "verified_unchanged"
                for path in loop.item_paths
            )
        )
        no_change_allowed = verified_unchanged_completion or (
            loop.repair_mode
            and no_change_required
            and bool(declared_unchanged)
            and not invalid_unchanged
        )
        required_test_mutations = set(
            _required_test_mutation_paths(loop.item, repair_mode=loop.repair_mode)
        ) - set(loop.mutated_paths)
        if required_test_mutations:
            loop.reject_completion(
                {"reason": "test_intent_evidence_missing", "missing_test_mutations": sorted(required_test_mutations)},
                {
                    "error": (
                        "complete requires each required_test_mutation_path to be mutated in this unit. "
                        "A passing pre-existing test file does not prove a directly affected intent. "
                        f"missing_test_mutations={sorted(required_test_mutations)}."
                    ),
                },
            )
            return False
        completed_unit = (
            bool(loop.mutated_paths)
            and loop.mutated_paths <= existing_paths
            and (
                not unresolved_paths
            )
        )
        if not completed_unit and not no_change_allowed:
            loop.reject_completion(
                {"reason": "path_disposition_or_mutation_missing", "unresolved_paths": sorted(unresolved_paths)},
                {
                    "error": (
                        "complete requires at least one successful mutation and a disposition for every unit "
                        "path. Repair feedback may instead use "
                        "no_change_required=true with unchanged_paths=[{path, reason}] after fresh inspection. "
                        f"invalid_paths={invalid_unchanged}; unresolved_paths={sorted(unresolved_paths)}."
                    ),
                },
            )
            return False
        required_stages = set(_string_items(loop.item.get("required_stage_ids")))
        current_hashes = {
            path: _file_hash(loop.target_path / path) for path in loop.execution_hash_paths
        }
        missing_stages = sorted(
            required_stages - _current_passed_stages(loop.history, current_hashes=current_hashes)
        )
        if missing_stages:
            loop.reject_completion(
                {"reason": "required_verification_missing", "missing_stage_ids": missing_stages},
                {
                    "error": "developer_unit_verification_missing",
                    "instruction": f"Run required verification stages on current hashes: {missing_stages}",
                },
            )
            return False
        runtime_config_issues = runtime_config_source_issues(
            repo_root=loop.target_path,
            changes=_dict_items(loop.item.get("runtime_config_changes")),
        )
        if runtime_config_issues:
            loop.reject_completion(
                {"reason": "runtime_config_contract_mismatch", "findings": _clip_json_value(runtime_config_issues, 4000)},
                {
                    "error": "developer_runtime_config_contract_mismatch",
                    "instruction": (
                        "Read the approved runtime configuration from mandatory_obligations, "
                        "then make the declared source read the exact environment variable "
                        "and use its approved default before completing this unit."
                    ),
                    "findings": runtime_config_issues,
                },
            )
            return False
        signature_evidence = _unit_signature_evidence(
            run_id=loop.run_id,
            target_path=loop.target_path,
            work_packet=loop.work_packet,
            item=loop.item,
        )
        if signature_evidence.get("status") == "failed":
            findings = _dict_items(signature_evidence.get("findings"))
            loop.reject_completion(
                {"reason": "approved_signature_mismatch", "findings": _clip_json_value(findings, 4000)},
                {
                    "error": "developer_unit_signature_mismatch",
                    "instruction": "Repair the approved signatures before completing this unit.",
                    "findings": findings,
                },
            )
            return False
        codegraph_evidence = _codegraph_affected_test_evidence(
            target_path=loop.target_path,
            mutated_paths=loop.mutated_paths,
            repo_context_provider=self.repo_context_provider,
        )
        loop.entry["signature_evidence"] = signature_evidence
        loop.entry["codegraph_evidence"] = codegraph_evidence
        loop.entry["actions"][-1]["completion_gate"] = {"status": "passed"}
        self._complete_item(
            item=loop.item,
            entry=loop.entry,
            target_path=loop.target_path,
            session_dir=loop.session_dir,
            summary=str(action.get("arguments", {}).get("summary") or ""),
            mutated_paths=loop.mutated_paths,
            no_change_required=no_change_allowed,
            verification_evidence=_stage_evidence(loop.history),
            codegraph_evidence=codegraph_evidence,
        )
        loop.journal["updated_at"] = utc_now_iso()
        _write_json_atomic(loop.journal_file, loop.journal)
        if loop.on_progress:
            loop.on_progress(_progress(loop.journal), loop.manifest_path, loop.journal_path)
        return True

    def _pause_unit_for_resume(
        self,
        loop: _UnitLoop,
        *,
        pending_step_review: dict[str, object] | None = None,
    ) -> None:
        """Pause at the action in progress, keeping its history: a step review or a transport retry resumes it."""
        loop.persist(loop.action_index, cursor=max(0, int(getattr(loop.llm_client, "cursor", 1)) - 1))
        journal = loop.journal
        journal["last_failed_unit_id"] = loop.item_id
        if pending_step_review is None:
            journal.pop("pending_step_review", None)
        else:
            journal["pending_step_review"] = pending_step_review
        loop.entry["status"] = "review_required"
        loop.entry["last_error_at"] = utc_now_iso()
        journal["status"] = "paused"
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(loop.journal_file, journal)

    def _pause_unit_at_budget(self, loop: _UnitLoop) -> None:
        """Pause a unit that spent its action budget: a raised budget resumes it at its next action."""
        loop.persist(loop.durable_next_action_index)
        journal = loop.journal
        journal.pop("pending_step_review", None)
        journal["last_failed_unit_id"] = loop.item_id
        loop.entry["status"] = "review_required"
        loop.entry["last_error_at"] = utc_now_iso()
        loop.entry["budget_resume"] = {
            "version": "developer_budget_resume_v1",
            "next_action_index": loop.durable_next_action_index,
            "configured_max_actions": loop.configured_max_actions,
            "paused_at": utc_now_iso(),
        }
        journal["status"] = "paused"
        journal["updated_at"] = utc_now_iso()
        _write_json_atomic(loop.journal_file, journal)

    def _roll_back_unit(self, loop: _UnitLoop, *, journal_status: str, mark_error_time: bool = False) -> None:
        """Restore the unit's files from its snapshot; its next attempt starts over."""
        self._rollback_item(target_path=loop.target_path, session_dir=loop.session_dir, item=loop.item, entry=loop.entry)
        loop.journal["last_failed_unit_id"] = loop.item_id
        loop.entry["status"] = "pending"
        if mark_error_time:
            loop.entry["last_error_at"] = utc_now_iso()
        loop.journal["status"] = journal_status
        loop.journal["updated_at"] = utc_now_iso()
        _write_json_atomic(loop.journal_file, loop.journal)

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
        codegraph_evidence: dict[str, object] | None = None,
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
                "codegraph_evidence": codegraph_evidence or {},
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
                    "codegraph_evidence": codegraph_evidence or {},
                    "signature_evidence": entry.get("signature_evidence", {}),
                    "summary": summary,
                    "completed_at": entry.get("completed_at"),
                },
            )

    def _revalidate_signature_rejected_completion(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        session_dir: Path,
        item: dict[str, object],
        entry: dict[str, object],
        history: list[dict[str, object]],
        mutated_paths: set[str],
    ) -> bool:
        """Replay only a stale signature gate before asking the LLM to repair code."""

        actions = _dict_items(entry.get("actions"))
        if not actions:
            return False
        completion = actions[-1]
        gate = _dict_value(completion.get("completion_gate"))
        if (
            completion.get("tool_action") != "execution.complete_unit"
            or gate.get("status") != "rejected"
            or gate.get("reason") != "approved_signature_mismatch"
        ):
            return False
        current_hashes = {
            path: _file_hash(target_path / path) for path in _item_execution_hash_paths(item)
        }
        required_stages = set(_string_items(item.get("required_stage_ids")))
        if required_stages - _current_passed_stages(history, current_hashes=current_hashes):
            return False
        signature_evidence = _unit_signature_evidence(
            run_id=run_id,
            target_path=target_path,
            work_packet=work_packet,
            item=item,
        )
        if signature_evidence.get("status") == "failed":
            return False
        completion_arguments = _dict_value(
            _dict_value(completion.get("tool_call")).get("arguments")
        )
        codegraph_evidence = _codegraph_affected_test_evidence(
            target_path=target_path,
            mutated_paths=mutated_paths,
            repo_context_provider=self.repo_context_provider,
        )
        entry["signature_evidence"] = signature_evidence
        entry["codegraph_evidence"] = codegraph_evidence
        completion["completion_gate"] = {
            "status": "passed",
            "revalidated": True,
            "revalidated_from": "approved_signature_mismatch",
            "revalidated_at": utc_now_iso(),
        }
        self._complete_item(
            item=item,
            entry=entry,
            target_path=target_path,
            session_dir=session_dir,
            summary=str(completion_arguments.get("summary") or ""),
            mutated_paths=mutated_paths,
            no_change_required=not mutated_paths,
            verification_evidence=_stage_evidence(history),
            codegraph_evidence=codegraph_evidence,
        )
        return True

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
            current_hashes = {
                path: _file_hash(target_path / path) for path in _item_execution_hash_paths(item)
            }
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
        allowed.update(str(row.get("path") or "") for row in _dict_items(journal.get("scope_extensions")))
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
        "developer_action_limit_exceeded",
        "developer_repair_budget_exhausted",
        "developer_repair_stalled",
        "developer_visual_profile_unavailable",
        "developer_visual_profile_invalid",
        "developer_visual_profile_required",
    }:
        return False
    execution = _dict_value(state.get("developer_execution"))
    mode = str(execution.get("mode") or "")
    phase = str(state.get("current_phase") or "")
    artifacts = _dict_value(state.get("latest_artifacts"))
    if mode == "runtime_closure_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "runtime_closure_repair_feedback",
                "runtime_closure_repair_execution_manifest",
                "runtime_closure_repair_execution_journal",
            )
        )
    if mode == "runtime_closure" or phase == "runtime_closure":
        return all(
            bool(artifacts.get(key))
            for key in (
                "runtime_closure_work_packet",
                "runtime_closure_execution_manifest",
                "runtime_closure_execution_journal",
            )
        )
    if mode == "browser_readiness_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "browser_readiness_repair_feedback",
                "browser_readiness_repair_execution_manifest",
                "browser_readiness_repair_execution_journal",
            )
        )
    if mode == "browser_readiness":
        return all(
            bool(artifacts.get(key))
            for key in (
                "browser_readiness_work_packet",
                "browser_readiness_execution_manifest",
                "browser_readiness_execution_journal",
            )
        )
    if mode == "ui_polish_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "ui_polish_repair_feedback",
                "ui_polish_repair_execution_manifest",
                "ui_polish_repair_execution_journal",
            )
        )
    if mode == "ui_polish":
        return all(
            bool(artifacts.get(key))
            for key in (
                "ui_polish_work_packet",
                "ui_polish_execution_manifest",
                "ui_polish_execution_journal",
            )
        )
    if mode == "browser_qa_repair":
        return all(
            bool(artifacts.get(key))
            for key in (
                "browser_qa_repair_feedback",
                "browser_qa_repair_execution_manifest",
                "browser_qa_repair_execution_journal",
            )
        )
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
        "runtime_closure": ("runtime_closure_work_packet",),
        "runtime_closure_repair": (
            "runtime_closure_work_packet",
            "runtime_closure_repair_feedback",
            "runtime_closure_repair_execution_manifest",
            "runtime_closure_repair_execution_journal",
        ),
        "browser_readiness": ("browser_readiness_work_packet",),
        "browser_readiness_repair": (
            "browser_readiness_work_packet",
            "browser_readiness_repair_feedback",
            "browser_readiness_repair_execution_manifest",
            "browser_readiness_repair_execution_journal",
        ),
        "ui_polish": ("ui_polish_work_packet",),
        "ui_polish_repair": (
            "ui_polish_work_packet",
            "ui_polish_repair_feedback",
            "ui_polish_repair_execution_manifest",
            "ui_polish_repair_execution_journal",
        ),
        "browser_qa_repair": (
            "developer_work_packet",
            "browser_qa_repair_feedback",
            "browser_qa_repair_execution_manifest",
            "browser_qa_repair_execution_journal",
        ),
    }
    required = required_by_mode.get(mode)
    return bool(required) and all(bool(artifacts.get(key)) for key in required)


def developer_session_name(
    *,
    repair_feedback: bool,
    repair_scope: str,
    session_scope: str,
    repair_iteration: int,
    session_iteration: int | None,
) -> str:
    """Session directory name shared by the executor and the workflow's error recovery."""

    ordinal = repair_iteration if session_iteration is None else max(0, session_iteration)
    if repair_feedback:
        return f"{repair_scope}_repair_v{ordinal}"
    return f"{session_scope or 'act'}_v{max(1, ordinal)}"


def _repair_objective_unit_ids(feedback: dict[str, object] | None) -> set[str]:
    if not feedback:
        return set()
    objective = _dict_value(feedback.get("repair_objective"))
    return {
        unit_id
        for finding in _dict_items(objective.get("contract_findings"))
        for unit_id in _string_items(finding.get("unit_ids"))
        if unit_id
    }


def _repair_dependency_closure(
    *,
    execution_units: list[dict[str, object]],
    seed_unit_ids: set[str],
    run_id: str,
) -> set[str]:
    unit_by_id = {
        str(unit.get("unit_id") or ""): unit
        for unit in execution_units
        if str(unit.get("unit_id") or "")
    }
    unknown = sorted(seed_unit_ids - set(unit_by_id))
    if unknown:
        raise HarnessError(
            "developer_repair_unit_ids_invalid",
            f"Validator repair findings reference unknown execution units: {unknown}",
            run_id=run_id,
        )
    selected = set(seed_unit_ids)
    pending = list(seed_unit_ids)
    while pending:
        unit_id = pending.pop()
        for dependency_id in _string_items(unit_by_id[unit_id].get("depends_on")):
            if dependency_id not in unit_by_id:
                raise HarnessError(
                    "developer_repair_unit_dependency_invalid",
                    f"Execution unit {unit_id} references unknown dependency {dependency_id}.",
                    run_id=run_id,
                )
            if dependency_id not in selected:
                selected.add(dependency_id)
                pending.append(dependency_id)
    return selected


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
    repair_unit_ids = _repair_objective_unit_ids(repair_feedback)
    repair_execution_paths: set[str] | None = None
    selected_repair_unit_ids: set[str] | None = None
    if repair_feedback and execution_units:
        if repair_unit_ids:
            selected_repair_unit_ids = _repair_dependency_closure(
                execution_units=execution_units,
                seed_unit_ids=repair_unit_ids,
                run_id=run_id,
            )
            affected_units = [
                unit
                for unit in execution_units
                if str(unit.get("unit_id") or "") in selected_repair_unit_ids
            ]
            repair_execution_paths = {
                path
                for unit in affected_units
                for path in _string_items(unit.get("write_paths"))
            }
        else:
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
    execution_paths = (
        set(allowed)
        if not repair_feedback
        else repair_execution_paths
        if repair_execution_paths is not None
        else context_selected
    )
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
    file_by_path = {str(item.get("path") or ""): item for item in files}
    items: list[dict[str, object]] = []
    if execution_units:
        # Units of one code entity share write paths; a unit-scoped repair still runs
        # only the cited units plus their dependency closure, never units that merely share a file.
        selected_unit_ids = (
            selected_repair_unit_ids
            if selected_repair_unit_ids is not None
            else {
                str(unit.get("unit_id") or "")
                for unit in execution_units
                if set(_string_items(unit.get("write_paths"))) & execution_paths
            }
        )
        for unit in execution_units:
            if str(unit.get("unit_id") or "") not in selected_unit_ids:
                continue
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
                    "ui_obligations": _dict_items(unit.get("ui_obligations")),
                    "required_stage_ids": _string_items(unit.get("required_stage_ids")),
                    "test_target_paths": _string_items(unit.get("test_target_paths")),
                    "test_intent_mutation_required": (
                        unit.get("test_intent_mutation_required", True)
                        if not repair_feedback
                        or str(unit.get("unit_id") or "") in repair_unit_ids
                        else False
                    ),
                    "package_dependencies": _dict_items(unit.get("package_dependencies")),
                    "runtime_config_changes": _dict_items(
                        unit.get("runtime_config_changes")
                    ),
                    "signature_entry_ids": unit_signature_ids,
                    "input_digest": str(unit.get("input_digest") or ""),
                    "requirements": [
                        {"id": value, "content": canonical.get(value, "")}
                        for value in _string_items(unit.get("requirement_refs"))
                    ],
                    "test_intents": _dict_items(unit.get("test_intents")),
                    "qa_test_intents": _dict_items(unit.get("qa_test_intents")),
                    "repair_closure": {
                        **closure,
                        "context_paths": unit_context,
                    } if repair_feedback else {},
                    # Runtime closure units carry write-time context; browser repair units carry read-only authority.
                    **(
                        {"closure_context": _dict_value(unit.get("closure_context"))}
                        if _dict_value(unit.get("closure_context"))
                        else {}
                    ),
                    **(
                        {"runtime_closure_authority": _dict_value(unit.get("runtime_closure_authority"))}
                        if _dict_value(unit.get("runtime_closure_authority"))
                        else {}
                    ),
                    **(
                        {"new_file_policy": _dict_value(unit.get("new_file_policy"))}
                        if _dict_value(unit.get("new_file_policy"))
                        else {}
                    ),
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
    workspace_scope = (
        [path for path in allowed if path in context_selected or path in execution_paths]
        if repair_feedback and repair_unit_ids
        else allowed
    )
    workspace_allowed_paths = list(
        dict.fromkeys(
            [
                *workspace_scope,
                *(path for item in items for path in _item_dependency_paths(item)),
            ]
        )
    )
    scoped_paths = set(workspace_allowed_paths)
    # A unit-scoped repair narrows writes, but approved outputs of other units remain in the
    # uncommitted worktree: keep them as hash-bound read-only baselines instead of drift.
    out_of_scope_plan_paths = (
        [
            path
            for path in dict.fromkeys(
                [
                    *allowed,
                    *(path for unit in execution_units for path in _item_dependency_paths(unit)),
                ]
            )
            if path not in scoped_paths
        ]
        if selected_repair_unit_ids is not None
        else []
    )
    expected_dirty_files = []
    for raw_path in sorted({*(expected_dirty_paths or []), *out_of_scope_plan_paths}):
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
        if relative in allowed and relative in scoped_paths:
            continue
        expected_dirty_files.append(
            {
                "path": relative,
                "initial_sha256": _file_hash(target),
            }
        )
    input_payload = {
        "approved_plan": work_packet.get("approved_plan"),
        "effective_design_contract": work_packet.get("effective_design_contract"),
        "requirement_contract": work_packet.get("requirement_contract"),
        "approved_runtime_decisions": _dict_items(work_packet.get("approved_runtime_decisions")),
        "developer_generation_context": generation_context,
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "workspace_allowed_paths": workspace_allowed_paths,
        "items": [
            {
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"repair_closure", "diagnostic_paths"}
                },
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
        "version": "developer_execution_manifest_v8",
        "run_id": run_id,
        "mode": mode,
        "repair_iteration": repair_iteration,
        "created_at": utc_now_iso(),
        "base_sha": _dict_value(work_packet.get("run_workspace")).get("base_sha"),
        "input_hash": _json_hash(input_payload),
        "workspace_allowed_paths": workspace_allowed_paths,
        "expected_dirty_files": expected_dirty_files,
        "repair_feedback": repair_feedback or {},
        "developer_generation_context": generation_context,
        "approved_runtime_decisions": _dict_items(work_packet.get("approved_runtime_decisions")),
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
    pending_package_dependency_ids: list[str] | None = None,
    uiux_available: bool = False,
    uiux_success_count: int = 0,
    visual_profile_required: bool = False,
    visual_profile_committed: bool = False,
    visual_profile_candidate_count: int = 0,
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
    pending_package_dependency_ids = pending_package_dependency_ids or []
    action_path = str(_dict_value(action.get("arguments")).get("path") or "").replace("\\", "/")
    if visual_profile_required and not visual_profile_committed:
        if name == "uiux.search_visual_profile":
            if not uiux_available:
                return {
                    "error": "developer_visual_profile_unavailable",
                    "instruction": "Pause this UI unit because its required visual-profile skill is unavailable.",
                }
            if visual_profile_candidate_count >= 2:
                return {
                    "error": "developer_visual_profile_invalid",
                    "instruction": "Two visual-profile candidates were already evaluated; pause instead of inventing another.",
                }
            return None
        if name == "uiux.commit_visual_profile":
            return None
        return {
            "error": "developer_visual_profile_required",
            "instruction": (
                "Before inspecting or mutating this new UI, call uiux.search_visual_profile, then commit one valid "
                "candidate with uiux.commit_visual_profile. The approved Design contract remains authoritative."
            ),
            "candidate_count": visual_profile_candidate_count,
        }
    if name == "uiux.search_guidance":
        if not uiux_available:
            return {
                "error": "developer_uiux_unavailable",
                "instruction": "Continue implementing the approved UI contract without optional UI/UX guidance.",
            }
        if uiux_success_count >= 2:
            return {
                "error": "developer_uiux_budget_exhausted",
                "instruction": "Reuse the two durable guidance results already attached to this screen.",
            }
    if pending_package_dependency_ids:
        dependency_id = str(_dict_value(action.get("arguments")).get("dependency_id") or "")
        if name == "package.ensure_workspace_dependency" and dependency_id in pending_package_dependency_ids:
            return None
        return {
            "error": "package_dependency_required",
            "instruction": (
                "Materialize one approved pending dependency before reading or changing consumer code. "
                "The only allowed action is package.ensure_workspace_dependency."
            ),
            "pending_dependency_ids": pending_package_dependency_ids,
        }
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
    observation_signatures = _observation_signatures(action)
    if (
        name in _OBSERVATION_ACTIONS
        and observation_signatures
        and observation_signatures <= seen_observations
    ):
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
    if name == "repo.read_batch":
        results = _dict_items(_dict_value(result).get("results")) if result else []
        signatures: set[str] = set()
        for index, request in enumerate(_dict_items(arguments.get("requests"))):
            child_result = next(
                (row for row in results if int(row.get("request_index") or 0) == index),
                None,
            )
            if result is not None and _dict_value(child_result).get("ok") is not True:
                continue
            signatures.update(
                _observation_signatures(
                    {"action": "repo.read", "arguments": request},
                    result=child_result,
                )
            )
        return signatures or {_action_signature(action)}
    if name == "repo.search_batch":
        results = _dict_items(_dict_value(result).get("results")) if result else []
        return {
            _action_signature(
                {
                    "action": "repo.search",
                    "arguments": {
                        "query": query,
                        "prefix": arguments.get("prefix"),
                        "limit": arguments.get("limit_per_query"),
                    },
                }
            )
            for index, query in enumerate(_string_items(arguments.get("queries")))
            if result is None
            or any(
                int(row.get("request_index") or 0) == index and row.get("ok") is True
                for row in results
            )
        } or {_action_signature(action)}
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
        if key in {"content", "patch", "old_text", "new_text"} and isinstance(value, str):
            safe[f"{key}_chars"] = len(value)
            safe[f"{key}_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        elif key == "expected_sha256" and isinstance(value, str):
            safe[key] = value[:12]
        else:
            safe[key] = value
    return safe


def _mutation_payload_keys(action_name: str) -> tuple[str, ...]:
    if action_name.endswith("replace_text"):
        return ("old_text", "new_text")
    if action_name.endswith("apply_patch"):
        return ("patch",)
    return ("content",)


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
    payload_hash_keys = [f"{key}_sha256" for key in _mutation_payload_keys(str(action.get("action") or ""))]
    fingerprint = tuple(str(current.get(key) or "") for key in payload_hash_keys)
    path = str(current.get("path") or "").replace("\\", "/")
    if not all(fingerprint) or not path:
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
            and tuple(str(previous.get(key) or "") for key in payload_hash_keys) == fingerprint
        ):
            count += 1
    return count


def _codegraph_seed_context(
    *,
    target_path: Path,
    item: dict[str, object],
    repo_context_provider: CodeGraphContextProvider,
) -> dict[str, object] | None:
    """Prefetch bounded file topology without spending one Developer action."""

    if not (target_path / ".codegraph").exists():
        return None
    freshness = repo_context_provider.sync_if_stale(target_path)
    snapshot = _dict_value(freshness.get("snapshot"))
    seed_rows: list[dict[str, object]] = []
    remaining = 12000
    paths = [path for path in _item_context_paths(item) if (target_path / path).is_file()][:6]
    for path in paths:
        payload = repo_context_provider.query_capability(
            target_path,
            operation="node",
            file=path,
            limit=80,
        )
        evidence = str(payload.get("evidence") or "")
        if remaining <= 0:
            break
        evidence = evidence[:remaining]
        seed_rows.append({"path": path, "evidence": evidence})
        remaining -= len(evidence)
    return {
        "action": "codegraph.seed_context",
        "arguments": {"files": paths},
        "result": {
            "ok": True,
            "operation": "node",
            "snapshot_digest": snapshot.get("artifact_digest"),
            "index_synced": freshness.get("synced") is True,
            "entries": seed_rows,
        },
        "summary": f"Seeded current CodeGraph topology for {len(seed_rows)} unit context file(s).",
    }


def _codegraph_affected_test_evidence(
    *,
    target_path: Path,
    mutated_paths: set[str],
    repo_context_provider: CodeGraphContextProvider,
) -> dict[str, object]:
    """Refresh the graph after mutations and record bounded affected-test evidence."""

    if not mutated_paths or not (target_path / ".codegraph").exists():
        return {}
    freshness = repo_context_provider.sync_if_stale(target_path)
    payload = repo_context_provider.query_capability(
        target_path,
        operation="affected_tests",
        files=sorted(mutated_paths),
        depth=5,
    )
    snapshot = _dict_value(freshness.get("snapshot"))
    return {
        "operation": "affected_tests",
        "files": sorted(mutated_paths),
        "snapshot_digest": snapshot.get("artifact_digest"),
        "index_synced": freshness.get("synced") is True,
        "result": payload,
    }


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


def _developer_llm_call_profiles(settings: object) -> dict[str, dict[str, object]]:
    fallback = int(getattr(settings, "llm_structured_max_tokens", 8192))
    return {
        profile_name: phase4_llm_call_options(
            settings,
            profile_name,
            fallback_max_tokens=fallback,
        )
        for profile_name in ("developer_action", "developer_repair")
    }


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
    uiux_provider: UiUxGuidanceProvider | None = None,
    journal: dict[str, object] | None = None,
    visual_profile_file: Path | None = None,
    visual_app_scope_id: str = "",
    package_tool_timeout_seconds: int = 120,
) -> dict[str, object]:
    raw_name = str(action["action"])
    name = _ACTION_TO_LEGACY.get(raw_name, raw_name)
    args = _dict_value(action.get("arguments"))
    unit_read_paths = set(_item_context_paths(item) or item_paths)

    def scoped_read_path(raw_path: object, *, allow_root: bool = False) -> str | None:
        target = _safe_read_path(
            repo_root,
            str(raw_path or ""),
            run_id=run_id,
            allow_root=allow_root,
        )
        relative = str(target.relative_to(repo_root.resolve())).replace("\\", "/")
        if relative == ".":
            relative = ""
        if relative in unit_read_paths:
            return relative
        if allow_root and any(
            not relative or path == relative or path.startswith(relative.rstrip("/") + "/")
            for path in unit_read_paths
        ):
            return relative
        return None

    def read_scope_error(raw_path: object) -> dict[str, object]:
        return {
            "ok": False,
            "error": "developer_unit_read_out_of_scope",
            "path": str(raw_path or ""),
            "approved_read_context_paths": sorted(unit_read_paths),
            "instruction": (
                "Inspect only execution_unit_contract.read_context_paths. "
                "A later dependency unit owns evidence outside this unit."
            ),
        }

    if raw_name == "uiux.search_visual_profile":
        if "ui" not in {value.strip().lower() for value in _string_items(item.get("surfaces"))}:
            return {"ok": False, "error": "developer_uiux_not_ui_unit"}
        if uiux_provider is None:
            return {"ok": False, "error": "developer_visual_profile_unavailable"}
        query = str(args.get("query") or "").strip()
        try:
            source = uiux_provider.design_system(args)
            capability = uiux_provider.preflight()
            design_refs = list(
                dict.fromkeys(
                    [
                        *_string_items(item.get("design_node_ids")),
                        *[
                            str(binding.get("design_node_id") or "")
                            for binding in _dict_items(item.get("surface_contracts"))
                            if str(binding.get("design_node_id") or "")
                        ],
                    ]
                )
            )
            candidate = normalize_visual_candidate(
                source,
                app_scope_id=visual_app_scope_id or app_visual_scope_id(item),
                skill_fingerprint=str(capability.get("fingerprint") or ""),
                design_refs=design_refs,
            )
        except HarnessError as exc:
            result: dict[str, object] = {"ok": False, "error": exc.error_code, "reason": exc.message}
            if exc.error_code == "developer_visual_profile_invalid":
                attempt_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12].upper()
                result["candidate"] = {
                    "candidate_id": f"VP-INVALID-{attempt_digest}",
                    "query": query,
                    "valid": False,
                    "reason": exc.message,
                }
            return result
        return {
            "ok": True,
            "purpose": "app_level_presentation_profile_candidate",
            "design_authority": False,
            "candidate": {**candidate, "valid": True},
        }
    if raw_name == "uiux.commit_visual_profile":
        candidate_id = str(args.get("candidate_id") or "").strip()
        rationale = str(args.get("rationale") or "").strip()
        candidates = _developer_visual_candidates(
            journal=journal or {},
            app_scope_id=visual_app_scope_id or app_visual_scope_id(item),
        )
        candidate = next(
            (
                row
                for row in candidates
                if str(row.get("candidate_id") or "") == candidate_id and row.get("valid") is True
            ),
            None,
        )
        if candidate is None or not rationale:
            return {
                "ok": False,
                "error": "developer_visual_profile_invalid",
                "reason": "Commit requires one exact valid candidate_id and a non-empty rationale.",
                "available_candidate_ids": [
                    str(row.get("candidate_id") or "") for row in candidates if row.get("valid") is True
                ],
            }
        if visual_profile_file is None:
            return {"ok": False, "error": "developer_visual_profile_unavailable"}
        store = read_visual_profiles(visual_profile_file)
        profiles = _dict_value(store.get("profiles"))
        committed = {
            key: value
            for key, value in candidate.items()
            if key != "valid"
        }
        committed["commit_rationale"] = rationale[:500]
        committed["committed_at"] = utc_now_iso()
        profiles[str(committed.get("app_scope_id") or visual_app_scope_id)] = committed
        store["profiles"] = profiles
        _write_json_atomic(visual_profile_file, store)
        return {
            "ok": True,
            "app_scope_id": committed.get("app_scope_id"),
            "candidate_id": candidate_id,
            "profile_digest": committed.get("digest"),
            "artifact": visual_profile_file.name,
        }
    if raw_name == "uiux.search_guidance":
        if "ui" not in {value.strip().lower() for value in _string_items(item.get("surfaces"))}:
            return {"ok": False, "error": "developer_uiux_not_ui_unit"}
        if uiux_provider is None:
            return {"ok": False, "error": "developer_uiux_unavailable"}
        try:
            guidance = uiux_provider.search(args)
        except HarnessError as exc:
            return {"ok": False, "error": exc.error_code, "reason": exc.message}
        return {
            "ok": True,
            "purpose": "technical_implementation_guidance_only",
            "design_authority": False,
            **guidance,
        }
    if raw_name == "package.ensure_workspace_dependency":
        dependency_id = str(args.get("dependency_id") or "").strip()
        requirement = next(
            (
                row
                for row in _dict_items(item.get("package_dependencies"))
                if str(row.get("dependency_id") or "") == dependency_id
            ),
            None,
        )
        if requirement is None:
            return {
                "ok": False,
                "error": "developer_package_dependency_not_approved",
                "dependency_id": dependency_id,
            }
        return ensure_workspace_dependency(
            repo_root=repo_root,
            requirement=requirement,
            run_id=run_id,
            timeout_seconds=package_tool_timeout_seconds,
        )
    if raw_name == "codegraph.query":
        operation = str(args.get("operation") or "").strip().lower()
        query = str(args.get("query") or "").strip()
        symbol = str(args.get("symbol") or "").strip()
        file = _normalized_codegraph_path(repo_root, args.get("file"), run_id=run_id)
        files = [
            path
            for raw_path in _string_items(args.get("files"))[:20]
            if (path := _normalized_codegraph_path(repo_root, raw_path, run_id=run_id))
        ]
        file_filter = _normalized_codegraph_path(
            repo_root,
            args.get("file_filter"),
            run_id=run_id,
        )
        pattern = str(args.get("pattern") or "").strip()
        allowed_operations = {
            "symbols",
            "node",
            "callers",
            "callees",
            "impact",
            "affected_tests",
            "files",
            "explore",
        }
        missing_required_input = (
            (operation == "symbols" and not (query or file))
            or (operation == "explore" and not query)
            or (operation in {"callers", "callees", "impact"} and not symbol)
            or (operation == "node" and not (symbol or file))
            or (operation == "affected_tests" and not files)
        )
        if (
            operation not in allowed_operations
            or missing_required_input
            or len(query) > 500
            or len(symbol) > 300
            or len(pattern) > 200
        ):
            return {"ok": False, "error": "invalid_codegraph_query"}
        freshness = repo_context_provider.sync_if_stale(repo_root)
        payload = repo_context_provider.query_capability(
            repo_root,
            operation=operation,
            query=query,
            symbol=symbol,
            file=file,
            files=files,
            kind=str(args.get("kind") or "").strip(),
            limit=int(args.get("limit") or 10),
            depth=int(args.get("depth") or 2),
            file_filter=file_filter,
            pattern=pattern,
        )
        snapshot = _dict_value(freshness.get("snapshot"))
        return {
            "ok": True,
            "operation": operation,
            "snapshot_digest": snapshot.get("artifact_digest"),
            "index_synced": freshness.get("synced") is True,
            **payload,
        }
    if raw_name == "codegraph.query_readonly":
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 500:
            return {"ok": False, "error": "invalid_codegraph_query"}
        freshness = repo_context_provider.sync_if_stale(repo_root)
        evidence = repo_context_provider.query_readonly(repo_root, query)
        snapshot = _dict_value(freshness.get("snapshot"))
        return {
            "ok": True,
            "operation": "explore",
            "query_sha256": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "snapshot_digest": snapshot.get("artifact_digest"),
            "index_synced": freshness.get("synced") is True,
            "evidence": evidence[:12000],
            "truncated": len(evidence) > 12000,
        }
    if raw_name == "repo.read_batch":
        requests = args.get("requests")
        if (
            not isinstance(requests, list)
            or not 1 <= len(requests) <= _MAX_BATCH_OBSERVATIONS
            or any(not isinstance(request, dict) for request in requests)
        ):
            return {
                "ok": False,
                "error": "invalid_read_batch",
                "max_requests": _MAX_BATCH_OBSERVATIONS,
            }
        results: list[dict[str, object]] = []
        for index, request in enumerate(requests):
            requested_path = request.get("path")
            if scoped_read_path(requested_path) is None:
                results.append({"request_index": index, **read_scope_error(requested_path)})
                continue
            try:
                result = _read_file(
                    repo_root,
                    request,
                    run_id=run_id,
                    max_content_chars=_BATCH_READ_CHARS_PER_ITEM,
                )
            except HarnessError as exc:
                result = {"ok": False, "error": exc.error_code, "reason": exc.message}
            results.append({"request_index": index, **result})
        succeeded = sum(result.get("ok") is True for result in results)
        return {
            "ok": succeeded > 0,
            "batch_kind": "read",
            "count": len(results),
            "succeeded": succeeded,
            "partial": succeeded != len(results),
            "results": results,
        }
    if raw_name == "repo.search_batch":
        raw_queries = args.get("queries")
        if (
            not isinstance(raw_queries, list)
            or not 1 <= len(raw_queries) <= _MAX_BATCH_OBSERVATIONS
            or any(not isinstance(query, str) or not query.strip() for query in raw_queries)
        ):
            return {
                "ok": False,
                "error": "invalid_search_batch",
                "max_queries": _MAX_BATCH_OBSERVATIONS,
            }
        limit = max(1, min(int(args.get("limit_per_query") or 10), 10))
        results = []
        # shortcut: at most four sequential literal scans; share one scan if the batch limit grows.
        for index, query in enumerate(raw_queries):
            search_args = {
                "query": query.strip(),
                "prefix": args.get("prefix"),
                "limit": limit,
            }
            try:
                result = _search_code(
                    repo_root,
                    search_args,
                    run_id=run_id,
                    allowed_paths=sorted(unit_read_paths),
                )
            except HarnessError as exc:
                result = {"ok": False, "error": exc.error_code, "reason": exc.message}
            results.append({"request_index": index, "query": query.strip(), **result})
        succeeded = sum(result.get("ok") is True for result in results)
        return {
            "ok": succeeded > 0,
            "batch_kind": "search",
            "count": len(results),
            "succeeded": succeeded,
            "partial": succeeded != len(results),
            "results": results,
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
            "input_hashes": {
                path: _file_hash(repo_root / path) for path in _item_execution_hash_paths(item)
            },
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
        if raw_name == "repo.list":
            prefix_value = args.get("prefix")
            relative_prefix = scoped_read_path(prefix_value, allow_root=True)
            if relative_prefix is None:
                return read_scope_error(prefix_value)
            limit = max(1, min(int(args.get("limit") or 100), 200))
            files = [
                path
                for path in sorted(unit_read_paths)
                if (not relative_prefix or path == relative_prefix or path.startswith(relative_prefix + "/"))
                and (repo_root / path).is_file()
            ]
            return {"ok": True, "files": files[:limit], "truncated": len(files) > limit}
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
        if raw_name == "repo.read" and scoped_read_path(args.get("path")) is None:
            return read_scope_error(args.get("path"))
        return _read_file(repo_root, args, run_id=run_id)
    if name == "search_code":
        return _search_code(
            repo_root,
            args,
            run_id=run_id,
            allowed_paths=sorted(unit_read_paths) if raw_name == "repo.search" else None,
        )
    if name == "read_diff":
        path = str(args.get("path") or "")
        if raw_name == "repo.read_diff" and scoped_read_path(path) is None:
            return read_scope_error(path)
        _safe_read_path(repo_root, path, run_id=run_id)
        completed = subprocess.run(
            ["git", "diff", "--", path], cwd=str(repo_root), check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return {"ok": True, "path": path, "diff": completed.stdout[:12000], "truncated": len(completed.stdout) > 12000}
    if name == "write_file":
        path = str(args.get("path") or "")
        if journal is not None and _new_file_extension_allowed(
            item=item, item_paths=item_paths, repo_root=repo_root, path=path
        ):
            # D11 (b): a closure unit may create a new file under its component root; recorded for the owner.
            journal.setdefault("scope_extensions", []).append(
                {"path": path, "item_id": str(item.get("id") or ""), "rule": "D11b", "recorded_at": utc_now_iso()}
            )
            item_paths.append(path)
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
    if name == "replace_text":
        path = str(args.get("path") or "")
        target = _safe_write_target(repo_root, path, item_paths, run_id=run_id)
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            return {"ok": False, "error": "replace_text_old_text_required"}
        if not isinstance(new_text, str) or len(old_text) + len(new_text) > max_write_chars:
            return {"ok": False, "error": "replace_text_too_large", "max_chars": max_write_chars}
        expected = str(args.get("expected_sha256") or "")
        before_sha256 = _file_hash(target)
        if expected != before_sha256:
            return {"ok": False, "error": "stale_file_hash", "actual_sha256": before_sha256}
        if not target.is_file():
            return {"ok": False, "error": "replace_text_file_not_found", "path": path}
        try:
            content = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "replace_text_file_not_utf8", "path": path}
        newline = "\r\n" if "\r\n" in content else "\n"
        matched_old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
        replacement_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
        if newline == "\r\n":
            matched_old_text = matched_old_text.replace("\n", newline)
            replacement_text = replacement_text.replace("\n", newline)
        match_count = content.count(matched_old_text)
        if match_count == 0:
            return {
                "ok": False,
                "error": "replace_text_not_found",
                "path": path,
                "instruction": "Read the current exact block and retry with byte-for-byte old_text.",
            }
        if match_count > 1:
            return {
                "ok": False,
                "error": "replace_text_ambiguous",
                "path": path,
                "match_count": match_count,
                "instruction": "Include more unchanged surrounding text so old_text matches exactly once.",
            }
        updated = content.replace(matched_old_text, replacement_text, 1)
        if updated == content:
            return {
                "ok": False,
                "error": "no_effect_mutation",
                "path": path,
                "sha256": before_sha256,
                "instruction": "old_text and new_text produce no file change; submit a materially different replacement.",
            }
        target.write_bytes(updated.encode("utf-8"))
        after_sha256 = _file_hash(target)
        return {
            "ok": True,
            "path": path,
            "sha256": after_sha256,
            "replacements": 1,
            "old_chars": len(old_text),
            "new_chars": len(new_text),
        }
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


def _normalized_codegraph_path(
    repo_root: Path,
    value: object,
    *,
    run_id: str,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    target = _safe_read_path(repo_root, raw, run_id=run_id)
    relative = target.relative_to(repo_root.resolve())
    if any(part.casefold() in _READ_DENIED_NAMES for part in relative.parts):
        raise HarnessError(
            "developer_tool_unsafe_path",
            f"CodeGraph path is protected: {raw}",
            run_id=run_id or None,
        )
    return relative.as_posix()


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


def _journal_scope_extension_paths(journal: dict[str, object] | None, item_id: str) -> list[str]:
    return [
        str(row.get("path") or "")
        for row in _dict_items(_dict_value(journal).get("scope_extensions"))
        if str(row.get("item_id") or "") == item_id and str(row.get("path") or "")
    ]


def _new_file_extension_allowed(
    *,
    item: dict[str, object],
    item_paths: list[str],
    repo_root: Path,
    path: str,
) -> bool:
    """D11 (b): only a not-yet-existing file under the unit's component root, never protected or Harness-owned."""

    policy = _dict_value(item.get("new_file_policy"))
    if not policy or not path or path in item_paths:
        return False
    if (
        path != path.strip()
        or "\\" in path
        or ":" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return False
    if not any(path.startswith(f"{root}/") for root in _string_items(policy.get("roots"))):
        return False
    if not path.endswith(tuple(_string_items(policy.get("suffixes")))):
        return False
    if any(path == entry or path.startswith(f"{entry}/") for entry in _string_items(policy.get("protected_paths"))):
        return False
    if path in _string_items(policy.get("reserved_paths")):
        return False
    return not (repo_root / path).exists()


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


def _read_file(
    repo_root: Path,
    args: dict[str, object],
    *,
    run_id: str,
    max_content_chars: int = 12000,
) -> dict[str, object]:
    path = _safe_read_path(repo_root, str(args.get("path") or ""), run_id=run_id)
    if not path.is_file() or not _read_path_allowed(repo_root, path):
        return {"ok": False, "error": "file_not_readable", "path": str(args.get("path") or "")}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(args.get("start_line") or 1))
    requested_end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 300)))
    rendered: list[str] = []
    rendered_chars = 0
    actual_end = start - 1
    for index in range(start - 1, requested_end):
        line = f"{index + 1}: {lines[index]}"
        added_chars = len(line) + (1 if rendered else 0)
        if rendered and rendered_chars + added_chars > max_content_chars:
            break
        if not rendered and len(line) > max_content_chars:
            line = line[:max_content_chars]
        rendered.append(line)
        rendered_chars += len(line) + (1 if len(rendered) > 1 else 0)
        actual_end = index + 1
        if rendered_chars >= max_content_chars:
            break
    has_more = actual_end < len(lines)
    return {
        "ok": True,
        "path": str(args.get("path")),
        "start_line": start,
        "end_line": actual_end,
        "requested_end_line": requested_end,
        "total_lines": len(lines),
        "content": "\n".join(rendered),
        "truncated": actual_end < requested_end,
        "has_more": has_more,
        **({"next_start_line": actual_end + 1} if has_more else {}),
    }


def _search_code(
    repo_root: Path,
    args: dict[str, object],
    *,
    run_id: str,
    allowed_paths: list[str] | None = None,
) -> dict[str, object]:
    query = str(args.get("query") or "")
    if not query or len(query) > 200:
        return {"ok": False, "error": "invalid_query"}

    raw_prefix = str(args.get("prefix") or "")
    prefix = _safe_read_path(repo_root, raw_prefix, run_id=run_id, allow_root=True)
    relative_parts = tuple(
        part.casefold()
        for part in prefix.relative_to(repo_root.resolve()).parts
    )
    if ".codegraph" in relative_parts:
        return {
            "ok": False,
            "error": "codegraph_search_not_supported",
            "instruction": "Use codegraph.query for graph/index evidence instead of searching .codegraph files.",
        }
    if "node_modules" in relative_parts and prefix.is_dir():
        dependency_parts = relative_parts[relative_parts.index("node_modules") + 1 :]
        package_selected = bool(dependency_parts) and (
            not dependency_parts[0].startswith("@") or len(dependency_parts) >= 2
        )
        if not package_selected:
            return {
                "ok": False,
                "error": "dependency_search_scope_required",
                "instruction": "Set prefix to one concrete package such as node_modules/@scope/package.",
            }

    limit = max(1, min(int(args.get("limit") or 30), 60))
    query_folded = query.casefold()
    hits: list[dict[str, object]] = []
    scanned_files = 0
    skipped_large_files = 0
    deadline = time.monotonic() + _SEARCH_TIMEOUT_SECONDS
    if allowed_paths is None:
        candidates = [prefix] if prefix.is_file() else _walk_search_files(prefix)
    else:
        resolved_prefix = prefix.resolve()
        candidates = [
            candidate
            for relative in allowed_paths
            if (
                (candidate := _safe_read_path(repo_root, relative, run_id=run_id)).is_file()
                and (candidate == resolved_prefix or resolved_prefix in candidate.parents)
            )
        ]
        if not candidates:
            return {
                "ok": False,
                "error": "developer_unit_read_out_of_scope",
                "prefix": raw_prefix,
                "approved_read_context_paths": sorted(allowed_paths),
            }
    for path in candidates:
        if time.monotonic() >= deadline:
            return {
                "ok": True,
                "hits": hits,
                "truncated": True,
                "truncation_reason": "timeout",
            }
        if not path.is_file() or not _read_path_allowed(repo_root, path):
            continue
        if scanned_files >= _SEARCH_MAX_SCANNED_FILES:
            return {
                "ok": True,
                "hits": hits,
                "truncated": True,
                "truncation_reason": "file_limit",
            }
        scanned_files += 1
        try:
            if path.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                skipped_large_files += 1
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if query_folded in line.casefold():
                hits.append(
                    {
                        "path": str(path.relative_to(repo_root)).replace("\\", "/"),
                        "line": line_no,
                        "text": line[:300],
                    }
                )
                if len(hits) >= limit:
                    return {"ok": True, "hits": hits, "truncated": True}
    result: dict[str, object] = {"ok": True, "hits": hits, "truncated": False}
    if skipped_large_files:
        result.update(
            {
                "truncated": True,
                "truncation_reason": "large_files_skipped",
                "skipped_large_files": skipped_large_files,
            }
        )
    return result


def _walk_search_files(prefix: Path):
    if not prefix.exists():
        return
    for root, directories, filenames in os.walk(prefix, topdown=True, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name.casefold() not in _SEARCH_PRUNED_DIR_NAMES
        )
        for filename in sorted(filenames):
            yield Path(root) / filename


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


def _item_ui_screen_ids(item: dict[str, object]) -> list[str]:
    screens = [
        str(runtime.get("screen_id") or "").strip()
        for binding in _dict_items(item.get("surface_contracts"))
        if str(binding.get("surface") or "").strip().lower() == "ui"
        for runtime in [
            _dict_value(_dict_value(binding.get("surface_contract")).get("runtime_surface"))
        ]
        if str(runtime.get("screen_id") or "").strip()
    ]
    if screens:
        return list(dict.fromkeys(screens))
    if "ui" in {value.strip().lower() for value in _string_items(item.get("surfaces"))}:
        return [f"unit:{str(item.get('id') or 'unknown')}"]
    return []


def _developer_uiux_available(
    *,
    manifest: dict[str, object],
    journal: dict[str, object],
    item: dict[str, object],
) -> bool:
    capability = _dict_value(manifest.get("uiux_capability"))
    return bool(
        _item_ui_screen_ids(item)
        and capability.get("status") == "available"
        and capability.get("optional") is True
        and not journal.get("uiux_disabled")
    )


def _pinned_visual_profile_action_reserve(
    *,
    working_state: dict[str, object],
    history: list[dict[str, object]],
    visual_required: bool,
    app_visual_profile: dict[str, object],
) -> int:
    """Keep the profile setup reserve stable when a paused unit resumes after committing it."""

    pinned = working_state.get("visual_profile_action_reserve")
    if type(pinned) is int:
        return max(0, pinned)
    profile_setup_started = not app_visual_profile or any(
        row.get("action") in {"uiux.search_visual_profile", "uiux.commit_visual_profile"}
        for row in history
    )
    return 3 if visual_required and profile_setup_started else 0


def _developer_uiux_success_count(
    *,
    journal: dict[str, object],
    item: dict[str, object],
) -> int:
    cache = _dict_value(journal.get("uiux_guidance_by_screen"))
    return max(
        (len(_dict_items(cache.get(screen_id))) for screen_id in _item_ui_screen_ids(item)),
        default=0,
    )


def _developer_uiux_guidance(
    *,
    journal: dict[str, object],
    item: dict[str, object],
) -> list[dict[str, object]]:
    cache = _dict_value(journal.get("uiux_guidance_by_screen"))
    return [
        {"screen_id": screen_id, "results": _dict_items(cache.get(screen_id))}
        for screen_id in _item_ui_screen_ids(item)
        if _dict_items(cache.get(screen_id))
    ]


def _remember_developer_uiux_guidance(
    *,
    journal: dict[str, object],
    item: dict[str, object],
    result: dict[str, object],
) -> None:
    cache = journal.setdefault("uiux_guidance_by_screen", {})
    if not isinstance(cache, dict):
        cache = {}
        journal["uiux_guidance_by_screen"] = cache
    durable = {
        key: result.get(key)
        for key in ("schema_version", "mode", "selector", "query", "count", "no_match", "results")
        if key in result
    }
    for screen_id in _item_ui_screen_ids(item):
        rows = _dict_items(cache.get(screen_id))
        if len(rows) < 2:
            cache[screen_id] = [*rows, durable]


def _developer_visual_candidates(
    *,
    journal: dict[str, object],
    app_scope_id: str,
) -> list[dict[str, object]]:
    cache = _dict_value(journal.get("uiux_visual_candidates_by_app"))
    return _dict_items(cache.get(app_scope_id))[:2]


def _remember_developer_visual_candidate(
    *,
    journal: dict[str, object],
    app_scope_id: str,
    candidate: dict[str, object],
) -> None:
    cache = journal.setdefault("uiux_visual_candidates_by_app", {})
    if not isinstance(cache, dict):
        cache = {}
        journal["uiux_visual_candidates_by_app"] = cache
    rows = _dict_items(cache.get(app_scope_id))
    candidate_id = str(candidate.get("candidate_id") or "")
    if candidate_id and not any(str(row.get("candidate_id") or "") == candidate_id for row in rows):
        cache[app_scope_id] = [*rows, candidate][:2]


def _merge_runtime_authority_manifest(
    existing: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object] | None:
    """Allow one additive, prompt-only runtime-authority upgrade in an active repair."""

    if not str(existing.get("mode") or "").endswith("_repair") or existing.get("mode") != candidate.get("mode"):
        return None
    if "uiux_capability" in candidate and candidate.get("uiux_capability") != existing.get("uiux_capability"):
        return None
    rebuilt_fields = {"created_at", "input_hash", "items", "uiux_capability"}
    old_top = {key: value for key, value in existing.items() if key not in rebuilt_fields}
    new_top = {key: value for key, value in candidate.items() if key not in rebuilt_fields}
    if old_top != new_top:
        return None
    old_items = _dict_items(existing.get("items"))
    new_items = _dict_items(candidate.get("items"))
    if [item.get("id") for item in old_items] != [item.get("id") for item in new_items]:
        return None

    upgraded = False
    for old_item, new_item in zip(old_items, new_items):
        old_stable = {
            key: value
            for key, value in old_item.items()
            if key not in {"input_digest", "runtime_closure_authority"}
        }
        new_stable = {
            key: value
            for key, value in new_item.items()
            if key not in {"input_digest", "runtime_closure_authority"}
        }
        old_authority = _dict_value(old_item.get("runtime_closure_authority"))
        new_authority = _dict_value(new_item.get("runtime_closure_authority"))
        if old_stable != new_stable or not new_authority or (old_authority and old_authority != new_authority):
            return None
        upgraded = upgraded or not old_authority
    if not upgraded:
        return None

    merged = json.loads(json.dumps(existing))
    merged["input_hash"] = candidate.get("input_hash")
    for old_item, new_item in zip(_dict_items(merged.get("items")), new_items):
        old_item["runtime_closure_authority"] = _dict_value(new_item.get("runtime_closure_authority"))
        old_item["input_digest"] = new_item.get("input_digest")
    return merged


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


def _merge_dependency_manifest_scope_manifest(
    existing: dict[str, object],
    candidate: dict[str, object],
) -> tuple[dict[str, object], list[str]] | None:
    """Accept deterministic migration between legacy manifest scope and package-tool contracts."""

    if existing.get("mode") != "act" or candidate.get("mode") != "act":
        return None
    stable_manifest_fields = (
        "version",
        "run_id",
        "mode",
        "repair_iteration",
        "base_sha",
        "expected_dirty_files",
        "repair_feedback",
        "developer_generation_context",
    )
    if any(existing.get(field) != candidate.get(field) for field in stable_manifest_fields):
        return None
    old_allowed = set(_string_items(existing.get("workspace_allowed_paths")))
    new_allowed = set(_string_items(candidate.get("workspace_allowed_paths")))
    existing_items = _dict_items(existing.get("items"))
    candidate_items = _dict_items(candidate.get("items"))
    if [item.get("id") for item in existing_items] != [item.get("id") for item in candidate_items]:
        return None

    changed_item_ids: list[str] = []
    reopened_item_ids: list[str] = []
    old_declared_manifests: set[str] = set()
    new_declared_manifests: set[str] = set()
    for old_item, new_item in zip(existing_items, candidate_items):
        if _dependency_scope_stable_item(old_item) != _dependency_scope_stable_item(new_item):
            return None
        old_paths = set(_item_paths(old_item))
        new_paths = set(_item_paths(new_item))
        added_paths = new_paths - old_paths
        removed_paths = old_paths - new_paths
        old_item_manifests = set(_item_dependency_paths(old_item))
        new_item_manifests = set(_item_dependency_paths(new_item))
        if removed_paths - old_item_manifests or added_paths - new_item_manifests:
            return None
        old_declared_manifests.update(old_item_manifests)
        new_declared_manifests.update(new_item_manifests)
        old_files = {
            str(file_item.get("path") or ""): _entrypoint_scope_stable_file(file_item)
            for file_item in _item_files(old_item)
        }
        new_files = {
            str(file_item.get("path") or ""): _entrypoint_scope_stable_file(file_item)
            for file_item in _item_files(new_item)
        }
        if any(
            new_files.get(path) != value
            for path, value in old_files.items()
            if path not in removed_paths
        ):
            return None
        if set(new_files) - set(old_files) != added_paths or set(old_files) - set(new_files) != removed_paths:
            return None
        dependency_contract_changed = (
            old_item.get("package_dependencies") != new_item.get("package_dependencies")
        )
        if added_paths or removed_paths or dependency_contract_changed:
            item_id = str(new_item.get("id") or "")
            changed_item_ids.append(item_id)
            if dependency_contract_changed and _dict_items(new_item.get("package_dependencies")):
                reopened_item_ids.append(item_id)
    if (
        (not changed_item_ids and old_allowed == new_allowed)
        or new_allowed - old_allowed - new_declared_manifests
        or old_allowed - new_allowed - old_declared_manifests
    ):
        return None

    merged = json.loads(json.dumps(existing))
    merged["input_hash"] = candidate.get("input_hash")
    merged["workspace_allowed_paths"] = candidate.get("workspace_allowed_paths", [])
    candidate_by_id = {str(item.get("id") or ""): item for item in candidate_items}
    for index, old_item in enumerate(_dict_items(merged.get("items"))):
        item_id = str(old_item.get("id") or "")
        if item_id in changed_item_ids:
            merged["items"][index] = json.loads(json.dumps(candidate_by_id[item_id]))
    return merged, reopened_item_ids


def _dependency_scope_stable_item(item: dict[str, object]) -> dict[str, object]:
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
            "package_dependencies",
            "input_digest",
        }
    }


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


def _reopen_scope_expanded_items(
    *,
    target_path: Path,
    session_dir: Path,
    manifest: dict[str, object],
    journal: dict[str, object],
    item_ids: list[str],
    reason: str,
) -> None:
    """Refresh pending scope or reopen completed units while preserving audit artifacts."""

    _supersede_pending_step_review(
        session_dir=session_dir,
        journal=journal,
        reason=f"{reason}_reopened",
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
                f"Scope migration is missing execution unit {item_id}.",
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
                "reason": f"{reason}_added",
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
    pending_exchange_id = pending_call_id.removesuffix("-input")
    for review_session_file in (session_dir / "step_review").glob("*/step_review_session_v1.json"):
        review_session = _read_json(review_session_file, run_id="")
        calls = _dict_items(review_session.get("calls"))
        changed = False
        for call in calls:
            if (
                str(call.get("call_id") or "") == pending_exchange_id
                and str(call.get("status") or "") in {"pending", "input_pending", "input_approved"}
            ):
                call["status"] = "superseded"
                call["superseded_at"] = utc_now_iso()
                call["superseded_reason"] = reason
                _write_json_atomic(review_session_file.parent / f"exchange_{pending_exchange_id}.json", call)
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


def _developer_action_limit(
    *,
    configured_max_actions: int,
    timeout_action_reserve: int,
    verification_repair_action_reserve: int,
    history: list[dict[str, object]],
) -> int:
    """Keep one bounded repair reserve once implementation verification has failed."""

    limit = _timeout_diagnostic_action_limit(
        configured_max_actions=configured_max_actions,
        timeout_action_reserve=timeout_action_reserve,
        history=history,
    )
    repair_started = any(
        row.get("action") == "verification.run_stage"
        and _dict_value(row.get("result")).get("status") == "failed"
        and _dict_value(row.get("result")).get("failure_kind") == "implementation"
        for row in history
    )
    return limit + verification_repair_action_reserve if repair_started else limit


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
    pending_statuses = {"input_pending", "input_approved", "pending"}
    if any(str(row.get("status") or "") in pending_statuses for row in calls):
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
    payload = str(
        arguments.get("content")
        or arguments.get("patch")
        or arguments.get("new_text")
        or ""
    )
    return (
        path in set(_string_items(timeout_diagnostic.get("test_paths")))
        and _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER in payload
        and _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX in payload
    )


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
            ),
            "manifest_input_hash": manifest.get("input_hash"),
            "mode": manifest.get("mode"),
            "status": status,
            **progress,
            "updated_at": utc_now_iso(),
        },
    )


def _failure_classification(error_code: str, *, retriable: bool) -> dict[str, object]:
    if error_code in {"developer_step_review_required"}:
        failure_class, owner, route = "operator_review", "harness_operator", "pause_and_resume"
    elif error_code in {"llm_transport_failed", "requirement_refiner_failed"}:
        failure_class, owner, route = "transport_failure", "llm_transport", "pause_and_resume"
    elif error_code in {
        "developer_visual_profile_unavailable",
        "developer_visual_profile_invalid",
        "developer_visual_profile_required",
    }:
        failure_class, owner, route = "developer_capability_failure", "developer_harness", "pause_and_resume"
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
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (2**attempt))


