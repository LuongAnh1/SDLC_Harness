"""H-OC Phase 2: the light frame of the Markdown Design and every check the Harness decides mechanically.

The Designer writes `design.md` by system unit (`U-n`, each with Kind, Status, Files, Serves, Decided by, Uses), plus
the Endpoint branches, Scenario paths, Unchanged surfaces, Design decisions (`DD-n`) and Requirement decisions (`Q-n`
the Designer settles itself) sections. Everything here reads text and the approved requirement trace; nothing calls an
LLM or judges meaning (Harness/Validator boundary): the Reviewer judges whether the units satisfy the requirements, the
Harness only checks IDs, coverage, links, ledgers and what must be reviewed again after a revision.

Ported from the owner-approved pilot (`.runtime/conduit-uc4-hoc-r4/pilot-design/pilot.py`, plan
`docs/maintenance/2026-11-26-h-oc-phase2-design-plan.md` sections 4, 5, 8a, 8b).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
import difflib
import json
from pathlib import Path
import re

DESIGN_TRACE_SCHEMA_VERSION = "design_trace_v1"
UNIT_KINDS = ("endpoint", "query", "data", "migration", "seed", "screen", "component", "other")
DATA_KINDS = frozenset({"data", "migration", "seed", "query"})
GROUP_SIZE = 3
FIX_STATUSES = ("fixed", "not fixed", "rejection accepted", "rejection refused")
AGENT_DECIDED = "agent_decided"

_DEF = re.compile(r"^#{2,4}\s+`?((?:U|DD|Q)-\d+)\b(.*)$", re.MULTILINE)
_UNIT = re.compile(r"\bU-\d+\b")
_ANY_REF = re.compile(r"\b(?:U|DD|AC|IMP|SC|D|UC|Q)-\d+\b")


def _num(ident: str) -> int:
    return int(ident.rsplit("-", 1)[1])


# Parsing -----------------------------------------------------------------------------------------------------------

def blocks(text: str) -> dict[str, str]:
    """ID -> its block: from its heading to the next ID heading or the next `## ` section (first definition wins)."""

    matches = list(_DEF.finditer(text))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        nxt = re.search(r"^## ", text[match.end():end], re.MULTILINE)
        out.setdefault(match.group(1), text[match.start(): match.end() + nxt.start() if nxt else end])
    return out


def units(text: str) -> dict[str, str]:
    return {ident: block for ident, block in blocks(text).items() if ident.startswith("U-")}


def field(block: str, name: str) -> str:
    match = re.search(rf"^[ \t]*[-*][ \t]*\**{name}\**[ \t]*:[ \t]*(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def unit_kind(block: str) -> str:
    return (field(block, "Kind").split() or ["?"])[0]


def section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def scenario_rows(text: str) -> dict[str, list[list[str]]]:
    rows: dict[str, list[list[str]]] = {}
    for line in section(text, "Scenario paths").splitlines():
        cells = _table_cells(line)
        if len(cells) >= 3 and re.fullmatch(r"SC-\d+", cells[0]):
            rows.setdefault(cells[0], []).append(cells[:3])
    return rows


def endpoint_rows(text: str) -> set[tuple[str, ...]]:
    rows = set()
    for line in section(text, "Endpoint branches").splitlines():
        cells = tuple(_table_cells(line))
        if len(cells) >= 4 and cells[0] != "Endpoint" and not set("".join(cells)) <= set("-: "):
            rows.add(cells)
    return rows


def uses(block: str) -> list[str]:
    return _UNIT.findall(field(block, "Uses"))


def reach(graph: dict[str, list[str]], starts: set[str]) -> set[str]:
    """Units reachable from `starts` through Uses (the starts themselves only if a cycle returns to them)."""

    seen: set[str] = set()
    todo = [ref for start in starts for ref in graph.get(start, [])]
    while todo:
        unit = todo.pop()
        if unit not in seen:
            seen.add(unit)
            todo += graph.get(unit, [])
    return seen


def scenarios(requirement_trace: dict[str, object]) -> dict[str, dict[str, list[str]]]:
    """SC-n -> its clause kind, clauses and the ACs covering any of them (from the approved requirement trace)."""

    result: dict[str, dict[str, list[str]]] = {}
    for clause in requirement_trace.get("uc_clauses") or []:
        if isinstance(clause, dict) and clause.get("scenario"):
            entry = result.setdefault(str(clause["scenario"]), {"kind": [str(clause.get("kind"))], "clauses": [], "acs": []})
            entry["clauses"].append(str(clause["id"]))
    for entry in result.values():
        entry["acs"] = [
            str(ac["id"]) for ac in requirement_trace.get("acceptance_criteria") or []
            if set(ac.get("covers") or []) & set(entry["clauses"])
        ]
    return dict(sorted(result.items(), key=lambda item: _num(item[0])))


# Frame -------------------------------------------------------------------------------------------------------------

def uses_issues(unit_blocks: dict[str, str], path_units: set[str]) -> list[str]:
    """Links name real units; an endpoint reaches stored data and a screen reaches an endpoint through Uses; every unit
    is on a Scenario path or reached from one through Uses."""

    issues: list[str] = []
    graph = {u: uses(b) for u, b in unit_blocks.items()}
    kind = {u: unit_kind(b) for u, b in unit_blocks.items()}
    for unit, block in unit_blocks.items():
        if not field(block, "Uses"):
            issues.append(f"{unit}: needs Uses (`-` when it uses no unit).")
        issues += [f"{unit}: Uses names {ref}, which is not a unit." for ref in graph[unit] if ref not in unit_blocks]
        issues += [f"{unit}: Uses names itself." for ref in graph[unit] if ref == unit]
        reached = {kind.get(ref) for ref in reach(graph, {unit})}
        if kind[unit] == "endpoint" and not reached & {"query", "data"}:
            issues.append(f"{unit}: endpoint reaches no query or data unit through Uses.")
        if kind[unit] == "screen" and "endpoint" not in reached:
            issues.append(f"{unit}: screen reaches no endpoint through Uses.")
    covered = path_units | reach(graph, path_units)
    issues += [
        f"{u}: on no Scenario path and reached from none through Uses."
        for u in sorted(unit_blocks, key=_num) if u not in covered
    ]
    return issues


def frame_issues(
    text: str,
    requirement_trace: dict[str, object],
    routes: list[str],
    repo_file_exists: Callable[[str], bool],
) -> list[str]:
    """Every mechanical check of the Design frame (plan section 4)."""

    issues: list[str] = []
    defs = blocks(text)
    all_defs = [match.group(1) for match in _DEF.finditer(text)]
    issues += [f"{ident} is defined more than once." for ident in sorted(set(all_defs)) if all_defs.count(ident) > 1]
    unit_blocks = {k: v for k, v in defs.items() if k.startswith("U-")}
    by_question = {str(d.get("question_id")): str(d.get("id")) for d in requirement_trace.get("decisions") or []}
    served: set[str] = set()
    decided: set[str] = set()
    for ident, block in unit_blocks.items():
        kind, status, files, serves = (field(block, n) for n in ("Kind", "Status", "Files", "Serves"))
        if kind.split()[:1] and kind.split()[0] not in UNIT_KINDS:
            issues.append(f"{ident}: Kind '{kind}' is not one of {', '.join(UNIT_KINDS)}.")
        if not kind or not status or not serves:
            issues.append(f"{ident}: needs Kind, Status and Serves.")
        elif status.split()[0] not in {"modify", "new"}:
            issues.append(f"{ident}: Status must be modify or new.")
        if status.startswith("modify"):
            for path in re.findall(r"`([^`]+)`(?!\s*\(new\))", files):
                if not repo_file_exists(path.split(":")[0].split("#")[0].strip()):
                    issues.append(f"{ident}: file {path} does not exist; mark a planned file `(new)`.")
        served |= set(re.findall(r"\b(?:AC|IMP)-\d+\b", serves))
        refs = field(block, "Decided by")
        decided |= set(re.findall(r"\bD-\d+\b", refs))
        # The Markdown the Designer reads has Q-n, not D-n: a cited question counts as its decision.
        decided |= {by_question[q] for q in re.findall(r"\bQ-\d+\b", refs) if q in by_question}
    issues += [f"{ac['id']} is served by no unit." for ac in requirement_trace.get("acceptance_criteria") or [] if ac["id"] not in served]
    issues += [f"{imp['id']} is served by no unit." for imp in requirement_trace.get("implied_obligations") or [] if imp["id"] not in served]
    issues += [
        f"{d['id']} ({d['question_id']}) is applied by no unit (Decided by)."
        for d in requirement_trace.get("decisions") or [] if d["id"] not in decided
    ]
    rows = scenario_rows(text)
    for sc, info in scenarios(requirement_trace).items():
        if len(rows.get(sc, [])) != 1:
            issues.append(f"{sc}: needs exactly one row under Scenario paths (found {len(rows.get(sc, []))}).")
            continue
        _sc, path, acs = rows[sc][0]
        issues += [f"{sc}: path names {ref}, which is not a unit." for ref in _UNIT.findall(path) if ref not in unit_blocks]
        if not _UNIT.findall(path):
            issues.append(f"{sc}: its path names no unit.")
        listed = set(re.findall(r"\bAC-\d+\b", acs))
        if listed != set(info["acs"]):
            issues.append(f"{sc}: ACs {sorted(listed, key=_num)} differ from the trace {info['acs']}.")
    path_units = {u for found in rows.values() for row in found for u in _UNIT.findall(row[1])}
    issues += uses_issues(unit_blocks, path_units & set(unit_blocks))
    flat = text.replace("\\", "")
    for route in routes:
        needle = route.split(" ", 1)[1] if route.startswith("web ") else route
        # The route itself, not a prefix of a longer one (`/items` inside `/api/items`, `GET /x` inside `GET /x/y`).
        if not re.search(rf"(?<![\w/:.-]){re.escape(needle)}(?![\w/:-])", flat):
            issues.append(f"route `{route}` is neither a unit nor an unchanged surface.")
    issues += design_question_issues(text, requirement_trace)
    known = set(defs) | {str(i["id"]) for key in ("acceptance_criteria", "implied_obligations", "uc_clauses", "questions", "decisions")
                         for i in requirement_trace.get(key) or []}
    known |= set(scenarios(requirement_trace))
    issues += [f"{ref} is referenced but not defined." for ref in sorted(set(_ANY_REF.findall(text)) - known)]
    return issues


def design_question_issues(text: str, requirement_trace: dict[str, object]) -> list[str]:
    """A question the Designer adds is its own decision: Disposition agent_decided, an Answer and a Reason, and at least
    one unit that applies it (Decided by). The requirement questions are final and not redefined."""

    asked = {str(q["id"]) for q in requirement_trace.get("questions") or []}
    defs = blocks(text)
    issues = [f"{ident} is a requirement question; its answer is final, do not redefine it." for ident in defs if ident in asked]
    applied = {q for b in units(text).values() for q in re.findall(r"\bQ-\d+\b", field(b, "Decided by"))}
    for ident, block in defs.items():
        if not ident.startswith("Q-") or ident in asked:
            continue
        if field(block, "Disposition").strip("`*_ ") != AGENT_DECIDED:
            issues.append(f"{ident}: a question settled in the Design needs '- Disposition: {AGENT_DECIDED}'.")
        issues += [f"{ident}: needs a '- {name}:' line." for name in ("Answer", "Reason") if not field(block, name)]
        if ident not in applied:
            issues.append(f"{ident}: no unit applies it; cite it under Decided by of every unit it decides.")
    return issues


def flow_mermaid(text: str) -> str:
    """Mermaid flowchart of the Uses links, drawn by the Harness (no LLM-drawn diagrams)."""

    unit_blocks = units(text)
    lines = ["flowchart TD"]
    for unit in sorted(unit_blocks, key=_num):
        title = _DEF.search(unit_blocks[unit]).group(2).strip().replace('"', "'")
        lines.append(f'  {unit.replace("-", "")}["{unit} ({unit_kind(unit_blocks[unit])}) {title}"]')
    for unit in sorted(unit_blocks, key=_num):
        lines += [f"  {unit.replace('-', '')} --> {ref.replace('-', '')}" for ref in uses(unit_blocks[unit]) if ref in unit_blocks]
    return "\n".join(lines)


# Review groups and ledgers -----------------------------------------------------------------------------------------

def groups(text: str, scenario_map: dict[str, dict[str, list[str]]], size: int = GROUP_SIZE) -> list[list[str]]:
    """Greedy and deterministic: an SC joins the first open group sharing an endpoint/screen unit with it, else the
    first open group of its clause kind, else a new group; at most `size` SCs per group."""

    defs = units(text)
    rows = scenario_rows(text)
    result: list[tuple[list[str], set[str], str]] = []
    for sc, info in scenario_map.items():
        path = _UNIT.findall(rows[sc][0][1]) if sc in rows else []
        keys = {u for u in path if unit_kind(defs.get(u, "")) in {"endpoint", "screen"}}
        kind = info["kind"][0]
        target = next((g for g in result if len(g[0]) < size and keys & g[1]), None)
        target = target or next((g for g in result if len(g[0]) < size and g[2] == kind), None)
        if target:
            target[0].append(sc)
            target[1].update(keys)
        else:
            result.append(([sc], set(keys), kind))
    return [members for members, _, _ in result]


def _ac_rows(body: str) -> list[str]:
    # A row may name the AC with a short label after the id: `| AC-6 (reopen draft) |`.
    return [line for line in body.splitlines() if re.match(r"\|\s*AC-\d+\b[^|]*\|", line)]


def _verdict_issues(where: str, body: str, rows: list[str], finding: str) -> list[str]:
    issues = []
    for line in rows:
        cells = _table_cells(line)
        verdict = cells[2] if len(cells) >= 4 else ""
        if not (verdict == "ok" or verdict.startswith("problem")):
            issues.append(f"{where}: verdict '{verdict[:30]}' is neither ok nor problem ({line[:40]}...).")
        for ref in re.findall(finding, verdict):
            if not re.search(rf"^\s*\d+\.\s*\**{re.escape(ref)}\b", body, re.MULTILINE):
                issues.append(f"{where}: {ref} is cited but not listed under Findings.")
    return issues


def group_ledger_issues(index: int, expected_acs: list[str], body: str) -> list[str]:
    """One ledger row per AC of the group, a valid verdict, and every cited finding listed."""

    if not re.search(rf"^#{{1,3}} [^\n]*\bGroup {index}\b", body, re.MULTILINE):
        return [f"Group {index}: section missing."]
    rows = _ac_rows(body)
    seen = [re.match(r"\|\s*(AC-\d+)", line).group(1) for line in rows]
    issues = [f"Group {index}: no ledger row for {ac}." for ac in expected_acs if ac not in seen]
    issues += [f"Group {index}: {ac} has {seen.count(ac)} rows." for ac in sorted(set(seen), key=_num) if seen.count(ac) > 1]
    return issues + _verdict_issues(f"Group {index}", body, rows, r"G\d+-\d+")


def global_rows(body: str) -> list[tuple[str, set[str]]]:
    """(first cell, cited units) of every row of the global turn's tables."""

    rows = []
    for line in body.splitlines():
        cells = _table_cells(line)
        if line.startswith("|") and len(cells) >= 3 and not set("".join(cells)) <= set("-: ") and cells[0] not in {"UC", "Unit", "Check", "Row"}:
            rows.append((cells[0][:80], set(_UNIT.findall(line))))
    return rows


def global_issues(body: str) -> list[str]:
    """The global section exists and every finding its verdicts cite is listed (`## Global` or `# Review — Global`)."""

    heading = re.search(r"^#{1,3} [^\n]*\bGlobal\b", body, re.MULTILINE)
    if not heading:
        return ["Global section missing."]
    tail = body[heading.start():]
    issues = []
    for cited in re.findall(r"problem:\s*([^|]*)", tail):
        for ref in re.findall(r"GL-\d+", cited):
            if not re.search(rf"^\s*\d+\.\s*\**{ref}\b", tail, re.MULTILINE):
                issues.append(f"Global: {ref} is cited but not listed under Findings.")
    return sorted(set(issues))


def finding_ids(review: str) -> list[str]:
    found = re.findall(r"^(?:\s*\d+\.\s*\**|#{2,4}\s+)(F-\d+)\b", review, re.MULTILINE)
    return sorted(set(found), key=_num)


def responses(text: str) -> dict[str, tuple[str, str]]:
    # [ \t] not \s: a line without a reason must not swallow the next line.
    pattern = r"^[ \t]*[-*][ \t]*\**(F-\d+)\**[ \t]*:[ \t]*\**(fixed|rejected)\**[ \t]*[-–—:]?[ \t]*(.*)$"
    return {f: (status.lower(), rest.strip()) for f, status, rest in re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)}


# Revision rounds ---------------------------------------------------------------------------------------------------

def _norm(block: str) -> str:
    return "\n".join(line.rstrip() for line in block.strip().splitlines())


def ledger_units(group_review: str) -> dict[str, set[str]]:
    """AC -> the units its ledger row cites (the reviewer's own evidence of what the verdict rests on)."""

    cited: dict[str, set[str]] = {}
    for line in group_review.splitlines():
        match = re.match(r"\|\s*(AC-\d+)\b", line)
        if match:
            cited.setdefault(match.group(1), set()).update(_UNIT.findall(line))
    return cited


def design_diff(before: str, after: str, before_name: str, after_name: str) -> str:
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), before_name, after_name, n=1, lineterm=""))


def rereview_scope(
    before: str,
    after: str,
    review: str,
    response: str,
    scenario_map: dict[str, dict[str, list[str]]],
    round_groups: list[list[str]],
    ledgers: dict[int, str] | None = None,
    global_review: str = "",
) -> dict[str, object]:
    """What the next review round covers, decided from the design diff (owner rule after pilot v3, plan 8b):
    - changed units = U-n blocks whose text changed; a changed DD/Q no applying unit follows is reported instead of
      spreading through Decided by;
    - a scenario is affected when a changed unit is on its path or reached from it through Uses, or its Scenario paths /
      Endpoint branches rows changed; `round_groups` stay fixed from round 1;
    - inside an affected scenario an AC is re-checked when its previous ledger row cites a changed unit, cites no unit
      (fail-safe), is missing, or its scenario's table rows changed; every other AC keeps its verdict;
    - the global turn re-checks only its rows citing a changed unit (all of it when no previous global review exists);
    - one fix check per F-n against the Designer's response."""

    ledgers = ledgers or {}
    b_defs, a_defs = blocks(before), blocks(after)
    changed = sorted(
        (i for i in set(b_defs) | set(a_defs) if _norm(b_defs.get(i, "")) != _norm(a_defs.get(i, ""))),
        key=lambda i: (i.split("-")[0], _num(i)),
    )
    status = {i: "added" if i not in b_defs else "removed" if i not in a_defs else "changed" for i in changed}
    changed_units = {i for i in changed if i.startswith("U-")}
    b_units = {k: v for k, v in b_defs.items() if k.startswith("U-")}
    a_units = {k: v for k, v in a_defs.items() if k.startswith("U-")}
    decision_issues = []
    for decision in (i for i in changed if not i.startswith("U-") and i in a_defs):
        appliers = {u for u, block in a_units.items() if decision in re.findall(r"\b(?:DD|Q)-\d+\b", field(block, "Decided by"))}
        if not appliers & changed_units:
            decision_issues.append(
                f"{decision} {status[decision]} but no unit applying it changed "
                f"(applied by {', '.join(sorted(appliers, key=_num)) or 'no unit'})."
            )
    graph = {u: sorted(set(uses(a_units.get(u, ""))) | set(uses(b_units.get(u, "")))) for u in set(a_units) | set(b_units)}

    rows_b, rows_a = scenario_rows(before), scenario_rows(after)
    reasons: dict[str, list[str]] = defaultdict(list)
    table_changed: set[str] = set()
    for sc in scenario_map:
        path = {u for rows in (rows_b, rows_a) for row in rows.get(sc, []) for u in _UNIT.findall(row[1])}
        on_path = sorted(path & changed_units, key=_num)
        below = sorted((reach(graph, path) - path) & changed_units, key=_num)
        if on_path:
            reasons[sc].append(f"path unit changed: {', '.join(on_path)}")
        if below:
            reasons[sc].append(f"unit reached through Uses changed: {', '.join(below)}")
        if [r[1:] for r in rows_b.get(sc, [])] != [r[1:] for r in rows_a.get(sc, [])]:
            reasons[sc].append("Scenario paths row changed")
            table_changed.add(sc)
    for row in sorted(endpoint_rows(before) ^ endpoint_rows(after)):
        for sc in re.findall(r"\bSC-\d+\b", row[3]):
            why = f"Endpoint branches row changed: {row[0]} / {row[1]}"
            if why not in reasons[sc]:  # an edited row is both a removed and an added row
                reasons[sc].append(why)
            table_changed.add(sc)

    plan_groups = []
    for index, members in enumerate(round_groups, start=1):
        hit = [sc for sc in members if sc in reasons]
        cited = ledger_units(ledgers.get(index, ""))
        acs = sorted({ac for sc in members for ac in scenario_map[sc]["acs"]}, key=_num)
        recheck: dict[str, str] = {}
        for ac in acs:
            own = [sc for sc in hit if ac in scenario_map[sc]["acs"]]
            if not own:
                continue
            if ac not in cited:
                recheck[ac] = "no ledger row"
            elif not cited[ac]:
                recheck[ac] = "ledger row cites no unit"
            elif cited[ac] & changed_units:
                recheck[ac] = "ledger cites " + ", ".join(sorted(cited[ac] & changed_units, key=_num))
            elif set(own) & table_changed:
                recheck[ac] = "table rows of " + ", ".join(sorted(set(own) & table_changed, key=_num)) + " changed"
        plan_groups.append({"group": index, "scenarios": members, "affected_scenarios": hit, "recheck_acs": recheck,
                            "keep_acs": [ac for ac in acs if ac not in recheck], "rerun": bool(recheck)})

    rows = global_rows(global_review)
    global_recheck = [{"row": label, "cites": sorted(found & changed_units, key=_num)} for label, found in rows if found & changed_units]
    found, answered = finding_ids(review), responses(response)
    fix_checks = [{"finding": f, "designer": answered.get(f, ("missing", ""))[0], "note": answered.get(f, ("", ""))[1]} for f in found]
    response_issues = [f"{f}: no response line." for f in found if f not in answered]
    response_issues += [f"{f}: rejected without a reason." for f in found if answered.get(f, ("", "x"))[0] == "rejected" and not answered[f][1]]
    response_issues += [f"{f}: answered but not a finding of the review." for f in answered if f not in found]
    return {
        "changed_blocks": [{"id": i, "status": status[i]} for i in changed],
        "decision_issues": decision_issues,
        "affected_scenarios": {sc: reasons[sc] for sc in sorted(reasons, key=_num)},
        "groups": plan_groups,
        # fail-safe: without a previous global review, any changed unit reruns the whole global turn
        "global": {"rerun": bool(global_recheck) or (not rows and bool(changed_units)), "rows_total": len(rows),
                   "recheck_rows": global_recheck},
        "fix_checks": fix_checks,
        "response_issues": response_issues,
    }


def round_issues(scope: dict[str, object], files: dict[str, str]) -> list[str]:
    """Mechanical checks of a re-review round. `files` maps `group-<n>`, `global` and `final` to their text (missing
    keys mean the file was not written): every voided row has exactly one new row, no extra rows, every cited finding
    is listed, the global turn answers every voided row, and the final fix-check table has a valid status per F-n."""

    issues = []
    for group in (g for g in scope["groups"] if g["rerun"]):
        body = files.get(f"group-{group['group']}", "")
        if not body:
            issues.append(f"Group {group['group']}: file missing.")
            continue
        rows = _ac_rows(body)
        seen = [re.match(r"\|\s*(AC-\d+)", line).group(1) for line in rows]
        issues += [f"Group {group['group']}: no row for {ac}." for ac in group["recheck_acs"] if ac not in seen]
        issues += [f"Group {group['group']}: {ac} has {seen.count(ac)} rows." for ac in sorted(set(seen), key=_num) if seen.count(ac) > 1]
        issues += [f"Group {group['group']}: row for {ac}, which was not voided." for ac in sorted(set(seen), key=_num) if ac not in group["recheck_acs"]]
        issues += _verdict_issues(f"Group {group['group']}", body, rows, r"R\d+-G\d+-\d+")
    if scope["global"]["rerun"]:
        body = files.get("global", "")
        rows = [line for line in body.splitlines() if line.startswith("|") and len(line.split("|")) >= 5
                and not set(line) <= set("|-: ") and not re.match(r"\|\s*Row\s*\|", line)]
        expected = len(scope["global"]["recheck_rows"]) or scope["global"]["rows_total"]
        if scope["global"]["recheck_rows"] and len(rows) != expected:
            issues.append(f"Global: {len(rows)} rows for {expected} voided rows.")
        if not body:
            issues.append("Global: file missing.")
        issues += _verdict_issues("Global", body, rows, r"R\d+-GL-\d+")
    if "final" in files:
        table = re.search(r"^## Fix checks\s*$(.*?)(?=^## |\Z)", files["final"], re.MULTILINE | re.DOTALL)
        statuses = {}
        for line in (table.group(1) if table else "").splitlines():
            cells = [cell.strip(" *") for cell in _table_cells(line)]
            if len(cells) >= 3 and re.fullmatch(r"F-\d+", cells[0]):
                statuses[cells[0]] = cells[1].lower()
        for check in scope["fix_checks"]:
            status = statuses.get(check["finding"])
            if status is None:
                issues.append(f"Final: no fix-check row for {check['finding']}.")
            elif status not in FIX_STATUSES:
                issues.append(f"Final: {check['finding']} status '{status}' is not one of {', '.join(FIX_STATUSES)}.")
    return issues


# Repository facts --------------------------------------------------------------------------------------------------

# Any JSX element named `...Route` (`<Route>`, a repository's own `<UserOnlyRoute>` wrapper) with a literal path.
_WEB_ROUTE = re.compile(r"<[A-Z]\w*Route\b[^>]*?\bpath\s*=\s*(?:\{\s*)?['\"]([^'\"]+)['\"]|<Route\b[^>]*?\bpath\s*=\s*(?:\{\s*)?['\"]([^'\"]+)['\"]")


def route_inventory(root: Path, component_roots: dict[str, list[str]]) -> list[str]:
    """Existing routes of the repository: `METHOD /path` for every literal API route the profile's `api` component
    registers (mount prefixes included, as repo discovery reads them) and `web /path` for every `<Route path=...>` of the
    `web` component. Every route must appear in the Design, as a unit or an unchanged surface."""

    from ...repo_context.landscape import _literal_route_anchors  # one route reader for discovery and design

    def sources(component: str, suffixes: tuple[str, ...]) -> dict[str, str]:
        found = {}
        for relative in component_roots.get(component, []):
            base = (root / relative).resolve()
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix in suffixes and "node_modules" not in path.parts and ".test." not in path.name:
                    found[path.resolve().relative_to(root.resolve()).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        return found

    api = []
    for anchor in _literal_route_anchors(sources("api", (".ts", ".mts")), component_id="api"):
        method, _, route = str(anchor["symbol"]).partition(" ")
        api.append(f"{method} {route.strip(chr(34) + chr(39) + '`')}")
    web = [
        f"web {path}" for text in sources("web", (".tsx", ".jsx", ".ts")).values()
        for groups_found in _WEB_ROUTE.findall(text) for path in groups_found if path and path != "*"
    ]
    return sorted(set(api)) + sorted(set(web), key=web.index)


# JSON trace --------------------------------------------------------------------------------------------------------

def _list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def design_trace_issues(trace: object, markdown: str, requirement_trace: dict[str, object]) -> list[str]:
    """The converted JSON states exactly what the document states: same IDs and the same Kind, Serves, Decided by and
    Uses per unit, the same Scenario paths; it adds no analysis."""

    keys = ("units", "scenario_paths", "endpoint_branches", "design_decisions")
    if not isinstance(trace, dict) or trace.get("schema_version") != DESIGN_TRACE_SCHEMA_VERSION or not all(
        isinstance(trace.get(key), list) for key in keys
    ):
        return [f'The file must be one object with "schema_version": "{DESIGN_TRACE_SCHEMA_VERSION}" and the lists {", ".join(keys)}.']
    issues = []
    doc_units = units(markdown)
    json_units = {str(u.get("id")): u for u in trace["units"] if isinstance(u, dict)}
    issues += [f"The document defines {u}, but the JSON has no unit {u}." for u in sorted(set(doc_units) - set(json_units), key=_num)]
    issues += [f"The JSON unit {u} is not defined in the document." for u in sorted(set(json_units) - set(doc_units), key=_num)]
    for ident in sorted(set(doc_units) & set(json_units), key=_num):
        block, item = doc_units[ident], json_units[ident]
        expected = {
            "kind": unit_kind(block),
            "status": (field(block, "Status").split() or [""])[0],
            "serves": sorted(re.findall(r"\b(?:AC|IMP)-\d+\b", field(block, "Serves")), key=lambda i: (i.split("-")[0], _num(i))),
            "decided_by": sorted(re.findall(r"\b(?:Q|DD|D)-\d+\b", field(block, "Decided by")), key=lambda i: (i.split("-")[0], _num(i))),
            "uses": sorted(uses(block), key=_num),
        }
        for name, value in expected.items():
            got = item.get(name)
            got = sorted(_list(got), key=lambda i: (i.split("-")[0], _num(i))) if isinstance(got, list) else got
            if got != value:
                issues.append(f"{ident}: {name} is {got!r} in the JSON but {value!r} in the document.")
        if not _list(item.get("files")):
            issues.append(f"{ident}: files must list the unit's files.")
    doc_paths = {sc: _UNIT.findall(rows[0][1]) for sc, rows in scenario_rows(markdown).items()}
    json_paths = {str(p.get("scenario")): _list(p.get("units")) for p in trace["scenario_paths"] if isinstance(p, dict)}
    if doc_paths != json_paths:
        issues.append("scenario_paths differ from the document's Scenario paths table (same scenarios, units in order).")
    doc_dd = {i for i in blocks(markdown) if i.startswith("DD-")}
    json_dd = {str(d.get("id")) for d in trace["design_decisions"] if isinstance(d, dict)}
    if doc_dd != json_dd:
        issues.append(f"design_decisions ids {sorted(json_dd, key=_num)} differ from the document {sorted(doc_dd, key=_num)}.")
    return issues


def complete_design_trace(trace: dict[str, object], markdown: str, requirement_trace: dict[str, object]) -> dict[str, object]:
    """Python-owned part: the Designer's own requirement decisions become decisions D-n numbered after the requirement's
    decisions, and every unit's `decision_refs` names the D-n it applies (requirement or design)."""

    completed = json.loads(json.dumps(trace))
    by_question = {str(d["question_id"]): str(d["id"]) for d in requirement_trace.get("decisions") or []}
    next_number = max((_num(str(d["id"])) for d in requirement_trace.get("decisions") or []), default=0)
    asked = {str(q["id"]) for q in requirement_trace.get("questions") or []}
    decisions = []
    for ident, block in sorted(blocks(markdown).items(), key=lambda item: (item[0].split("-")[0], _num(item[0]))):
        if not ident.startswith("Q-") or ident in asked:
            continue
        next_number += 1
        decision = {
            "id": f"D-{next_number}", "question_id": ident, "answer": field(block, "Answer"), "reason": field(block, "Reason"),
            "source": AGENT_DECIDED, "answered_by": "designer", "answered_at": None,
        }
        decisions.append(decision)
        by_question[ident] = decision["id"]
    for unit in completed["units"]:
        refs = _list(unit.get("decided_by"))
        unit["decision_refs"] = sorted({by_question[q] for q in refs if q in by_question} | {r for r in refs if r.startswith("D-")}, key=_num)
    completed["design_decisions_from_questions"] = decisions
    return completed
