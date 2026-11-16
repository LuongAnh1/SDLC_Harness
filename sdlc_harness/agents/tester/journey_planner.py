"""Browser Journey Planner (L5.4.2): one grounded JSON call that proposes typed browser steps.

The Planner never writes TypeScript (D12). It returns views of typed steps that make the
approved UI obligations observable; the Harness assigns view IDs, checks gates J1-J7 and
renders the capture spec. The call is fail-closed like the closure planner: empty, truncated,
unparsable or transport-failed output is inconclusive, and an output that breaks the output
contract is retried once with the exact error.
"""

from __future__ import annotations

import json
from typing import Any

from ...core.errors import HarnessError
from ..developer.closure_planner import _object_list, _parse_json_object, call_structured_json

JOURNEY_PLANNER_PROMPT_MARKER = "Browser Journey Planner v1"
JOURNEY_INCONCLUSIVE = "browser_journey_inconclusive"
JOURNEY_CONTRACT_INVALID = "browser_journey_planner_contract_invalid"

_SYSTEM = (
    "You are the SDLC Harness Browser Journey Planner. Requirement, Design and Plan are frozen and approved, and "
    "the app is built and served. Plan typed browser steps that make the approved UI obligations observable. "
    "Return one JSON object only."
)

_RULES = (
    "You never write code. Every step is a typed object from `step_actions`; the Harness renders and runs them.",
    "Each obligation has `summary` (the approved trigger or statement) and, when the Design declares more, `contract` "
    "(the approved presentation, response, outcome or focus target that must be observable for it). `scenarios` gives "
    "the approved Requirement text each scenario realizes. These are the frozen authority for what a view must show; "
    "`ui_sources` and ARIA snapshots only show how to reach and locate it.",
    "Use only roles from `aria_roles`. A locator uses exactly one of `name` (the exact accessible name from "
    "aria-label, aria-labelledby or native labelling) or `text` (the exact visible text from `ui_sources`). An ARIA "
    "snapshot can display element content after a role even when that content is not the accessible name; use `text` "
    "for such status or alert content instead of copying it into `name`.",
    "Use `ui_sources` only as locator and interaction evidence. Current implementation code is never authority "
    "to weaken an approved obligation or declare approved behavior unreachable.",
    "When several elements share a role and name, scope the step with `within` (a container role and name) or "
    "pick one with `nth`.",
    "Every view starts with exactly one `goto` step and never navigates again.",
    "Put a step where an obligation becomes observable: the Harness records evidence after every step, so a state, "
    "feedback, focus move or dialog must be on screen right after some step of the view that cites it.",
    "A view runs at every supplied viewport. It may cite an obligation only when the same intended post-step state "
    "is deterministic at every viewport; a result seen only because one viewport settles faster is not coverage.",
    "Do not end an asynchronous outcome path on the click or press that starts it. For a stable outcome, add a later "
    "exact `wait_for`. For a transient state caused by a click or press, attach an `observe` locator to that triggering "
    "step: as `execution_facts` describe, the Harness holds network responses during that step and captures while the "
    "locator is on screen, so a state that lasts until a server response is observable at every viewport. Never put a "
    "standalone `wait_for` after the trigger for a transient state that may disappear first.",
    "Reach states with the approved demo data in `demo_data` only; never assume data the approved decisions do not "
    "state.",
    "Each obligation id goes either in `obligation_refs` of the view(s) whose steps observe it, or in "
    "`not_reachable` with a concrete reason, never both. A view lists in `design_node_ids` the nodes of every "
    "obligation it cites, and only nodes that serve its `scenario_id`.",
    "Use `not_reachable` only when approved demo data, the typed action vocabulary, or capture timing cannot make "
    "the obligation observable, for example composition before the first render, a state the approved data cannot "
    "produce, or a transient state that no click or press with `observe` can hold on screen (it follows `goto`, or it "
    "ends without waiting for a network response). Never use it because current code appears to omit or break "
    "approved behavior; plan the intended typed path so execution review can classify it.",
    "A transient state that a click or press starts and a server response ends belongs in a view with `observe`, not "
    "in `not_reachable`. When no typed step can hold a transient state on screen at every viewport, keep it in "
    "`not_reachable` with the exact constraint and explain it in `observations`; never invent a fragile path only to "
    "remove `not_reachable`.",
    "Every runnable scenario needs at least one view. Stay inside `limits`.",
    "When approved Design contracts contradict each other, follow the reading that satisfies the Requirement and "
    "record both Design IDs in `observations`.",
)

_OUTPUT_SCHEMA = {
    "views": [
        {
            "key": "lowercase-kebab-key",
            "scenario_id": "SC-...",
            "design_node_ids": ["VDN-..."],
            "obligation_refs": ["VDN-...:feedback.1"],
            "steps": [
                {"action": "goto", "path": "/"},
                {"action": "click", "role": "button", "name": "exact accessible name", "within": {"role": "row", "name": "exact accessible name"}},
                {
                    "action": "click",
                    "role": "button",
                    "name": "exact accessible name",
                    "observe": {"role": "status", "text": "exact visible transient text"},
                },
                {"action": "press", "key": "Tab"},
                {"action": "fill", "role": "textbox", "name": "exact accessible name", "value": "text"},
                {"action": "wait_for", "role": "status", "text": "exact visible stable text", "nth": 0},
            ],
        }
    ],
    "not_reachable": [
        {"obligation_id": "VDN-...:state.loading", "reason": "typed-action or post-step capture constraint"}
    ],
    "observations": ["optional note"],
}


# A patch round returns only what changes; the Harness merges it onto the accepted plan before the gates.
_PATCH_OUTPUT_SCHEMA = {
    "views": ["only the views you change or add, each a complete view object as in `output_schema`"],
    "removed_view_keys": ["key of an accepted view to drop"],
    "not_reachable": [
        {"obligation_id": "an affected obligation that stays unobservable", "reason": "typed-action or capture constraint"}
    ],
    "observations": ["optional note"],
}


class BrowserJourneyPlanner:
    """Build the Journey Planner prompt, call the provider once (plus one contract retry) and parse."""

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def propose(
        self,
        *,
        planner_view: dict[str, object],
        previous_round: dict[str, object] | None = None,
        run_id: str = "",
    ) -> dict[str, object]:
        return call_structured_json(
            self.llm_client,
            system=_SYSTEM,
            prompt=journey_planner_prompt(planner_view=planner_view, previous_round=previous_round),
            profile_name="journey_planner",
            parse=parse_journey_planner_output,
            contract_error_code=JOURNEY_CONTRACT_INVALID,
            label="Journey Planner",
            run_id=run_id,
            inconclusive_code=JOURNEY_INCONCLUSIVE,
        )


def journey_planner_prompt(
    *,
    planner_view: dict[str, object],
    previous_round: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "prompt_kind": JOURNEY_PLANNER_PROMPT_MARKER,
        "rules": list(_RULES),
        "output_schema": _OUTPUT_SCHEMA,
        **planner_view,
    }
    if previous_round:
        payload["previous_round"] = (
            {**previous_round, "patch_output_schema": _PATCH_OUTPUT_SCHEMA}
            if previous_round.get("patch_scope")
            else previous_round
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_journey_planner_output(raw: str) -> dict[str, object]:
    """Structural output contract only (a full plan or a patch); gates J1-J7 run on the parsed plan."""

    payload = _parse_json_object(raw)
    problems = []
    if not _object_list(payload.get("views")):
        problems.append("`views` must be a list of objects.")
    if payload.get("not_reachable") is not None and not _object_list(payload.get("not_reachable")):
        problems.append("`not_reachable` must be a list of objects when present.")
    observations = payload.get("observations")
    if observations is not None and not (
        isinstance(observations, list) and all(isinstance(item, str) for item in observations)
    ):
        problems.append("`observations` must be a list of strings when present.")
    removed = payload.get("removed_view_keys")
    if removed is not None and not (isinstance(removed, list) and all(isinstance(item, str) for item in removed)):
        problems.append("`removed_view_keys` must be a list of strings when present.")
    if problems:
        raise HarnessError(JOURNEY_CONTRACT_INVALID, " ".join(problems))
    return {
        "views": list(payload["views"]),
        "not_reachable": list(payload.get("not_reachable") or []),
        "observations": list(observations or []),
        **({"removed_view_keys": list(removed)} if removed else {}),
    }
