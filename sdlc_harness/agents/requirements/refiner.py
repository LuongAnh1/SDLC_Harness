"""Requirement Refiner cho Phase 1.

Vai trò này được tách thành hai output: `formal_use_case_v1` học trực tiếp từ
TraceDev grammar để feed các agent phía sau, và `requirement_packet_v1` là bản
dễ đọc để Niko/người dùng duyệt ở checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ...core.errors import HarnessError
from ...llm.client import LlmClient
from ...sources.fixtures import UseCaseSource
from .validator import (
    FormalCheckReport,
    FormalUseCaseValidator,
    RequirementValidator,
    ValidationReport,
    normalize_formal_use_case,
)

_PROMPT_DIR = Path(__file__).with_name("prompts")
_PACKET_PROMPT_PATH = _PROMPT_DIR / "requirement_packet_v1.md"
_PACKET_REPAIR_PROMPT_PATH = _PROMPT_DIR / "requirement_packet_repair_v1.md"
_FORMAL_PROMPT_PATH = _PROMPT_DIR / "formal_use_case_v1.txt"
_FORMAL_REPAIR_PROMPT_PATH = _PROMPT_DIR / "formal_use_case_repair_v1.txt"


@dataclass(frozen=True)
class FormalRefinementResult:
    """Kết quả refiner formal use case, gồm cả vòng repair nếu có."""

    initial_use_case: str
    final_use_case: str
    initial_report: FormalCheckReport
    final_report: FormalCheckReport
    repaired: bool

    def to_report_dict(self) -> dict[str, object]:
        return {
            "repaired": self.repaired,
            "initial_report": self.initial_report.to_dict(),
            "final_report": self.final_report.to_dict(),
        }


@dataclass(frozen=True)
class RequirementPacketRefinementResult:
    """Kết quả sinh requirement packet, gồm vòng repair nếu validator bắt lỗi."""

    initial_packet: str
    final_packet: str
    initial_report: ValidationReport
    final_report: ValidationReport
    repaired: bool

    def to_report_dict(self) -> dict[str, object]:
        return {
            "repaired": self.repaired,
            "initial_report": self.initial_report.to_dict(),
            "final_report": self.final_report.to_dict(),
        }


class RequirementRefiner:
    """Agent chuẩn hóa requirement theo hai artifact của Phase 1.

    Refiner sinh formal use case kiểu TraceDev cho execution graph về sau và
    requirement packet dễ đọc cho người duyệt. Validator có thể yêu cầu repair
    một vòng, nhưng module này không ghi artifact hay sync tool.
    """

    def __init__(
        self,
        llm_client: LlmClient,
        formal_validator: FormalUseCaseValidator | None = None,
        requirement_validator: RequirementValidator | None = None,
    ):
        self.llm_client = llm_client
        self.formal_validator = formal_validator or FormalUseCaseValidator()
        self.requirement_validator = requirement_validator or RequirementValidator()

    def refine_formal_use_case(self, source: UseCaseSource) -> FormalRefinementResult:
        """Sinh formal use case theo grammar TraceDev và repair một vòng nếu sai."""

        prompt = _FORMAL_PROMPT_PATH.read_text(encoding="utf-8").replace("{OriginalUseCase}", source.text)
        try:
            initial = normalize_formal_use_case(
                self.llm_client.complete(
                    system="You are Requirement Refiner. Follow the formal use case grammar exactly.",
                    prompt=prompt,
                )
            )
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - chặn exception lạ từ adapter.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc

        initial_report = self.formal_validator.validate(initial)
        if initial_report.ok:
            return FormalRefinementResult(
                initial_use_case=initial,
                final_use_case=initial,
                initial_report=initial_report,
                final_report=initial_report,
                repaired=False,
            )

        repaired = self._repair_formal_use_case(
            source=source,
            formal_check_report=initial_report,
            reconstructed_use_case=initial,
        )
        final_report = self.formal_validator.validate(repaired)
        if not final_report.ok:
            raise HarnessError(
                "formal_use_case_validator_failed",
                "Formal use case failed validation after repair: " + "; ".join(final_report.issues),
            )
        return FormalRefinementResult(
            initial_use_case=initial,
            final_use_case=repaired,
            initial_report=initial_report,
            final_report=final_report,
            repaired=True,
        )

    def refine(self, source: UseCaseSource, *, formal_use_case: str | None = None) -> str:
        """Sinh requirement packet human-readable từ source và formal use case."""

        system = (
            "You are a senior requirements analyst. Normalize a raw software "
            "use case into a concise requirement packet. Do not invent facts. "
            "If information is missing, put it in Open Questions."
        )
        prompt = _PACKET_PROMPT_PATH.read_text(encoding="utf-8").format(
            dataset=source.dataset,
            use_case=source.use_case,
            source_text=source.text,
            formal_use_case=formal_use_case or "Not generated.",
        )
        try:
            refined = self.llm_client.complete(system=system, prompt=prompt)
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - chặn exception lạ từ adapter.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc
        return refined.strip()

    def refine_requirement_packet(
        self,
        source: UseCaseSource,
        *,
        formal_use_case: str | None = None,
    ) -> RequirementPacketRefinementResult:
        """Sinh requirement packet và repair một vòng nếu validator bắt lỗi."""

        initial = self.refine(source, formal_use_case=formal_use_case)
        initial_report = self.requirement_validator.validate(initial)
        if initial_report.ok:
            return RequirementPacketRefinementResult(
                initial_packet=initial,
                final_packet=initial,
                initial_report=initial_report,
                final_report=initial_report,
                repaired=False,
            )

        repaired = self._repair_requirement_packet(
            source=source,
            formal_use_case=formal_use_case or "Not generated.",
            requirement_packet=initial,
            validation_report=initial_report,
        )
        final_report = self.requirement_validator.validate(repaired)
        if not final_report.ok:
            raise HarnessError(
                "requirement_validator_failed",
                "Requirement packet failed validation after repair: " + "; ".join(final_report.issues),
            )
        return RequirementPacketRefinementResult(
            initial_packet=initial,
            final_packet=repaired,
            initial_report=initial_report,
            final_report=final_report,
            repaired=True,
        )

    def _repair_formal_use_case(
        self,
        *,
        source: UseCaseSource,
        formal_check_report: FormalCheckReport,
        reconstructed_use_case: str,
    ) -> str:
        prompt = _FORMAL_REPAIR_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{OriginalUseCase}", source.text)
        prompt = prompt.replace("{FormalCheckResult}", json.dumps(formal_check_report.to_dict(), ensure_ascii=False))
        prompt = prompt.replace("{ReconstructedUseCase}", reconstructed_use_case)
        try:
            return normalize_formal_use_case(
                self.llm_client.complete(
                    system="You are RequirementRefiner. Repair the formal use case only.",
                    prompt=prompt,
                )
            )
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - chặn exception lạ từ adapter.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc

    def _repair_requirement_packet(
        self,
        *,
        source: UseCaseSource,
        formal_use_case: str,
        requirement_packet: str,
        validation_report: ValidationReport,
    ) -> str:
        prompt = _PACKET_REPAIR_PROMPT_PATH.read_text(encoding="utf-8")
        prompt = prompt.replace("{dataset}", source.dataset)
        prompt = prompt.replace("{use_case}", source.use_case)
        prompt = prompt.replace("{source_text}", source.text)
        prompt = prompt.replace("{formal_use_case}", formal_use_case)
        prompt = prompt.replace("{RequirementValidationReport}", json.dumps(validation_report.to_dict(), ensure_ascii=False))
        prompt = prompt.replace("{RequirementPacket}", requirement_packet)
        try:
            return self.llm_client.complete(
                system="You are Requirement Refiner. Repair the requirement packet only.",
                prompt=prompt,
            ).strip()
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - chặn exception lạ từ adapter.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc
