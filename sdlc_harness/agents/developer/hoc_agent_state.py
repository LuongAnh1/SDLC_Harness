"""H-OC Phase 4 agent event projection (plan sections 2.2, 2.5 and 3.4).

All event dicts carry type, agent_id and version. The additional fields are:
- TurnStarted: groups, optional session_id and fix (whether this is a repair turn).
- TurnChecked: groups, result, optional gate, files, session_id and queue_position.
  Includes sha and seq when a commit was made.
  Scope rejections may include causes (file, cause and detail for each rejected path).
- MergeFinished: sha, result, integration_head, optional gate, files, suspects, held,
  released and carried (the last three are lists of SHAs affected by the merge).
- FixRequested: groups, kind (scope, gate, conflict, reviewer or integration), optional sha, finding_id or route_id.
- ScopeExtended: groups, files.
- Delivered: delivered_version, the version of the snapshot the agent received.
- AgentStopped: reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

_EVENT_TYPES = {"TurnStarted", "TurnChecked", "MergeFinished", "FixRequested", "ScopeExtended", "Delivered", "AgentStopped"}
_CHECK_RESULTS = {"queued", "own_gate_failed", "held"}
_NO_COMMIT_RESULTS = {"scope_rejected", "no_changes"}
_MERGE_RESULTS = {"merged", "merge_conflict", "integration_gate_failed"}
_REPAIR_RESULTS = {"own_gate_failed", "merge_conflict", "integration_gate_failed"}
_FIX_KINDS = _REPAIR_RESULTS | {"scope_rejected", "reviewer", "integration"}
_SCOPE_CAUSES = {"parallel", "review_rejected"}


def _agent_events(agent_id: str, events: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    selected = []
    seen = set()
    for event in events:
        if event["type"] not in _EVENT_TYPES:
            raise ValueError(f"Unknown H-OC event type {event['type']!r}")
        if event["agent_id"] == agent_id:
            version = event["version"]
            if type(version) is not int or version < 1 or version in seen:
                raise ValueError(f"Invalid H-OC event version {version!r} for {agent_id}")
            seen.add(version)
            selected.append(event)
    return sorted(selected, key=lambda event: event["version"])


def undelivered(
    state: Mapping[str, object], events: Iterable[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    """Return this agent's events newer than its delivery cursor."""

    return [event for event in _agent_events(state["agent_id"], events) if event["version"] > state["delivered_version"]]


def scope_claims(states: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Return accepted path claims for the run, rejecting conflicting agents."""

    claims: dict[str, str] = {}
    for state in states:
        agent = state["agent_id"]
        for extension in state.get("scope_extensions", ()):
            for path in extension["files"]:
                if claims.setdefault(path, agent) != agent:
                    raise ValueError(f"Conflicting scope claims for {path!r}")
    return claims


def _gate(event: Mapping[str, object]) -> dict[str, object] | None:
    gate = event.get("gate")
    return {key: gate[key] for key in ("stage", "log_ref", "summary")} if gate is not None else None


def project_agent(
    agent_id: str,
    branch: str,
    groups: Mapping[str, Sequence[str]],
    events: Iterable[Mapping[str, object]],
    *,
    fix_budget: int,
) -> dict[str, object]:
    """Build hoc_agent_state_v1 from versioned events and the agent's scheduled groups."""

    if fix_budget < 0:
        raise ValueError("The fix budget must be non-negative")
    history = _agent_events(agent_id, events)
    commits = {}
    attempts = dict.fromkeys(groups, 0)
    requests = dict.fromkeys(groups, 0)
    routed = set()
    latest_turn = ()
    started, checked, delivered = 0, 0, 0
    session_id = None
    stopped = False
    finishes = []
    extensions = []
    for event in history:
        kind, version = event["type"], event["version"]
        if kind in {"TurnStarted", "TurnChecked", "FixRequested", "ScopeExtended"}:
            task_groups = tuple(event["groups"])
            if not task_groups or set(task_groups) - groups.keys():
                raise ValueError(f"Unknown or empty H-OC groups {task_groups!r} for {agent_id}")
        if kind == "TurnStarted":
            started, latest_turn = version, task_groups
            if event.get("fix", False):
                for group in task_groups:
                    attempts[group] += 1
        elif kind == "TurnChecked":
            checked = version
            result = event["result"]
            if result in _NO_COMMIT_RESULTS:
                if event.get("sha") is not None or event.get("seq") is not None:
                    raise ValueError(f"Unexpected commit for turn result {result!r}")
                if result == "scope_rejected":
                    for cause in event.get("causes", ()):
                        if cause.get("cause") not in _SCOPE_CAUSES:
                            raise ValueError(f"Unknown scope rejection cause {cause.get('cause')!r}")
                    for group in task_groups:
                        requests[group] = version
                        routed.discard(group)
                elif event.get("fulfilled"):
                    for group in task_groups:
                        requests[group] = 0
                        routed.discard(group)
            else:
                sha = event["sha"]
                if not sha or sha in commits or result not in _CHECK_RESULTS:
                    raise ValueError(f"Invalid check outcome for commit {sha!r}: {result!r}")
                commits[sha] = {
                    "seq": event["seq"], "sha": sha, "groups": list(task_groups), "created_version": version,
                    "result": result, "result_version": version, "integration_head": None, "gate": _gate(event),
                    "suspects": [],
                }
        elif kind == "MergeFinished":
            finishes.append(event)
        elif kind == "FixRequested":
            if event["kind"] not in _FIX_KINDS:
                raise ValueError(f"Unknown H-OC fix kind {event['kind']!r}")
            for group in task_groups:
                requests[group] = version
                if event.get("route_id"):
                    routed.add(group)
                else:
                    routed.discard(group)
        elif kind == "ScopeExtended":
            extensions.append({"version": version, "groups": list(task_groups), "files": list(event["files"])})
        elif kind == "Delivered":
            cursor = event["delivered_version"]
            if type(cursor) is not int or not delivered <= cursor < version:
                raise ValueError(f"Invalid H-OC delivery cursor {cursor!r}")
            delivered = cursor
        elif kind == "AgentStopped":
            stopped = True
        if kind in {"TurnStarted", "TurnChecked"} and event.get("session_id") is not None:
            session_id = event["session_id"]

    # Finishes can arrive alongside checks of other commits without replacing the checked turn.
    for event in finishes:
        sha, result = event["sha"], event["result"]
        if (result not in _MERGE_RESULTS or sha not in commits or commits[sha]["result"] != "queued"
                or event["version"] <= commits[sha]["created_version"]):
            raise ValueError(f"Invalid merge outcome for commit {sha!r}: {result!r}")
        if result != "merged" and (event.get("released") or event.get("carried")):
            raise ValueError(f"Invalid released or carried commits in merge outcome for {sha!r}")
        outcome = {
            "result": result, "result_version": event["version"], "integration_head": event["integration_head"],
            "gate": _gate(event), "suspects": list(event.get("suspects", ())),
        }
        commits[sha].update(outcome)
        for key, next_result, allowed in (
            ("held", "held", {"queued"}),
            ("released", "queued", {"held"}),
            ("carried", "merged", {"held"} | _REPAIR_RESULTS),
        ):
            for other in event.get(key, ()):
                if other not in commits or commits[other]["result"] not in allowed:
                    raise ValueError(f"Invalid {key} commit {other!r} in merge outcome for {sha!r}")
                if key == "carried":
                    commits[other].update(outcome, result="merged")
                else:
                    commits[other].update(result=next_result, result_version=event["version"])

    ordered = sorted(commits.values(), key=lambda commit: commit["seq"])
    working = started > checked
    group_states = []
    for group_id, units in groups.items():
        related = [commit for commit in ordered if group_id in commit["groups"]]
        last = related[-1] if related else None
        status = last["result"] if last else "pending"
        if requests[group_id] > (last["created_version"] if last else 0) or status in _REPAIR_RESULTS:
            external = requests[group_id] > (last["created_version"] if last else 0) and group_id in routed
            status = "fix_requested"
        else:
            external = False
        if working and group_id in latest_turn:
            status = "working"
        if status == "fix_requested" and not external and attempts[group_id] >= fix_budget:
            stopped = True
        group_states.append({
            "group_id": group_id, "units": list(units), "status": status, "commits": [c["sha"] for c in related],
            "fix_attempts": attempts[group_id], "fix_budget": fix_budget,
        })

    status = "idle"
    if stopped:
        status = "stopped"
    elif working:
        status = "working"
    elif all(group["status"] == "merged" for group in group_states) and all(
        commit["result"] == "merged" for commit in ordered
    ):
        status = "done"
    return {
        "schema": "hoc_agent_state_v1", "agent_id": agent_id, "state_version": history[-1]["version"] if history else 0,
        "delivered_version": delivered, "session_id": session_id, "branch": branch, "status": status,
        "groups": group_states, "commits": ordered, "scope_extensions": extensions,
    }


def needs_fix(state: Mapping[str, object]) -> bool:
    """Only a pending fix wakes an idle agent."""

    return state["status"] == "idle" and any(group["status"] == "fix_requested" for group in state["groups"])
