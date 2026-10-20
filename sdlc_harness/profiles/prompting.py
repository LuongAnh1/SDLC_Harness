"""Directive prompt theo language profile cho các agent của SDLC Harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.errors import HarnessError
from .model import LanguageProfile

if TYPE_CHECKING:
    from .snapshot import ProfileSnapshotV1


@dataclass(frozen=True)
class LanguagePromptDirective:
    """Phần hướng dẫn theo ngôn ngữ, tách khỏi invariant của từng agent role."""

    directive_id: str
    language_profile_id: str
    language_name: str
    developer_plan_rules: tuple[str, ...]
    validator_review_rules: tuple[str, ...]

    def developer_plan_context(self) -> dict[str, object]:
        return {
            "prompt_directive_id": self.directive_id,
            "language_profile_id": self.language_profile_id,
            "language_name": self.language_name,
            "rules": list(self.developer_plan_rules),
        }

    def validator_review_context(self) -> dict[str, object]:
        return {
            "prompt_directive_id": self.directive_id,
            "language_profile_id": self.language_profile_id,
            "language_name": self.language_name,
            "rules": list(self.validator_review_rules),
        }


_TYPESCRIPT_PROMPT = LanguagePromptDirective(
    directive_id="typescript_prompt_v1",
    language_profile_id="typescript_v1",
    language_name="TypeScript",
    developer_plan_rules=(
        "Plan TypeScript source files and use TSX only for files that contain JSX.",
        "Use the source and test suffixes supplied by the immutable planning path policy.",
        "Follow module, import, async, and error-handling conventions found in repository evidence.",
        "Do not choose or change component_id or deterministic code targets owned by the harness.",
    ),
    validator_review_rules=(
        "Review source and test targets as TypeScript artifacts under the immutable planning path policy.",
        "Require test intent for observable behavior without inventing a framework absent from repository evidence.",
        "Treat component routing and deterministic code targets as harness-owned inputs.",
    ),
)

_DIRECTIVES = {_TYPESCRIPT_PROMPT.directive_id: _TYPESCRIPT_PROMPT}


def get_prompt_directive(directive_id: str) -> LanguagePromptDirective:
    """Resolve một directive đã đăng ký hoặc fail closed bằng error code ổn định."""

    try:
        return _DIRECTIVES[directive_id]
    except KeyError as exc:
        raise HarnessError(
            "prompt_directive_not_supported",
            f"Prompt directive is not supported: {directive_id}",
        ) from exc


def resolve_language_prompt_directive(language: LanguageProfile) -> LanguagePromptDirective:
    """Resolve directive và chặn mapping sai language profile."""

    directive = get_prompt_directive(language.prompt_directive_id)
    if directive.language_profile_id != language.profile_id:
        raise HarnessError(
            "prompt_directive_mismatch",
            (
                f"Prompt directive {directive.directive_id} belongs to "
                f"{directive.language_profile_id}, not {language.profile_id}."
            ),
        )
    return directive


def prompt_directive_from_snapshot(
    snapshot: ProfileSnapshotV1,
    *,
    run_id: str | None = None,
) -> LanguagePromptDirective:
    """Resolve directive từ frozen run snapshot thay vì nhận lựa chọn từ LLM."""

    _, language = snapshot.resolve_profiles(run_id=run_id)
    try:
        return resolve_language_prompt_directive(language)
    except HarnessError as exc:
        if run_id and exc.run_id is None:
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        raise
