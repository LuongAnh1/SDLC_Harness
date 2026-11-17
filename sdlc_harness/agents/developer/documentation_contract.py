"""Documentation Closure (L5.5.2): a Harness-owned README section built only from proven run evidence.

No LLM (owner D1). The section lists the reviewed commands of verification stages that passed, the env the
run proved (`.env.example` keys and the demo flag of the passed browser capture), and the Browser QA outcome
per scenario. It lives between two markers in ``README.md``; the rest of the README never changes (D2).
Every input is a typed artifact; nothing here interprets use-case meaning.
"""

from __future__ import annotations

from collections.abc import Iterable
import json

DOCUMENTATION_CONTEXT_SCHEMA_VERSION = "documentation_closure_context_v1"
DOCUMENTATION_PATH = "README.md"
DOC_BLOCK_BEGIN = "<!-- sdlc-harness:run-doc begin -->"
DOC_BLOCK_END = "<!-- sdlc-harness:run-doc end -->"
# Their commands run Harness-rendered specs that are not published with the branch (owner D4).
HARNESS_ONLY_STAGE_IDS = frozenset({"runtime_probe", "browser_capture"})
DEMO_BUILD_STAGE_ID = "frontend_build"


def stage_commands(spec_evidence: dict[str, object]) -> list[str]:
    """The reviewed command lines of one stage spec: argv first, then each additional argv."""

    argvs = [_texts(spec_evidence.get("argv")), *[_texts(item) for item in _lists(spec_evidence.get("additional_argv"))]]
    return [" ".join(argv) for argv in argvs if argv]


def proven_commands(
    *,
    specs: Iterable[dict[str, object]],
    stage_results: Iterable[dict[str, object]],
) -> dict[str, list[str]]:
    """Commands of every stage that passed, in policy order; Harness-only stages excluded."""

    passed = {_text(item.get("stage_id")) for item in stage_results if _text(item.get("status")) == "passed"}
    return {
        _text(spec.get("stage_id")): stage_commands(spec)
        for spec in specs
        if _text(spec.get("stage_id")) in passed and _text(spec.get("stage_id")) not in HARNESS_ONLY_STAGE_IDS
    }


def parse_env_example(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def documentation_context(
    *,
    run_id: str,
    issue_key: str,
    source_verification: str,
    specs: Iterable[dict[str, object]],
    stage_results: Iterable[dict[str, object]],
    env_example: dict[str, str],
    demo_flag_env_key: str,
    scenario_reviews: Iterable[dict[str, object]],
    not_reachable: Iterable[dict[str, object]],
) -> dict[str, object]:
    results = [dict(item) for item in stage_results]
    by_stage = {_text(item.get("stage_id")): item for item in results}
    commands = proven_commands(specs=specs, stage_results=results)
    env = [
        {"name": key, "source": "env_example", "value": value}
        for key, value in sorted(env_example.items())
    ]
    capture = by_stage.get("browser_capture") or {}
    if demo_flag_env_key and _text(capture.get("status")) == "passed" and DEMO_BUILD_STAGE_ID in commands:
        recorded = _dict(_dict(capture.get("env_keys")).get("harness_constants"))
        # Results written before L5.5.1 carry no env_keys; the passed capture stage itself is then the proof.
        if not capture.get("env_keys") or recorded.get(demo_flag_env_key) == "1":
            env.append(
                {
                    "name": demo_flag_env_key,
                    "source": "demo_flag",
                    "value": "1",
                    "command": commands[DEMO_BUILD_STAGE_ID][0],
                }
            )
    reasons = {_text(item.get("obligation_id")): _text(item.get("reason")) for item in not_reachable}
    scenarios = []
    for review in scenario_reviews:
        verdicts = _dicts(review.get("verdicts"))
        blockers = _dicts(review.get("blocking_findings"))
        scenarios.append(
            {
                "scenario_id": _text(review.get("scenario_id")),
                "result": "pass" if review.get("ok") is True and not blockers else "fail",
                "satisfied": sum(1 for item in verdicts if _text(item.get("verdict")) == "satisfied"),
                "not_observable": [
                    {
                        "obligation_id": _text(item.get("obligation_id")),
                        **({"reason": reasons[_text(item.get("obligation_id"))]} if reasons.get(_text(item.get("obligation_id"))) else {}),
                    }
                    for item in verdicts
                    if _text(item.get("verdict")) == "not_observable"
                ],
            }
        )
    return {
        "schema_version": DOCUMENTATION_CONTEXT_SCHEMA_VERSION,
        "run_id": run_id,
        "issue_key": issue_key,
        "source_verification": source_verification,
        "commands": [{"stage_id": stage_id, "commands": lines} for stage_id, lines in commands.items()],
        "env": env,
        "scenarios": scenarios,
    }


def render_documentation_block(context: dict[str, object]) -> str:
    """Deterministic Markdown between the markers; LF newlines (the writer adapts them to the README)."""

    issue = _text(context.get("issue_key"))
    run_id = _text(context.get("run_id"))
    lines = [
        DOC_BLOCK_BEGIN,
        " ".join(part for part in ("## Verified run", issue, f"(`{run_id}`)") if part),
        "",
        "Generated by SDLC Harness from verification that passed for this change. Every command below ran "
        "and passed in that order; edit outside this section only.",
        "",
        "### Commands",
        "",
        "```powershell",
        *[line for item in _dicts(context.get("commands")) for line in _texts(item.get("commands"))],
        "```",
    ]
    env = _dicts(context.get("env"))
    if env:
        lines.extend(["", "### Environment", "", "| Variable | Source | Value |", "| --- | --- | --- |"])
        for item in env:
            if _text(item.get("source")) == "demo_flag":
                source = f"demo data for `{_text(item.get('command'))}`"
            else:
                source = "`.env.example`"
            lines.append(f"| `{_text(item.get('name'))}` | {source} | `{_text(item.get('value'))}` |")
    scenarios = _dicts(context.get("scenarios"))
    if scenarios:
        lines.extend(["", "### Browser QA", "", "| Scenario | Result | Satisfied | Not observable |", "| --- | --- | --- | --- |"])
        for item in scenarios:
            missing = _dicts(item.get("not_observable"))
            lines.append(
                f"| {_text(item.get('scenario_id'))} | {_text(item.get('result'))} | {item.get('satisfied', 0)} | "
                f"{len(missing)} |"
            )
        notes = [(item, row) for item in scenarios for row in _dicts(item.get("not_observable"))]
        if notes:
            lines.extend(["", "Not observable in the browser:", ""])
            for item, row in notes:
                reason = _text(row.get("reason"))
                lines.append(
                    f"- {_text(item.get('scenario_id'))} `{_text(row.get('obligation_id'))}`"
                    + (f": {reason}" if reason else "")
                )
    lines.append(DOC_BLOCK_END)
    return "\n".join(lines)


def splice_documentation_block(readme: str, block: str) -> str:
    """Replace the marked section, or append it; keep the README's own newline style."""

    newline = "\r\n" if "\r\n" in readme else "\n"
    rendered = block.replace("\n", newline)
    begin = readme.find(DOC_BLOCK_BEGIN)
    end = readme.find(DOC_BLOCK_END)
    if begin >= 0 and end > begin:
        return readme[:begin] + rendered + readme[end + len(DOC_BLOCK_END):]
    stripped = readme.rstrip("\r\n")
    return (stripped + newline + newline if stripped else "") + rendered + newline


def strip_documentation_block(readme: str) -> str:
    begin = readme.find(DOC_BLOCK_BEGIN)
    end = readme.find(DOC_BLOCK_END)
    if begin < 0 or end <= begin:
        return readme.rstrip("\r\n")
    return (readme[:begin].rstrip("\r\n") + readme[end + len(DOC_BLOCK_END):].lstrip("\r\n")).rstrip("\r\n")


def documentation_rule_issues(
    *,
    context: dict[str, object],
    block: str,
    original_readme: str,
    new_readme: str,
    proven: dict[str, list[str]],
    env_example: dict[str, str],
    demo_flag_env_key: str,
) -> list[dict[str, str]]:
    """DC1-DC5; mechanical only. Whether the section is useful is outside the Harness."""

    issues: list[dict[str, str]] = []
    proven_lines = {line for lines in proven.values() for line in lines}
    fence = documented_commands(block)
    unproven = sorted(line for line in fence if line not in proven_lines)
    if unproven:
        issues.append({"gate": "DC1", "issue": f"Commands that no passed stage ran: {unproven}"})
    allowed_env = set(env_example) | ({demo_flag_env_key} if demo_flag_env_key else set())
    named = [_text(item.get("name")) for item in _dicts(context.get("env"))]
    unknown_env = sorted(name for name in named if name not in allowed_env)
    if unknown_env:
        issues.append({"gate": "DC2", "issue": f"Env names the run did not prove: {unknown_env}"})
    if strip_documentation_block(original_readme) != strip_documentation_block(new_readme):
        issues.append({"gate": "DC3", "issue": f"{DOCUMENTATION_PATH} changed outside the Harness section."})
    allowed_values = set(env_example.values()) | {"1"}
    leaked = sorted(
        _text(item.get("name")) for item in _dicts(context.get("env")) if _text(item.get("value")) not in allowed_values
    )
    if leaked:
        issues.append({"gate": "DC4", "issue": f"Env values outside `.env.example` and reviewed constants: {leaked}"})
    if render_documentation_block(json.loads(json.dumps(context))) != block:
        issues.append({"gate": "DC5", "issue": "The section does not render deterministically from its context."})
    return issues


def documented_commands(block: str) -> list[str]:
    """Command lines inside the section's code fence, as Final Verification reruns them."""

    lines: list[str] = []
    inside = False
    for line in block.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if inside and line.strip():
            lines.append(line.strip())
    return lines


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _lists(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: object) -> str:
    return str(value or "").strip()


def _texts(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, (list, tuple)) else []
