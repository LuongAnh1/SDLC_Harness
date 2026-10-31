"""LLM adapter tối giản cho SDLC Harness.

Phase đầu có hai chế độ: `fake` để test không tốn API và `deepseek`/
`openai_compatible` để gọi API thật qua endpoint Chat Completions tương thích
OpenAI.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import http.client
import json
from typing import Any
import urllib.error
import urllib.request
import time

from ..core.config import Settings
from ..core.errors import HarnessError
from ..events import emit_current


@dataclass(frozen=True)
class LlmCompletion:
    """Nội dung cùng metadata cần để chẩn đoán output bị cắt hoặc sai schema."""

    content: str
    finish_reason: str = ""
    usage: dict[str, object] = field(default_factory=dict)
    reasoning_content: str = ""


class BudgetedLlmClient:
    """Count and hard-limit calls while preserving the LlmClient interface."""

    def __init__(self, base: Any, *, limit: int):
        self.base = base
        self.settings = base.settings
        self.limit = max(1, int(limit))
        self.calls: list[dict[str, object]] = []

    @property
    def used(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def require(self, count: int, *, category: str) -> None:
        if self.remaining < count:
            raise HarnessError(
                "design_llm_budget_exhausted",
                f"Need {count} {category} call(s), but only {self.remaining}/{self.limit} remain.",
            )

    def complete(self, *, system: str, prompt: str) -> str:
        self._reserve(system)
        before = self._base_usage_summary()
        result = self.base.complete(system=system, prompt=prompt)
        self._record_usage_delta(before, self._base_usage_summary())
        return result

    def complete_with_metadata(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        self._reserve(system)
        completion = self.base.complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=json_mode,
            max_tokens=max_tokens,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        self._record_usage(completion)
        return completion

    def complete_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        system = next((str(item.get("content") or "") for item in messages if item.get("role") == "system"), "")
        self._reserve(system)
        completion = self.base.complete_messages(
            messages=messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        self._record_usage(completion)
        return completion

    def summary(self) -> dict[str, object]:
        counts = Counter(str(item["category"]) for item in self.calls)
        usage = {
            key: sum(int(_dict(item.get("usage")).get(key) or 0) for item in self.calls)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "by_category": dict(sorted(counts.items())),
            "token_usage": usage,
            "usage_reported_calls": sum(1 for item in self.calls if _dict(item.get("usage"))),
            "calls": self.calls,
        }

    def _reserve(self, system: str) -> None:
        if self.used >= self.limit:
            raise HarnessError(
                "design_llm_budget_exhausted",
                f"Design workflow reached its {self.limit}-call LLM budget.",
            )
        self.calls.append({"ordinal": self.used + 1, "category": _call_category(system)})

    def _record_usage(self, completion: object) -> None:
        usage = getattr(completion, "usage", None)
        if not isinstance(usage, dict) or not self.calls:
            return
        self.calls[-1]["usage"] = {
            key: int(usage.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }

    def _base_usage_summary(self) -> dict[str, int] | None:
        summary = getattr(self.base, "usage_summary", None)
        if not callable(summary):
            return None
        value = summary()
        if not isinstance(value, dict):
            return None
        return {
            key: int(value.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }

    def _record_usage_delta(
        self,
        before: dict[str, int] | None,
        after: dict[str, int] | None,
    ) -> None:
        if before is None or after is None or not self.calls:
            return
        self.calls[-1]["usage"] = {
            key: max(0, after[key] - before[key])
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }


class LlmClient:
    """Adapter nhỏ để workflow không phụ thuộc trực tiếp SDK của provider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self._usage_reported_calls = 0

    def usage_summary(self) -> dict[str, object]:
        """Return aggregate provider usage without retaining prompts or completions."""

        return {
            **self._usage_totals,
            "reported_calls": self._usage_reported_calls,
        }

    def complete(self, *, system: str, prompt: str) -> str:
        """Sinh text từ LLM hoặc fake output dùng cho unit/smoke test."""

        return self.complete_with_metadata(system=system, prompt=prompt).content

    def complete_with_metadata(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmCompletion:
        """Sinh completion và giữ finish_reason/usage cho structured-output flow."""

        return self.complete_messages(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            json_mode=json_mode,
            max_tokens=max_tokens,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )

    def complete_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmCompletion:
        """Complete a bounded transcript for controlled agent loops."""

        selected_model = str(model or self.settings.llm_model).strip()
        selected_effort = str(reasoning_effort or "").strip().lower()
        if not selected_model:
            raise HarnessError("requirement_refiner_failed", "LLM model must not be empty.")
        if selected_effort and selected_effort not in {"none", "low", "high", "max"}:
            raise HarnessError(
                "requirement_refiner_failed",
                f"Unsupported reasoning effort: {selected_effort}",
            )
        metadata = {
            "mode": self.settings.llm_mode,
            "model": selected_model,
            "json_mode": json_mode,
            "reasoning_effort": selected_effort or None,
        }
        emit_current(
            category="llm",
            event_type="llm_call_started",
            message="LLM call started.",
            data=metadata,
        )
        started = time.monotonic()
        try:
            if self.settings.llm_mode == "fake":
                from .fake import fake_completion

                prompt = next(
                    (str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"),
                    "",
                )
                completion = LlmCompletion(content=fake_completion(prompt), finish_reason="fake")
            elif self.settings.llm_mode in {"deepseek", "openai_compatible"}:
                completion = self._call_chat_completions_messages(
                    messages=messages,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    model=selected_model,
                    reasoning_effort=selected_effort or None,
                    timeout_seconds=timeout_seconds,
                )
            else:
                raise HarnessError(
                    "requirement_refiner_failed",
                    f"Unsupported LLM mode: {self.settings.llm_mode}",
                )
        except Exception as exc:
            emit_current(
                category="llm",
                event_type="llm_call_failed",
                message="LLM call failed.",
                data={
                    **metadata,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_code": exc.error_code if isinstance(exc, HarnessError) else "internal_error",
                },
            )
            raise
        usage = completion.usage
        if usage:
            self._usage_reported_calls += 1
            for key in self._usage_totals:
                self._usage_totals[key] += int(usage.get(key) or 0)
        emit_current(
            category="llm",
            event_type="llm_call_completed",
            message="LLM call completed.",
            data={
                **metadata,
                "finish_reason": completion.finish_reason,
                "duration_seconds": round(time.monotonic() - started, 3),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )
        return completion

    def _call_chat_completions(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Gọi API Chat Completions tương thích OpenAI bằng stdlib."""

        return self._call_chat_completions_messages(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def _call_chat_completions_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmCompletion:
        """Gọi Chat Completions với transcript đã được Harness kiểm soát."""

        if not self.settings.llm_api_key:
            raise HarnessError(
                "requirement_refiner_failed",
                "SDLC_LLM_API_KEY is required when SDLC_LLM_MODE is not fake.",
            )
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model or self.settings.llm_model, "messages": messages}
        if self.settings.llm_mode == "deepseek":
            thinking_mode = (
                "disabled"
                if reasoning_effort == "none"
                else ("enabled" if reasoning_effort else self.settings.llm_thinking_mode)
            )
            payload["thinking"] = {"type": thinking_mode}
            if reasoning_effort and reasoning_effort != "none":
                payload["reasoning_effort"] = reasoning_effort
            if thinking_mode == "disabled":
                payload["temperature"] = 0.1
        else:
            payload["temperature"] = 0.1
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds or self.settings.llm_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise HarnessError(
                "requirement_refiner_failed",
                f"LLM HTTP error {exc.code}: {body[:500]}",
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise HarnessError(
                "llm_transport_failed",
                f"LLM request failed: {exc}",
            ) from exc

        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            reasoning_content = str(message.get("reasoning_content") or "")
            finish_reason = str(choice.get("finish_reason") or "")
            usage_value = data.get("usage")
            usage = usage_value if isinstance(usage_value, dict) else {}
        except (AttributeError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "requirement_refiner_failed",
                "LLM response did not match Chat Completions schema.",
            ) from exc
        return LlmCompletion(
            content=strip_code_fence(str(content)).strip(),
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=reasoning_content,
        )


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _call_category(system: str) -> str:
    lowered = system.casefold()
    if "targeted verifier" in lowered:
        return "semantic_targeted_verification"
    if "adjudicator" in lowered:
        return "semantic_adjudication"
    if "semantic judge" in lowered:
        return "semantic_claim_review"
    if "traceability" in lowered:
        return "traceability"
    if "update_design" in lowered:
        return "designer_repair"
    if "designer" in lowered:
        return "designer_draft"
    return "other"


def strip_code_fence(text: str) -> str:
    """Bỏ markdown code fence nếu model bọc toàn bộ output trong fence."""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped
