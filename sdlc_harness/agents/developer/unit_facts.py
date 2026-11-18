"""Read-only facts about a Developer execution unit and its action history, shared by the loop and the prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ...core.coerce import dict_items as _dict_items, dict_value as _dict_value, string_items as _string_items
from .package_dependency import workspace_dependency_status


_OBSERVATION_ACTIONS = {
    "list_files", "read_file", "search_code", "read_diff",
    "repo.list", "repo.read", "repo.read_batch", "repo.search", "repo.search_batch", "repo.read_diff",
    "codegraph.query", "codegraph.query_readonly", "codegraph.seed_context",
    "uiux.search_guidance",
    "uiux.search_visual_profile",
    "uiux.commit_visual_profile",
}


_MUTATION_ACTIONS = {
    "write_file",
    "apply_patch",
    "repo.write_file",
    "repo.apply_patch",
    "repo.replace_text",
}


_TIMEOUT_DIAGNOSTIC_SOURCE_MARKER = "SDLC_DIAGNOSTIC_INSTRUMENTATION"


_TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX = "SDLC_DIAGNOSTIC_TIMEOUT:"


_READ_DENIED_NAMES = {".env", ".git", ".runtime", ".codegraph"}


_MAX_BATCH_OBSERVATIONS = 4


_TEXT_SUFFIXES = {
    ".java", ".kt", ".py", ".js", ".ts", ".tsx", ".jsx", ".xml", ".json",
    ".yaml", ".yml", ".toml", ".gradle", ".properties", ".md", ".txt", ".sh", ".ps1",
    ".prisma", ".sql", ".css",
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
        if action_name == "repo.read_batch" and any(
            child.get("ok") is True
            and (observed := str(child.get("path") or "").replace("\\", "/")).casefold() != target
            and _is_typescript_test_path(observed)
            for child in _dict_items(result.get("results"))
        ):
            return True
        if action_name in {"search_code", "repo.search"}:
            query = str(arguments.get("query") or "").casefold()
            if _dict_items(result.get("hits")) and any(marker in query for marker in query_markers):
                return True
        if action_name == "repo.search_batch" and any(
            _dict_items(child.get("hits"))
            and any(marker in str(child.get("query") or "").casefold() for marker in query_markers)
            for child in _dict_items(result.get("results"))
        ):
            return True
        if action_name in {"codegraph.query", "codegraph.query_readonly"}:
            query = str(arguments.get("query") or "").casefold()
            serialized = json.dumps(result, ensure_ascii=False).casefold()
            if serialized.strip() and any(
                marker in query for marker in query_markers
            ):
                return True
    return False


def _is_typescript_test_path(path: str) -> bool:
    return path.casefold().endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))


def _has_successful_target_inspection(history: list[dict[str, object]], *, item_path: str) -> bool:
    expected = item_path.strip().replace("\\", "/").lstrip("/").casefold()
    for row in history:
        if row.get("action") == "repo.read_batch":
            if any(
                child.get("ok") is True
                and str(child.get("path") or "").strip().replace("\\", "/").lstrip("/").casefold()
                == expected
                for child in _dict_items(_dict_value(row.get("result")).get("results"))
            ):
                return True
            continue
        if row.get("action") not in {"read_file", "read_diff", "repo.read", "repo.read_diff"}:
            continue
        if not _dict_value(row.get("result")).get("ok"):
            continue
        observed = str(_dict_value(row.get("arguments")).get("path") or "").strip().replace("\\", "/").lstrip("/")
        if observed.casefold() == expected:
            return True
    return False


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


def _read_path_allowed(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    if any(part.casefold() in _READ_DENIED_NAMES for part in rel.parts):
        return False
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in {"pom.xml", "build.gradle", "settings.gradle", "gradlew"}


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


def _item_dependency_paths(item: dict[str, object]) -> list[str]:
    return list(
        dict.fromkeys(
            path
            for dependency in _dict_items(item.get("package_dependencies"))
            for path in (
                str(
                    dependency.get("consumer_manifest_path")
                    or dependency.get("manifest_path")
                    or ""
                ).replace("\\", "/"),
                str(dependency.get("lockfile_path") or "").replace("\\", "/"),
            )
            if path
        )
    )


def _item_execution_hash_paths(item: dict[str, object]) -> list[str]:
    return list(dict.fromkeys([*_item_paths(item), *_item_dependency_paths(item)]))


def _package_dependency_statuses(
    *,
    target_path: Path,
    item: dict[str, object],
    run_id: str,
) -> list[dict[str, object]]:
    return [
        workspace_dependency_status(
            repo_root=target_path,
            requirement=requirement,
            run_id=run_id,
        )
        for requirement in _dict_items(item.get("package_dependencies"))
    ]


def _item_context_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("context_paths"))]
    if not declared:
        declared = [path.replace("\\", "/") for path in _string_items(item.get("required_paths"))]
    return list(dict.fromkeys(declared)) if declared else _item_paths(item)


def _item_direct_repair_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("direct_repair_paths"))]
    return list(dict.fromkeys(declared))


def _required_test_mutation_paths(
    item: dict[str, object],
    *,
    repair_mode: bool,
) -> list[str]:
    if (
        not _dict_items(item.get("test_intents"))
        or item.get("test_intent_mutation_required") is False
    ):
        return []
    test_targets = _string_items(item.get("test_target_paths"))
    if not repair_mode:
        return test_targets
    direct_repair_paths = set(_item_direct_repair_paths(item))
    return [path for path in test_targets if path in direct_repair_paths]


def _item_diagnostic_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("diagnostic_paths"))]
    if not declared:
        declared = _item_direct_repair_paths(item)
    return list(dict.fromkeys(declared))


def _item_contract_anchor_paths(item: dict[str, object]) -> list[str]:
    declared = [path.replace("\\", "/") for path in _string_items(item.get("contract_anchor_paths"))]
    return list(dict.fromkeys(declared))


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


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _clip_json_value(value: object, limit: int) -> object:
    raw = json.dumps(value, ensure_ascii=False)
    if len(raw) <= limit:
        return value
    return {"truncated": True, "preview": raw[:limit]}
