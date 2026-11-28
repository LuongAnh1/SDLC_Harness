"""Drive H-OC checkpoint and recovery commands using a local Git repository and fake turns."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest

from sdlc_harness.application.action_specs import public_dashboard_schema
from sdlc_harness.application.commands import ApplicationCommand
from sdlc_harness.application.service import ApplicationService
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.events import current_event_context
from sdlc_harness.interfaces.cli import build_parser
from sdlc_harness.profiles import CURRENT_CONTRACT_PINS, build_profile_snapshot
from sdlc_harness.workflows import hoc_execution as wiring
from sdlc_harness.workflows import hoc_execution_flow as flow
from sdlc_harness.workspaces.agent_worktrees import AgentWorktrees
from sdlc_harness.workspaces.manager import WORKSPACE_ARTIFACT
from tests import test_agent_worktrees as git_fixture
from tests import test_hoc_execution_flow as early
from tests import test_hoc_execution_flow_late as late
from tests._support import populate_typescript_target

pytestmark = early.pytestmark
workspace = git_fixture.workspace
RUN = git_fixture.RUN
PLAN = "waiting_implementation_plan_review"
BASE = "waiting_hoc_base_review"
FINAL = "waiting_hoc_execution_review"
RUNNING = "hoc_executing"
GIT = early.GIT


@pytest.fixture
def run(workspace, monkeypatch):
    worktrees, owner, h, _ = workspace
    store = worktrees.manager.store
    record = worktrees.manager.read(RUN)
    GIT(owner, "remote", "add", "origin", record.remote)
    populate_typescript_target(h)
    GIT(h, "add", "-A")
    GIT(h, "commit", "-m", "Target verification profile")
    baseline = GIT(h, "rev-parse", "HEAD")
    store.write_json_artifact(RUN, WORKSPACE_ARTIFACT, replace(record, head_sha=baseline).to_dict())
    plan = late.make_context(workspace).plan
    plan["test_rules"] = {"e2e_roots": ["tests/e2e"]}
    docs = {
        "requirement_markdown": "# Approved Requirement",
        "design_markdown": "# Approved Design",
        "requirement_trace": {"acceptance_criteria": [{"id": "R-1"}], "decisions": [{"id": "Q-1"}]},
        "design_trace": {
            "units": [{"id": "U-1", "status": "new", "files": ["src/base.ts", "src/shared.ts"]}],
            "design_decisions": [{"id": "D-1"}],
            "scenario_paths": [{"scenario": "SC-1", "units": ["U-1"]}],
        },
        "plan_graph": plan,
        "plan_graph_report": "# Approved Plan",
        "plan_repo_facts": {"barrels": [git_fixture.BARREL]},
    }
    refs = {}
    for key, data in docs.items():
        if isinstance(data, str):
            path = store.artifact_path(RUN, f"{key}.md")
            path.write_text(data, encoding="utf-8")
        else:
            path = store.write_json_artifact(RUN, f"{key}.json", data)
        refs[key] = store.relative(path)
    checkpoint = {"checkpoint_id": "checkpoint-plan", "type": "implementation_plan_review", "artifact_path": refs["plan_graph"]}
    store.write_state({
        "run_id": RUN, "source_ref": record.source_ref, "status": PLAN, "current_phase": "implementation_planning",
        "plan_opencode_flow": "plan_graph_v1", "latest_artifacts": refs,
        "approved_artifacts": {key: ref for key, ref in refs.items() if not key.startswith("plan_")},
        "pending_checkpoint": checkpoint, **CURRENT_CONTRACT_PINS,
        "profile_snapshot": build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1", target_remote=record.remote, target_base_branch="main",
        ).to_dict(),
    })
    service = ApplicationService(root=store.repo_root)
    overrides = {
        "SDLC_TARGET_REPO_PATH": str(owner), "SDLC_TARGET_WORKTREE_ROOT": str(h.parent),
        "SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock",
    }

    def execute(name, settings=None, **args):
        return service.execute(ApplicationCommand(
            name=name, arguments={"run_id": RUN, **args}, runtime_root=store.runtime_root,
            settings_overrides={**overrides, **(settings or {})},
        ))

    def default_turn(role, root, task, read_only):
        context = current_event_context()
        assert context is not None and context.run_id == RUN
        if role == "integration":
            assert "E2E roots: tests/e2e" in task
            assert "All specs share that seeded database." in task
            early.WRITE(root, "tests/api.spec.ts" if "Work on check files" in task else "tests/ui.spec.ts", "spec\n")
            return "DONE"
        if role == "reviewer-final":
            assert read_only
            assert (root / "docs/sdlc-local/plan_graph_v1.md").read_text(encoding="utf-8") == "# Approved Plan"
            return "none"
        return None

    fakes = {"turn": default_turn, "worker_args": [], "contexts": []}
    worker = early.Worker(lambda *args: fakes["turn"](*args))
    fakes["worker"] = worker
    pipeline = late.Pipeline()

    def new_worker(settings, *, run_id, model, effort, reviewer_model, reviewer_effort):
        assert run_id == RUN
        fakes["worker_args"].append((model, effort, reviewer_model, reviewer_effort))
        return worker

    def advance(context):
        fakes["contexts"].append(context)
        return flow.run_hoc_execution(replace(context, pipeline_runner=pipeline))

    monkeypatch.setattr(wiring, "CodexWorker", new_worker)
    monkeypatch.setattr(wiring, "run_hoc_execution", advance)
    def fake_gate(*, store, run_id, verification_policy, extra_stage_ids=()):  # the keywords of make_merge_gate
        assert store is not None and run_id == RUN and verification_policy.stage_ids
        fakes["gates"].append(tuple(extra_stage_ids))
        return lambda root: early.GREEN

    @contextmanager
    def fake_e2e_session(root, *, store, run_id, verification_policy):  # the keywords of integration_e2e_session
        assert run_id == RUN and root == h
        yield {}

    fakes["gates"] = []
    fakes["pipeline"] = pipeline
    monkeypatch.setattr(wiring, "make_merge_gate", fake_gate)
    monkeypatch.setattr(wiring, "integration_e2e_session", fake_e2e_session)
    monkeypatch.setattr(AgentWorktrees, "prepare", lambda *_args, **_kwargs: ())
    return execute, store, worktrees, h, baseline, checkpoint, fakes


def test_plan_approval_exposes_each_stage_and_pins_inputs_and_config(run):
    execute, store, _, h, _, checkpoint, fakes = run
    started = execute("approve-checkpoint", {
        "SDLC_HOC_CODEX_MODEL": "pinned-model", "SDLC_HOC_CODEX_EFFORT": "high", "SDLC_HOC_EXECUTION_MODE": "a",
        "SDLC_HOC_CODEX_REVIEWER_MODEL": "pinned-review-model", "SDLC_HOC_CODEX_REVIEWER_EFFORT": "medium",
    }, checkpoint_id=checkpoint["checkpoint_id"])
    assert started["status"] == BASE
    base = started["pending_checkpoint"]
    assert base["type"] == "hoc_base_review"
    assert (store.repo_root / base["artifact_path"]).is_file()
    state = store.read_state(RUN)
    execution = state["hoc_execution"]
    assert execution["plan_checkpoint"] == checkpoint
    assert execution["config"] == {
        "worker": "codex", "model": "pinned-model", "effort": "high", "mode": "a", "pause_after_base": True,
        "reviewer_model": "pinned-review-model", "reviewer_effort": "medium",
    }
    inputs = fakes["contexts"][0]
    assert inputs.known_ids == {"R-1", "Q-1", "U-1", "D-1", "SC-1"}
    assert inputs.new_design_files == {"src/base.ts"}
    assert inputs.barrels == [git_fixture.BARREL]
    assert inputs.timeout == 1800
    assert fakes["gates"]  # the base gate was built through the real keyword signature
    pinned_report = execution["input_artifacts"]["plan_graph_report"]
    assert state["approved_artifacts"]["plan_graph_report"] == pinned_report["ref"]
    source = store.repo_root / pinned_report["source"]
    source.write_text("Changed after approval", encoding="utf-8")
    calls = len(fakes["worker"].calls)
    assert execute("continue-run")["status"] == BASE
    assert len(fakes["worker"].calls) == calls

    new_settings = {
        "SDLC_HOC_WORKER": "opencode", "SDLC_HOC_EXECUTION_MODE": "b",
        "SDLC_HOC_CODEX_MODEL": "different", "SDLC_HOC_CODEX_EFFORT": "low",
        "SDLC_HOC_CODEX_REVIEWER_MODEL": "different-review", "SDLC_HOC_CODEX_REVIEWER_EFFORT": "max",
    }
    assert execute("approve-checkpoint", new_settings, checkpoint_id=base["checkpoint_id"])["status"] == RUNNING
    assert (h / "src/c.ts").is_file()
    assert not (h / "tests/api.spec.ts").exists()
    assert execute("continue-run", new_settings)["status"] == RUNNING
    assert (h / "tests/ui.spec.ts").is_file()
    final = execute("continue-run", new_settings)
    assert final["status"] == FINAL
    report = final["pending_checkpoint"]
    assert report["type"] == "hoc_execution_review"
    for key in ("artifact_path", "review_projection_ref", "metrics_ref"):
        assert (store.repo_root / report[key]).is_file()
    assert fakes["worker_args"] == [("pinned-model", "high", "pinned-review-model", "medium")] * 4
    calls = len(fakes["worker"].calls)
    assert execute("approve-checkpoint", checkpoint_id=report["checkpoint_id"])["status"] == "hoc_execution_approved"
    assert execute("continue-run")["allowed_next_actions"] == ["get_status"]
    with pytest.raises(HarnessError) as error:
        execute("restart-execution", reason="already approved")
    assert error.value.error_code == "execution_restart_not_allowed"
    assert len(fakes["worker"].calls) == calls


def test_budget_stop_offers_adjust_budget_and_resumes_in_place(run):
    execute, store, _, h, _, checkpoint, fakes = run
    default = fakes["turn"]

    def repair(role, root, task, read_only):
        if role == "integration" and task.startswith("# Fix"):
            spec = root / "tests/api.spec.ts"
            early.WRITE(root, "tests/api.spec.ts", spec.read_text(encoding="utf-8") + "again\n")
            return "DONE"
        return default(role, root, task, read_only)

    fakes["turn"] = repair
    assert execute("approve-checkpoint", checkpoint_id=checkpoint["checkpoint_id"])["status"] == BASE
    base = store.read_state(RUN)["pending_checkpoint"]
    assert execute("approve-checkpoint", checkpoint_id=base["checkpoint_id"])["status"] == RUNNING
    fakes["pipeline"].failures = ["unit"] * 3
    blocked = execute("continue-run")
    assert blocked["status"] == "blocked" and "adjust_budget" in blocked["allowed_next_actions"]
    head = early.GIT(h, "rev-parse", "HEAD")
    with pytest.raises(HarnessError) as error:
        execute("adjust-budget", budget_id="hoc_other", limit="3", reason="wrong budget")
    assert error.value.error_code == "budget_id_invalid"
    resumed = execute("adjust-budget", budget_id="hoc_integration_repairs", limit="3", reason="UI specs need rounds")
    assert resumed["status"] == RUNNING and resumed["last_error"] is None
    recovery = store.read_state(RUN)["recoveries"][-1]
    assert (recovery["code"], recovery["previous_limit"], recovery["limit"]) == ("adjust_budget", 2, 3)
    assert early.GIT(h, "rev-parse", "HEAD") == head
    assert execute("continue-run")["status"] == RUNNING
    with pytest.raises(HarnessError) as error:
        execute("adjust-budget", budget_id="hoc_integration_repairs", limit="4", reason="not blocked")
    assert error.value.error_code == "budget_not_exhausted"


def test_base_pause_off_still_advances_one_stage_per_call(run):
    execute, _, _, h, _, _, _ = run
    assert execute("approve-checkpoint", {"SDLC_HOC_BASE_REVIEW_PAUSE": "off"})["status"] == RUNNING
    assert not (h / "src/a.ts").exists()
    assert execute("continue-run")["status"] == RUNNING
    assert (h / "src/a.ts").is_file()
    assert not (h / "tests/api.spec.ts").exists()


def test_opencode_refused_with_plan_checkpoint_unchanged(run):
    execute, store, _, _, _, checkpoint, fakes = run
    with pytest.raises(HarnessError) as error:
        execute("approve-checkpoint", {"SDLC_HOC_WORKER": "opencode"})
    assert error.value.error_code == "hoc_worker_unavailable"
    assert store.read_state(RUN)["pending_checkpoint"] == checkpoint
    assert not fakes["contexts"]


@pytest.mark.parametrize("role", ["base", "reviewer-base", "agent-1"])
def test_worker_transport_failure_leaves_turn_resumable(run, role):
    execute, store, _, h, _, _, fakes = run
    original = fakes["turn"]
    attempts = []
    if role == "agent-1":
        assert execute("approve-checkpoint", {"SDLC_HOC_EXECUTION_MODE": "a"})["status"] == BASE

    def fail_once(actual, root, task, read_only):
        if actual == role and len(attempts) < 2:
            attempts.append((actual, root, read_only))
            if len(attempts) == 1:
                if role == "base":
                    early.WRITE(h, "src/base.ts", "partial\n")
                raise HarnessError("llm_transport_failed", "temporary")
            assert flow._INTERRUPT in task
            if role == "base":
                assert (h / "src/base.ts").read_text() == "partial\n"
        return original(actual, root, task, read_only)

    fakes["turn"] = fail_once
    first = execute("approve-checkpoint")
    assert first["status"] == RUNNING
    assert first["last_error"]["error_code"] == "llm_transport_failed"
    assert "continue_run" in first["allowed_next_actions"]
    ctx = fakes["contexts"][-1]
    assert not any(event["type"] == "AgentStopped" for event in early.events(ctx))
    assert "stopped" not in json.loads(early.record(ctx, "flow_v1.json").read_bytes())
    assert not any(event["type"] == "StageFinished" and event["status"] == "stopped" for event in early.events(ctx))
    resumed = execute("continue-run")
    assert resumed["status"] == (RUNNING if role == "agent-1" else BASE)
    assert resumed["last_error"] is None
    assert attempts[0] == attempts[1]
    assert len([event for event in early.events(ctx) if event["type"] == "TurnMeasured"
                and event["role"] == role and event.get("transport_failed")]) == 1
    assert not store.read_state(RUN)["hoc_call_active"]


def test_non_transport_worker_failure_blocks_and_is_persisted(run):
    execute, _, _, _, _, _, fakes = run

    def fail(*args):
        raise HarnessError("unexpected_worker_error", "permanent")

    fakes["turn"] = fail
    result = execute("approve-checkpoint")
    assert (result["status"], result["current_phase"]) == ("blocked", "hoc_execution")
    assert result["last_error"]["agent"] == "base"
    assert "continue_run" not in result["allowed_next_actions"]
    ctx = fakes["contexts"][-1]
    assert any(event["type"] == "AgentStopped" for event in early.events(ctx))
    assert json.loads(early.record(ctx, "flow_v1.json").read_bytes())["stopped"]["reason"] == "permanent"
    assert execute("continue-run")["status"] == "blocked"


@pytest.mark.parametrize("status", [RUNNING, BASE, FINAL, "blocked"])
def test_restart_restores_plan_and_bundles_all_branch_tips(run, status):
    execute, store, worktrees, h, baseline, checkpoint, _ = run
    assert execute("approve-checkpoint")["status"] == BASE
    state = store.read_state(RUN)
    state["status"] = status
    store.write_state(state)
    owner = h.parent.parent / "owner checkout"
    owner_tip = GIT(owner, "rev-parse", "HEAD")
    h_tip = GIT(h, "rev-parse", "HEAD")
    agent = worktrees.create(1, base_sha=baseline)
    agent_tip = early.COMMIT(agent.root, "src/agent.ts", "unmerged\n")
    early.WRITE(agent.root, "temp.txt", "scratch\n")
    early.WRITE(h, "src/base.ts", "dirty\n")
    staged_only = status == "blocked"
    if staged_only:
        GIT(h, "add", "--", "src/base.ts")
        early.WRITE(h, "src/base.ts", "foundation\n")
    else:
        early.WRITE(h, "untracked file.txt", "scratch\n")
    for name in ("codex_base_session_v1.json", "codex_base_answer.txt", "codex_base_stderr.log",
                 "hoc_gate_1.json", "hoc_pipeline_1.json"):
        store.artifact_path(RUN, name).write_bytes(b"artifact")
    outcome = execute("restart-execution", reason="owner requested")
    assert outcome["status"] == PLAN
    assert outcome["pending_checkpoint"] == checkpoint
    assert GIT(h, "rev-parse", "HEAD") == baseline
    assert not GIT(h, "status", "--porcelain")
    assert GIT(owner, "rev-parse", "HEAD") == owner_tip and GIT(owner, "branch", "--show-current") == "main"
    assert not agent.root.exists()
    assert not [ref for ref in GIT(owner, "branch", "--list").splitlines() if "-agent-" in ref]
    state = store.read_state(RUN)
    assert "hoc_execution" not in state
    recovery = state["recoveries"][-1]
    assert (recovery["code"], recovery["from_status"], recovery["reason"]) == ("restart_execution", status, "owner requested")
    audit = store.repo_root / recovery["audit_dir"]
    assert GIT(h, "bundle", "list-heads", str(audit / "integration.bundle")).split()[0] == h_tip
    assert GIT(h, "bundle", "list-heads", str(audit / "agent-1.bundle")).split()[0] == agent_tip
    assert (audit / "agent-2.bundle").is_file()
    if staged_only:
        assert "+dirty" in (audit / "integration.index.diff").read_text()
        assert (audit / "integration.diff").read_text() == ""
        assert (audit / "integration.untracked.txt").read_text() == ""
    else:
        assert "+dirty" in (audit / "integration.diff").read_text()
        assert (audit / "integration.untracked.txt").read_text().strip() == "untracked file.txt"
    assert (audit / "hoc_execution/flow_v1.json").is_file()
    assert (audit / "hoc_execution_inputs/plan_graph.json").is_file()
    assert {path.name for path in audit.glob("codex_*")} == {
        "codex_base_session_v1.json", "codex_base_answer.txt", "codex_base_stderr.log",
    }
    assert (audit / "hoc_gate_1.json").is_file() and (audit / "hoc_pipeline_1.json").is_file()
    assert worktrees.manager.read(RUN).head_sha == baseline
    assert execute("approve-checkpoint")["status"] == BASE


def test_restart_refused_while_execution_lock_is_held(run):
    execute, store, worktrees, _, _, _, _ = run
    assert execute("approve-checkpoint", {"SDLC_HOC_BASE_REVIEW_PAUSE": "off"})["status"] == RUNNING
    with worktrees.manager.hoc_execution_lock(RUN), pytest.raises(HarnessError) as error:
        execute("restart-execution", reason="busy")
    assert error.value.error_code == "run_workspace_locked"
    assert "hoc_restart" not in store.read_state(RUN)


def test_cleanup_bundles_unmerged_agents_before_removing_h(run, monkeypatch):
    execute, store, worktrees, h, baseline, _, _ = run
    assert execute("approve-checkpoint")["status"] == BASE
    agent = worktrees.create(1, base_sha=baseline)
    tip = early.COMMIT(agent.root, "src/agent.ts", "unmerged\n")
    state = store.read_state(RUN)
    state["status"] = "cancelled"
    store.write_state(state)
    monkeypatch.setattr(type(worktrees.manager), "_ensure_cleanup_recovery", lambda *_a, **_k: None)
    assert execute("cleanup-run-workspace")["cleaned"]
    assert not h.exists() and not agent.root.exists()
    owner = h.parent.parent / "owner checkout"
    assert not [ref for ref in GIT(owner, "branch", "--list").splitlines() if "-agent-" in ref]
    bundle, = (store.run_dir(RUN) / "audit").glob("agent-1-*.bundle")
    assert GIT(owner, "bundle", "list-heads", str(bundle)).split()[0] == tip


def test_restart_execution_cli_arguments_and_dashboard_config(tmp_path):
    parsed = build_parser().parse_args(["restart-execution", "--run-id", RUN, "--reason", "reset", "--runtime-root", "dry"])
    assert (parsed.run_id, parsed.reason, parsed.runtime_root) == (RUN, "reset", "dry")
    settings = load_settings(root=tmp_path)
    assert (settings.hoc_worker, settings.hoc_execution_mode, settings.hoc_base_review_pause) == ("codex", "b", True)
    assert public_dashboard_schema(settings)["actions"]["restart_execution"]["required_arguments"] == ["reason"]
    with pytest.raises(HarnessError, match="SDLC_HOC_BASE_REVIEW_PAUSE must be a boolean"):
        load_settings(root=tmp_path, overrides={"SDLC_HOC_BASE_REVIEW_PAUSE": "typo"})
