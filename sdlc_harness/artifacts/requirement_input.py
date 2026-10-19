"""Đọc contract requirement đã duyệt tại biên Phase 1 -> Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from ..agents.requirements import (
    RequirementModelV2,
    normalize_and_validate_requirement_model_v2,
    require_valid_requirement_v2_projections,
)
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore
from .requirement_v2 import read_requirement_model_v2, read_source_snapshot_v2


@dataclass(frozen=True)
class CanonicalRequirementInput:
    contract_version: str
    requirement_packet: str
    formal_use_case: str
    approved_artifacts: dict[str, str]
    canonical_model: RequirementModelV2


ApprovedRequirementInput = CanonicalRequirementInput


def read_approved_requirement_input(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    run_id: str,
) -> ApprovedRequirementInput:
    """Read and verify the approved requirement without mutating workflow state."""

    artifacts = _string_dict(state.get("latest_artifacts"))
    return _read_requirement_input(
        store=store,
        state=state,
        run_id=run_id,
        artifacts=artifacts,
        require_current_checkpoint=True,
    )


def read_frozen_requirement_input(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    run_id: str,
) -> ApprovedRequirementInput:
    """Read the immutable requirement input frozen at the Phase 1 approval boundary."""

    artifacts = _string_dict(state.get("approved_artifacts"))
    return _read_requirement_input(
        store=store,
        state=state,
        run_id=run_id,
        artifacts=artifacts,
        require_current_checkpoint=False,
    )


def _read_requirement_input(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    run_id: str,
    artifacts: dict[str, str],
    require_current_checkpoint: bool,
) -> ApprovedRequirementInput:
    contract_version = str(state.get("requirement_contract_version") or "v2")
    if contract_version != "v2":
        raise HarnessError(
            "requirement_contract_unsupported",
            f"Only Requirement Contract V2 is supported; run uses {contract_version!r}.",
            run_id=run_id,
        )

    model_ref = _required_ref(artifacts, "requirement_model", run_id=run_id)
    packet_ref = _required_ref(artifacts, "requirement_packet", run_id=run_id)
    formal_ref = _required_ref(artifacts, "formal_use_case", run_id=run_id)
    _require_artifact_exists(store, model_ref, run_id=run_id)
    model = read_requirement_model_v2(store=store, run_id=run_id, artifact_ref=model_ref)
    if model.run_id != run_id:
        raise HarnessError(
            "requirement_model_v2_invalid",
            "Canonical Requirement V2 belongs to a different workflow run.",
            run_id=run_id,
        )
    if require_current_checkpoint:
        _require_current_v2_checkpoint(state, model_ref=model_ref, revision=model.revision, run_id=run_id)

    source_text = read_source_snapshot_v2(
        store=store,
        run_id=run_id,
        artifact_ref=str(model.source.get("artifact_ref") or ""),
        expected_source_ref=str(model.source.get("source_ref") or ""),
        expected_fingerprint=str(model.source.get("fingerprint") or ""),
    )
    validation = normalize_and_validate_requirement_model_v2(model, source_text)
    if validation.model.to_dict() != model.to_dict():
        raise HarnessError(
            "requirement_model_v2_invalid",
            "Canonical Requirement V2 is not normalized or its validation metadata is stale.",
            run_id=run_id,
        )
    if not validation.report.ok or validation.report.status != "valid" or validation.report.blocking_question_ids:
        raise HarnessError(
            "requirement_approval_blocked",
            "Requirement V2 still has validation errors or blocking clarification questions.",
            run_id=run_id,
        )

    packet = _read_text(store, packet_ref, run_id=run_id)
    formal = _read_text(store, formal_ref, run_id=run_id)
    require_valid_requirement_v2_projections(
        model,
        requirement_packet=packet,
        formal_use_case=formal,
    )
    approved = {
        key: value
        for key, value in artifacts.items()
        if key
        in {
            "requirement_model",
            "requirement_packet",
            "formal_use_case",
            "requirement_validation",
            "requirement_decisions",
        }
    }
    return CanonicalRequirementInput(
        contract_version="v2",
        requirement_packet=packet,
        formal_use_case=formal,
        approved_artifacts=approved,
        canonical_model=model,
    )


def _require_current_v2_checkpoint(
    state: dict[str, object],
    *,
    model_ref: str,
    revision: int,
    run_id: str,
) -> None:
    checkpoint = state.get("pending_checkpoint")
    if not isinstance(checkpoint, dict):
        raise HarnessError("no_pending_checkpoint", f"Run does not have a pending checkpoint: {run_id}", run_id=run_id)
    expected_ref = f"requirement_model_v2_r{revision}"
    if checkpoint.get("artifact_path") != model_ref or checkpoint.get("artifact_ref") != expected_ref:
        raise HarnessError(
            "requirement_revision_mismatch",
            "Requirement approval checkpoint does not reference the latest canonical V2 revision.",
            run_id=run_id,
        )


def _required_ref(artifacts: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifacts.get(key, "")
    if not value:
        raise HarnessError(
            "approved_requirement_not_found",
            f"Run does not have an approved {key} artifact: {run_id}",
            run_id=run_id,
        )
    return value


def _read_text(store: RuntimeStore, artifact_ref: str, *, run_id: str) -> str:
    path = (store.repo_root / artifact_ref).resolve()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(
            "approved_requirement_not_found",
            f"Requirement artifact does not exist or cannot be read: {artifact_ref}",
            run_id=run_id,
        ) from exc


def _require_artifact_exists(store: RuntimeStore, artifact_ref: str, *, run_id: str) -> None:
    path = (store.repo_root / artifact_ref).resolve()
    if not path.is_file():
        raise HarnessError(
            "approved_requirement_not_found",
            f"Requirement artifact does not exist: {artifact_ref}",
            run_id=run_id,
        )


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
