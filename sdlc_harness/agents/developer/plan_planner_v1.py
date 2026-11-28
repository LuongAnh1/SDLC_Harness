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
_TEST_ROW = re.compile(r"^\|\s*(.*?)\s*\|\s*\**(unit|api|ui)\**\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$",
                       re.IGNORECASE)
_TEST_LAYOUT = "| Proves | Level | Drives | Test file | What it checks |"
_REQ_ID = re.compile(r"\b(?:AC|IMP)-\d+\b")
_UNIT_ID = re.compile(r"\bU-\d+\b")
SYSTEM_LEVELS = ("api", "ui")  # whole-system checks run once every group is merged (plan 11.7 (1))
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts")


def requirement_items(requirement_trace: Mapping[str, object]) -> dict[str, list[dict[str, str]]]:
    """The approved Requirement's acceptance criteria and implied obligations, as the Planner sees them."""

    def text(item: Mapping[str, object], key: str) -> str:
        return " ".join(str(item.get(key) or "").split())

    return {
        "acs": [{"id": str(item["id"]), "title": text(item, "title"), "api": text(item, "api"), "web": text(item, "web")}
                for item in requirement_trace.get("acceptance_criteria") or [] if isinstance(item, dict)],
        "imps": [{"id": str(item["id"]), "text": text(item, "text")}
                 for item in requirement_trace.get("implied_obligations") or [] if isinstance(item, dict)],
    }


def requirement_lines(requirement: Mapping[str, list[dict[str, str]]]) -> str:
    lines = [f"- {ac['id']} {ac['title']}. API: {ac['api']} Web: {ac['web']}" for ac in requirement.get("acs") or []]
    lines += [f"- {imp['id']} {imp['text']}" for imp in requirement.get("imps") or []]
    return "\n".join(lines) or "- none"


def test_lines(plan_all: Mapping[str, object]) -> str:
    lines = [f"- {item.get('task') or '?'}: {item['path']} (imports {', '.join(item['units'])})"
             for item in plan_all.get("tests") or []]
    return "\n".join(lines) or "- none"


def ui_callers(plan_all: Mapping[str, object]) -> list[str]:
    """Units that call a cut http edge: only a `ui` test that drives them checks that web and API agree."""

    return list((plan_all.get("test_rules") or {}).get("ui_callers") or [])


def ui_caller_lines(plan_all: Mapping[str, object]) -> str:
    files = {str(unit["id"]): unit["files"] for unit in plan_all["units"]}
    lines = [f"- {unit} ({', '.join(files.get(unit, []))})" for unit in ui_callers(plan_all)]
    return "\n".join(lines) or "- none (no http edge is cut)"


def check_files(plan_all: Mapping[str, object]) -> list[str]:
    checks = set(plan_all.get("check_units") or [])
    return [path for unit in plan_all["units"] if unit["id"] in checks for path in unit["files"]]


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


def planner_prompt(
    *,
    design_ref: str,
    report_ref: str,
    output_ref: str,
    plan_all: Mapping[str, object],
    requirement: Mapping[str, list[dict[str, str]]],
) -> str:
    template = (_PROMPTS / "hoc_planner.md").read_text(encoding="utf-8").rstrip()
    roots = plan_all.get("test_rules", {}).get("e2e_roots") or []
    return (
        template.replace("{{DESIGN_REF}}", design_ref)
        .replace("{{REPORT_REF}}", report_ref)
        .replace("{{OUTPUT_REF}}", output_ref)
        .replace("{{CANDIDATES}}", candidate_lines(plan_all))
        .replace("{{OPTIONS}}", option_lines(plan_all))
        .replace("{{REQUIREMENT}}", requirement_lines(requirement))
        .replace("{{TESTS}}", test_lines(plan_all))
        .replace("{{CHECK_FILES}}", ", ".join(check_files(plan_all)) or "none")
        .replace("{{E2E_ROOTS}}", ", ".join(f"{root}/" for root in roots) or "the e2e folder")
        .replace("{{UI_CALLERS}}", ui_caller_lines(plan_all))
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
    tests = []
    bad_test_rows = []
    for line in _section(text, "Tests").splitlines():
        line = line.strip()
        match = _TEST_ROW.match(line)
        if not match:
            # header and separator rows are layout; any other table row is an intent the Harness cannot read
            if line.startswith("|") and not re.fullmatch(r"[|\s:-]+", line) and not re.match(
                    r"^\|\s*Proves\s*\|", line, re.IGNORECASE):
                bad_test_rows.append(line)
            continue
        proves, level, drives, path, what = match.groups()
        new = bool(re.search(r"\(new\)", path, re.IGNORECASE))
        clean = re.sub(r"\(new\)", "", path, flags=re.IGNORECASE).strip().strip("`").strip()
        tests.append({"proves": _REQ_ID.findall(proves), "proves_text": proves.strip(), "level": level.lower(),
                      "drives": _UNIT_ID.findall(drives), "path": clean, "new": new, "intent": what.strip()})
    return {
        "sections": [title for title in ("Reliance", "Base", "Stubs", "Tests", "Notes") if re.search(
            rf"^##\s+{title}\s*$", text, re.MULTILINE | re.IGNORECASE)],
        "tests": tests,
        "bad_test_rows": bad_test_rows,
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


def resolve_test(plan_all: Mapping[str, object], path: str, level: str, new: bool) -> tuple[str | None, str]:
    """The task anchor of a test intent's file (a unit id, "integration", or "attached" for an attached existing
    test) and an error text when the file breaks the rules of plan section 11.3."""

    rules = plan_all.get("test_rules") or {}
    suffixes = [str(item) for item in rules.get("test_suffixes") or []]
    unit_suffixes = [item for item in suffixes if ".test." in item]
    spec_suffixes = [item for item in suffixes if ".spec." in item]
    roots = [str(item).rstrip("/") for item in rules.get("e2e_roots") or []]
    attached = {str(item["path"]): item for item in plan_all.get("tests") or []}
    owner = {path: str(unit["id"]) for unit in plan_all["units"] for path in unit["files"]}
    checks = set(check_files(plan_all))
    system = level in SYSTEM_LEVELS
    if not new:
        if path in attached:
            if attached[path].get("check") and not system:
                return None, f"{path} is an existing whole-system test: use level api or ui"
            if not attached[path].get("check") and level != "unit":
                return None, f"{path} is an existing unit test: use level unit"
            return "attached", ""
        if path in checks:
            return ("integration", "") if system else (None, f"{path} is a whole-system check: use level api or ui")
        if path in owner:
            return None, f"{path} is a source file of {owner[path]}, not a test file"
        return None, f"{path} is not an existing test that imports a Design unit; mark a new file with (new)"
    if path in attached or path in owner:
        return None, f"{path} already exists: remove (new)"
    if level == "unit":
        suffix = next((item for item in unit_suffixes if path.endswith(item)), "")
        stem = path[: -len(suffix)] if suffix else ""
        source = next((stem + ext for ext in _SOURCE_SUFFIXES if suffix and stem + ext in owner), "")
        if not source or owner[source] in set(plan_all.get("check_units") or []):
            return None, (f"new unit test {path} must be X.test.ts or X.test.tsx next to a file X.ts or X.tsx that a "
                          "unit lists in its Files")
        return owner[source], ""
    if any(path.startswith(root + "/") for root in roots) and any(path.endswith(item) for item in spec_suffixes):
        return "integration", ""
    return None, f"new {level} test {path} must be a .spec.ts file under {', '.join(roots) or 'the e2e folder'}"


def planner_issues(
    parsed: Mapping[str, object],
    plan_all: Mapping[str, object],
    requirement: Mapping[str, list[dict[str, str]]],
) -> list[str]:
    """Layout and consistency of the Planner's answer against the plan and Requirement it was shown."""

    issues: list[str] = []
    for title in ("Reliance", "Base", "Stubs", "Tests", "Notes"):
        if title not in parsed["sections"]:
            issues.append(f"missing section ## {title}")
    ac_ids = [str(item["id"]) for item in requirement.get("acs") or []]
    known = set(ac_ids) | {str(item["id"]) for item in requirement.get("imps") or []}
    units = {str(unit["id"]) for unit in plan_all["units"]}
    covered: set[str] = set()
    driven: set[str] = set()
    for line in parsed.get("bad_test_rows") or []:
        issues.append(f"test row does not follow the layout {_TEST_LAYOUT} with level unit, api or ui: {line}")
    for row in parsed.get("tests") or []:
        label = f"test row for {row['proves_text'] or '?'} ({row['path'] or 'no file'})"
        unknown = [ident for ident in row["proves"] if ident not in known]
        drives = list(row.get("drives") or [])
        strange = [unit for unit in drives if unit not in units]
        if not row["proves"]:
            issues.append(f"{label} proves no AC-n or IMP-n code")
        if unknown:
            issues.append(f"{label} names codes not in the Requirement: {', '.join(unknown)}")
        if not row["intent"]:
            issues.append(f"{label} does not say what it checks")
        if drives and row["level"] != "ui":
            issues.append(f"{label} is level {row['level']}: only a ui row drives screens (write - under Drives)")
        if strange:
            issues.append(f"{label} drives units that are not in the Design: {', '.join(strange)}")
        anchor, error = resolve_test(plan_all, row["path"], row["level"], row["new"])
        if error:
            issues.append(error)
            continue
        if not unknown:
            covered |= set(row["proves"])
        if row["level"] == "ui" and not strange:
            driven |= set(drives)
    missing = [ident for ident in ac_ids if ident not in covered]
    if missing:
        issues.append("these acceptance criteria have no valid test intent: " + ", ".join(missing))
    undriven = [unit for unit in ui_callers(plan_all) if unit not in driven]
    if undriven:
        issues.append("these units call a cut http edge but no valid ui test intent drives them: " + ", ".join(undriven))
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


def test_intents(parsed: Mapping[str, object], plan_all: Mapping[str, object]) -> list[dict[str, object]]:
    """The Planner's test intents with their task anchor (call after `planner_issues` found nothing)."""

    result = []
    for row in parsed.get("tests") or []:
        anchor, _ = resolve_test(plan_all, row["path"], row["level"], row["new"])
        result.append({"proves": list(row["proves"]), "level": row["level"], "drives": list(row.get("drives") or []),
                       "path": row["path"], "new": row["new"], "anchor": None if anchor == "attached" else anchor,
                       "intent": row["intent"]})
    return result


def chosen_option(parsed: Mapping[str, object], plan_all: Mapping[str, object]) -> dict[str, list[str]]:
    option = plan_all["search"]["top"][int(parsed["option"])]
    return {"base": list(option["base"]), "stubs": list(option["stubs"])}
