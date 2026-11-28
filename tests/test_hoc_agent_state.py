from __future__ import annotations

import pytest

from sdlc_harness.agents.developer.hoc_agent_state import needs_fix, project_agent, scope_claims, undelivered

GROUPS = {"G-1": ["U-1"], "G-2": ["U-2"]}
GATE = {"stage": "unit", "summary": "unit failed", "log_ref": "unit.log"}


def event(event_type, version, **fields):
    return {"type": event_type, "agent_id": "agent-1", "version": version, **fields}


def checked(version, sha, group, *, seq, result="queued", **fields):
    return event("TurnChecked", version, sha=sha, seq=seq, groups=[group], result=result, **fields)


def finished(version, sha, *, result="merged", **fields):
    return event("MergeFinished", version, sha=sha, result=result, integration_head="246abcd", **fields)


def state(events, *, groups=GROUPS, fix_budget=2):
    return project_agent("agent-1", "hoc/run/agent-1", groups, events, fix_budget=fix_budget)


def test_projected_state_shape_and_delivery_cursor():
    history = [
        event("TurnStarted", 10, groups=["G-2"]),
        checked(11, "abcdef1", "G-2", seq=3, session_id="ses_123"),
        event("Delivered", 12, delivered_version=11),
        finished(14, "abcdef1", result="integration_gate_failed", gate=GATE, suspects=["345abcd"]),
        {**event("AgentStopped", 15, reason="another agent"), "agent_id": "agent-2"},
    ]
    projected = state(history)
    assert projected == {
        "schema": "hoc_agent_state_v1", "agent_id": "agent-1", "state_version": 14, "delivered_version": 11,
        "session_id": "ses_123", "branch": "hoc/run/agent-1", "status": "idle",
        "groups": [
            {"group_id": "G-1", "units": ["U-1"], "status": "pending", "commits": [],
             "fix_attempts": 0, "fix_budget": 2},
            {"group_id": "G-2", "units": ["U-2"], "status": "fix_requested", "commits": ["abcdef1"],
             "fix_attempts": 0, "fix_budget": 2},
        ],
        "commits": [
            {"seq": 3, "sha": "abcdef1", "groups": ["G-2"], "created_version": 11,
             "result": "integration_gate_failed", "result_version": 14, "integration_head": "246abcd",
             "gate": GATE, "suspects": ["345abcd"]},
        ],
        "scope_extensions": [],
    }
    assert undelivered(projected, history) == [history[2], history[3]]
    assert needs_fix(projected)


def test_idle_agent_is_not_woken_by_a_passing_merge():
    events = [checked(1, "a", "G-1", seq=1)]
    assert state(events)["status"] == "idle"
    events.append(finished(2, "a"))
    projected = state(events)
    assert projected["status"] == "idle"
    assert not needs_fix(projected)
    only_group = state(events, groups={"G-1": ["U-1"]})
    assert only_group["status"] == "done"
    assert not needs_fix(only_group)


def test_turn_checked_and_other_commit_merge_finished_commute():
    prefix = [
        checked(1, "a", "G-1", seq=1),
        event("TurnStarted", 2, groups=["G-2"], session_id="ses_123"),
    ]
    check = checked(3, "b", "G-2", seq=2)
    merge = finished(4, "a", result="merge_conflict", files=["src/index.ts"])
    assert state([*prefix, check, merge]) == state([*prefix, merge, check])
    other_order = state([*prefix, {**merge, "version": 3}, {**check, "version": 4}])
    projected = state([*prefix, check, merge])
    assert other_order["groups"] == projected["groups"]
    assert [row["result"] for row in projected["commits"]] == ["merge_conflict", "queued"]
    assert needs_fix(projected)


def test_working_agent_waits_until_turn_boundary_to_receive_fix():
    events = [
        checked(1, "a", "G-1", seq=1),
        event("TurnStarted", 2, groups=["G-2"]),
        finished(3, "a", result="merge_conflict", files=["src/index.ts"]),
    ]
    assert state(events)["status"] == "working"
    assert not needs_fix(state(events))
    events.append(checked(4, "b", "G-2", seq=2, result="held"))
    assert needs_fix(state(events))


def test_agent_is_done_only_after_the_fix_and_carried_commits_merge():
    history = [
        checked(1, "a", "G-1", seq=1),
        checked(2, "b", "G-2", seq=2),
        finished(3, "a", result="merge_conflict", held=["b"]),
        event("FixRequested", 4, groups=["G-1"], kind="merge_conflict", sha="a"),
        event("TurnStarted", 5, groups=["G-1"], fix=True),
        checked(6, "fix", "G-1", seq=3),
    ]
    pending = state(history)
    assert not needs_fix(pending)
    assert pending["status"] == "idle"
    assert pending["groups"][0]["fix_attempts"] == 1
    assert [commit["result"] for commit in pending["commits"]] == ["merge_conflict", "held", "queued"]
    complete = state([*history, finished(7, "fix", carried=["a", "b"])])
    assert complete["status"] == "done"
    assert all(commit["result"] == "merged" for commit in complete["commits"])


def test_exhausted_fix_budget_stops_the_agent():
    events = [
        checked(1, "a", "G-1", seq=1, result="own_gate_failed", gate=GATE),
        event("TurnStarted", 2, groups=["G-1"], fix=True),
        checked(3, "fix", "G-1", seq=2, result="own_gate_failed", gate=GATE),
    ]
    assert state(events, fix_budget=1)["status"] == "stopped"
    assert not needs_fix(state(events, fix_budget=1))


@pytest.mark.parametrize("cause", ["parallel", "review_rejected"])
def test_scope_rejected_requests_fix_without_recording_a_commit(cause):
    history = [
        event("TurnStarted", 1, groups=["G-1", "G-2"]),
        event(
            "TurnChecked", 2, groups=["G-1", "G-2"], result="scope_rejected", files=["src/other.ts"],
            causes=[{"file": "src/other.ts", "cause": cause, "detail": "Reason recorded for the fix."}],
        ),
    ]
    rejected = state(history)
    assert rejected["status"] == "idle"
    assert rejected["commits"] == []
    assert [group["status"] for group in rejected["groups"]] == ["fix_requested", "fix_requested"]
    assert [group["fix_attempts"] for group in rejected["groups"]] == [0, 0]
    assert needs_fix(rejected)

    history.append(event("TurnStarted", 3, groups=["G-1", "G-2"], fix=True))
    fixing = state(history)
    assert fixing["status"] == "working"
    assert [group["fix_attempts"] for group in fixing["groups"]] == [1, 1]

    fixed = state([*history, event("TurnChecked", 4, groups=["G-1", "G-2"], result="queued", sha="a", seq=1)])
    assert [row["sha"] for row in fixed["commits"]] == ["a"]
    assert [group["status"] for group in fixed["groups"]] == ["queued", "queued"]
    assert not needs_fix(fixed)

    failed = state([*history, {**history[1], "version": 4}], fix_budget=1)
    assert failed["status"] == "stopped"
    assert failed["commits"] == []
    assert not needs_fix(failed)


def test_scope_rejected_rejects_an_unknown_cause():
    with pytest.raises(ValueError, match="Unknown scope rejection cause"):
        state([event(
            "TurnChecked", 1, groups=["G-1"], result="scope_rejected",
            causes=[{"file": "src/other.ts", "cause": "other", "detail": "Unknown reason."}],
        )])


def test_scope_extended_projects_files_without_ending_the_turn():
    first = event("ScopeExtended", 2, groups=["G-1"], files=["src/shared.ts"])
    second = event("ScopeExtended", 4, groups=["G-2"], files=["src/adapter.ts"])
    history = [
        event("TurnStarted", 1, groups=["G-1"]), first,
        event("Delivered", 3, delivered_version=2), second,
    ]
    projected = state(history, fix_budget=0)
    assert projected["status"] == "working"
    assert projected["scope_extensions"] == [
        {"version": 2, "groups": ["G-1"], "files": ["src/shared.ts"]},
        {"version": 4, "groups": ["G-2"], "files": ["src/adapter.ts"]},
    ]
    assert scope_claims([projected, projected]) == {"src/shared.ts": "agent-1", "src/adapter.ts": "agent-1"}
    assert undelivered(projected, history) == history[2:]
    assert projected["commits"] == []
    assert all(group["fix_attempts"] == 0 for group in projected["groups"])


def test_scope_claims_rejects_a_file_claimed_by_two_agents():
    accepted = event("ScopeExtended", 1, groups=["G-1"], files=["src/shared.ts"])
    first = state([accepted])
    other = project_agent(
        "agent-2", "hoc/run/agent-2", GROUPS, [{**accepted, "agent_id": "agent-2"}], fix_budget=2,
    )
    with pytest.raises(ValueError, match=r"Conflicting scope claims for 'src/shared\.ts'"):
        scope_claims([first, other])


def test_no_changes_ends_turn_and_preserves_group_status():
    history = [
        checked(1, "a", "G-1", seq=1),
        finished(2, "a"),
        event("TurnStarted", 3, groups=["G-1", "G-2"]),
        event("TurnChecked", 4, groups=["G-1", "G-2"], result="no_changes", session_id="ses_123"),
    ]
    projected = state(history)
    assert projected["status"] == "idle"
    assert projected["session_id"] == "ses_123"
    assert [group["status"] for group in projected["groups"]] == ["merged", "pending"]
    assert [row["sha"] for row in projected["commits"]] == ["a"]
    assert not needs_fix(projected)


def test_rejects_unknown_event_type():
    with pytest.raises(ValueError, match="Unknown H-OC event type"):
        state([event("SomeOtherEvent", 1)])
