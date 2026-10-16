"""Revisioned artifact bundle for the canonical Requirement Model V2."""

from __future__ import annotations

from pathlib import Path

from ..agents.requirements.projection_v2 import require_valid_requirement_v2_projections
from ..agents.requirements.refiner_v2 import RequirementModelV2RefinementResult
from ..agents.requirements.renderer_v2 import render_formal_use_case_v2, render_requirement_packet_v2
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore, utc_now_iso


def write_requirement_v2_artifacts(
    *,
    store: RuntimeStore,
    run_id: str,
    refinement_result: RequirementModelV2RefinementResult,
) -> dict[str, str]:
    """Write the canonical model, deterministic projections, and audit report."""

    model = refinement_result.model
    report = refinement_result.final_report
    if model.run_id != run_id:
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            f"Requirement model run_id {model.run_id!r} does not match writer run_id {run_id!r}.",
            run_id=run_id,
        )
    if model.revision < 1:
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            "Requirement model revision must start at 1.",
            run_id=run_id,
        )
    if not report.ok or report.status not in {"valid", "clarification_required"}:
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

    revision = model.revision
    packet = render_requirement_packet_v2(model)
    formal = render_formal_use_case_v2(model)
    projection_report = require_valid_requirement_v2_projections(
        model,
        requirement_packet=packet,
        formal_use_case=formal,
    )

    model_path = store.write_json_artifact(
        run_id,
        f"requirement_model_v2_r{revision}.json",
        model.to_dict(),
    )
    packet_path = store.artifact_path(run_id, f"requirement_packet_v2_r{revision}.md")
    formal_path = store.artifact_path(run_id, f"formal_use_case_v2_r{revision}.txt")
    _write_text(packet_path, packet, run_id=run_id)
    _write_text(formal_path, formal, run_id=run_id)
    validation_path = store.write_json_artifact(
        run_id,
        f"requirement_validation_v2_r{revision}.json",
        {
            "artifact_version": "requirement_validation_v2",
            "run_id": run_id,
            "revision": revision,
            "created_at": utc_now_iso(),
            **refinement_result.to_report_dict(),
            "projection_report": projection_report.to_dict(),
        },
    )
    return {
        "requirement_model": store.relative(model_path),
        "requirement_packet": store.relative(packet_path),
        "formal_use_case": store.relative(formal_path),
        "requirement_validation": store.relative(validation_path),
    }


def _write_text(path: Path, content: str, *, run_id: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise HarnessError("runtime_write_failed", str(exc), run_id=run_id) from exc
