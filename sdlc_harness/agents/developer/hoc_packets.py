"""H-OC Phase 4 packets and scope review (plan sections 2.4, 2.5, 3.1, 3.3, G9 and G14)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import re

from ...repo_context.plan_facts import clean_path

_INTENT_RULE = (
    "an intent states what must be proven, not a literal assert; "
    "if an intent contradicts the Requirement, follow the Requirement and say so in your answer"
)
_FORBIDDEN_RULE = (  # G15, enforced by the Codex transport
    "Never run docker, database clients or migrations, seed scripts, `npm run test:e2e` or other browser-test runs "
    "(except the command a Running your specs section gives), server scripts, network clients, inline node or "
    "python scripts, package installs, git commands that change history or branches, or process control: the Harness "
    "stops the run when a turn starts one."
)


@dataclass(frozen=True)
class TurnEnd:
    status: str
    path: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OutsideFile:
    kind: str
    tasks: tuple[str, ...]
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeVerdict:
    verdict: str
    reason: str


def _is_repo_path(path: str) -> bool:
    return not set(path.casefold().split("/")) & {"", ".", "..", ".git"} and not re.search(
        r"[\\:\x00-\x1f\x7f-\x9f]", path
    )


def _answer_lines(answer: str) -> list[str]:
    lines = [line.removesuffix("\r") for line in answer.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def parse_turn_end(answer: str) -> TurnEnd:
    """Parse the last non-empty line. The caller checks that a BLOCKED path is outside scope."""

    lines = _answer_lines(answer)
    line = lines[-1] if lines else ""
    if line == "DONE":
        return TurnEnd("done")
    blocked = re.fullmatch(r'BLOCKED: (?:"([^"]+)"|([^\s"]+)) +(\S.*)', line)
    if blocked:
        path = blocked[1] or blocked[2]
        if _is_repo_path(path):
            return TurnEnd("blocked", path, blocked[3])
    raise ValueError("Turn protocol error: expected DONE or BLOCKED: <repo-relative file> <why>")


def _tasks(plan: Mapping[str, object], tasks: Sequence[str]) -> list[Mapping[str, object]]:
    if not tasks:
        raise ValueError("A turn must name at least one Plan task")
    if tuple(tasks) in {("base",), ("integration",)}:
        return [plan[tasks[0]]]

    groups = {group["group_id"]: group for group in plan["groups"]}
    selected = []
    for task in tasks:
        group_id, marker, number = task.partition("#")
        group = groups[group_id]
        if marker:
            if not re.fullmatch(r"[1-9][0-9]*", number) or int(number) > len(group["slices"]):
                raise ValueError(f"Unknown Plan slice {task}")
            units = group["slices"][int(number) - 1]
            files = [path for unit in plan["units"] if unit["id"] in units for path in unit["files"]]
            group = {**group, "units": units, "files": files}
        selected.append(group)
    return selected


def _task_files(task: Mapping[str, object]) -> set[str]:
    return {clean_path(path) for key in ("files", "tests", "new_tests") for path in task[key]}


def _barrel_paths(plan: Mapping[str, object], barrels: Iterable[str]) -> set[str]:
    return set(barrels) | {
        row["file"] for row in plan["conflicts"] if row["resolution"] == "barrel_union"
    }


def write_scope(
    plan: Mapping[str, object], tasks: Sequence[str], *, barrels: Iterable[str]
) -> frozenset[str]:
    """Design files, attached tests and shared barrels for the selected Plan tasks."""

    return frozenset(_barrel_paths(plan, barrels).union(*map(_task_files, _tasks(plan, tasks))))


def classify_outside_files(
    plan: Mapping[str, object],
    files: Iterable[str],
    *,
    mode: str,
    agent_id: str,
    barrels: Iterable[str],
    claims: Mapping[str, str],
    concurrent: Collection[str] | None = None,
) -> dict[str, OutsideFile]:
    """Classify paths using task ownership and accepted claims, excluding barrels.

    `concurrent` lists the agents that may edit files while this turn runs (``None``: every scheduled agent and
    claimant). The base turn passes none: no agent has branched yet, so a later task's file is `shared`, which the
    reviewer may accept, never `parallel` (owner 2026-11-28).
    """

    if mode not in {"a", "b"}:
        raise ValueError(f"Unknown H-OC execution mode {mode!r}")
    task_files = {task: _task_files(plan[task]) for task in ("base", "integration")}
    task_files.update((group["group_id"], _task_files(group)) for group in plan["groups"])
    group_agents = {
        group: agent["agent_id"]
        for agent in plan["schedule"][f"mode_{mode}"]["agents"]
        for group in agent["groups"]
    }
    result = {}
    for path in sorted(set(files) - _barrel_paths(plan, barrels)):
        owners = tuple(sorted(task for task, paths in task_files.items() if path in paths))
        agents = {group_agents[task] for task in owners if mode == "b" and task in group_agents}
        if path in claims:
            agents.add(claims[path])
        agents.discard(agent_id)
        if concurrent is not None:
            agents &= set(concurrent)
        kind = "parallel" if agents else "shared" if owners else "unowned"
        result[path] = OutsideFile(kind, owners, tuple(sorted(agents)))
    return result


def _table_rows(lines: Sequence[str], header: tuple[str, ...]) -> list[tuple[str, ...]]:
    rows = []
    for line in "\n".join(lines).strip().split("\n"):
        cells = re.split(r"(?<!\\)\|", line.strip())
        if len(cells) < 3 or cells[0] or cells[-1]:
            raise ValueError(f"Expected a Markdown table with columns {', '.join(header)}")
        path, *values = cells[1:-1]
        rows.append((path.strip(" ").replace(r"\|", "|"), *(cell.strip().replace(r"\|", "|") for cell in values)))
    if (len(rows) < 3 or rows[0] != header or len(rows[1]) != len(header)
            or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1])):
        raise ValueError(f"Expected a non-empty Markdown table with columns {', '.join(header)}")
    for path, *values in rows[2:]:
        if len(values) != len(header) - 1 or not all(values) or not _is_repo_path(path):
            raise ValueError(f"Invalid or duplicate {header[0]} row for {path!r}")
    return rows[2:]


def _unique_rows(rows: list[tuple[str, ...]], label: str) -> dict[str, tuple[str, ...]]:
    result = {}
    for key, *values in rows:
        if key in result:
            raise ValueError(f"Invalid or duplicate {label} row for {key!r}")
        result[key] = tuple(values)
    return result


def _path_table(lines: Sequence[str], header: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return _unique_rows(_table_rows(lines, header), header[0])


def parse_out_of_scope(answer: str) -> dict[str, str]:
    """Read file reasons from the Out of scope section before the turn's status line."""

    parse_turn_end(answer)
    lines = _answer_lines(answer)[:-1]
    headings = [index for index, line in enumerate(lines) if line.strip() == "## Out of scope"]
    if len(headings) != 1:
        raise ValueError("Expected one ## Out of scope section before the turn status")
    section = lines[headings[0] + 1:]
    end = next((i for i, line in enumerate(section) if re.match(r"#{1,6} ", line.strip())), len(section))
    return {path: values[0] for path, values in _path_table(section[:end], ("File", "Why")).items()}


def parse_scope_review(answer: str, files: Collection[str]) -> dict[str, ScopeVerdict]:
    """Require one accept or reject verdict with a reason for every reviewed file."""

    rows = _path_table(_answer_lines(answer), ("File", "Verdict", "Reason"))
    if rows.keys() != set(files):
        raise ValueError("Scope review file set differs from the requested files")
    result = {}
    for path, (verdict, reason) in rows.items():
        if verdict not in {"accept", "reject"}:
            raise ValueError(f"Invalid scope review verdict for {path!r}: {verdict!r}")
        result[path] = ScopeVerdict(verdict, reason)
    return result


def reasons_only_packet(files: Iterable[str]) -> str:
    """Ask for the missing scope reasons without further edits."""

    return "\n".join([
        "# Out-of-scope reasons", "", "Do not change any file.", "",
        *(f"- `{path}`" for path in sorted(set(files))), "",
        "Add the `## Out of scope` table with columns `| File | Why |` for exactly these files.",
        "Use plain paths in the File column. End with `DONE` or `BLOCKED: <file> <why>`.", "",
    ])


def _task_lines(tasks: Sequence[str], selected: Sequence[Mapping[str, object]]) -> list[str]:
    return [f"- `{name}` · {', '.join(task['units'])}" for name, task in zip(tasks, selected)]


def reviewer_scope_packet(
    plan: Mapping[str, object],
    tasks: Sequence[str],
    *,
    outside: Mapping[str, OutsideFile],
    reasons: Mapping[str, str],
    diff: str,
) -> str:
    """Render a read-only review request for shared or unowned changes."""

    if not outside or outside.keys() != reasons.keys():
        raise ValueError("Scope review requires reasons for exactly the outside files")
    if any(file.kind not in {"shared", "unowned"} for file in outside.values()):
        raise ValueError("Only shared or unowned files can be reviewed")
    lines = [
        "# reviewer-scope", "", "Read-only review. Do not change any file.",
        "Read the approved Requirement and `design.md` in `docs/sdlc-local/`.",
        "", "## Tasks and units", "", *_task_lines(tasks, _tasks(plan, tasks)),
        "", "## Outside files", "", "| File | Class | Owning tasks | Why |", "| --- | --- | --- | --- |",
    ]
    for path, file in sorted(outside.items()):
        cells = (path, file.kind, ", ".join(file.tasks) or "none", reasons[path])
        lines.append("| " + " | ".join(cell.replace("|", r"\|").replace("\n", "<br>") for cell in cells) + " |")
    lines += [
        "", "## Outside diff", "", "```diff", diff.rstrip("\n"), "```", "",
        "For each file, decide whether the edit is needed for the turn's units and acceptable for the tasks "
        "that own or use the file.",
        "Return only a `| File | Verdict | Reason |` table with one row per listed file. "
        "Use plain paths, `accept` or `reject`, and a non-empty reason for each verdict.", "",
    ]
    return "\n".join(lines)


def _feedback(events: Iterable[Mapping[str, object]]) -> list[str]:
    lines = []
    for event in events:
        kind = event["type"]
        if kind in {"TurnChecked", "MergeFinished"}:
            result = event["result"]
            if kind == "TurnChecked" and result in {"scope_rejected", "no_changes"}:
                line = f"- {result} for {', '.join(event['groups'])}"
            else:
                line = f"- `{event['sha']}` {result}"
            if event.get("gate"):
                gate = event["gate"]
                line += f" ({gate['stage']}) {gate['summary']} {gate['log_ref']}".rstrip()
            if event.get("queue_position") is not None:
                line += f" · queue position {event['queue_position']}"
            if event.get("files"):
                line += " · files " + ", ".join(event["files"])
            if event.get("suspects"):
                line += " · suspects " + ", ".join(event["suspects"])
            for key in ("held", "released", "carried"):
                if event.get(key):
                    line += f" · {key} " + ", ".join(event[key])
            lines.append(line)
        elif kind == "FixRequested":
            lines.append(f"- Fix requested for {', '.join(event['groups'])} ({event['kind']})")
        elif kind == "ScopeExtended":
            lines.append(f"- Scope extended for {', '.join(event['groups'])} · files {', '.join(event['files'])}")
        elif kind not in {"TurnStarted", "Delivered", "AgentStopped"}:
            raise ValueError(f"Unknown H-OC event type {kind!r}")
    return lines or ["- None."]


def reviewer_base_packet(units: Sequence[str], diff_ref: str) -> str:
    return "\n".join([
        "# reviewer-base", "", "Read-only review. Do not change any file.",
        f"Read the base diff in `{diff_ref}` and the approved Requirement and Design in `docs/sdlc-local/`.",
        f"Review the base units {', '.join(units) or 'none'}. Use only their unit IDs and approved Requirement/Design IDs.",
        "", "Return `none` or a `## Findings` section with this table.",
        "", "| F-n | Units | Requirement/Design ID | Problem | Evidence |",
        "| --- | --- | --- | --- | --- |", "",
    ])


def reviewer_final_packet(plan_ref: str, diff_ref: str, pipeline_ref: str) -> str:
    return "\n".join([
        "# reviewer-final", "", "Read-only review. Do not change any file.",
        "Read the approved Requirement and Design in `docs/sdlc-local/`.",
        f"Review the Plan in `{plan_ref}`, the full diff in `{diff_ref}` and the pipeline result in `{pipeline_ref}`.",
        "Return `none` or a `## Findings` section with this table. Cite approved unit and Requirement/Design IDs.",
        "", "| F-n | Units | Requirement/Design ID | Problem | Evidence |",
        "| --- | --- | --- | --- | --- |", "",
    ])


def parse_base_review(answer: str, units: Collection[str], known_ids: Collection[str]) -> list[dict[str, object]]:
    """Validate findings against the reviewed units and approved IDs."""

    lines = _answer_lines(answer.strip())
    if lines and lines[0].strip() == "## Findings":
        lines = _answer_lines("\n".join(lines[1:]).strip())
    elif lines != ["none"]:
        raise ValueError("Expected ## Findings or none")
    if lines == ["none"]:
        return []
    rows = _path_table(lines, ("F-n", "Units", "Requirement/Design ID", "Problem", "Evidence"))
    findings = []
    for finding, (unit_text, id_text, problem, evidence) in rows.items():
        unit_ids = [value.strip() for value in unit_text.split(",")]
        refs = [value.strip() for value in id_text.split(",")]
        if not re.fullmatch(r"F-[1-9][0-9]*", finding) or set(unit_ids) - set(units) or set(refs) - set(known_ids):
            raise ValueError(f"Unknown ID in base finding {finding!r}")
        findings.append({"finding_id": finding, "units": unit_ids, "refs": refs, "problem": problem, "evidence": evidence})
    return findings


def _optional_turn_table(answer: str, title: str, header: tuple[str, ...]) -> list[tuple[str, ...]]:
    parse_turn_end(answer)
    lines = _answer_lines(answer)[:-1]
    headings = [i for i, line in enumerate(lines) if line.strip() == title]
    if not headings:
        return []
    if len(headings) != 1:
        raise ValueError(f"Expected at most one {title} section")
    section = lines[headings[0] + 1:]
    end = next((i for i, line in enumerate(section) if re.match(r"#{1,6} ", line.strip())), len(section))
    return _table_rows(section[:end], header)


def parse_product_fixes(answer: str, units: Collection[str]) -> list[dict[str, str]]:
    rows = _optional_turn_table(answer, "## Product fixes", ("Unit", "Failing check", "Evidence"))
    if {row[0] for row in rows} - set(units):
        raise ValueError("G14 product fixes cite unknown units")
    return [{"unit": unit, "failing_check": check, "evidence": evidence}
            for unit, check, evidence in rows]


def parse_rejected_findings(answer: str, finding_ids: Collection[str]) -> dict[str, str]:
    rows = _unique_rows(_optional_turn_table(answer, "## Rejected findings", ("F-n", "Evidence")), "F-n")
    if rows.keys() - set(finding_ids):
        raise ValueError("G14 rejection cites a finding not assigned to this owner")
    return {finding: evidence[0] for finding, evidence in rows.items()}


def _fix_lines(fix: Mapping[str, object]) -> list[str]:
    kind = fix["kind"]
    if kind == "scope_rejected":
        lines = [
            "- Failure `scope_rejected`. The Harness did not commit anything. Your changes are still in the worktree.",
        ]
        if fix.get("causes"):
            for cause in fix["causes"]:
                why = cause["cause"]
                if why not in {"parallel", "review_rejected"}:
                    raise ValueError(f"Unknown scope rejection cause {why!r}")
                lines.append(f"- `{cause['file']}` · `{why}` · {cause['detail']}")
        else:
            lines.append("- Outside-scope files " + ", ".join(f"`{path}`" for path in fix["files"]))
        if fix.get("conflict_markers"):
            lines.append("- Resolve the remaining conflict markers in " + ", ".join(fix["conflict_markers"]) + ".")
            lines.append("- Undo any other out-of-scope edit or return `BLOCKED: <file> <why>`.")
        else:
            lines.append("- Undo your changes to those files or return `BLOCKED: <file> <why>`.")
        return lines
    lines = [f"- Failure `{kind}`."]
    if fix.get("pending_conflicts"):
        lines.append("- The Harness started merging H. Resolve the pending conflicts in "
                     + ", ".join(fix["pending_conflicts"]) + ". The Harness will commit the merge.")
    elif fix.get("merged_latest", True):
        lines.append("- The Harness already merged the latest H into your branch.")
    if kind in {"own_gate_failed", "integration_gate_failed"}:
        lines.append(f"- Gate log `{fix['log_ref']}`")
    if kind == "integration_gate_failed":
        lines.append("- Suspect commits " + (", ".join(fix["suspects"]) or "none"))
    elif kind == "merge_conflict":
        lines.append("- Conflicting files " + ", ".join(f"`{path}`" for path in fix["files"]))
    elif kind == "reviewer":
        if fix.get("findings"):
            lines += [
                f"- `{item['finding_id']}` · {', '.join(item['units'])} · {', '.join(item['refs'])} · "
                f"{item['problem']} · {item['evidence']}" for item in fix["findings"]
            ]
            if fix.get("final"):
                lines += [
                    "Fix each assigned finding or reject it with evidence in a `## Rejected findings` section.",
                    "| F-n | Evidence |", "| --- | --- |",
                ]
            else:
                lines.append("For each finding, explain the repair or give evidence for rejecting it.")
            return lines
        finding = fix["finding_id"]
        if not re.fullmatch(r"F-[1-9][0-9]*", finding):
            raise ValueError(f"Invalid reviewer finding ID {finding!r}")
        lines.append(f"- Reviewer finding `{finding}`")
    elif kind == "integration":
        if fix.get("requests"):
            lines += [f"- {item['unit']} · {item['failing_check']} · {item['evidence']}" for item in fix["requests"]]
        else:
            lines.append(f"- Pipeline failed at `{fix['stage']}`. Read the log at `{fix['log_ref']}`.")
            if fix.get("failures"):
                lines += [
                    "- Every failing test of that stage. A browser test stops at its first failure, so a later step "
                    "of the same test may fail the same way: check the whole spec before DONE.",
                    "", "```text", str(fix["failures"]).replace("```", "'''"), "```", "",
                ]
            if fix.get("error_contexts"):
                lines.append("- Page snapshot at each failure: " + ", ".join(f"`{path}`" for path in fix["error_contexts"]))
            lines += [
                "Fix the test side or request product fixes in a `## Product fixes` section before DONE.",
                "| Unit | Failing check | Evidence |", "| --- | --- | --- |",
            ]
    elif kind != "own_gate_failed":
        raise ValueError(f"Unknown fix kind {kind!r}")
    return lines


def turn_packet(
    plan: Mapping[str, object],
    tasks: Sequence[str],
    *,
    mode: str,
    role: str,
    barrels: Iterable[str],
    new_design_files: Collection[str],
    typecheck: Sequence[str],
    unit: Sequence[str],
    events: Iterable[Mapping[str, object]] = (),
    fix: Mapping[str, object] | None = None,
    extensions: Iterable[str] = (),
    integration_pass: int | None = None,
    integration_facts: str = "",
    e2e_command: str | None = None,
) -> str:
    """Render a normal or fix turn. The caller supplies new Design paths and undelivered events."""

    if mode not in {"a", "b"}:
        raise ValueError(f"Unknown H-OC execution mode {mode!r}")
    selected = _tasks(plan, tasks)
    shared_worktree = tuple(tasks) in {("base",), ("integration",)}
    parents = {task.partition("#")[0] for task in tasks}
    if shared_worktree:
        if role != tasks[0]:
            raise ValueError(f"{role!r} cannot run the {tasks[0]!r} task")
    else:
        agent = next((row for row in plan["schedule"][f"mode_{mode}"]["agents"] if row["agent_id"] == role), None)
        if agent is None or not parents <= set(agent["groups"]):
            raise ValueError(f"{role!r} does not own these Plan groups")
        if fix is None and list(tasks) not in agent["turns"]:
            raise ValueError(f"These Plan groups do not form a scheduled turn for {role!r}")

    lines = [
        f"# {'Fix' if fix is not None else 'Work'} turn for {role}", "",
        f"Mode {mode.upper()}.",
        "Work in the integration worktree H." if shared_worktree else "Work in your own agent worktree.",
        "Other agents work in parallel on separate files (shared barrels are allowed)."
        if mode == "b" and not shared_worktree else "You are the only agent working on this task.",
        "The Harness commits your changes and runs the gate.", "",
        "Read the approved Requirement and `design.md` in `docs/sdlc-local/`.",
    ]
    if role != "base":
        lines.append("The base commit is already in your branch.")
        if plan["base"]["units"]:
            lines.append(f"Base units: {', '.join(plan['base']['units'])}.")
    if fix is not None:
        lines += ["", "## Fix", "", *_fix_lines(fix)]
    if role == "integration":
        if integration_pass is not None:
            lines += ["", "Work on check files and non-UI intents." if integration_pass == 1 else "Work on UI intents."]
        if integration_facts:
            lines += ["", "## Integration facts", "", integration_facts]
        if e2e_command:
            lines += [
                "", "## Running your specs", "",
                "The Harness has prepared the run database and the web build and started the API and web services "
                "for this turn. Run the browser specs you write or change, as often as you need, with exactly:",
                f"`{e2e_command}`",
                "Any other browser-test command stops the run. Make every spec you changed pass before DONE; the "
                "Harness still runs the full pipeline after your turn.",
            ]

    lines += ["", "## Tasks and units", ""]
    lines += _task_lines(tasks, selected)
    new_files = {clean_path(path) for path in new_design_files}
    new_files.update(clean_path(path) for task in selected for path in task["new_tests"])
    extended = set(extensions)
    scope = write_scope(plan, tasks, barrels=barrels) | extended
    lines += ["", "## Write scope", ""]
    lines += [
        f"- `{path}`" + (" (new)" if path in new_files else "") + (" (extended)" if path in extended else "")
        for path in sorted(scope)
    ] or ["- None."]
    lines += ["", "## Existing tests", ""]
    tests = {clean_path(path) for task in selected for path in task["tests"]}
    lines += [f"- `{path}`" for path in sorted(tests)] or ["- None."]
    lines += ["", "## Test intents", "", "| Proves | Level | File | What it checks |", "| --- | --- | --- | --- |"]
    for intent in plan["test_intents"]:
        if intent["task"] in parents:
            if integration_pass is not None and (intent["level"] == "ui") != (integration_pass == 2):
                continue
            cells = (", ".join(intent["proves"]), intent["level"], f"`{intent['path']}`", intent["intent"])
            lines.append("| " + " | ".join(cell.replace("|", r"\|").replace("\n", "<br>") for cell in cells) + " |")
    lines += ["", _INTENT_RULE, "", "## Allowed commands", ""]
    lines += [f"- {stage} argv `{json.dumps(list(argv), ensure_ascii=False)}`"
              for stage, argv in (("typecheck", typecheck), ("unit", unit))]
    lines.append(_FORBIDDEN_RULE)
    lines += [
        "", "If you need to edit outside the write scope, keep the changes and add a `## Out of scope` table "
        "with columns `| File | Why |` before the last line. List each outside file and its reason using plain paths.",
        "A reviewer decides before anything is committed. " + (
            "No agent has started yet: the gate runs typecheck and unit tests on the whole repository, so you may "
            "edit a later task's file only as far as your base change breaks it (for example a type or a test "
            "fixture), never to do that task's own work."
            if role == "base" else "Files owned by or already claimed by another agent are never accepted."),
        "If you cannot go on, return `BLOCKED: <file> <why>`. Use double quotes around a path with spaces. "
        "Otherwise end your answer with `DONE`.",
        "", "## Since your last turn", "", *_feedback(events), "",
    ]
    return "\n".join(lines)

