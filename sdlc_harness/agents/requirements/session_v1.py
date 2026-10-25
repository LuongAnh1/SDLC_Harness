"""Session bền vững cho Requirement tool-loop và khôi phục sau gián đoạn."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from ...core.errors import HarnessError
from ...loop import canonical_json_hash
from ...storage.runtime import RuntimeStore, utc_now_iso


SESSION_VERSION = "requirement_loop_session_v1"
SESSION_FILENAME = "requirement_loop_session_v1.json"
ACTION_INTENT_VERSION = "requirement_action_intent_v1"
_SESSION_STATUSES = {"created", "running", "paused", "completed", "blocked"}
_CALL_ID = re.compile(r"^RST-[1-9][0-9]*$")


def build_requirement_session_authority(
    *,
    manifest_input_hash: str,
    source_fingerprints: list[str],
    requirement_contract_version: str,
    prompt_policy_sha256: str,
    llm_mode: str,
    llm_model: str,
) -> dict[str, object]:
    """Tạo authority input bắt biến mà resume phải giữ nguyên."""

    return {
        "manifest_input_hash": manifest_input_hash,
        "source_fingerprints": sorted(source_fingerprints),
        "requirement_contract_version": requirement_contract_version,
        "prompt_policy_sha256": prompt_policy_sha256,
        "llm_mode": llm_mode,
        "llm_model": llm_model,
    }


def open_requirement_session(
    *,
    store: RuntimeStore | None,
    run_id: str,
    authority: dict[str, object],
    action_limit: int,
    action_retry_limit: int,
    resume: bool,
) -> tuple[dict[str, Any], str]:
    """Tạo session mới hoặc mở lại đúng session cũ sau khi kiểm authority."""

    authority_sha256 = canonical_json_hash(authority)
    if store is None:
        if resume:
            raise HarnessError(
                "requirement_session_not_resumable",
                "Requirement resume requires a durable RuntimeStore.",
                run_id=run_id,
            )
        return _new_session(
            run_id=run_id,
            authority=authority,
            authority_sha256=authority_sha256,
            action_limit=action_limit,
            action_retry_limit=action_retry_limit,
        ), ""

    path = store.artifact_path(run_id, SESSION_FILENAME)
    if not path.exists():
        if resume:
            raise HarnessError(
                "requirement_session_not_resumable",
                "Historical Requirement run has no durable loop session.",
                run_id=run_id,
            )
        session = _new_session(
            run_id=run_id,
            authority=authority,
            authority_sha256=authority_sha256,
            action_limit=action_limit,
            action_retry_limit=action_retry_limit,
        )
        write_requirement_session(store=store, run_id=run_id, session=session)
        return session, store.relative(path)

    session = read_requirement_session(store=store, run_id=run_id)
    if session["authority_sha256"] != authority_sha256:
        raise HarnessError(
            "requirement_session_input_drift",
            "Requirement source, prompt policy, contract or LLM identity changed after session creation.",
            run_id=run_id,
        )
    budgets = _dict(session["budgets"], code="requirement_session_invalid", run_id=run_id)
    if budgets.get("action_limit") != action_limit or budgets.get("action_retry_limit") != action_retry_limit:
        raise HarnessError(
            "requirement_session_input_drift",
            "Requirement loop budgets changed after session creation.",
            run_id=run_id,
        )
    if not resume:
        raise HarnessError(
            "requirement_session_exists",
            "Requirement loop session already exists; use continue_run to resume it.",
            run_id=run_id,
        )
    if session["status"] not in {"running", "paused", "created"}:
        raise HarnessError(
            "requirement_session_not_resumable",
            f"Requirement session cannot resume from status {session['status']}.",
            run_id=run_id,
        )
    session["status"] = "running"
    session["resume_count"] = int(session.get("resume_count") or 0) + 1
    session["last_error"] = None
    session["updated_at"] = utc_now_iso()
    write_requirement_session(store=store, run_id=run_id, session=session)
    return session, store.relative(path)


def read_requirement_session(*, store: RuntimeStore, run_id: str) -> dict[str, Any]:
    path = store.artifact_path(run_id, SESSION_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement loop session is missing or unreadable.",
            run_id=run_id,
        ) from exc
    validate_requirement_session(payload, run_id=run_id)
    return payload


def write_requirement_session(
    *, store: RuntimeStore | None, run_id: str, session: dict[str, Any]
) -> str:
    validate_requirement_session(session, run_id=run_id)
    session["updated_at"] = utc_now_iso()
    if store is None:
        return ""
    path = store.write_json_artifact(run_id, SESSION_FILENAME, session)
    return store.relative(path)


def reserve_requirement_call(
    *, store: RuntimeStore | None, run_id: str, session: dict[str, Any]
) -> str:
    """Giữ call ID và budget trước khi gọi LLM để crash không reset quota."""

    budgets = _dict(session["budgets"], code="requirement_session_invalid", run_id=run_id)
    cursor = _dict(session["cursor"], code="requirement_session_invalid", run_id=run_id)
    if cursor.get("pending_call") is not None:
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement session already has a pending call.",
            run_id=run_id,
        )
    used = int(budgets.get("action_used") or 0)
    limit = int(budgets.get("action_limit") or 0)
    if used >= limit:
        raise HarnessError(
            "requirement_tool_action_limit_exceeded",
            f"Requirement Refiner did not complete within {limit} actions.",
            run_id=run_id,
        )
    action_index = int(cursor.get("next_action_index") or 1)
    call_id = f"RST-{action_index}"
    cursor["pending_call"] = {
        "call_id": call_id,
        "action_index": action_index,
        "status": "llm_requested",
        "intent_ref": None,
        "intent_sha256": None,
        "candidate_reserved": False,
        "started_at": utc_now_iso(),
    }
    budgets["action_used"] = used + 1
    budgets["action_remaining"] = limit - used - 1
    session["status"] = "running"
    write_requirement_session(store=store, run_id=run_id, session=session)
    return call_id


def persist_requirement_action_intent(
    *,
    store: RuntimeStore | None,
    run_id: str,
    session: dict[str, Any],
    action: dict[str, object],
    completion_metadata: dict[str, object],
    reserve_candidate: bool,
) -> str:
    """Persist structured action; đây không phải raw completion của model."""

    pending = pending_requirement_call(session, run_id=run_id)
    call_id = str(pending["call_id"])
    budgets = _dict(session["budgets"], code="requirement_session_invalid", run_id=run_id)
    if reserve_candidate and not pending.get("candidate_reserved"):
        candidate_used = int(budgets.get("candidate_used") or 0)
        candidate_limit = int(budgets.get("candidate_limit") or 0)
        if candidate_used >= candidate_limit:
            raise HarnessError(
                "requirement_model_v2_validation_failed",
                "Requirement candidate budget is exhausted.",
                run_id=run_id,
            )
        budgets["candidate_used"] = candidate_used + 1
        budgets["candidate_remaining"] = candidate_limit - candidate_used - 1
        pending["candidate_reserved"] = True
    payload = {
        "schema_version": ACTION_INTENT_VERSION,
        "run_id": run_id,
        "session_id": session["session_id"],
        "call_id": call_id,
        "action": deepcopy(action),
        "action_sha256": canonical_json_hash(action),
        "completion": deepcopy(completion_metadata),
        "created_at": utc_now_iso(),
    }
    if store is None:
        reference = ""
    else:
        filename = f"requirement_action_intent_v1_{call_id}.json"
        path = store.write_json_artifact(run_id, filename, payload)
        reference = store.relative(path)
    pending["status"] = "action_persisted"
    pending["intent_ref"] = reference
    pending["intent_sha256"] = payload["action_sha256"]
    pending["completion"] = deepcopy(completion_metadata)
    write_requirement_session(store=store, run_id=run_id, session=session)
    return reference


def read_requirement_action_intent(
    *, store: RuntimeStore, run_id: str, session: dict[str, Any]
) -> tuple[dict[str, object], dict[str, object]]:
    pending = pending_requirement_call(session, run_id=run_id)
    reference = str(pending.get("intent_ref") or "")
    call_id = str(pending.get("call_id") or "")
    expected = store.artifact_path(run_id, f"requirement_action_intent_v1_{call_id}.json").resolve()
    path = Path(reference)
    if not path.is_absolute():
        path = store.repo_root / path
    if not reference or path.resolve() != expected:
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement action intent reference is invalid.",
            run_id=run_id,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement action intent is unreadable.",
            run_id=run_id,
        ) from exc
    action = payload.get("action") if isinstance(payload, dict) else None
    completion = payload.get("completion") if isinstance(payload, dict) else None
    if (
        not isinstance(action, dict)
        or not isinstance(completion, dict)
        or payload.get("schema_version") != ACTION_INTENT_VERSION
        or payload.get("run_id") != run_id
        or payload.get("session_id") != session.get("session_id")
        or payload.get("call_id") != call_id
        or payload.get("action_sha256") != canonical_json_hash(action)
        or payload.get("action_sha256") != pending.get("intent_sha256")
    ):
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement action intent identity or hash is invalid.",
            run_id=run_id,
        )
    return deepcopy(action), deepcopy(completion)


def complete_requirement_call(
    *, store: RuntimeStore | None, run_id: str, session: dict[str, Any]
) -> None:
    pending = pending_requirement_call(session, run_id=run_id)
    cursor = _dict(session["cursor"], code="requirement_session_invalid", run_id=run_id)
    cursor["next_action_index"] = int(pending.get("action_index") or 0) + 1
    cursor["pending_call"] = None
    write_requirement_session(store=store, run_id=run_id, session=session)


def pending_requirement_call(session: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    cursor = _dict(session.get("cursor"), code="requirement_session_invalid", run_id=run_id)
    pending = cursor.get("pending_call")
    if not isinstance(pending, dict) or not _CALL_ID.fullmatch(str(pending.get("call_id") or "")):
        raise HarnessError(
            "requirement_session_invalid",
            "Requirement session has no valid pending call.",
            run_id=run_id,
        )
    return pending


def set_requirement_session_status(
    *,
    store: RuntimeStore | None,
    run_id: str,
    session: dict[str, Any],
    status: str,
    error: HarnessError | None = None,
) -> None:
    if status not in _SESSION_STATUSES:
        raise HarnessError("requirement_session_invalid", f"Invalid session status: {status}", run_id=run_id)
    session["status"] = status
    session["last_error"] = error.to_response() if error is not None else None
    write_requirement_session(store=store, run_id=run_id, session=session)


def requirement_session_summary(session: dict[str, Any]) -> dict[str, object]:
    validate_requirement_session(session, run_id=str(session.get("run_id") or ""))
    budgets = _dict(session["budgets"], code="requirement_session_invalid", run_id=str(session["run_id"]))
    cursor = _dict(session["cursor"], code="requirement_session_invalid", run_id=str(session["run_id"]))
    return {
        "schema_version": SESSION_VERSION,
        "status": session["status"],
        "next_action_index": cursor["next_action_index"],
        "action_budget": {
            "used": budgets["action_used"],
            "remaining": budgets["action_remaining"],
            "limit": budgets["action_limit"],
        },
        "candidate_budget": {
            "used": budgets["candidate_used"],
            "remaining": budgets["candidate_remaining"],
            "limit": budgets["candidate_limit"],
        },
        "resume_count": session["resume_count"],
    }


def validate_requirement_session(value: object, *, run_id: str) -> None:
    if not isinstance(value, dict):
        raise HarnessError("requirement_session_invalid", "Requirement session must be an object.", run_id=run_id)
    if (
        value.get("schema_version") != SESSION_VERSION
        or value.get("run_id") != run_id
        or value.get("role") != "requirement"
        or value.get("executor") != "requirement_refiner_v2"
        or value.get("status") not in _SESSION_STATUSES
        or not isinstance(value.get("authority"), dict)
        or value.get("authority_sha256") != canonical_json_hash(value.get("authority"))
    ):
        raise HarnessError("requirement_session_invalid", "Requirement session identity is invalid.", run_id=run_id)
    budgets = _dict(value.get("budgets"), code="requirement_session_invalid", run_id=run_id)
    cursor = _dict(value.get("cursor"), code="requirement_session_invalid", run_id=run_id)
    working = _dict(value.get("working"), code="requirement_session_invalid", run_id=run_id)
    integer_fields = (
        "action_limit", "action_used", "action_remaining", "action_retry_limit",
        "candidate_limit", "candidate_used", "candidate_remaining", "invalid_failures",
        "truncated_failures",
    )
    if any(not isinstance(budgets.get(key), int) or int(budgets[key]) < 0 for key in integer_fields):
        raise HarnessError("requirement_session_invalid", "Requirement session budgets are invalid.", run_id=run_id)
    if budgets["action_used"] + budgets["action_remaining"] != budgets["action_limit"]:
        raise HarnessError("requirement_session_invalid", "Requirement action budget does not balance.", run_id=run_id)
    if budgets["candidate_used"] + budgets["candidate_remaining"] != budgets["candidate_limit"]:
        raise HarnessError("requirement_session_invalid", "Requirement candidate budget does not balance.", run_id=run_id)
    if not isinstance(cursor.get("next_action_index"), int) or int(cursor["next_action_index"]) < 1:
        raise HarnessError("requirement_session_invalid", "Requirement session cursor is invalid.", run_id=run_id)
    pending = cursor.get("pending_call")
    if pending is not None and (
        not isinstance(pending, dict)
        or not _CALL_ID.fullmatch(str(pending.get("call_id") or ""))
        or pending.get("status") not in {"llm_requested", "action_persisted"}
    ):
        raise HarnessError("requirement_session_invalid", "Requirement pending call is invalid.", run_id=run_id)
    if not isinstance(working.get("attempts"), list) or not isinstance(
        working.get("delta_artifact_paths"), list
    ):
        raise HarnessError("requirement_session_invalid", "Requirement working state is invalid.", run_id=run_id)
    if not isinstance(value.get("resume_count"), int) or int(value["resume_count"]) < 0:
        raise HarnessError("requirement_session_invalid", "Requirement resume count is invalid.", run_id=run_id)


def _new_session(
    *,
    run_id: str,
    authority: dict[str, object],
    authority_sha256: str,
    action_limit: int,
    action_retry_limit: int,
) -> dict[str, Any]:
    if action_limit < 1 or action_retry_limit < 0:
        raise HarnessError("requirement_session_invalid", "Requirement loop budgets are invalid.", run_id=run_id)
    now = utc_now_iso()
    return {
        "schema_version": SESSION_VERSION,
        "session_id": f"requirement:{run_id}:v1",
        "run_id": run_id,
        "role": "requirement",
        "executor": "requirement_refiner_v2",
        "status": "created",
        "authority": deepcopy(authority),
        "authority_sha256": authority_sha256,
        "cursor": {"next_action_index": 1, "pending_call": None},
        "budgets": {
            "action_limit": action_limit,
            "action_used": 0,
            "action_remaining": action_limit,
            "action_retry_limit": action_retry_limit,
            "candidate_limit": 2,
            "candidate_used": 0,
            "candidate_remaining": 2,
            "invalid_failures": 0,
            "truncated_failures": 0,
        },
        "working": {
            "listed": False,
            "observation_count": 0,
            "attempts": [],
            "initial_report": None,
            "repair_base_ref": None,
            "repair_base_sha256": None,
            "repair_report": None,
            "delta_artifact_paths": [],
        },
        "resume_count": 0,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }


def _dict(value: object, *, code: str, run_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError(code, "Requirement session object is invalid.", run_id=run_id)
    return value
