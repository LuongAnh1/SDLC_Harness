"""Closure Planner (L5.3.3): one grounded JSON call that proposes runtime_closure_contract_v1.

The Planner decides how each runtime input is provided, which closure units write it, how
it is verified and which demo decisions the owner must approve. Harness owns every ID and
checks the result mechanically; the Closure Validator judges meaning. The call is
fail-closed: empty, truncated, unparsable or transport-failed output is inconclusive, and an
output that breaks the Planner output contract is retried once with the exact error.
"""

from __future__ import annotations

import json
from typing import Any

from ...core.config import phase4_llm_call_options
from ...core.errors import HarnessError
from ...llm.json_utils import final_response_text, parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired
from .closure_contract import (
    CLOSURE_DEVELOPER_STAGE_IDS,
    CLOSURE_EXECUTION_FACTS,
    CLOSURE_PROBE_SERVICES,
    CLOSURE_REVIEW_DUTIES,
    DEMO_DECISION_SCOPE,
    PROBE_BODY_METHODS,
    PROBE_METHODS,
    RESOLUTION_COMPATIBILITY,
    STAND_IN_START_SIGNATURE,
)

CLOSURE_PLANNER_PROMPT_MARKER = "Runtime Closure Planner v1"
INCONCLUSIVE = "runtime_closure_inconclusive"
_TRANSPORT_ERROR_CODES = {"llm_transport_failed", "requirement_refiner_failed"}
_REQUIRED_LISTS = ("resolutions", "closure_units", "runtime_probes")
_OPTIONAL_LISTS = (
    "closure_declared_inputs",
    "demo_decisions",
    "runtime_config_changes",
    "verification_stand_ins",
    "unresolved",
)

_PLANNER_SYSTEM = (
    "You are the SDLC Harness Closure Planner. Requirement, Design and Plan are frozen and approved. "
    "Propose how each runtime input is provided and verified so the approved journey runs locally. "
    "Return one JSON object only."
)

_PLANNER_RULES = (
    "Requirement, Design and Plan are the authority. Closure never changes approved business behavior; it only "
    "provides runtime inputs, wires the composition and makes the journey verifiable.",
    "Goal: a locally runnable, verifiable composition. Resolve inputs so every scenario can be probed. Use "
    "unresolved_external only when no demo provider can stand in without changing approved behavior, and say why.",
    "Give exactly one entry in `resolutions` for every input in closure_context.runtime_inputs, by its exact input_id.",
    "The resolution kind must be allowed for the input's design_source (resolution_compatibility). design_source is "
    "frozen; you choose only how the closure provides the input.",
    "existing_runtime: the provider already exists in the repository (cite it). new_boundary: a production provider "
    "a closure unit writes. approved_demo_fixture: a demo/verification provider the owner approves through a demo "
    "decision. unresolved_external: no provider; provider=null and an `unresolved` row with the reason.",
    "A symbol provider is {\"kind\":\"symbol\",\"ref\":\"<path>#<symbol>\"}. For new_boundary and "
    "approved_demo_fixture its path must be in write_paths of a closure unit whose resolves_input_ids contains the input.",
    "An environment-variable provider is {\"kind\":\"env_var\",\"env_key\":...,\"read_by\":...,\"change_ref\":\"plan\"|\"closure\"}. "
    "change_ref=plan cites an approved Plan env change; change_ref=closure cites one of your runtime_config_changes. "
    "An env input that repository code already reads is existing_runtime; a stand-in only verifies it and does not "
    "change its kind.",
    "resolves_input_ids lists the inputs a unit provides or makes verifiable (it writes the provider or the "
    "stand-in). A unit that only supports other units, for example by preparing demo data, leaves it empty.",
    "approved_demo_fixture for an external_use_case input cites a new closure decision CD-<n> in demo_decisions that "
    f"lists the input, with scope `{DEMO_DECISION_SCOPE}`. A demo_fixture input cites its own approved D-* decision.",
    "An env-provided input keeps its production default. To verify it locally add a verification_stand_ins entry: a "
    "module written by an e2e closure unit (see path_policy.stand_in_module_rule) whose env_key equals the provider "
    "env_key and which cites a CD-<n> decision. Without a stand-in the input needs an `unresolved` row.",
    "Name your closure units and any closure-declared inputs with a short `key` (letters, digits, - or _, not "
    "starting with RI- or CU-). Cite those keys, or exact Harness input_ids, wherever an ID is expected. Harness "
    "assigns the final IDs.",
    "write_paths list every file a unit creates or edits: inside path_policy.component_editable_roots, never a "
    "protected or Harness-owned path. test_target_paths are test files that are also in write_paths. "
    f"required_stage_ids may only use {list(CLOSURE_DEVELOPER_STAGE_IDS)}.",
    "runtime_probes are request-only HTTP checks run against the booted services "
    f"{list(CLOSURE_PROBE_SERVICES)}: method {list(PROBE_METHODS)}, a request path, expect_status and optionally "
    "expect_json_array_min_items. headers_from_input_ids name inputs whose value the probe sends, and `headers` gives "
    "those exact literal header values (for example an approved demo session credential); the owner approves them. "
    f"`json_body` is null or the exact literal JSON object sent as the request body (only with {list(PROBE_BODY_METHODS)}); "
    "the owner approves it too. Probes observe no DOM, focus, CSS or layout. Every scenario that no unresolved input "
    "blocks needs at least one probe. When a scenario sends a server request, its probe sends that request and "
    "expects the approved outcome status; a probe that only checks a service boots is for scenarios with no server "
    "request. Probes run after stage_capabilities.verification_sequence prepared the database (it is migrated empty "
    "and seeded by the repository seed script twice, so seeding must be idempotent), then in contract order against "
    "that one database, so order destructive requests last.",
    "Every input that is not unresolved_external must be proven by a probe or a closure test intent "
    "(proves_input_ids).",
    "runtime_config_changes use the runtime_config_policy kinds and cite a unit by `unit_key`; only env_var is "
    "executable now.",
    "Copy IDs exactly. Do not invent Requirement, Design or decision IDs.",
    "execution_facts state how Harness runs stand-ins and which paths units may write; plan with them.",
    "The Closure Validator reviews your contract against validator_review_duties; meet them in this proposal.",
)

_PLANNER_OUTPUT_SCHEMA = {
    "resolutions": [
        {"input_id": "RI-...", "kind": "existing_runtime|new_boundary|approved_demo_fixture|unresolved_external",
         "provider": "symbol provider | env_var provider | null", "decision_ref": "CD-1|D-1|null", "reason": "..."}
    ],
    "closure_declared_inputs": [
        {"key": "...", "name": "...", "consumer": "<path>#<symbol>", "provided_by": "<path>#<symbol>",
         "source_refs": ["SC-1|VDN-...|PRE-..."], "resolution": {"kind": "...", "provider": {}, "decision_ref": None,
                                                                "reason": "..."}}
    ],
    "demo_decisions": [{"id": "CD-1", "input_ids": ["RI-...|key"], "statement": "...", "scope": DEMO_DECISION_SCOPE}],
    "closure_units": [
        {"key": "...", "component_id": "...", "objective": "...", "depends_on": ["unit key"],
         "resolves_input_ids": ["RI-...|key"], "write_paths": ["..."], "test_target_paths": ["..."],
         "required_stage_ids": ["typecheck", "unit"],
         "test_intents": [{"intent": "...", "test_path": "...", "proves_input_ids": ["RI-...|key"]}],
         "context_paths": ["..."]}
    ],
    "runtime_config_changes": [
        {"kind": "env_var", "unit_key": "...", "name": "...", "default": "...", "read_by": "...", "purpose": "..."}
    ],
    "runtime_probes": [
        {"probe_id": "RP-1", "scenario_ids": ["SC-1"], "service": "api|web", "method": "GET", "path": "/...",
         "headers_from_input_ids": ["RI-...|key"], "headers": {"Header-Name": "literal value"},
         "json_body": {"field": "literal JSON value"},
         "expect_status": 200, "expect_json_array_min_items": 1,
         "proves_input_ids": ["RI-...|key"]}
    ],
    "verification_stand_ins": [
        {"stand_in_id": "VS-1", "for_input_id": "RI-...|key", "decision_ref": "CD-2", "module": "...",
         "start_export": "...", "env_key": "..."}
    ],
    "unresolved": [{"input_id": "RI-...|key", "reason": "..."}],
}


class RuntimeClosurePlanner:
    """Build the Planner prompt, call the provider once (plus one contract retry) and parse."""

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def propose(
        self,
        *,
        context: dict[str, object],
        planner_view: dict[str, object],
        previous_round: dict[str, object] | None = None,
        run_id: str = "",
    ) -> dict[str, object]:
        prompt = closure_planner_prompt(planner_view=planner_view, previous_round=previous_round)
        return call_structured_json(
            self.llm_client,
            system=_PLANNER_SYSTEM,
            prompt=prompt,
            profile_name="closure_planner",
            parse=parse_closure_planner_output,
            contract_error_code="runtime_closure_planner_contract_invalid",
            label="Closure Planner",
            run_id=run_id or str(context.get("run_id") or ""),
        )


def closure_planner_prompt(
    *,
    planner_view: dict[str, object],
    previous_round: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "prompt_kind": CLOSURE_PLANNER_PROMPT_MARKER,
        "rules": list(_PLANNER_RULES),
        "resolution_compatibility": {
            str(source): list(kinds) for source, kinds in RESOLUTION_COMPATIBILITY.items()
        },
        "stand_in_start_signature": STAND_IN_START_SIGNATURE,
        "execution_facts": list(CLOSURE_EXECUTION_FACTS),
        "validator_review_duties": list(CLOSURE_REVIEW_DUTIES),
        "output_schema": _PLANNER_OUTPUT_SCHEMA,
        **planner_view,
    }
    if previous_round:
        payload["previous_round"] = {
            "instruction": (
                "Your previous contract was rejected. Return the full corrected output. Fix every blocking issue; "
                "keep unit keys stable for units you keep."
            ),
            **previous_round,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_closure_planner_output(raw: str) -> dict[str, object]:
    """Structural output contract only; gates G1-G15 run on the assembled contract."""

    payload = _parse_json_object(raw)
    problems = [f"`{key}` must be a list of objects." for key in _REQUIRED_LISTS if not _object_list(payload.get(key))]
    problems += [
        f"`{key}` must be a list of objects when present."
        for key in _OPTIONAL_LISTS
        if key in payload and payload[key] is not None and not _object_list(payload.get(key))
    ]
    if problems:
        raise HarnessError("runtime_closure_planner_contract_invalid", " ".join(problems))
    return {key: list(payload.get(key) or []) for key in (*_REQUIRED_LISTS, *_OPTIONAL_LISTS)}


def call_structured_json(
    client: Any,
    *,
    system: str,
    prompt: str,
    profile_name: str,
    parse,
    contract_error_code: str,
    label: str,
    run_id: str = "",
) -> dict[str, object]:
    """One provider call with one exact-error retry; every other failure is inconclusive."""

    settings = getattr(client, "settings", None)
    options = phase4_llm_call_options(
        settings,
        profile_name,
        fallback_max_tokens=int(getattr(settings, "llm_structured_max_tokens", 8192)),
    )
    attempts: list[dict[str, object]] = []
    contract_error = ""
    for attempt in (1, 2):
        request_prompt = prompt
        if contract_error:
            request_prompt += (
                "\n\nYour previous answer broke the output contract:\n"
                f"{contract_error}\n"
                "Return one corrected JSON object only. Copy exact IDs from the supplied context."
            )
        try:
            method = getattr(client, "complete_with_metadata", None)
            if callable(method):
                completion = method(system=system, prompt=request_prompt, json_mode=True, **options)
                raw = str(getattr(completion, "content", "") or "")
                finish_reason = str(getattr(completion, "finish_reason", "") or "")
            else:
                raw = str(client.complete(system=system, prompt=request_prompt) or "")
                finish_reason = ""
        except LlmStepReviewRequired:
            raise
        except HarnessError as exc:
            if exc.error_code not in _TRANSPORT_ERROR_CODES:
                raise
            raise _inconclusive(label, f"provider call failed: {exc.message}", attempts, run_id) from exc
        attempts.append({"attempt": attempt, "finish_reason": finish_reason, "chars": len(raw)})
        if finish_reason == "length":
            raise _inconclusive(label, "provider output was truncated (finish_reason=length).", attempts, run_id, raw)
        if not raw.strip():
            raise _inconclusive(label, "provider output was empty.", attempts, run_id, raw)
        try:
            parsed = parse(raw)
        except HarnessError as exc:
            if exc.error_code == contract_error_code and attempt == 1:
                contract_error = exc.message
                attempts[-1]["contract_error"] = exc.message
                continue
            raise _inconclusive(label, exc.message, attempts, run_id, raw) from exc
        return {"output": parsed, "raw": raw, "attempts": attempts}
    raise AssertionError("unreachable")


def _parse_json_object(raw: str) -> dict[str, object]:
    text = final_response_text(raw)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Output that starts as JSON but is malformed must not fall through to a nested object.
        if text.lstrip().startswith("{"):
            raise HarnessError("runtime_closure_output_unparsable", "Output is not valid JSON.") from None
        try:
            value = parse_first_json_object(text)
        except json.JSONDecodeError:
            raise HarnessError("runtime_closure_output_unparsable", "Output contains no JSON object.") from None
    if not isinstance(value, dict):
        raise HarnessError("runtime_closure_output_unparsable", "Output JSON is not an object.")
    return value


def _inconclusive(
    label: str,
    reason: str,
    attempts: list[dict[str, object]],
    run_id: str,
    raw: str = "",
) -> HarnessError:
    error = HarnessError(INCONCLUSIVE, f"{label} output is inconclusive: {reason}", run_id=run_id or None)
    error.attempts = list(attempts)  # type: ignore[attr-defined]
    error.raw_response = raw  # type: ignore[attr-defined]
    return error


def _object_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)
