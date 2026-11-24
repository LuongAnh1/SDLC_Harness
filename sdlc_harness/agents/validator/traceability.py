"""Validator traceability cho Phase 2.

Module này là lát đầu của Validator theo tinh thần TraceDev: requirement đã
duyệt được chuyển thành graph máy đọc được, class/sequence graph từ Designer
được dùng làm miền target hợp lệ, rồi LLM chỉ được phép tạo link vào các target
đã tồn tại. Repair loop sẽ dùng missing links ở lát sau.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..requirements import RequirementModelV2
from ...core.errors import HarnessError




@dataclass(frozen=True)
class TraceabilityResult:
    """Kết quả traceability đã normalize và đã tính missing links."""

    requirement_graph: dict[str, object]
    class_trace: list[dict[str, object]]
    sequence_trace: list[dict[str, object]]
    missing_links: dict[str, list[dict[str, str]]]
    mode: str = "traceability_v1"

    @property
    def ok(self) -> bool:
        return not self.missing_links["class_diagram"] and not self.missing_links["sequence_diagram"]


def build_requirement_graph_v2(model: RequirementModelV2) -> dict[str, object]:
    """Build traceability input from canonical V2 items while preserving stable IDs."""

    requirements: list[dict[str, str]] = []
    confirmed_statuses = {"explicit", "human_decision"}
    for section, items in (("Preconditions", model.preconditions),):
        for item in items:
            if item.get("status") not in confirmed_statuses:
                continue
            _append_v2_requirement(requirements, section=section, item=item, content=str(item.get("text") or ""))

    flows = model.flows
    for item in flows.get("main", []):
        if not isinstance(item, dict):
            continue
        content = " ".join(
            part
            for part in (
                str(item.get("actor") or ""),
                str(item.get("action") or ""),
                str(item.get("target") or ""),
            )
            if part
        )
        _append_v2_requirement(requirements, section="Main Flow", item=item, content=content)
    for group, section in (("alternatives", "Alternative Flows"), ("exceptions", "Exception Flows")):
        for item in flows.get(group, []):
            if not isinstance(item, dict):
                continue
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            content = " ".join(
                part
                for part in (
                    str(item.get("trigger") or ""),
                    " ".join(str(step) for step in steps),
                    str(item.get("outcome") or ""),
                )
                if part
            )
            _append_v2_requirement(requirements, section=section, item=item, content=content)

    for section, items in (
        ("Postconditions", model.postconditions),
        ("Business Rules", model.business_rules),
        ("Acceptance Criteria", model.acceptance_criteria),
        ("Quality Requirements", model.quality_requirements),
    ):
        for item in items:
            if item.get("status") not in confirmed_statuses:
                continue
            _append_v2_requirement(requirements, section=section, item=item, content=str(item.get("text") or ""))

    if not requirements:
        raise HarnessError(
            "validator_traceability_failed",
            "Canonical Requirement V2 did not contain confirmed traceable requirement items.",
            run_id=model.run_id,
        )
    return {
        "version": "requirement_graph_v2",
        "requirement_contract_version": "v2",
        "schema_version": model.schema_version,
        "revision": model.revision,
        "requirements": requirements,
        "count": len(requirements),
    }


def _append_v2_requirement(
    requirements: list[dict[str, str]],
    *,
    section: str,
    item: dict[str, Any],
    content: str,
) -> None:
    requirement_id = str(item.get("id") or "").strip()
    content = content.strip()
    if requirement_id and content:
        requirements.append({"id": requirement_id, "section": section, "content": content})


def _section_items(section_text: str) -> list[str]:
    items: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or line.startswith("#"):
            continue
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.lstrip("- ").strip()
        if line:
            items.append(line)
    return items

