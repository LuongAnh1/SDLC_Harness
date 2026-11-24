"""UI Polish Planner: propose a bounded presentation-only contract from approved evidence."""

from __future__ import annotations

import json
from typing import Any

from ...core.errors import HarnessError
from .closure_planner import _parse_json_object, call_structured_json


POLISH_PLANNER_PROMPT_MARKER = "UI Polish Planner v1"
POLISH_PLANNER_CONTRACT_INVALID = "ui_polish_planner_contract_invalid"
POLISH_INCONCLUSIVE = "ui_polish_inconclusive"

_SYSTEM = (
    "You are the SDLC Harness UI Polish Planner. Improve visual hierarchy, layout, spacing and consistency without "
    "changing approved behavior. Return one JSON object only."
)

_RULES = (
    "Requirement and Design are frozen. The visual_contract of a Design node overrides the app visual profile.",
    "This contract changes presentation only: never change behavior, data flow, accessibility semantics, copy, "
    "runtime configuration, tests, demo fixtures or Harness-rendered files.",
    "Copy authority.visual_profile_ref exactly from planner_context and list only supplied Design node IDs.",
    "Each unit uses a stable lowercase key. write_paths are existing files; new_paths are source or stylesheet files "
    "that do not exist yet. Keep every unit within one UI component.",
    "authority_refs contains exact profile token keys and/or Design node IDs supplied in planner_context. Cite every "
    "authority that materially guides the objective.",
    "Use baseline text evidence for observable structure and the attached baseline images for visual judgment. Image "
    "labels are evidence keys, not file paths.",
    "When approved Design contracts conflict, follow the reading that satisfies the Requirement and record both "
    "Design IDs in out_of_scope instead of changing Design.",
)

_GUIDANCE_RULE = (
    "planner_context.uiux_guidance is ui-ux-pro-max advice on how to apply the approved authority, ranked below "
    "Design and the profile. Use it to judge which baseline presentation issues deserve a unit and to phrase their "
    "objectives; it never adds tokens, colors, fonts or styles and is never an authority_ref."
)

# Images reach the provider without captions: without this list the r9 Polish reviews guessed which picture was
# which capture and cited image labels that do not exist (2026-11-16).
IMAGE_INPUTS_RULE = (
    "`image_inputs` lists the attached images in attachment order: the Nth attached image is item N. Refer to an "
    "image only by its label."
)


def image_inputs(images: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"label": item.get("label"), "sha256": item.get("sha256")} for item in images]


_OUTPUT_SCHEMA = {
    "authority": {
        "visual_profile_ref": {"app_scope_id": "web:apps/...", "sha256": "sha256:..."},
        "design_node_ids": ["VDN-..."],
    },
    "polish_units": [
        {
            "key": "lowercase-kebab-key",
            "objective": "presentation-only outcome",
            "write_paths": ["existing/path.tsx"],
            "new_paths": ["optional/new.css"],
            "depends_on": ["other-unit-key"],
            "authority_refs": ["primary", "VDN-..."],
        }
    ],
    "out_of_scope": ["explicit behavior or area intentionally unchanged"],
}


class UiPolishPlanner:
    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def propose(
        self,
        *,
        planner_context: dict[str, object],
        images: list[dict[str, object]],
        previous_round: dict[str, object] | None = None,
        run_id: str = "",
    ) -> dict[str, object]:
        return call_structured_json(
            self.llm_client,
            system=_SYSTEM,
            prompt=polish_planner_prompt(planner_context=planner_context, previous_round=previous_round, images=images),
            profile_name="polish_planner",
            parse=parse_polish_planner_output,
            contract_error_code=POLISH_PLANNER_CONTRACT_INVALID,
            label="UI Polish Planner",
            run_id=run_id,
            inconclusive_code=POLISH_INCONCLUSIVE,
            images=images,
        )


def polish_planner_prompt(
    *,
    planner_context: dict[str, object],
    previous_round: dict[str, object] | None = None,
    images: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "prompt_kind": POLISH_PLANNER_PROMPT_MARKER,
        "rules": [
            *_RULES,
            *([_GUIDANCE_RULE] if planner_context.get("uiux_guidance") else []),
            *([IMAGE_INPUTS_RULE] if images else []),
        ],
        "output_schema": _OUTPUT_SCHEMA,
        "planner_context": planner_context,
        **({"image_inputs": image_inputs(images)} if images else {}),
    }
    if previous_round:
        payload["previous_round"] = {
            "instruction": "Return the full corrected contract. Fix every blocker and keep unaffected unit keys stable.",
            **previous_round,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_polish_planner_output(raw: str) -> dict[str, object]:
    payload = _parse_json_object(raw)
    problems: list[str] = []
    if not isinstance(payload.get("authority"), dict):
        problems.append("`authority` must be an object.")
    if not isinstance(payload.get("polish_units"), list) or not all(
        isinstance(item, dict) for item in payload.get("polish_units") or []
    ):
        problems.append("`polish_units` must be a list of objects.")
    if not isinstance(payload.get("out_of_scope"), list) or not all(
        isinstance(item, str) for item in payload.get("out_of_scope") or []
    ):
        problems.append("`out_of_scope` must be a list of strings.")
    if problems:
        raise HarnessError(POLISH_PLANNER_CONTRACT_INVALID, " ".join(problems))
    return {
        "authority": dict(payload["authority"]),
        "polish_units": [dict(item) for item in payload["polish_units"]],
        "out_of_scope": [str(item) for item in payload["out_of_scope"]],
    }
