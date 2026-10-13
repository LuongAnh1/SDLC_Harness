"""Designer Agent cho Phase 2.

Module này học theo TraceDev ở hai bước: `analysis` tạo khuyến nghị kiến trúc
ngắn, còn `act` sinh PlantUML class/sequence diagram từ requirement đã duyệt.
Nó không ghi artifact và không quyết định workflow state; PhaseTwoWorkflow sở
hữu checkpoint, state và sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.errors import HarnessError
from ...llm.client import LlmClient

_PROMPT_DIR = Path(__file__).with_name("prompts")
_RECOMMENDATION_PROMPT_PATH = _PROMPT_DIR / "recommendation.txt"
_DESIGNER_PROMPT_PATH = _PROMPT_DIR / "designer.txt"


@dataclass(frozen=True)
class DesignDraft:
    """Kết quả thô của Designer trước khi parser/Validator xử lý."""

    analysis_report: str
    design_model: str


class DesignerAgent:
    """Vai Designer trong luồng requirement -> design."""

    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def analysis(self, *, requirement_packet: str, formal_use_case: str = "") -> str:
        """Sinh khuyến nghị kiến trúc ngắn từ requirement đã duyệt."""

        use_case_input = _combined_requirement_input(
            requirement_packet=requirement_packet,
            formal_use_case=formal_use_case,
        )
        prompt = _RECOMMENDATION_PROMPT_PATH.read_text(encoding="utf-8").replace(
            "{Use Case}",
            use_case_input,
        )
        try:
            return self.llm_client.complete(
                system="You are Designer.analysis. Recommend architecture briefly.",
                prompt=prompt,
            ).strip()
        except HarnessError as exc:
            raise HarnessError("designer_failed", exc.message) from exc
        except Exception as exc:
            raise HarnessError("designer_failed", str(exc)) from exc

    def act(self, *, requirement_packet: str, formal_use_case: str, analysis_report: str) -> str:
        """Sinh artifact thiết kế PlantUML class/sequence diagram."""

        use_case_input = _combined_requirement_input(
            requirement_packet=requirement_packet,
            formal_use_case=formal_use_case,
        )
        prompt = _DESIGNER_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{UC}", use_case_input)
        prompt = prompt.replace("{architect_report}", analysis_report)
        try:
            return self.llm_client.complete(
                system="You are Designer.act. Return only the requested PlantUML design artifact.",
                prompt=prompt,
            ).strip()
        except HarnessError as exc:
            raise HarnessError("designer_failed", exc.message) from exc
        except Exception as exc:
            raise HarnessError("designer_failed", str(exc)) from exc

    def draft(self, *, requirement_packet: str, formal_use_case: str = "") -> DesignDraft:
        """Chạy đủ hai bước Designer để workflow nhận một draft hoàn chỉnh."""

        analysis_report = self.analysis(
            requirement_packet=requirement_packet,
            formal_use_case=formal_use_case,
        )
        design_model = self.act(
            requirement_packet=requirement_packet,
            formal_use_case=formal_use_case,
            analysis_report=analysis_report,
        )
        return DesignDraft(analysis_report=analysis_report, design_model=design_model)


def _combined_requirement_input(*, requirement_packet: str, formal_use_case: str) -> str:
    """Ghép input người đọc được và formal use case máy đọc được cho Designer."""

    parts = ["# Requirement Packet", requirement_packet.strip()]
    if formal_use_case.strip():
        parts.extend(["", "# Formal Use Case", formal_use_case.strip()])
    return "\n".join(parts).strip()
