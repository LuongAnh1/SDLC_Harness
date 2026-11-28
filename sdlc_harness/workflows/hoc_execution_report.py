"""Project H-OC checkpoint metrics from the private execution journal."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

_RESULTS = ("merged", "scope_rejected", "own_gate_failed", "merge_conflict", "integration_gate_failed", "held", "no_changes")


def _seconds(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def execution_metrics(events: Sequence[dict[str, object]]) -> dict[str, object]:
    """Summarize stage, turn, commit and repair evidence without reading the worktrees."""

    starts, stages, turns, commits = {}, {}, [], {}
    active, busy = {}, Counter()
    outcomes = dict.fromkeys(_RESULTS, 0)
    scope, routes, dispositions = Counter(), [], {}
    for event in events:
        kind, agent = event["type"], event["agent_id"]
        sha = event.get("sha")
        if kind == "StageStarted":
            starts[event["stage"]] = event
        elif kind == "StageFinished":
            start = starts[event["stage"]]["started_at"]
            end = event["ended_at"]
            stages[event["stage"]] = {"started_at": start, "ended_at": end,
                                      "duration_seconds": _seconds(start, end), "status": event["status"]}
        elif kind == "TurnMeasured":
            turns.append({
                **{key: event[key] for key in ("agent_id", "role", "turn_kind", "started_at", "ended_at", "usage")},
                "duration_seconds": _seconds(event["started_at"], event["ended_at"]),
            })
        elif kind == "TurnStarted":
            active[agent] = event["started_at"]
        elif kind in {"TurnChecked", "TurnStopped"}:
            start = active.pop(agent, None)
            s3 = starts.get("S3")
            if start and s3 and "S3" not in stages and agent in s3.get("agents", ()):
                busy[agent] += _seconds(max(start, s3["started_at"]), event.get("received_at") or event["ended_at"])
            if kind == "TurnChecked":
                result = event["result"]
                if result in outcomes:
                    outcomes[result] += 1
                for cause in event.get("causes", ()):
                    scope[cause["cause"]] += 1
                if sha:
                    commits[sha] = {"agent_id": agent, "received_at": event["received_at"], "merge_started_at": None,
                                    "merged_at": None, "queue_wait_seconds": None, "merge_attempts": []}
        elif kind == "MergeStarted":
            commit = commits[sha]
            started = event["started_at"]
            commit["merge_attempts"].append(started)
            if commit["merge_started_at"] is None:
                commit["merge_started_at"] = started
                commit["queue_wait_seconds"] = _seconds(commit["received_at"], started)
        elif kind == "MergeFinished":
            outcomes[event["result"]] += 1
            outcomes["held"] += len(event.get("held", ()))
            if event["result"] == "merged":
                outcomes["merged"] += len(event.get("carried", ()))
                for merged in (sha, *event.get("carried", ())):
                    commits[merged]["merged_at"] = event["merged_at"]
        elif kind == "ScopeExtended":
            scope["extensions"] += 1
            scope["extended_files"] += len(event["files"])
        elif kind == "FixRequested" and event["kind"] == "integration":
            routes.append({"agent_id": agent, "groups": event["groups"], "requests": event["requests"]})
        elif kind == "FindingDisposition":
            dispositions.setdefault(event["finding_id"], set()).add(event["result"])

    wave_time = stages.get("S3", {}).get("duration_seconds", 0.0)
    findings_fixed = sum("fixed" in results for results in dispositions.values())
    return {
        "schema": "hoc_execution_metrics_v1", "stages": stages, "turns": turns, "commits": commits,
        "idle_seconds_s3": {agent: max(0.0, wave_time - busy[agent]) for agent in starts.get("S3", {}).get("agents", ())},
        "commit_results": outcomes, "interaction_failures": outcomes["integration_gate_failed"],
        "scope": {"extensions": scope["extensions"], "extended_files": scope["extended_files"],
                  "parallel_blocks": scope["parallel"], "reviewer_scope_rejections": scope["review_rejected"]},
        "integration_routes": routes, "flaky_specs": [],
        "findings": {"fixed": findings_fixed, "rejected": len(dispositions) - findings_fixed},
    }
