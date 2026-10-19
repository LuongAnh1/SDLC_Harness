"""Kiểm tra chéo projection cho các view tất định của Requirement Model V2."""

from __future__ import annotations

from dataclasses import dataclass

from ...core.errors import HarnessError
from .model_v2 import RequirementModelV2
from .renderer_v2 import render_formal_use_case_v2, render_requirement_packet_v2


@dataclass(frozen=True)
class RequirementProjectionV2Report:
    ok: bool
    issues: list[str]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "issues": list(self.issues)}


def validate_requirement_v2_projections(
    model: RequirementModelV2,
    *,
    requirement_packet: str,
    formal_use_case: str,
) -> RequirementProjectionV2Report:
    """Compare supplied projections byte-for-byte with canonical renderer output."""

    issues: list[str] = []
    if requirement_packet != render_requirement_packet_v2(model):
        issues.append("requirement_packet_v2 does not match the canonical model projection.")
    if formal_use_case != render_formal_use_case_v2(model):
        issues.append("formal_use_case_v2 does not match the canonical model projection.")
    return RequirementProjectionV2Report(ok=not issues, issues=issues)


def require_valid_requirement_v2_projections(
    model: RequirementModelV2,
    *,
    requirement_packet: str,
    formal_use_case: str,
) -> RequirementProjectionV2Report:
    report = validate_requirement_v2_projections(
        model,
        requirement_packet=requirement_packet,
        formal_use_case=formal_use_case,
    )
    if not report.ok:
        raise HarnessError("requirement_projection_mismatch", "; ".join(report.issues), run_id=model.run_id)
    return report
