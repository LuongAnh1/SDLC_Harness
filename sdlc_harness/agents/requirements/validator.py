"""Validator cho requirement artifacts ở checkpoint Phase 1.

`FormalUseCaseValidator` là bản tối giản học từ TraceDev: kiểm tra use case theo
các keyword/khối bắt buộc trước khi feed sang Designer. `RequirementValidator`
kiểm tra bản human-readable packet để Niko/người dùng duyệt.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from ...core.errors import HarnessError


REQUIRED_SECTIONS = (
    "## Goal",
    "## Actors",
    "## Preconditions",
    "## Main Flow",
    "## Postconditions / Acceptance Criteria",
    "## Open Questions",
    "## Source Evidence",
)

FORMAL_SECTION_HEADERS = (
    "UsecaseName:",
    "Description:",
    "Actors:",
    "Entry Conditions:",
    "Flow of Events:",
    "Exit Conditions:",
    "Quality Requirement:",
)


@dataclass(frozen=True)
class ValidationReport:
    """Kết quả kiểm tra requirement packet trước checkpoint."""

    ok: bool
    issues: list[str]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "issues": self.issues}


@dataclass(frozen=True)
class FormalCheckReport:
    """Kết quả kiểm tra formal use case theo grammar tối thiểu."""

    ok: bool
    issues: list[str]
    section_presence: dict[str, bool]
    flow_step_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "section_presence": self.section_presence,
            "flow_step_count": self.flow_step_count,
        }


def normalize_formal_use_case(text: str) -> str:
    """Bỏ wrapper thường gặp từ LLM để giữ lại use case thuần."""

    stripped = text.strip()
    prefixes = (
        "- ReconstructedUseCase:",
        "ReconstructedUseCase:",
        "- UpdatedUseCase:",
        "UpdatedUseCase:",
    )
    for prefix in prefixes:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
    return stripped


class FormalUseCaseValidator:
    """Parser/validator nhẹ cho formal use case kiểu TraceDev."""

    def validate(self, formal_use_case: str) -> FormalCheckReport:
        text = normalize_formal_use_case(formal_use_case)
        lines = [line.rstrip() for line in text.splitlines()]
        issues: list[str] = []
        section_presence = {header: any(line.startswith(header) for line in lines) for header in FORMAL_SECTION_HEADERS}
        for header, present in section_presence.items():
            if not present:
                issues.append(f"Missing required formal section: {header}")

        blocks = self._blocks(lines)
        for header in FORMAL_SECTION_HEADERS:
            if header in {"UsecaseName:", "Description:", "Actors:"}:
                line_value = self._inline_value(lines, header)
                if section_presence[header] and not line_value:
                    issues.append(f"Formal section must not be empty: {header}")
            elif section_presence.get(header) and not blocks.get(header):
                issues.append(f"Formal block must not be empty: {header}")

        entry_conditions = blocks.get("Entry Conditions:", [])
        for condition in entry_conditions:
            if not re.search(r"\b(IS|HAS)\b", condition):
                issues.append(
                    "Entry condition must use TraceDev grammar form '<Entity> IS <State>' "
                    "or '<Entity> HAS <Attribute>': " + condition
                )

        flow_steps = blocks.get("Flow of Events:", [])
        step_numbers: list[int] = []
        for step in flow_steps:
            match = re.match(r"^(\d+)\.\s+\S", step)
            if not match:
                issues.append("Flow step must start with '<number>. <description>': " + step)
                continue
            step_numbers.append(int(match.group(1)))
        expected = list(range(1, len(step_numbers) + 1))
        if step_numbers and step_numbers != expected:
            issues.append(f"Flow step numbering must be continuous from 1: {step_numbers}")
        if not step_numbers:
            issues.append("Flow of Events must contain at least one numbered step.")

        return FormalCheckReport(
            ok=not issues,
            issues=issues,
            section_presence=section_presence,
            flow_step_count=len(step_numbers),
        )

    def require_valid(self, formal_use_case: str) -> FormalCheckReport:
        report = self.validate(formal_use_case)
        if not report.ok:
            raise HarnessError(
                "formal_use_case_validator_failed",
                "Formal use case failed validation: " + "; ".join(report.issues),
            )
        return report

    def _blocks(self, lines: list[str]) -> dict[str, list[str]]:
        blocks: dict[str, list[str]] = {}
        current: str | None = None
        block_headers = {"Entry Conditions:", "Flow of Events:", "Exit Conditions:", "Quality Requirement:"}
        all_headers = set(FORMAL_SECTION_HEADERS)
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            matched_header = next((header for header in FORMAL_SECTION_HEADERS if line.startswith(header)), None)
            if matched_header:
                current = matched_header if matched_header in block_headers else None
                if current:
                    inline = line[len(matched_header):].strip()
                    blocks.setdefault(current, [])
                    if inline:
                        blocks[current].append(inline)
                continue
            if current and not any(line.startswith(header) for header in all_headers):
                blocks.setdefault(current, []).append(line)
        return blocks

    def _inline_value(self, lines: list[str], header: str) -> str:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(header):
                return stripped[len(header):].strip()
        return ""


class RequirementValidator:
    """Validator tối thiểu để ngăn packet rỗng/sai format đi vào checkpoint."""

    def validate(self, requirement_packet: str) -> ValidationReport:
        issues: list[str] = []
        for section in REQUIRED_SECTIONS:
            if section not in requirement_packet:
                issues.append(f"Missing required section: {section}")
        if not re.search(r"(?m)^\s*1[\.)]\s+", requirement_packet):
            issues.append("Main Flow must contain at least one numbered step.")
        if "## Source Evidence" in requirement_packet:
            evidence = requirement_packet.split("## Source Evidence", 1)[1].strip()
            if not evidence:
                issues.append("Source Evidence must not be empty.")
        if "## Open Questions" in requirement_packet:
            open_questions = requirement_packet.split("## Open Questions", 1)[1]
            open_questions = open_questions.split("## Source Evidence", 1)[0].strip()
            if not open_questions:
                issues.append("Open Questions must exist; use 'None' only when source is complete.")
        return ValidationReport(ok=not issues, issues=issues)

    def require_valid(self, requirement_packet: str) -> ValidationReport:
        """Trả report nếu hợp lệ, hoặc raise lỗi JSON-friendly nếu không hợp lệ."""

        report = self.validate(requirement_packet)
        if not report.ok:
            raise HarnessError(
                "requirement_validator_failed",
                "Requirement packet failed validation: " + "; ".join(report.issues),
            )
        return report
