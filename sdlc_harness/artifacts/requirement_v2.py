"""Artifact bất biến, có revision cho Requirement Model V2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ..agents.requirements.model_v2 import RequirementModelV2
from ..agents.requirements.projection_v2 import require_valid_requirement_v2_projections
from ..agents.requirements.refiner_v2 import RequirementModelV2RefinementResult
from ..agents.requirements.renderer_v2 import render_formal_use_case_v2, render_requirement_packet_v2
from ..agents.requirements.validator_v2 import RequirementModelV2ValidationReport
from ..core.errors import HarnessError
from ..sources.fixtures import UseCaseSource, source_provenance_entry
from ..storage.runtime import RuntimeStore, utc_now_iso


SOURCE_SNAPSHOT_VERSION = "source_snapshot_v2"


def write_source_snapshot_v2(*, store: RuntimeStore, run_id: str, source: UseCaseSource) -> str:
    path = _source_snapshot_path(store=store, run_id=run_id, source_id=source.source_id)
    artifact_ref = store.relative(path)
    provenance = source_provenance_entry(source=source, artifact_ref=artifact_ref)
    payload: dict[str, object] = {
        "artifact_version": SOURCE_SNAPSHOT_VERSION,
        "run_id": run_id,
        "dataset": source.dataset,
        "use_case": source.use_case,
        **provenance,
        "text": source.text,
    }
    _write_json_immutable(path, payload, run_id=run_id)
    return artifact_ref


def read_source_snapshot_v2(
    *,
    store: RuntimeStore,
    run_id: str,
    expected_source: dict[str, Any],
) -> str:
    source_id = str(expected_source.get("id") or "")
    artifact_ref = str(expected_source.get("artifact_ref") or "")
    path = (store.repo_root / artifact_ref).resolve()
    expected_path = _source_snapshot_path(store=store, run_id=run_id, source_id=source_id).resolve()
    if path != expected_path:
        raise HarnessError(
            "source_snapshot_corrupted",
            "Requirement V2 source snapshot path does not match its stable source ID.",
            run_id=run_id,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "source_snapshot_corrupted",
            f"Cannot read Requirement V2 source snapshot: {artifact_ref}",
            run_id=run_id,
        ) from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    expected_fields = ("id", "type", "source_ref", "locator", "revision", "artifact_ref", "fingerprint")
    if (
        not isinstance(text, str)
        or payload.get("artifact_version") != SOURCE_SNAPSHOT_VERSION
        or payload.get("run_id") != run_id
        or any(payload.get(field) != expected_source.get(field) for field in expected_fields)
        or _fingerprint(text) != expected_source.get("fingerprint")
    ):
        raise HarnessError(
            "source_snapshot_corrupted",
            "Requirement V2 source snapshot identity or fingerprint does not match the canonical model.",
            run_id=run_id,
        )
    return text


def load_source_snapshot_v2(
    *,
    store: RuntimeStore,
    run_id: str,
    artifact_ref: str,
) -> UseCaseSource:
    """Khôi phục source từ snapshot bất biến, không gọi lại adapter bên ngoài."""

    path = (store.repo_root / artifact_ref).resolve()
    artifacts_dir = store.artifact_path(run_id, "placeholder.json").parent.resolve()
    if path.parent != artifacts_dir:
        raise HarnessError(
            "source_snapshot_corrupted",
            "Requirement source snapshot is outside its owning run.",
            run_id=run_id,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "source_snapshot_corrupted",
            "Requirement source snapshot is missing or unreadable.",
            run_id=run_id,
        ) from exc
    if not isinstance(payload, dict):
        raise HarnessError("source_snapshot_corrupted", "Requirement source snapshot is invalid.", run_id=run_id)
    source_id = str(payload.get("id") or "")
    expected_path = _source_snapshot_path(store=store, run_id=run_id, source_id=source_id).resolve()
    text = payload.get("text")
    required_text = ("dataset", "use_case", "source_ref", "type", "locator", "fingerprint")
    if (
        path != expected_path
        or payload.get("artifact_version") != SOURCE_SNAPSHOT_VERSION
        or payload.get("run_id") != run_id
        or payload.get("artifact_ref") != artifact_ref
        or not isinstance(text, str)
        or not source_id
        or any(not isinstance(payload.get(name), str) for name in required_text)
        or _fingerprint(text) != payload.get("fingerprint")
    ):
        raise HarnessError(
            "source_snapshot_corrupted",
            "Requirement source snapshot identity or fingerprint is invalid.",
            run_id=run_id,
        )
    revision = payload.get("revision")
    if revision is not None and not isinstance(revision, str):
        raise HarnessError("source_snapshot_corrupted", "Requirement source revision is invalid.", run_id=run_id)
    return UseCaseSource(
        dataset=str(payload["dataset"]),
        use_case=str(payload["use_case"]),
        source_ref=str(payload["source_ref"]),
        source_path=None,
        text=text,
        source_id=source_id,
        source_type=str(payload["type"]),
        locator=str(payload["locator"]),
        revision=revision,
    )


def read_requirement_source_texts(
    *,
    store: RuntimeStore,
    run_id: str,
    model: RequirementModelV2,
) -> dict[str, str]:
    """Đọc và xác minh toàn bộ immutable source snapshots được canonical model tham chiếu."""

    ledger = model.provenance_ledger
    sources = ledger.get("sources") if isinstance(ledger, dict) else None
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise HarnessError(
            "requirement_model_v2_invalid",
            "Requirement provenance ledger does not contain a valid sources list.",
            run_id=run_id,
        )
    return {
        str(source.get("id") or ""): read_source_snapshot_v2(
            store=store,
            run_id=run_id,
            expected_source=source,
        )
        for source in sources
    }


def write_requirement_v2_artifacts(
    *,
    store: RuntimeStore,
    run_id: str,
    refinement_result: RequirementModelV2RefinementResult,
) -> dict[str, str]:
    return write_requirement_v2_revision_artifacts(
        store=store,
        run_id=run_id,
        model=refinement_result.model,
        validation_report=refinement_result.final_report,
        audit=refinement_result.to_report_dict(),
    )


def write_requirement_v2_revision_artifacts(
    *,
    store: RuntimeStore,
    run_id: str,
    model: RequirementModelV2,
    validation_report: RequirementModelV2ValidationReport,
    audit: dict[str, object],
) -> dict[str, str]:
    """Write one immutable model/projection/decision revision bundle."""

    _require_writable_model(model, validation_report, run_id=run_id)
    revision = model.revision
    packet = render_requirement_packet_v2(model)
    formal = render_formal_use_case_v2(model)
    projection_report = require_valid_requirement_v2_projections(
        model,
        requirement_packet=packet,
        formal_use_case=formal,
    )
    model_path = store.artifact_path(run_id, f"requirement_model_v2_r{revision}.json")
    packet_path = store.artifact_path(run_id, f"requirement_packet_v2_r{revision}.md")
    formal_path = store.artifact_path(run_id, f"formal_use_case_v2_r{revision}.txt")
    validation_path = store.artifact_path(run_id, f"requirement_validation_v2_r{revision}.json")
    decisions_path = store.artifact_path(run_id, f"requirement_decisions_v2_r{revision}.json")
    created_at = utc_now_iso()

    _write_json_immutable(model_path, model.to_dict(), run_id=run_id)
    _write_text_immutable(packet_path, packet, run_id=run_id)
    _write_text_immutable(formal_path, formal, run_id=run_id)
    _write_json_immutable(
        validation_path,
        {
            "artifact_version": "requirement_validation_v2",
            "run_id": run_id,
            "revision": revision,
            "created_at": created_at,
            **audit,
            "final_report": validation_report.to_dict(),
            "projection_report": projection_report.to_dict(),
        },
        run_id=run_id,
        ignore_created_at=True,
    )
    _write_json_immutable(
        decisions_path,
        {
            "artifact_version": "requirement_decisions_v2",
            "run_id": run_id,
            "revision": revision,
            "created_at": created_at,
            "decisions": model.to_dict()["decisions"],
        },
        run_id=run_id,
        ignore_created_at=True,
    )
    return {
        "requirement_model": store.relative(model_path),
        "requirement_packet": store.relative(packet_path),
        "formal_use_case": store.relative(formal_path),
        "requirement_validation": store.relative(validation_path),
        "requirement_decisions": store.relative(decisions_path),
    }


def read_requirement_model_v2(*, store: RuntimeStore, run_id: str, artifact_ref: str) -> RequirementModelV2:
    path = (store.repo_root / artifact_ref).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RequirementModelV2.from_dict(payload)
    except HarnessError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "requirement_model_v2_invalid",
            f"Cannot read Requirement Model V2 artifact: {artifact_ref}",
            run_id=run_id,
        ) from exc


def _require_writable_model(
    model: RequirementModelV2,
    report: RequirementModelV2ValidationReport,
    *,
    run_id: str,
) -> None:
    if model.run_id != run_id:
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            f"Requirement model run_id {model.run_id!r} does not match writer run_id {run_id!r}.",
            run_id=run_id,
        )
    if model.revision < 1 or not report.ok or report.status not in {"valid", "clarification_required"}:
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            "Cannot write artifacts for an invalid Requirement Model V2.",
            run_id=run_id,
        )
    if model.validation.get("status") != report.status:
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            "Canonical model validation status does not match the final report.",
            run_id=run_id,
        )


def _write_text_immutable(path: Path, content: str, *, run_id: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") == content:
                return
            raise HarnessError(
                "requirement_revision_conflict",
                f"Requirement revision artifact already exists with different content: {path.name}",
                run_id=run_id,
            )
        path.write_text(content, encoding="utf-8")
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("runtime_write_failed", str(exc), run_id=run_id) from exc


def _write_json_immutable(
    path: Path,
    data: dict[str, object],
    *,
    run_id: str,
    ignore_created_at: bool = False,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            expected: Any = data
            if ignore_created_at and isinstance(current, dict):
                current = {key: value for key, value in current.items() if key != "created_at"}
                expected = {key: value for key, value in data.items() if key != "created_at"}
            if current == expected:
                return
            raise HarnessError(
                "requirement_revision_conflict",
                f"Requirement revision artifact already exists with different content: {path.name}",
                run_id=run_id,
            )
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except HarnessError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("runtime_write_failed", str(exc), run_id=run_id) from exc


def _fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_snapshot_path(*, store: RuntimeStore, run_id: str, source_id: str) -> Path:
    if re.fullmatch(r"SRC-[1-9]\d*", source_id) is None:
        raise HarnessError(
            "source_snapshot_corrupted",
            f"Invalid Requirement V2 source ID: {source_id!r}.",
            run_id=run_id,
        )
    return store.artifact_path(run_id, f"source_snapshot_{source_id}_v2.json")
