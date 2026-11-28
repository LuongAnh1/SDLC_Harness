from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc_harness.agents.developer.hoc_agent_state import project_agent, undelivered
from sdlc_harness.agents.developer.hoc_packets import (
    OutsideFile,
    ScopeVerdict,
    TurnEnd,
    classify_outside_files,
    parse_out_of_scope,
    parse_scope_review,
    parse_turn_end,
    reasons_only_packet,
    reviewer_scope_packet,
    turn_packet,
    write_scope,
)


@pytest.fixture
def plan() -> dict[str, object]:
    fixture = Path(__file__).parent / "fixtures" / "hoc_plan_r4" / "plan_graph_v1.json"
    return json.loads(fixture.read_bytes())


def test_r4_scopes_include_design_files_attached_tests_and_barrels(plan):
    barrels = {"apps/api/src/controllers/articlesController/index.ts"}
    shared = barrels | {"packages/db/src/index.ts"}
    for task_id, task in [("G-1", plan["groups"][0]), ("base", plan["base"]), ("integration", plan["integration"])]:
        assert write_scope(plan, [task_id], barrels=barrels) == (
            set(task["files"]) | set(task["tests"]) | set(task["new_tests"]) | shared
        )


def test_r4_classifies_outside_files_from_the_selected_schedule(plan):
    other_group_file = "apps/web/src/types/error.ts"
    other_group_test = "apps/web/src/components/App/App.test.tsx"
    base_test = "apps/api/src/view/articleViewer.test.ts"
    integration_test = "tests/e2e/article-draft-write-api.spec.ts"
    own_group_file = "packages/db/prisma/migrations/20261126000100_add_article_drafts/migration.sql"
    facts_barrel = "apps/api/src/controllers/articlesController/index.ts"
    files = {
        other_group_file, other_group_test, base_test, integration_test, own_group_file, facts_barrel,
        "packages/db/src/index.ts", "packages/db/src/adapter.ts",
    }

    def classify(mode):
        return classify_outside_files(plan, files, mode=mode, agent_id="agent-1", barrels={facts_barrel}, claims={})

    assert classify("b") == {
        other_group_file: OutsideFile("parallel", ("G-2", "base"), ("agent-2",)),
        other_group_test: OutsideFile("parallel", ("G-2",), ("agent-2",)),
        base_test: OutsideFile("shared", ("base",)),
        integration_test: OutsideFile("shared", ("integration",)),
        own_group_file: OutsideFile("shared", ("G-8",)),
        "packages/db/src/adapter.ts": OutsideFile("unowned", ()),
    }
    result = classify("a")
    assert all(file.kind != "parallel" for file in result.values())
    assert result[other_group_file] == OutsideFile("shared", ("G-2", "base"))
    assert result[other_group_test] == OutsideFile("shared", ("G-2",))


def test_base_turn_sees_later_task_files_as_shared(plan):
    files = {"apps/web/src/types/error.ts", "apps/web/src/components/App/App.test.tsx"}
    result = classify_outside_files(
        plan, files, mode="b", agent_id="base", barrels=set(), claims={"src/claimed.ts": "agent-2"}, concurrent=(),
    )
    assert {path: item.kind for path, item in result.items()} == dict.fromkeys(files, "shared")
    assert result["apps/web/src/components/App/App.test.tsx"].tasks == ("G-2",)
    tasks = ["base"]
    packet = turn_packet(
        plan, tasks, mode="b", role="base", barrels=set(), new_design_files=set(),
        typecheck=["npm", "run", "typecheck"], unit=["npm", "run", "test:unit"], events=[],
    )
    assert "No agent has started yet: the gate runs typecheck and unit tests on the whole repository" in packet
    assert "never accepted" not in packet


def test_claims_take_precedence_over_plan_ownership():
    empty_task = {"files": [], "tests": [], "new_tests": []}
    plan = {
        "base": {**empty_task, "files": ["src/base.ts"]},
        "integration": empty_task,
        "groups": [{"group_id": "G-1", **empty_task, "new_tests": ["src/group.test.ts"]}],
        "schedule": {"mode_a": {"agents": [{"agent_id": "agent-1", "groups": ["G-1"]}]}},
        "conflicts": [],
    }
    claims = {"src/base.ts": "agent-2", "src/claimed.ts": "agent-2", "src/index.ts": "agent-2"}
    assert classify_outside_files(
        plan, [*claims, "src/group.test.ts"], mode="a", agent_id="agent-1", barrels=["src/index.ts"], claims=claims,
    ) == {
        "src/base.ts": OutsideFile("parallel", ("base",), ("agent-2",)),
        "src/claimed.ts": OutsideFile("parallel", (), ("agent-2",)),
        "src/group.test.ts": OutsideFile("shared", ("G-1",)),
    }


def test_r4_group_packet_includes_intents_and_undelivered_feedback(plan):
    tasks = plan["schedule"]["mode_a"]["agents"][0]["turns"][0]
    check = {
        "type": "TurnChecked", "agent_id": "agent-1", "version": 1,
        "seq": 1, "sha": "a1b2c3d", "groups": ["G-1"], "result": "queued",
    }
    events = [
        check, {"type": "Delivered", "agent_id": "agent-1", "version": 2, "delivered_version": 1},
        {"type": "MergeFinished", "agent_id": "agent-1", "version": 3, "sha": "a1b2c3d",
         "result": "integration_gate_failed", "integration_head": "234abcd",
         "gate": {"stage": "unit", "summary": "unit failed", "log_ref": "gate.log"}, "suspects": ["123abcd"]},
        {**check, "version": 4, "agent_id": "agent-2", "sha": "unrelated"},
        {"type": "ScopeExtended", "agent_id": "agent-1", "version": 5, "groups": ["G-1"],
         "files": ["packages/db/src/adapter.ts"]},
    ]
    state = project_agent("agent-1", "hoc/agent-1", {"G-1": plan["groups"][0]["units"]}, events, fix_budget=2)
    packet = turn_packet(
        plan, tasks, mode="a", role="agent-1", barrels={"apps/api/src/controllers/articlesController/index.ts"},
        new_design_files={"packages/db/src/article/articleSaveDraftPrisma.ts"},
        typecheck=["npm", "run", "typecheck"], unit=["npm", "run", "unit", "--", "filter value"],
        events=undelivered(state, events), extensions=["packages/db/src/adapter.ts"],
    )
    assert "Mode A." in packet
    assert "your own agent worktree" in packet
    assert "The Harness commits" in packet
    assert "docs/sdlc-local/" in packet
    assert "`G-1` · U-5, U-6" in packet
    assert "`G-8`" not in packet
    assert "packages/db/src/article/articleSaveDraftPrisma.ts` (new)" in packet
    assert "packages/db/src/article/articleSlug.test.ts` (new)" in packet
    assert "articlesCreateValidator.test.ts" in packet
    assert "| Proves | Level | File | What it checks |" in packet
    intent = next(row for row in plan["test_intents"] if row["task"] == "G-1")
    assert intent["intent"] in packet
    assert "article-draft-write-api.spec.ts" not in packet
    assert "an intent states what must be proven, not a literal assert; if an intent contradicts the Requirement, " \
        "follow the Requirement and say so in your answer" in packet
    assert '["npm", "run", "unit", "--", "filter value"]' in packet
    assert "packages/db/src/adapter.ts` (extended)" in packet
    assert "keep the changes and add a `## Out of scope` table" in packet
    assert "| File | Why |" in packet
    assert "A reviewer decides before anything is committed." in packet
    assert "Files owned by or already claimed by another agent are never accepted." in packet
    assert "Never run docker, database clients or migrations" in packet
    assert "BLOCKED: <file> <why>" in packet
    feedback = packet.split("## Since your last turn", 1)[1]
    assert "a1b2c3d" in feedback
    assert "integration_gate_failed (unit) unit failed gate.log" in feedback
    assert "suspects 123abcd" in feedback
    assert "Scope extended for G-1 · files packages/db/src/adapter.ts" in feedback
    assert "queued" not in feedback
    assert "unrelated" not in feedback


def test_r4_mode_b_packet_contains_all_groups_in_the_scheduled_turn(plan):
    tasks = plan["schedule"]["mode_b"]["agents"][0]["turns"][0]
    packet = turn_packet(
        plan, tasks, mode="b", role="agent-1", barrels=(), new_design_files=(),
        typecheck=["tsc"], unit=["vitest", "run"],
    )
    assert "Other agents work in parallel" in packet
    assert "`G-1`" in packet and "`G-8` · U-2" in packet
    assert "20261126000100_add_article_drafts/migration.sql" in packet


def test_slice_scope_limits_design_files_to_its_units(plan):
    group = plan["groups"][0]
    group["slices"] = [group["units"][:1], group["units"][1:]]
    scope = write_scope(plan, ["G-1#1"], barrels=())
    assert "packages/db/src/slugfy.ts" in scope
    assert "packages/db/src/article/articleCreatePrisma.ts" not in scope
    assert set(group["tests"] + group["new_tests"]) <= scope


@pytest.mark.parametrize(("fix", "details"), [
    ({"kind": "own_gate_failed", "log_ref": "own.log"}, ["own_gate_failed", "own.log"]),
    ({"kind": "merge_conflict", "files": ["src/file.ts"]}, ["merge_conflict", "src/file.ts"]),
    ({"kind": "integration_gate_failed", "log_ref": "integration.log", "suspects": ["123abcd"]},
     ["integration_gate_failed", "integration.log", "123abcd"]),
    ({"kind": "reviewer", "finding_id": "F-2"}, ["Reviewer finding", "F-2"]),
])
def test_fix_packet_reports_failure_and_latest_h(plan, fix, details):
    packet = turn_packet(
        plan, ["G-1"], mode="b", role="agent-1", barrels=(), new_design_files=(),
        typecheck=["tsc"], unit=["vitest", "run"], fix=fix,
    )
    assert "# Fix turn" in packet
    assert "The Harness already merged the latest H into your branch." in packet
    assert all(detail in packet for detail in details)


def test_scope_rejected_fix_packet_explains_the_uncommitted_changes(plan):
    files = ["src/outside.ts", "src/new.ts"]
    causes = [
        {"file": files[0], "cause": "parallel", "detail": "Owned by G-2 on agent-2"},
        {"file": files[1], "cause": "review_rejected", "detail": "The unit does not need this adapter."},
    ]
    packet = turn_packet(
        plan, ["G-1"], mode="b", role="agent-1", barrels=(), new_design_files=(),
        typecheck=["tsc"], unit=["vitest", "run"], fix={"kind": "scope_rejected", "causes": causes},
        events=[
            {"type": "TurnChecked", "result": "scope_rejected", "groups": ["G-1"], "files": files, "causes": causes},
            {"type": "TurnChecked", "result": "no_changes", "groups": ["G-2"]},
        ],
    )
    assert "The Harness did not commit anything." in packet
    assert "Your changes are still in the worktree." in packet
    assert "`src/outside.ts` · `parallel` · Owned by G-2 on agent-2" in packet
    assert "`src/new.ts` · `review_rejected` · The unit does not need this adapter." in packet
    assert "Undo your changes to those files or return `BLOCKED: <file> <why>`." in packet
    assert "The Harness already merged the latest H into your branch." not in packet
    feedback = packet.split("## Since your last turn", 1)[1]
    assert "scope_rejected for G-1 · files src/outside.ts, src/new.ts" in feedback
    assert "no_changes for G-2" in feedback


def test_reasons_only_packet_lists_files_and_forbids_edits():
    packet = reasons_only_packet(["src/outside.ts", "src/new.ts"])
    assert "Do not change any file." in packet
    assert "- `src/outside.ts`" in packet and "- `src/new.ts`" in packet
    assert "Add the `## Out of scope` table with columns `| File | Why |` for exactly these files." in packet
    assert "End with `DONE` or `BLOCKED: <file> <why>`." in packet


def test_reviewer_scope_packet_supplies_tasks_reasons_and_diff(plan):
    outside = {"src/base.ts": OutsideFile("shared", ("base",)), "src/new.ts": OutsideFile("unowned", ())}
    reasons = {"src/base.ts": "Expose the adapter.", "src/new.ts": "Support a new branch."}
    diff = "diff --git a/src/base.ts b/src/base.ts\n+export { adapter };"
    packet = reviewer_scope_packet(plan, ["G-1"], outside=outside, reasons=reasons, diff=diff)
    assert "# reviewer-scope" in packet
    assert "Read-only review. Do not change any file." in packet
    assert "`G-1` · U-5, U-6" in packet
    assert "| src/base.ts | shared | base | Expose the adapter. |" in packet
    assert "| src/new.ts | unowned | none | Support a new branch. |" in packet
    assert diff in packet
    assert "needed for the turn's units and acceptable for the tasks that own or use the file." in packet
    assert "| File | Verdict | Reason |" in packet


def test_reviewer_scope_packet_rejects_parallel_files(plan):
    with pytest.raises(ValueError, match="Only shared or unowned"):
        reviewer_scope_packet(
            plan, ["G-1"], outside={"src/other.ts": OutsideFile("parallel", ("G-2",), ("agent-2",))},
            reasons={"src/other.ts": "Need an API."}, diff="",
        )


_OUT_OF_SCOPE = "## Out of scope\n\n| File | Why |\n| --- | --- |\n"
_REVIEW = "| File | Verdict | Reason |\n| --- | --- | --- |\n"


def test_parse_out_of_scope_reads_reasons_before_turn_status():
    answer = (
        "Tests passed.\n\n" + _OUT_OF_SCOPE + "| src/my file.ts | Defines `A \\| B`. |\n"
        "| src/new.ts | Add an adapter. |\n\n## Checks\n\nRan unit tests.\n\nDONE\n"
    )
    assert parse_out_of_scope(answer) == {"src/my file.ts": "Defines `A | B`.", "src/new.ts": "Add an adapter."}


@pytest.mark.parametrize("answer", [
    "DONE",
    _OUT_OF_SCOPE + "DONE",
    _OUT_OF_SCOPE + "| src/new.ts | |\nDONE",
    _OUT_OF_SCOPE + "| src/new.ts | First reason. |\n| src/new.ts | Second reason. |\nDONE",
    _OUT_OF_SCOPE + "| ../src/new.ts | Not relative to the repo. |\nDONE",
    _OUT_OF_SCOPE + "| src/new.ts\t | Contains a control character. |\nDONE",
    _OUT_OF_SCOPE + "| src/new.ts | A reason. |\n",
    _OUT_OF_SCOPE + "\n| src/new.ts | A reason. |\nDONE",
    _OUT_OF_SCOPE.replace("| --- | --- |", "| -- | --- |") + "| src/new.ts | A reason. |\nDONE",
])
def test_parse_out_of_scope_rejects_malformed_tables(answer):
    with pytest.raises(ValueError):
        parse_out_of_scope(answer)


def test_parse_scope_review_requires_one_verdict_per_file():
    answer = _REVIEW + "| src/one.ts | accept | Needed for U-5. |\n| src/two.ts | reject | Conflicts with G-2. |"
    assert parse_scope_review(answer, {"src/one.ts", "src/two.ts"}) == {
        "src/one.ts": ScopeVerdict("accept", "Needed for U-5."),
        "src/two.ts": ScopeVerdict("reject", "Conflicts with G-2."),
    }


@pytest.mark.parametrize("rows", [
    "| src/one.ts | accept | Needed. |",
    "| src/one.ts | accept | Needed. |\n| src/two.ts | accept | Also needed. |\n| src/three.ts | reject | Extra. |",
    "| src/one.ts | maybe | Unclear. |\n| src/two.ts | reject | Conflicts with G-2. |",
    "| src/one.ts | accept | |\n| src/two.ts | reject | Conflicts with G-2. |",
    "| src/one.ts | accept | Needed. |\n| src/two.ts | reject | Unneeded. |\n| src/two.ts | accept | Duplicate. |",
])
def test_parse_scope_review_rejects_invalid_verdicts_or_files(rows):
    with pytest.raises(ValueError):
        parse_scope_review(_REVIEW + rows, {"src/one.ts", "src/two.ts"})


def test_parse_done_and_blocked_with_new_repo_path():
    assert parse_turn_end("Work complete.\nDONE\n\n") == TurnEnd("done")
    assert parse_turn_end("Details\r\nBLOCKED: src/new.ts needs a shared adapter\r\n") == (
        TurnEnd("blocked", "src/new.ts", "needs a shared adapter")
    )
    assert parse_turn_end('BLOCKED: "src/with space.ts" need an API') == (
        TurnEnd("blocked", "src/with space.ts", "need an API")
    )
    assert parse_turn_end("BLOCKED: LICENSE needs an update") == TurnEnd("blocked", "LICENSE", "needs an update")


@pytest.mark.parametrize("answer", [
    "", "done", "DONE.", " DONE", "DONE ", "DONE\nMore text", "`DONE`", "BLOCKED: src/other.ts",
    'BLOCKED: "src/unclosed.ts reason',
    "BLOCKED: /src/other.ts reason", "BLOCKED: C:/src/other.ts reason", "BLOCKED: C:other.ts reason",
    "BLOCKED: ../src/other.ts reason", "BLOCKED: src/./other.ts reason", "BLOCKED: src//other.ts reason",
    "BLOCKED: src/other.ts/ reason", "BLOCKED: src\\other.ts reason", "BLOCKED: .GIT/config reason",
    "BLOCKED: src/.git/config reason", "BLOCKED: src/other:ts reason", "BLOCKED: src/oth\x00er.ts reason",
    'BLOCKED: "src/oth\ter.ts" reason', "BLOCKED: src/oth\ter.ts reason", "BLOCKED: src/oth\x9fer.ts reason",
])
def test_parse_rejects_a_protocol_error(answer):
    with pytest.raises(ValueError, match="Turn protocol error"):
        parse_turn_end(answer)
