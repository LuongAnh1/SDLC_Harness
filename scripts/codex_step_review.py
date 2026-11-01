"""Drive a step-reviewed Phase 2 Designer run with Codex as root-cause analyst.

Observation calls continue automatically. For every mutating Designer step, invalid output and Design Validator
response, the local Codex CLI (read-only) checks both the Harness input (prompt/context) and the model output.
A blocking defect stops the run and writes <run>/codex_findings/<call_id>.json with the root cause
(harness_context | harness_contract | model | requirement_ambiguity), evidence and a proposed Harness fix.
The lead verifies the finding, fixes the Harness if needed, then resumes with --decision approve|reject.
Minor defects that deterministic gates already repair are logged and the run continues.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHADOW = ROOT / "scripts" / "run_l3_designer_shadow.py"
AUTO_CONTINUE_TOOLS = frozenset(
    {
        "repo.read",
        "repo.search",
        "codegraph.query",
        "landscape.search",
        "landscape.get_anchor",
        "uiux.search_guidance",
        "design.inspect_partition",
    }
)
ROOT_CAUSES = frozenset({"none", "harness_context", "harness_contract", "model", "requirement_ambiguity"})
REVIEW_PROMPT = """You are the root-cause analyst for one paused LLM step of the SDLC_Harness Phase 2 Designer
(repo-aware design of a React/Fastify/Prisma use case). Do NOT modify any file. Read the exchange JSON at:
  {exchange}
It holds the exact prompt the Harness sent, the model's raw completion, the parsed action (if any) and any rejection.
Work fast: first compare the output with the prompt only. If the output is acceptable, reply verdict "ok" right away
and do NOT open any other file. Read Harness source under sdlc_harness/ only after you have found a defect and need
to locate its root cause.

Experience on this project: when the Designer model (DeepSeek) is wrong, the cause is usually the Harness input,
not the model: missing or wrong context, contradictory instructions, a scaffold or contract that invites the
mistake, Requirement IDs or repo facts the task needs but the prompt does not supply. So inspect the prompt as
carefully as the output.

0. The Designer is expected to make UX decisions. A presentation choice that reasonably follows from an approved
   outcome (for example reflecting a completed action in what the screen shows, or where focus goes) is design work,
   not invented scope. Standard technical robustness is also design work: input validation, conventional HTTP
   4xx/5xx responses and unexpected-failure handling for an approved operation (the prompt itself asks to map them).
   Flag only behavior that contradicts the Requirement or adds business capability/scope. Before confirming a
   "model" verdict, search the prompt for instructions that ask for the very thing you are about to flag.
   Partitions are built in order (ui, api, business_rule, data) and the ordered behavior path is gated only at
   bundle completion: a partition whose counterpart nodes do not exist yet may legitimately add no calls/returns
   edges. Flag missing edges only between nodes that already exist in the graph or the same delta.
1. Decide whether the output is correct for this step: follows the prompt contract, stays inside the active
   partition/repair task and requirement slice, invents no dependency/endpoint/scope, classifies UI runtime inputs
   consistently with the Requirement, and for a Design Validator response gives evidence-backed findings.
2. If it is wrong, find where the error originates and prove it with evidence quoted from the prompt and output.
   root_cause: harness_context (prompt lacks/misstates facts the task needs), harness_contract (instructions,
   scaffold or schema conflict or invite the error), model (the prompt was correct and sufficient), or
   requirement_ambiguity (the approved Requirement itself is unclear or mis-allocated; needs the human owner).
   Before blaming the model for out-of-slice behavior, check whether that behavior exists elsewhere in the approved
   Requirement (postconditions, acceptance criteria, other flow steps) but is allocated to no surface the model could
   use; a repeated out-of-slice addition is usually that allocation gap, i.e. requirement_ambiguity.
3. severity: blocking for semantic errors, Harness input problems, or anything the deterministic gates cannot
   repair; minor only for a one-off structural slip (missing edge, prop, JSON shape) that the Harness gates
   detect and route back automatically.
4. context_warnings: only Harness context problems you already noticed while reading the prompt; do not go looking for more.

Reply with ONLY one JSON object, all text in English:
{{"verdict":"ok|defect","severity":"none|minor|blocking","root_cause":"none|harness_context|harness_contract|model|requirement_ambiguity",
"summary":"<=600 chars","evidence":[{{"where":"prompt|output|harness_source","locator":"json path, quote or file:line","detail":"..."}}],
"harness_fix":"concrete Harness change (file/function when identifiable), empty if none",
"designer_note":"<=500 chars note to feed back to the Designer if this step is rejected",
"context_warnings":["..."]}}
"""


CHALLENGE_PROMPT = """A first analysis of the paused Designer step below blamed the MODEL. Are you sure it is a model error?
Experience on this project says it is usually the Harness. Re-audit the Harness side before you keep that verdict.
Exchange JSON: {exchange}
First analysis: {analysis}

Do NOT modify any file. Check, for every defect in the first analysis:
- Did the prompt state the needed rule clearly, specifically and in a findable place, or only buried in long prose?
- Did the scaffold / delta_scaffold already contain what the model had to produce, or did the model have to invent
  structure (node kind, runtime_kind, owner, evidence) the Harness could have pre-filled from data it already has?
- Did any other instruction, example, guidance result or Requirement allocation contradict or crowd out the rule?
- Was every fact the model needed (repo facts, Requirement text, ids) actually present in the prompt?
Read the relevant Harness source under sdlc_harness/ (prompt builder, scaffolds) as needed.
Keep root_cause "model" ONLY if you can quote the exact clear instruction that the model ignored AND the scaffold and
context gave it everything it needed. Otherwise reclassify as harness_context, harness_contract or
requirement_ambiguity and give a GENERAL harness_fix (valid for any use case on this profile, not tied to this UC).

Reply with ONLY the same JSON object shape as the first analysis, all text in English.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex root-cause review loop for the L3 Designer shadow runner.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--worktree-root", required=True)
    parser.add_argument("--run-id", help="Continue an existing step-review run.")
    parser.add_argument("--approved-requirement")
    parser.add_argument("--source-file")
    parser.add_argument("--decision", choices=("approve", "reject"), help="Lead decision for the pending call on resume.")
    parser.add_argument("--note", default="", help="Designer feedback when --decision reject.")
    parser.add_argument("--vertical-trace-mode", choices=("shadow", "enforce"), default="enforce")
    parser.add_argument(
        "--ui-contract-version",
        choices=("ui_experience_contract_v1", "ui_experience_contract_v2"),
        default="ui_experience_contract_v2",
    )
    parser.add_argument("--max-steps", type=int, default=150)
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--codex-effort", default="high")
    args = parser.parse_args()
    if not args.run_id and not (args.approved_requirement and args.source_file):
        parser.error("Start needs --approved-requirement and --source-file, or continue with --run-id.")
    if args.decision and not args.run_id:
        parser.error("--decision applies to a resumed --run-id.")
    if args.decision == "reject" and not args.note.strip():
        parser.error("--decision reject needs --note.")

    common = [
        "--runtime-root", args.runtime_root,
        "--target-repo", args.target_repo,
        "--worktree-root", args.worktree_root,
        "--vertical-trace-mode", args.vertical_trace_mode,
        "--ui-contract-version", args.ui_contract_version,
        "--step-review", "--review-output",
    ]
    if args.run_id:
        report = _shadow([*common, "--review-run-id", args.run_id])
    else:
        report = _shadow(
            [*common, "--approved-requirement", args.approved_requirement, "--source-file", args.source_file]
        )
    run_id = str(report["run_id"])
    run_dir = Path(args.runtime_root).resolve() / "runs" / run_id
    log_path = run_dir / "codex_step_review_log.jsonl"
    lead_decision = args.decision

    for step in range(args.max_steps):
        review = report.get("step_review") or {}
        if not review.get("required"):
            print(json.dumps({"run_id": run_id, "status": report.get("status"), "steps": step}, ensure_ascii=False))
            return 0 if report.get("status") == "waiting_design_approval" else 2
        call_id = str(review["call_id"])
        exchange_path = ROOT / str(review["exchange_path"])
        exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
        action = str((exchange.get("parsed_action") or {}).get("action") or "") or "unparsed"

        if lead_decision:
            decision, note = lead_decision, args.note
            _append(log_path, {"call_id": call_id, "action": action, "decision": decision, "note": note, "reviewer": "lead"})
            lead_decision = None
        elif action == "unparsed" and _previous_action(log_path) != "unparsed":
            # A one-off JSON syntax slip goes straight back with a precise hint; repeated slips get Codex analysis.
            from sdlc_harness.agents.designer.execution import _json_structure_hint

            decision = "reject"
            note = "Your previous output was invalid JSON." + _json_structure_hint(str(exchange.get("raw_completion") or ""))
            _append(log_path, {"call_id": call_id, "action": action, "decision": decision, "note": note, "reviewer": "auto_invalid_json"})
        elif action in AUTO_CONTINUE_TOOLS and not exchange.get("rejection"):
            decision, note = "approve", ""
            _append(log_path, {"call_id": call_id, "action": action, "decision": "approve", "reviewer": "auto_observation"})
        else:
            analysis = _codex_analysis(
                REVIEW_PROMPT.format(exchange=exchange_path), model=args.codex_model, effort=args.codex_effort
            )
            _append(log_path, {"call_id": call_id, "action": action, "reviewer": "codex", **analysis})
            if analysis["verdict"] == "defect" and analysis["root_cause"] == "model":
                # Codex tends to blame the model; force a Harness re-audit before accepting that verdict.
                analysis = _codex_analysis(
                    CHALLENGE_PROMPT.format(exchange=exchange_path, analysis=json.dumps(analysis, ensure_ascii=False)),
                    model=args.codex_model,
                    effort=args.codex_effort,
                )
                _append(log_path, {"call_id": call_id, "action": action, "reviewer": "codex_challenge", **analysis})
            if analysis["verdict"] == "defect" and analysis["severity"] != "minor":
                finding = run_dir / "codex_findings" / f"{call_id}.json"
                finding.parent.mkdir(parents=True, exist_ok=True)
                finding.write_text(
                    json.dumps({"call_id": call_id, "action": action, "exchange": str(exchange_path), **analysis},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(f"[{step + 1}] {call_id} {action} -> STOP {analysis['root_cause']}: {analysis['summary'][:300]}")
                print(json.dumps({"run_id": run_id, "status": "stopped_for_lead_review", "call_id": call_id,
                                  "finding": str(finding)}, ensure_ascii=False))
                return 3
            decision, note = "approve", ""
        print(f"[{step + 1}] {call_id} {action} -> {decision}")
        arguments = ["--review-call-id", call_id, "--review-decision", decision]
        if decision == "reject":
            arguments += ["--review-note", note]
        report = _shadow([*common, "--review-run-id", run_id, *arguments])
    print(json.dumps({"run_id": run_id, "status": "max_steps_reached"}, ensure_ascii=False))
    return 4


def _shadow(arguments: list[str]) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(SHADOW), *arguments],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ.copy(),
    )
    text = completed.stdout
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError as exc:
        raise SystemExit(f"Shadow runner returned no report (exit {completed.returncode}):\n{completed.stderr[-2000:]}") from exc


CODEX_TIMEOUT_SECONDS = 600


def _codex_analysis(prompt: str, *, model: str, effort: str) -> dict[str, object]:
    text = ""
    for _attempt in range(2):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "analysis.txt"
            try:
                subprocess.run(
                    [
                        shutil.which("codex") or "codex", "exec",
                        "-m", model,
                        "-c", f"model_reasoning_effort={effort}",
                        "-s", "read-only",
                        "-C", str(ROOT),
                        "-o", str(output),
                        prompt,
                    ],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=CODEX_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                # A stalled Codex session (network or idle wait) is retried once, then surfaced to the lead.
                print(f"Codex analysis exceeded {CODEX_TIMEOUT_SECONDS}s; retrying." if _attempt == 0 else "Codex analysis timed out twice.")
                continue
            text = output.read_text(encoding="utf-8") if output.exists() else ""
        if text:
            break
    try:
        raw = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        raw = {}
    verdict = str(raw.get("verdict") or "")
    if verdict not in {"ok", "defect"}:
        # An unusable analysis must not let the run continue silently.
        return {
            "verdict": "defect", "severity": "blocking", "root_cause": "none",
            "summary": "Codex returned no parseable analysis.", "evidence": [], "harness_fix": "",
            "designer_note": "", "context_warnings": [text[-500:]],
        }
    return {
        "verdict": verdict,
        "severity": str(raw.get("severity") or ("none" if verdict == "ok" else "blocking")),
        "root_cause": str(raw.get("root_cause") or "none") if str(raw.get("root_cause") or "none") in ROOT_CAUSES else "none",
        "summary": str(raw.get("summary") or "")[:600],
        "evidence": [item for item in raw.get("evidence") or [] if isinstance(item, dict)][:12],
        "harness_fix": str(raw.get("harness_fix") or "")[:1200],
        "designer_note": str(raw.get("designer_note") or "")[:500],
        "context_warnings": [str(item) for item in raw.get("context_warnings") or []][:10],
    }


def _previous_action(path: Path) -> str:
    if not path.exists():
        return ""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return str(json.loads(lines[-1]).get("action") or "") if lines else ""


def _append(path: Path, entry: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
