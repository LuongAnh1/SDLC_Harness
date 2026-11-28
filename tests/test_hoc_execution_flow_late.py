"""Late H-OC stages with local Git, fake turns and a typed fake pipeline."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.verification.runner import HARNESS_LANE_STAGES, VerificationResult, VerificationStageResult
from sdlc_harness.workflows import hoc_execution_flow as flow
from sdlc_harness.workflows.hoc_execution_report import execution_metrics
from tests import test_hoc_execution_flow as early

pytestmark = early.pytestmark
workspace = early.workspace
WRITE = early.WRITE
PRODUCT = "## Product fixes\n\n| Unit | Failing check | Evidence |\n| --- | --- | --- |\n"
FINDINGS = "## Findings\n\n| F-n | Units | Requirement/Design ID | Problem | Evidence |\n| --- | --- | --- | --- | --- |\n"
REJECTED = "## Rejected findings\n\n| F-n | Evidence |\n| --- | --- |\n"


class Pipeline:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = []

    def run_harness_stages(self, *, target_path, run_id, verification_policy, stage_ids):
        assert stage_ids == tuple(stage for stage in verification_policy.stage_ids if stage not in HARNESS_LANE_STAGES)
        self.calls.append((target_path, stage_ids))
        fail = self.failures.pop(0) if self.failures else None
        rows = []
        for stage in stage_ids:
            rows.append(VerificationStageResult(
                stage, "failed" if stage == fail else "passed", (), ".", 30, int(stage == fail), .5, "", "",
                f"{stage} result",
            ))
            if stage == fail:
                break
        return VerificationResult(
            "failed" if fail else "passed", "", str(target_path), int(bool(fail)), 1, "", "", "Pipeline result",
            planned_stage_ids=stage_ids, executed_stage_ids=tuple(row.stage_id for row in rows), stage_results=tuple(rows),
        )


def make_context(workspace, *, override=None, failures=(), mode="a", findings="none", gate=None):
    plan = early.plan()
    plan["integration"] = {"units": ["U-5"], "files": ["src/check.ts"], "tests": [],
                           "new_tests": ["tests/api.spec.ts", "tests/ui.spec.ts", "tests/other.spec.ts"]}
    plan["test_intents"] = [
        {"task": "integration", "level": level, "proves": ["R-1"], "path": path, "intent": intent}
        for level, path, intent in [
            ("api", "tests/api.spec.ts", "Only API intent"),
            ("ui", "tests/ui.spec.ts", "Only UI intent"),
            ("other", "tests/other.spec.ts", "Other check intent"),
        ]
    ]
    pipeline = Pipeline(failures)

    def worker(role, root, task, read_only):
        if override:
            response = override(role, root, task, read_only)
            if response is not None:
                return response
        if role == "integration":
            if "Work on check files" in task:
                assert "Only API intent" in task and "Other check intent" in task and "Only UI intent" not in task
                assert "Canned profile facts." in task
                WRITE(root, "src/check.ts", "check\n")
                WRITE(root, "tests/api.spec.ts", "api\n")
                return "DONE"
            if "Work on UI intents" in task:
                assert "Only UI intent" in task and "Only API intent" not in task
                WRITE(root, "tests/ui.spec.ts", "ui\n")
                return "DONE"
            raise RuntimeError("Unscripted integration repair")
        if role == "reviewer-final":
            assert read_only
            local = root / "docs/sdlc-local"
            assert (local / "plan_graph_v1.md").read_text() == "# Approved Plan"
            assert "src/base.ts" in (local / "execution.diff").read_text()
            assert json.loads((local / "pipeline.json").read_text())["status"] == "passed"
            return findings
        return None

    ctx = early.context(workspace, mode=mode, callback=worker, approved=plan, gate=gate)
    return replace(ctx, integration_facts="Canned profile facts.", plan_report="# Approved Plan", pipeline_runner=pipeline)


def advance(ctx, *statuses):
    for status in statuses:
        result = flow.run_hoc_execution(ctx)
        assert result.status == status, result
    return result


def artifact(ctx, filename):
    return json.loads(early.record(ctx, filename).read_bytes())


def test_mode_a_end_to_end_stages_and_final_none(workspace):
    def worker(role, root, task, read_only):
        if role == "reviewer-final" and not task.startswith("Format-only"):
            return FINDINGS + "| F-1 | U-999 | R-1 | Unknown unit | Evidence |\n"
        return None

    ctx = make_context(workspace, override=worker)
    advance(ctx, "base_done", "wave_done", "integration_done")
    assert len(ctx.pipeline_runner.calls) == 2
    result = advance(ctx, "execution_review_pending")
    calls = len(ctx.worker.calls)
    assert advance(ctx, "execution_review_pending").artifacts == result.artifacts
    assert len(ctx.worker.calls) == calls and len(ctx.pipeline_runner.calls) == 3
    assert len([role for role, _, _ in ctx.worker.calls if role == "reviewer-final"]) == 2
    report = artifact(ctx, "hoc_execution_report_v1.json")
    assert not report["findings"]
    assert all(stage["status"] == "passed" for stage in report["pipeline"]["stages"])
    assert "ui.spec.ts" in report["diff_stat"]["integration"]
    assert early.record(ctx, "hoc_execution_report_v1.md").is_file()
    metrics = artifact(ctx, "hoc_execution_metrics_v1.json")
    assert set(metrics["stages"]) == {f"S{number}" for number in range(7)}
    assert all(value["duration_seconds"] >= 0 for value in metrics["stages"].values())
    assert metrics["commit_results"]["merged"] == 6
    assert sum(commit["queue_wait_seconds"] is not None for commit in metrics["commits"].values()) == 3
    assert metrics["idle_seconds_s3"]["agent-1"] >= 0
    assert any(row["usage"] == {"total_tokens": 5} for row in metrics["turns"])


@pytest.mark.parametrize("repair", ["tests", "product"])
def test_integration_failure_is_repaired_and_rerun(workspace, repair):
    def worker(role, root, task, read_only):
        if role == "integration" and task.startswith("# Fix"):
            assert "Pipeline failed at" in task and "browser_e2e" in task and "docs/sdlc-local/pipeline.json" in task
            assert json.loads((root / "docs/sdlc-local/pipeline.json").read_text())["status"] == "failed"
            if repair == "tests":
                WRITE(root, "tests/api.spec.ts", "repaired check\n")
                return "DONE"
            return (PRODUCT + "| U-1 | contract | Base evidence |\n| U-2 | browser | Agent evidence |\n"
                    "| U-2 | check | More agent evidence |\nDONE")
        if repair == "product" and task.startswith("# Fix") and role in {"agent-1", "base"}:
            target = "src/a.ts" if role == "agent-1" else "src/base.ts"
            assert ("Agent evidence" if role == "agent-1" else "Base evidence") in task
            if role == "agent-1":
                assert (root / "tests/api.spec.ts").is_file()
            WRITE(root, target, "product repair\n")
            return "DONE"
        return None

    ctx = make_context(workspace, override=worker, failures=("browser_e2e",))
    advance(ctx, "base_done", "wave_done", "integration_done", "execution_review_pending")
    report = artifact(ctx, "hoc_execution_report_v1.json")
    assert report["flaky_specs"] == [] and report["browser_failures"][0]["stage"] == "browser_e2e"
    if repair == "product":
        assert {row["agent_id"] for row in report["integration_routes"]} == {"agent-1", "base"}
        assert len(next(row["requests"] for row in report["integration_routes"] if row["agent_id"] == "agent-1")) == 2
        history = early.events(ctx)
        routed = next(row for row in history if row["type"] == "TurnChecked" and row.get("route_id") == "pipeline-repair-1:agent-1")
        assert any(row["type"] == "MergeFinished" and row["sha"] == routed["sha"] for row in history)
    else:
        assert report["integration_routes"] == []
    assert len(ctx.pipeline_runner.calls) == 4


@pytest.mark.parametrize(("rows", "reason"), [
    ("| U-1 | check | |", "Invalid or duplicate"),
    ("| U-999 | check | evidence |", "G14"),
])
def test_product_request_must_parse_before_commit(workspace, rows, reason):
    def worker(role, root, task, read_only):
        if role == "integration" and task.startswith("# Fix"):
            WRITE(root, "tests/api.spec.ts", "unchecked repair\n")
            return PRODUCT + rows + "\nDONE"
        return None

    ctx = make_context(workspace, failures=("unit",), override=worker)
    advance(ctx, "base_done", "wave_done")
    result = advance(ctx, "stopped", "stopped")
    assert reason in result.reason and result.agent == "integration"
    assert len(ctx.pipeline_runner.calls) == 1
    assert not any(row["type"] == "FixRequested" for row in early.events(ctx))
    assert "unchecked repair" in (ctx.worktrees.root(None) / "tests/api.spec.ts").read_text()
    assert early.GIT(ctx.worktrees.root(None), "show", "HEAD:tests/api.spec.ts") == "api"


def test_integration_pipeline_repair_budget(workspace):
    def worker(role, root, task, read_only):
        if role == "integration" and task.startswith("# Fix"):
            file = root / "tests/api.spec.ts"
            WRITE(root, "tests/api.spec.ts", file.read_text() + "try again\n")
            return "DONE"
        return None

    ctx = make_context(workspace, failures=("unit", "unit", "unit"), override=worker)
    advance(ctx, "base_done", "wave_done")
    stopped = advance(ctx, "stopped")
    assert "Integration repair budget exhausted" in stopped.reason
    assert sum(row["type"] == "PipelineRepairStarted" for row in early.events(ctx)) == 2
    with pytest.raises(HarnessError, match="must exceed the current 2"):
        flow.extend_integration_budget(ctx.store, ctx.run_id, limit=2, reason="same")

    # the owner raises the budget: the run resumes the same repair loop in place and finishes S4
    event = flow.extend_integration_budget(ctx.store, ctx.run_id, limit=3, reason="UI specs need more rounds")
    assert (event["previous"], event["limit"]) == (2, 3)
    head = early.GIT(ctx.worktrees.root(None), "rev-parse", "HEAD")
    advance(ctx, "integration_done")
    history = early.events(ctx)
    assert sum(row["type"] == "PipelineRepairStarted" for row in history) == 3
    assert early.GIT(ctx.worktrees.root(None), "merge-base", "--is-ancestor", head, "HEAD") == ""
    finishes = [row["status"] for row in history if row["type"] == "StageFinished" and row["stage"] == "S4"]
    assert finishes == ["stopped", "done"]
    with pytest.raises(HarnessError, match="did not stop on its integration repair budget"):
        flow.extend_integration_budget(ctx.store, ctx.run_id, limit=4, reason="not stopped")


def test_integration_turns_run_specs_inside_an_e2e_session(workspace):
    opened = []

    @contextmanager
    def session(root):
        opened.append(root)
        yield {"SDLC_E2E_CONFIG": "docs/sdlc-local/playwright.harness.config.ts", "DATABASE_URL": "postgresql://x"}

    def worker(role, root, task, read_only):
        if role == "integration":
            assert "## Running your specs" in task and "--config docs/sdlc-local/playwright.harness.config.ts" in task
        elif not read_only:
            assert "## Running your specs" not in task
        return None

    ctx = replace(make_context(workspace, override=worker), e2e_session=session)
    advance(ctx, "base_done", "wave_done", "integration_done")
    envs = [env for role, env in ctx.worker.envs if role == "integration"]
    assert len(envs) == 2 and all(env["DATABASE_URL"] == "postgresql://x" for env in envs)
    assert all(env is None for role, env in ctx.worker.envs if role != "integration")
    assert opened == [ctx.worktrees.root(None)] * 2


class PlaywrightPipeline(Pipeline):
    """A browser_e2e failure whose output looks like Playwright's line reporter."""

    OUTPUT = (
        "[1/3] [chromium] › tests\\e2e\\a.spec.ts:5:1 › first\n"
        "  1) [chromium] › tests\\e2e\\a.spec.ts:5:1 › first ────\n\n"
        "    Error: strict mode violation: getByRole('button', { name: 'Publish Article' }) resolved to 2 elements\n"
        "    Error Context: test-results\\a-first-chromium\\error-context.md\n\n"
        "    attachment #2: trace (application/zip) ──\n    test-results\\a-first-chromium\\trace.zip\n\n"
        "  2) [chromium] › tests\\e2e\\b.spec.ts:9:1 › second ────\n\n"
        "    Error: element(s) not found\n"
        "    Error Context: test-results\\b-second-chromium\\error-context.md\n\n"
        "  2 failed\n  14 passed (24.7s)\n"
    )

    def run_harness_stages(self, **kwargs):
        result = super().run_harness_stages(**kwargs)
        return replace(result, stage_results=tuple(
            replace(row, stdout=self.OUTPUT) if row.status == "failed" else row for row in result.stage_results
        ))


def test_repair_packet_lists_every_failing_test_and_page_snapshot(workspace):
    def worker(role, root, task, read_only):
        if role == "integration" and task.startswith("# Fix"):
            assert "check the whole spec before DONE" in task
            assert "a.spec.ts:5:1 › first" in task and "b.spec.ts:9:1 › second" in task
            assert "Publish Article" in task and "attachment #2" not in task
            assert ("`test-results/a-first-chromium/error-context.md`, `test-results/b-second-chromium/error-context.md`"
                    in task)
            WRITE(root, "tests/api.spec.ts", "repaired check\n")
            return "DONE"
        return None

    ctx = make_context(workspace, override=worker)
    ctx = replace(ctx, pipeline_runner=PlaywrightPipeline(["browser_e2e"]))
    advance(ctx, "base_done", "wave_done", "integration_done")


@pytest.mark.parametrize("pipeline_red", [False, True])
def test_final_finding_routes_to_both_owners_and_can_repair_pipeline(workspace, pipeline_red):
    findings = FINDINGS + "| F-1 | U-2, U-3 | D-1 | Problem | Evidence |\n"

    def worker(role, root, task, read_only):
        if task.startswith("# Fix") and role in {"agent-1", "agent-2"}:
            assert "F-1" in task
            WRITE(root, "src/a.ts" if role == "agent-1" else "src/b.ts", "review repair\n")
            return "DONE"
        if task.startswith("# Fix") and role == "integration":
            assert pipeline_red and "Pipeline failed at" in task
            WRITE(root, "tests/ui.spec.ts", "post-review check\n")
            return "DONE"
        return None

    ctx = make_context(workspace, mode="b", override=worker, findings=findings,
                       failures=(None, None, "browser_e2e") if pipeline_red else ())
    advance(ctx, "base_done", "wave_done", "integration_done", "execution_review_pending")
    report = artifact(ctx, "hoc_execution_report_v1.json")
    dispositions = report["findings"][0]["dispositions"]
    assert {row["agent_id"] for row in dispositions} == {"agent-1", "agent-2"}
    assert len(dispositions) == 2 and all(row["result"] == "fixed" and row["sha"] for row in dispositions)
    assert len(ctx.pipeline_runner.calls) == (4 if pipeline_red else 3)
    assert artifact(ctx, "hoc_execution_metrics_v1.json")["findings"] == {"fixed": 1, "rejected": 0}


def test_final_finding_can_be_rejected_with_evidence_without_a_commit(workspace):
    def worker(role, root, task, read_only):
        if role == "agent-1" and task.startswith("# Fix"):
            assert "## Rejected findings" in task
            return REJECTED + "| F-1 | The approved requirement covers this case. |\nDONE"
        return None

    ctx = make_context(workspace, override=worker, findings=FINDINGS + "| F-1 | U-2 | R-1 | Problem | Evidence |\n")
    advance(ctx, "base_done", "wave_done", "integration_done", "execution_review_pending")
    report = artifact(ctx, "hoc_execution_report_v1.json")
    assert report["findings"][0]["dispositions"] == [{
        "agent_id": "agent-1", "finding_id": "F-1", "units": ["U-2"], "result": "rejected",
        "evidence": "The approved requirement covers this case.",
    }]
    assert artifact(ctx, "hoc_execution_metrics_v1.json")["findings"]["rejected"] == 1


def test_base_reviewer_repair_gets_own_gate_fix(workspace):
    def worker(role, root, task, read_only):
        if role == "reviewer-base":
            return FINDINGS + "| F-1 | U-1 | R-1 | Problem | Evidence |\n"
        if role == "base" and task.startswith("# Fix"):
            WRITE(root, "src/base.ts", "good\n" if "own_gate_failed" in task else "bad\n")
            return "DONE"
        return None

    def gate(**options):
        return lambda root: early.RED if (root / "src/base.ts").read_text() == "bad\n" else early.GREEN

    ctx = make_context(workspace, override=worker, gate=gate)
    advance(ctx, "base_done")
    checks = artifact(ctx, "base_report_v1.json")["gate_results"]
    assert [item["ok"] for item in checks] == [True, False, True]
    assert [role for role, _, _ in ctx.worker.calls].count("reviewer-base") == 1


def test_metrics_are_computed_only_from_typed_journal_evidence():
    def time(seconds):
        return f"2026-11-28T00:00:{seconds:02}+00:00"

    def event(kind, **fields):
        return {"type": kind, "agent_id": "agent-1", **fields}

    rows = [
        event("StageStarted", stage="S3", agents=["agent-1"], started_at=time(0)),
        event("TurnStarted", started_at=time(1)),
        event("TurnMeasured", role="agent-1", turn_kind="group", started_at=time(1), ended_at=time(5),
              usage={"input_tokens": 12, "cached_input_tokens": 4}),
        event("TurnChecked", sha="abc", result="queued", received_at=time(6)),
        event("MergeStarted", sha="abc", started_at=time(9)),
        event("MergeFinished", sha="abc", result="merged", merged_at=time(11)),
        event("StageFinished", stage="S3", ended_at=time(12), status="done"),
    ]
    metrics = execution_metrics(rows)
    assert metrics["stages"]["S3"]["duration_seconds"] == 12
    assert metrics["turns"][0]["duration_seconds"] == 4
    assert metrics["turns"][0]["usage"]["cached_input_tokens"] == 4
    assert metrics["idle_seconds_s3"] == {"agent-1": 7}
    assert metrics["commits"]["abc"]["queue_wait_seconds"] == 3
    assert metrics["commit_results"]["merged"] == 1
