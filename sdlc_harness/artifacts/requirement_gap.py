"""Đọc và ghi artifact phân loại khoảng trống Requirement của một run."""

from __future__ import annotations

import json

from ..agents.requirements.gap_report import validate_requirement_gap_report
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore


GAP_REPORT_FILENAME = "requirement_gap_report_v1.json"


def write_requirement_gap_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
) -> str:
    validate_requirement_gap_report(report, run_id=run_id)
    path = store.write_json_artifact(run_id, GAP_REPORT_FILENAME, report)
    return store.relative(path)


def read_requirement_gap_report(
    *,
    store: RuntimeStore,
    run_id: str,
    artifact_ref: str,
) -> dict[str, object]:
    path = (store.repo_root / artifact_ref).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "requirement_gap_report_invalid",
            f"Cannot read Requirement gap report: {artifact_ref}",
            run_id=run_id,
        ) from exc
    validate_requirement_gap_report(payload, run_id=run_id)
    return payload
