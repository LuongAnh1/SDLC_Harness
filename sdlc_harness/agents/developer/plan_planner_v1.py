"""H-OC Phase 3: the one Planner turn, its prompt inputs and every mechanical check of its answer.

The Planner keeps or drops each `reliance` candidate the Harness found (with a reason citing the Design) and confirms
one of the base options. The options come from the plan built with every candidate kept, so the chosen option stays
valid for any subset of kept edges. Nothing here judges meaning: the Harness checks the layout, that every candidate is
answered once, that IDs exist, that reasons are present and that a stub option is justified per stub unit.

Plan `docs/maintenance/2026-11-27-h-oc-phase3-plan-graph.md` sections 6 and 10.4.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import re

PLANNER_OUTPUT_NAME = "plan-planner.md"
CANDIDATE_REASON = "Harness candidate, assumed kept until the Planner decides"
_PROMPTS = Path(__file__).resolve().parent / "prompts"
_ROW = re.compile(r"^\|\s*`?(U-\d+)`?\s*(?:->|→)\s*`?(U-\d+)`?\s*\|\s*\**(keep|drop|add)\**\s*\|\s*(.*?)\s*\|\s*$",
                  re.IGNORECASE | re.MULTILINE)
_STUB_ROW = re.compile(r"^\|\s*`?(U-\d+)`?\s*\|\s*\**(yes|no)\**\s*\|\s*(.*?)\s*\|\s*$", re.IGNORECASE | re.MULTILINE)
_OPTION = re.compile(r"^\s*\**Option\**\s*:\s*\**\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_REASON = re.compile(r"^\s*\**Reason\**\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def assumed_reliance(candidates: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Every candidate as a kept edge: the plan the Planner's base options are computed on."""

    return [{**dict(item), "reason": CANDIDATE_REASON} for item in candidates]


def candidate_lines(plan_all: Mapping[str, object]) -> str:
    lines = []
    for item in plan_all.get("reliance_candidates") or []:
        evidence = (item.get("evidence") or [{}])[0]
        mounted = evidence.get("mounted_file")
        reached = "" if evidence.get("unit_file") == mounted else f"{evidence.get('unit_file')} is reached from "
        where = (
            f" ({reached}{mounted}, which {evidence.get('registered_by')} mounts before {evidence.get('middleware')} "
            f"at line {evidence.get('line')})"
            if evidence.get("middleware") else ""
        )
        lines.append(f"- {item['from']} -> {item['to']}{where}")
    return "\n".join(lines) or "- none (write `none` under ## Reliance)"


def option_lines(plan_all: Mapping[str, object]) -> str:
    lines = []
    for index, option in enumerate(plan_all["search"]["top"]):
        lines.append(
            f"- Option {index}: base {', '.join(option['base'])}; stubs {', '.join(option['stubs']) or 'none'}; "
            f"estimated T {option['T']}; group weights {option['group_weights']}"
        )
    return "\n".join(lines)


def planner_prompt(*, design_ref: str, report_ref: str, output_ref: str, plan_all: Mapping[str, object]) -> str:
    template = (_PROMPTS / "hoc_planner.md").read_text(encoding="utf-8").rstrip()
    return (
        template.replace("{{DESIGN_REF}}", design_ref)
        .replace("{{REPORT_REF}}", report_ref)
        .replace("{{OUTPUT_REF}}", output_ref)
        .replace("{{CANDIDATES}}", candidate_lines(plan_all))
        .replace("{{OPTIONS}}", option_lines(plan_all))
    )


def _section(text: str, title: str) -> str:
    match = re.search(rf"^##\s+{title}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else ""


def parse_planner(text: str) -> dict[str, object]:
    """The Planner's answer as data; missing parts stay empty and `planner_issues` names them."""

    reliance = _section(text, "Reliance")
    base = _section(text, "Base")
    stubs = _section(text, "Stubs")
    option = _OPTION.search(base)
    reason = _REASON.search(base)
    return {
        "sections": [title for title in ("Reliance", "Base", "Stubs", "Notes") if re.search(
            rf"^##\s+{title}\s*$", text, re.MULTILINE | re.IGNORECASE)],
        "rows": [
            {"from": a, "to": b, "decision": decision.lower(), "reason": why.strip()}
            for a, b, decision, why in _ROW.findall(reliance)
        ],
        "reliance_none": bool(re.fullmatch(r"\s*none\s*", reliance, re.IGNORECASE)),
        "option": int(option.group(1)) if option else None,
        "option_reason": reason.group(1).strip() if reason else "",
        "stubs": {unit: (decision.lower(), why.strip()) for unit, decision, why in _STUB_ROW.findall(stubs)},
        "notes": _section(text, "Notes").strip(),
    }


def planner_issues(parsed: Mapping[str, object], plan_all: Mapping[str, object]) -> list[str]:
    """Layout and consistency of the Planner's answer against the plan it was shown."""

    issues: list[str] = []
    for title in ("Reliance", "Base", "Stubs", "Notes"):
        if title not in parsed["sections"]:
            issues.append(f"missing section ## {title}")
    units = {str(unit["id"]) for unit in plan_all["units"]}
    candidates = {(str(c["from"]), str(c["to"])) for c in plan_all.get("reliance_candidates") or []}
    seen: dict[tuple[str, str], int] = {}
    for row in parsed["rows"]:
        pair = (row["from"], row["to"])
        seen[pair] = seen.get(pair, 0) + 1
        if not row["reason"]:
            issues.append(f"{pair[0]} -> {pair[1]} has no reason")
        if row["decision"] == "add":
            if pair in candidates:
                issues.append(f"{pair[0]} -> {pair[1]} is a candidate: answer keep or drop, not add")
            if pair[0] not in units or pair[1] not in units or pair[0] == pair[1]:
                issues.append(f"added edge {pair[0]} -> {pair[1]} names an unknown unit or itself")
        elif pair not in candidates:
            issues.append(f"{pair[0]} -> {pair[1]} is not a candidate (use `add` with a Design quote)")
    for pair in sorted(candidates):
        if seen.get(pair, 0) == 0:
            issues.append(f"candidate {pair[0]} -> {pair[1]} is not answered")
    for pair, count in sorted(seen.items()):
        if count > 1:
            issues.append(f"{pair[0]} -> {pair[1]} is answered {count} times")
    top = plan_all["search"]["top"]
    option = parsed["option"]
    if option is None or not 0 <= option < len(top):
        issues.append(f"Option must be a number from 0 to {len(top) - 1}")
    else:
        if not parsed["option_reason"]:
            issues.append("the base option has no Reason")
        for unit in top[option]["stubs"]:
            decision, why = parsed["stubs"].get(unit, ("", ""))
            if decision != "yes" or not why:
                issues.append(f"option {option} stubs {unit}: write `| {unit} | yes | <Design evidence> |` or choose "
                              "an option without that stub")
        for unit in parsed["stubs"]:
            if unit not in top[option]["stubs"]:
                issues.append(f"{unit} is not a stub of option {option}")
    return issues


def kept_reliance(parsed: Mapping[str, object], plan_all: Mapping[str, object]) -> list[dict[str, object]]:
    """The edges the Planner kept or added, each with its reason and the Harness evidence of the candidate."""

    evidence = {(str(c["from"]), str(c["to"])): list(c.get("evidence") or []) for c in plan_all.get("reliance_candidates") or []}
    return [
        {"from": row["from"], "to": row["to"], "reason": row["reason"], "evidence": evidence.get((row["from"], row["to"]), []),
         "decided_by": "planner"}
        for row in parsed["rows"]
        if row["decision"] in {"keep", "add"}
    ]


def chosen_option(parsed: Mapping[str, object], plan_all: Mapping[str, object]) -> dict[str, list[str]]:
    option = plan_all["search"]["top"][int(parsed["option"])]
    return {"base": list(option["base"]), "stubs": list(option["stubs"])}
