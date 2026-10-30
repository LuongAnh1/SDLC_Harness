"""Persist and pause after each controlled LLM exchange."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore, utc_now_iso
from .client import LlmCompletion
from .json_utils import final_response_text, parse_first_json_object


class LlmStepReviewRequired(HarnessError):
    """A controlled LLM input or output is waiting for operator review."""

    def __init__(
        self,
        *,
        run_id: str,
        call_id: str,
        exchange_path: str,
        error_code: str = "implementation_plan_step_review_required",
        label: str = "Implementation Plan",
        review_stage: str = "output",
    ):
        super().__init__(
            error_code,
            f"{label} LLM exchange {call_id} requires operator review.",
            run_id=run_id,
        )
        self.progress = {
            "call_id": call_id,
            "exchange_path": exchange_path,
            "review_stage": review_stage,
        }


class ReviewableLlmClient:
    """Persist controlled exchanges and optionally review both sides of a provider call."""

    def __init__(
        self,
        base: Any,
        *,
        store: RuntimeStore,
        run_id: str,
        review: dict[str, object] | None = None,
        call_prefix: str = "implementation-plan",
        execution_dir: str = "implementation_plan_execution",
        error_prefix: str = "implementation_plan",
        label: str = "Implementation Plan",
        start_ordinal: int = 0,
    ):
        self.base = base
        self.settings = getattr(base, "settings", None)
        self.store = store
        self.run_id = run_id
        self.review = dict(review or {})
        self.review_input = bool(self.review.get("review_input"))
        self.review_output = bool(self.review.get("review_output", True))
        self.call_prefix = call_prefix
        self.error_prefix = error_prefix
        self.label = label
        self.cursor = max(0, start_ordinal)
        self.last_replay_kind = ""
        self.root = store.run_dir(run_id) / execution_dir
        self.session_path = self.root / "step_review_session_v1.json"

    def complete(self, *, system: str, prompt: str) -> str:
        return self._complete(system=system, prompt=prompt, with_metadata=False, request_options={})

    def complete_with_metadata(
        self,
        *,
        system: str,
        prompt: str,
        **request_options: object,
    ) -> LlmCompletion:
        result = self._complete(
            system=system,
            prompt=prompt,
            with_metadata=True,
            request_options=request_options,
        )
        if not isinstance(result, LlmCompletion):
            raise TypeError("Reviewable metadata completion did not return LlmCompletion.")
        return result

    def _complete(
        self,
        *,
        system: str,
        prompt: str,
        with_metadata: bool,
        request_options: dict[str, object],
    ) -> str | LlmCompletion:
        self.last_replay_kind = ""
        self.cursor += 1
        ordinal = self.cursor
        prompt_hash = _prompt_hash(system=system, prompt=prompt)
        session = self._read_session()
        calls = [item for item in session.get("calls", []) if isinstance(item, dict)]
        attempts = [item for item in calls if int(item.get("ordinal") or 0) == ordinal]
        latest = attempts[-1] if attempts else None

        if latest and str(latest.get("status") or "") in {"approved", "completed"}:
            if _resume_prompt_hash(latest, calls) != prompt_hash:
                raise HarnessError(
                    f"{self.error_prefix}_step_review_drift",
                    f"Approved exchange {latest.get('call_id')} no longer matches the {self.label} prompt.",
                    run_id=self.run_id,
                )
            self.last_replay_kind = "approved"
            return _replay_completion(latest, with_metadata=with_metadata)

        if latest and str(latest.get("status") or "") == "input_approved":
            if _resume_prompt_hash(latest, calls) != prompt_hash:
                raise HarnessError(
                    f"{self.error_prefix}_step_review_drift",
                    f"Approved input {latest.get('input_review_call_id')} no longer matches the {self.label} prompt.",
                    run_id=self.run_id,
                )
            return self._call_provider(
                session=session,
                calls=calls,
                exchange=latest,
                with_metadata=with_metadata,
                request_options=(
                    dict(latest.get("request_options"))
                    if isinstance(latest.get("request_options"), dict)
                    else request_options
                ),
            )

        if latest and str(latest.get("status") or "") == "input_pending":
            return self._resolve_input_pending(
                session=session,
                calls=calls,
                pending=latest,
                system=system,
                prompt=prompt,
                prompt_hash=prompt_hash,
                with_metadata=with_metadata,
                request_options=request_options,
            )

        if latest and str(latest.get("status") or "") == "pending":
            return self._resolve_pending(
                session=session,
                calls=calls,
                pending=latest,
                system=system,
                prompt=prompt,
                prompt_hash=prompt_hash,
                with_metadata=with_metadata,
                request_options=request_options,
            )

        return self._prepare_exchange(
            session=session,
            calls=calls,
            ordinal=ordinal,
            attempt=len(attempts) + 1,
            system=system,
            prompt=prompt,
            prompt_hash=prompt_hash,
            with_metadata=with_metadata,
            request_options=request_options,
        )

    def _resolve_input_pending(
        self,
        *,
        session: dict[str, object],
        calls: list[dict[str, object]],
        pending: dict[str, object],
        system: str,
        prompt: str,
        prompt_hash: str,
        with_metadata: bool,
        request_options: dict[str, object],
    ) -> str | LlmCompletion:
        review_call_id = str(pending.get("input_review_call_id") or "")
        decision = str(self.review.get("decision") or "")
        if str(self.review.get("call_id") or "") != review_call_id or decision not in {"approve", "reject"}:
            self._raise_pending(pending)
        if decision == "approve":
            if _resume_prompt_hash(pending, calls) != prompt_hash:
                stored_prompt = str(pending.get("prompt") or "")
                first_difference = next(
                    (
                        index
                        for index, (stored, current) in enumerate(zip(stored_prompt, prompt))
                        if stored != current
                    ),
                    min(len(stored_prompt), len(prompt)),
                )
                raise HarnessError(
                    f"{self.error_prefix}_step_review_drift",
                    (
                        f"Pending input {review_call_id} no longer matches the {self.label} prompt "
                        f"(stored={len(stored_prompt)}, current={len(prompt)}, first_difference={first_difference})."
                    ),
                    run_id=self.run_id,
                )
            pending["status"] = "input_approved"
            pending["input_review"] = {
                "decision": "approve",
                "note": str(self.review.get("note") or "").strip(),
                "reviewed_at": utc_now_iso(),
            }
            self._write_session({**session, "calls": calls})
            _write_json(self.root / f"exchange_{pending['call_id']}.json", pending)
            return self._call_provider(
                session=session,
                calls=calls,
                exchange=pending,
                with_metadata=with_metadata,
                request_options=(
                    dict(pending.get("request_options"))
                    if isinstance(pending.get("request_options"), dict)
                    else request_options
                ),
            )

        note = str(self.review.get("note") or "").strip()
        if not note:
            raise HarnessError(
                f"{self.error_prefix}_step_review_invalid",
                f"A rejected {self.label} input requires a review note.",
                run_id=self.run_id,
            )
        pending["status"] = "input_rejected"
        pending["input_review"] = {
            "decision": "reject",
            "note": note,
            "reviewed_at": utc_now_iso(),
        }
        if str(pending.get("prompt_hash") or "") != prompt_hash:
            pending["replacement_prompt_hash"] = prompt_hash
        self._write_session({**session, "calls": calls})
        _write_json(self.root / f"exchange_{pending['call_id']}.json", pending)
        amended = (
            "Operator review rejected the previous input. Address this exact feedback before calling the provider: "
            f"{note}\n\n{prompt}"
        )
        return self._prepare_exchange(
            session=session,
            calls=calls,
            ordinal=int(pending.get("ordinal") or 0),
            attempt=int(pending.get("attempt") or 1) + 1,
            system=system,
            prompt=amended,
            prompt_hash=_prompt_hash(system=system, prompt=amended),
            resume_prompt_hash=prompt_hash,
            with_metadata=with_metadata,
            request_options=request_options,
        )

    def _resolve_pending(
        self,
        *,
        session: dict[str, object],
        calls: list[dict[str, object]],
        pending: dict[str, object],
        system: str,
        prompt: str,
        prompt_hash: str,
        with_metadata: bool,
        request_options: dict[str, object],
    ) -> str | LlmCompletion:
        call_id = str(pending.get("call_id") or "")
        decision = str(self.review.get("decision") or "")
        if str(self.review.get("call_id") or "") != call_id or decision not in {"approve", "reject"}:
            self._raise_pending(pending)
        if decision == "approve":
            if _resume_prompt_hash(pending, calls) != prompt_hash:
                stored_prompt = str(pending.get("prompt") or "")
                first_difference = next(
                    (
                        index
                        for index, (stored, current) in enumerate(zip(stored_prompt, prompt))
                        if stored != current
                    ),
                    min(len(stored_prompt), len(prompt)),
                )
                raise HarnessError(
                    f"{self.error_prefix}_step_review_drift",
                    (
                        f"Pending exchange {call_id} no longer matches the {self.label} prompt "
                        f"(stored={len(stored_prompt)}, current={len(prompt)}, first_difference={first_difference})."
                    ),
                    run_id=self.run_id,
                )
            pending["status"] = "approved"
            pending["reviewed_at"] = utc_now_iso()
            self._write_session({**session, "calls": calls})
            _write_json(self.root / f"exchange_{call_id}.json", pending)
            self.last_replay_kind = "pending_approved"
            return _replay_completion(pending, with_metadata=with_metadata)

        note = str(self.review.get("note") or "").strip()
        if not note:
            raise HarnessError(
                f"{self.error_prefix}_step_review_invalid",
                f"A rejected {self.label} exchange requires a review note.",
                run_id=self.run_id,
            )
        pending["status"] = "rejected"
        pending["review_note"] = note
        pending["reviewed_at"] = utc_now_iso()
        if str(pending.get("prompt_hash") or "") != prompt_hash:
            pending["replacement_prompt_hash"] = prompt_hash
        self._write_session({**session, "calls": calls})
        _write_json(self.root / f"exchange_{call_id}.json", pending)
        amended = (
            "Operator review rejected the previous response. Address this exact feedback: "
            f"{note}\n\n{prompt}"
        )
        return self._prepare_exchange(
            session=session,
            calls=calls,
            ordinal=int(pending.get("ordinal") or 0),
            attempt=int(pending.get("attempt") or 1) + 1,
            system=system,
            prompt=amended,
            prompt_hash=_prompt_hash(system=system, prompt=amended),
            resume_prompt_hash=prompt_hash,
            with_metadata=with_metadata,
            request_options=request_options,
        )

    def _prepare_exchange(
        self,
        *,
        session: dict[str, object],
        calls: list[dict[str, object]],
        ordinal: int,
        attempt: int,
        system: str,
        prompt: str,
        prompt_hash: str,
        resume_prompt_hash: str = "",
        with_metadata: bool = False,
        request_options: dict[str, object] | None = None,
    ) -> str | LlmCompletion:
        options = dict(request_options or {})
        call_id = f"{self.call_prefix}-{ordinal:04d}-a{attempt:02d}"
        exchange = {
            "call_id": call_id,
            "ordinal": ordinal,
            "attempt": attempt,
            "status": "input_pending" if self.review_input else "prepared",
            "created_at": utc_now_iso(),
            "prompt_hash": prompt_hash,
            "resume_prompt_hash": resume_prompt_hash or prompt_hash,
            "system": system,
            "prompt": prompt,
            "raw_completion": "",
            "parsed_output": None,
            "request_options": options,
            "finish_reason": "",
            "usage": {},
        }
        if self.review_input:
            exchange["input_review_call_id"] = f"{call_id}-input"
            calls.append(exchange)
            self._write_session({**session, "calls": calls})
            exchange_path = self.root / f"exchange_{call_id}.json"
            _write_json(exchange_path, exchange)
            raise LlmStepReviewRequired(
                run_id=self.run_id,
                call_id=str(exchange["input_review_call_id"]),
                exchange_path=self.store.relative(exchange_path),
                error_code=f"{self.error_prefix}_step_review_required",
                label=self.label,
                review_stage="input",
            )
        return self._call_provider(
            session=session,
            calls=calls,
            exchange=exchange,
            with_metadata=with_metadata,
            request_options=options,
        )

    def _call_provider(
        self,
        *,
        session: dict[str, object],
        calls: list[dict[str, object]],
        exchange: dict[str, object],
        with_metadata: bool,
        request_options: dict[str, object] | None = None,
    ) -> str | LlmCompletion:
        options = dict(request_options or {})
        system = str(exchange.get("system") or "")
        prompt = str(exchange.get("prompt") or "")
        self.last_replay_kind = "provider"
        if with_metadata:
            completion = self.base.complete_with_metadata(system=system, prompt=prompt, **options)
            raw = str(completion.content or "")
            finish_reason = str(completion.finish_reason or "")
            usage = dict(completion.usage or {})
        else:
            raw = self.base.complete(system=system, prompt=prompt)
            finish_reason = ""
            usage = {}
        exchange.update(
            {
                "status": "pending" if self.review_output else "completed",
                "provider_called_at": utc_now_iso(),
                "raw_completion": raw,
                "parsed_output": _parsed_output(raw),
                "request_options": options,
                "finish_reason": finish_reason,
                "usage": usage,
            }
        )
        if exchange not in calls:
            calls.append(exchange)
        self._write_session({**session, "calls": calls})
        exchange_path = self.root / f"exchange_{exchange['call_id']}.json"
        _write_json(exchange_path, exchange)
        if not self.review_output:
            return completion if with_metadata else raw
        raise LlmStepReviewRequired(
            run_id=self.run_id,
            call_id=str(exchange["call_id"]),
            exchange_path=self.store.relative(exchange_path),
            error_code=f"{self.error_prefix}_step_review_required",
            label=self.label,
            review_stage="output",
        )

    def _raise_pending(self, pending: dict[str, object]) -> None:
        review_stage = "input" if str(pending.get("status") or "") == "input_pending" else "output"
        exchange_call_id = str(pending.get("call_id") or "")
        call_id = (
            str(pending.get("input_review_call_id") or exchange_call_id)
            if review_stage == "input"
            else exchange_call_id
        )
        exchange_path = self.root / f"exchange_{exchange_call_id}.json"
        raise LlmStepReviewRequired(
            run_id=self.run_id,
            call_id=call_id,
            exchange_path=self.store.relative(exchange_path),
            error_code=f"{self.error_prefix}_step_review_required",
            label=self.label,
            review_stage=review_stage,
        )

    def _read_session(self) -> dict[str, object]:
        if not self.session_path.exists():
            return {"schema_version": f"{self.error_prefix}_step_review_v1", "calls": []}
        value = json.loads(self.session_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise HarnessError(
                f"{self.error_prefix}_step_review_corrupted",
                "Phase 3 step-review session is not a JSON object.",
                run_id=self.run_id,
            )
        return value

    def _write_session(self, session: dict[str, object]) -> None:
        session["updated_at"] = utc_now_iso()
        _write_json(self.session_path, session)


def _prompt_hash(*, system: str, prompt: str) -> str:
    return "sha256:" + hashlib.sha256(f"{system}\0{prompt}".encode("utf-8")).hexdigest()


def _resume_prompt_hash(
    exchange: dict[str, object],
    calls: list[dict[str, object]],
) -> str:
    value = str(exchange.get("resume_prompt_hash") or "")
    if value:
        return value
    ordinal = int(exchange.get("ordinal") or 0)
    attempt = int(exchange.get("attempt") or 0)
    if attempt > 1:
        for previous in reversed(calls):
            if previous is exchange:
                continue
            if int(previous.get("ordinal") or 0) != ordinal:
                continue
            replacement = str(previous.get("replacement_prompt_hash") or "")
            if replacement:
                return replacement
    return str(exchange.get("prompt_hash") or "")


def _parsed_output(raw: str) -> object:
    try:
        return parse_first_json_object(final_response_text(raw))
    except json.JSONDecodeError:
        return None


def _replay_completion(exchange: dict[str, object], *, with_metadata: bool) -> str | LlmCompletion:
    raw = str(exchange.get("raw_completion") or "")
    if not with_metadata:
        return raw
    usage = exchange.get("usage")
    return LlmCompletion(
        content=raw,
        finish_reason=str(exchange.get("finish_reason") or ""),
        usage=dict(usage) if isinstance(usage, dict) else {},
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
