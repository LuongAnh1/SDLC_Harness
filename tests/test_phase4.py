"""Kiểm thử Phase 4 Slice 4.1: review implementation plan -> DeveloperWorkPacket."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.developer import (
    DeveloperExecutionError,
    DeveloperExecutionResult,
    build_developer_work_packet,
)
from sdlc_harness.agents.tester import QaGeneratedTestSuite, QaTesterAgent
from sdlc_harness.agents.validator import CodeTraceValidator
from sdlc_harness.agents.validator.typescript_ast import TypeScriptAstEvidence, TypeScriptAstExtractor
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.agents.developer.closure_planner import RuntimeClosurePlanner
from sdlc_harness.agents.validator.closure_review import RuntimeClosureValidator
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.llm.fake import fake_qa_generated_tests, fake_runtime_closure_plan, fake_runtime_closure_review
from sdlc_harness.profiles import (
    build_code_trace_policy,
    build_planning_path_policy,
    build_qa_test_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.verification import VerificationResult, VerificationRunner, VerificationStageResult
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase4 import (
    PhaseFourWorkflow,
    _code_review_pr_body,
    _verification_repair_feedback,
    _verification_attempt_count,
)
from sdlc_harness.workspaces import RunWorkspaceManager
from tests._support import (
    DirectoryOverlayTemplate,
    GitRepositoryTemplate,
    RuntimeDirectoryTemplate,
    create_fast_run_workspace,
    populate_typescript_target,
)


pytestmark = pytest.mark.integration


def _code_validation_task_response(
    prompt: str,
    *,
    category: str = "",
    owner: str = "developer",
    action: str = "developer_repair",
) -> dict[str, object]:
    payload = json.loads(prompt)
    contract = payload["task_contract"]
    if "previous_output" in payload:
        return dict(payload["previous_output"])
    result: dict[str, object] = {
        "task_id": contract["task_id"],
        "reviewed_claim_ids": contract["authority_claim_ids"],
        "reviewed_design_ids": contract["required_reviewed_design_ids"],
        "reviewed_boundary_contract_ids": contract["required_reviewed_boundary_contract_ids"],
        "claim_assessments": [
            {"claim_id": claim_id, "verdict": "pass", "finding_ids": []}
            for claim_id in contract["authority_claim_ids"]
        ],
        "ok": not category,
        "summary": "Task passed." if not category else "Task found one blocker.",
        "findings": [],
        "observations": [],
    }
    if not category:
        return result
    claim = payload["authority_claims"][0]
    result["findings"] = [
        {
            "finding_id": f"F-{contract['task_id']}",
            "category": category,
            "severity": "blocker",
            "blocking": True,
            "scenario_ids": contract["scenario_ids"],
            "claim_ids": [claim["claim_id"]],
            "unit_ids": [contract["unit_ids"][0]],
            "requirement_refs": [claim["requirement_ref"]] if claim.get("requirement_ref") else [],
            "design_refs": [],
            "code_refs": [],
            "test_refs": [],
            "issue": "Approved behavior is not covered by code/test evidence.",
            "expected_change": "Implement and test the approved behavior.",
            "evidence_ref": claim["claim_id"],
            "evidence_quote": claim["requirement_content"],
            "owner": owner,
            "recommended_action": action,
        }
    ]
    result["claim_assessments"] = [
        {
            "claim_id": claim_id,
            "verdict": "finding" if claim_id == claim["claim_id"] else "pass",
            "finding_ids": [f"F-{contract['task_id']}"] if claim_id == claim["claim_id"] else [],
        }
        for claim_id in contract["authority_claim_ids"]
    ]
    return result


class RejectingCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(_code_validation_task_response(prompt, category="missing_behavior"))


class TraceMappingIssueCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            _code_validation_task_response(
                prompt,
                category="trace_mapping_issue",
                owner="validator",
                action="validator_mapping_fix",
            )
        )


class CapturingCodeTraceLlm:
    def __init__(self) -> None:
        self.prompt = ""
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompt = prompt
        self.prompts.append(prompt)
        return json.dumps(_code_validation_task_response(prompt))


class ProseWrappedCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return (
            "Here is the review:\n"
            + json.dumps(_code_validation_task_response(prompt))
        )


class FailingQaTester:
    def generate_tests(self, *, context: dict[str, object], qa_policy=None) -> QaGeneratedTestSuite:
        assert qa_policy is not None
        path = "apps/api/src/FailingQaAcceptance.test.ts"
        requirement_ids = [
            str(item.get("id"))
            for item in context.get("canonical_requirements", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return QaGeneratedTestSuite(
            summary="QA generated a failing acceptance test for workflow state coverage.",
            test_files=[
                {
                    "path": path,
                    "content": 'import { describe, expect, it } from "vitest";\n',
                    "scenarios": ["Delete confirmation"],
                }
            ],
            scenarios=[
                {
                    "name": "Delete confirmation",
                    "expected_result": "The runner should surface the QA acceptance failure.",
                    "test_file": path,
                    "risk": "User may delete without confirmation.",
                    "requirement_ids": requirement_ids,
                }
            ],
            risks=["Confirmation behavior is unclear."],
            generation_context={"section_count": 1, "chars_sent": 10, "chars_total": 10, "any_truncated": False},
            requirement_contract=context.get("requirement_contract"),
            generation_profile=qa_policy.metadata(),
        )


class RecordingQaTester:
    def __init__(self):
        self.context: dict[str, object] = {}

    def generate_tests(self, *, context: dict[str, object], qa_policy=None) -> QaGeneratedTestSuite:
        assert qa_policy is not None
        self.context = context
        path = "apps/api/src/RecordingQaAcceptance.test.ts"
        requirement_ids = [
            str(item.get("id"))
            for item in context.get("canonical_requirements", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return QaGeneratedTestSuite(
            summary="Recording QA generated compact acceptance tests.",
            test_files=[
                {
                    "path": path,
                    "content": 'import { describe, expect, it } from "vitest";\n',
                    "scenarios": ["Compact QA packet"],
                }
            ],
            scenarios=[
                {
                    "name": "Compact QA packet",
                    "expected_result": "Packet includes accepted requirement, code, verification, and trace summary.",
                    "test_file": path,
                    "risk": "",
                    "requirement_ids": requirement_ids,
                }
            ],
            risks=[],
            generation_context=context.get("context_budget") if isinstance(context.get("context_budget"), dict) else {},
            requirement_contract=context.get("requirement_contract"),
            generation_profile=qa_policy.metadata(),
        )


class InvalidQaLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return '{"status": "pass", "summary": "cut off before scenarios"'


class ScriptedClosureLlm:
    """Serve scripted closure completions: text, (text, finish_reason), callable(prompt) or an exception."""

    def __init__(self, responses: list[object] | None = None, default=None):
        self.responses = list(responses or [])
        self.default = default
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def complete_with_metadata(self, *, system: str, prompt: str, **options) -> LlmCompletion:
        del system, options
        self.prompts.append(prompt)
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt)
        content, finish_reason = item if isinstance(item, tuple) else (item, "stop")
        return LlmCompletion(content=str(content), finish_reason=finish_reason)


class ClosureVerificationRunnerStub:
    """Scripted closure-suite outcomes; any other policy (QA) passes like SequenceVerificationRunner."""

    def __init__(self, outcomes: list[str] | None = None):
        self.outcomes = list(outcomes or [])
        self.closure_calls: list[dict[str, object]] = []
        self.other = SequenceVerificationRunner([])

    def run(self, *, target_path: Path, work_packet: dict[str, object], verification_policy=None, runtime_probe=None, **kwargs):
        if verification_policy is None or verification_policy.policy_version != "runtime_closure_stage_policy_v1":
            return self.other.run(target_path=target_path, work_packet=work_packet, verification_policy=verification_policy, **kwargs)
        self.closure_calls.append({"runtime_probe": runtime_probe, "stage_ids": list(work_packet["verification_stage_ids"])})
        outcome = self.outcomes.pop(0) if self.outcomes else "passed"
        probe_ids = list(runtime_probe.probe_ids)
        failing = {
            "env": ("database_ready", "environment", "Error: port is already allocated"),
            "policy": ("runtime_probe", "policy", "Playwright JSON report is missing or invalid."),
            "webserver": ("runtime_probe", "policy", "Error: Process from config.webServer was not able to start. [api] exited"),
            "probe_fail": ("runtime_probe", "implementation", "Playwright test failure: RP-1: failed"),
        }.get(outcome)
        stages: list[VerificationStageResult] = []
        stopped = False
        for stage_id in verification_policy.stage_ids:
            if stopped and stage_id != "cleanup":
                continue
            failed = failing is not None and failing[0] == stage_id
            probe_results: tuple[dict[str, object], ...] = ()
            if stage_id == "runtime_probe":
                probe_results = tuple(
                    {
                        "probe_id": probe_id,
                        "status": "failed" if outcome == "probe_fail" and index == 0 else "passed",
                        "observation": {"probe_id": probe_id, "actual_status": 500 if outcome == "probe_fail" and index == 0 else 200, "actual_items": None, "response_body_excerpt": "boom"},
                    }
                    for index, probe_id in enumerate(probe_ids)
                ) if outcome in {"passed", "probe_fail"} else ()
            stages.append(
                VerificationStageResult(
                    stage_id=stage_id,
                    status="failed" if failed else "passed",
                    argv=("npm", "run", stage_id),
                    cwd=".",
                    timeout_seconds=120,
                    exit_code=1 if failed else 0,
                    duration_seconds=0.01,
                    stdout="",
                    stderr=failing[2] if failed else "",
                    reason=failing[2] if failed else f"Verification stage completed successfully: {stage_id}",
                    failure_kind=failing[1] if failed else "",
                    probe_results=probe_results,
                )
            )
            stopped = stopped or failed
        terminal = next((item for item in stages if item.status == "failed"), stages[-1])
        return VerificationResult(
            status="failed" if failing else "passed",
            command="",
            cwd=str(target_path),
            exit_code=terminal.exit_code,
            duration_seconds=0.1,
            stdout="",
            stderr=terminal.stderr,
            reason=terminal.reason,
            failure_kind=terminal.failure_kind,
            planned_stage_ids=tuple(verification_policy.stage_ids),
            executed_stage_ids=tuple(item.stage_id for item in stages),
            stage_results=tuple(stages),
        )


def closure_code_blocker(prompt: str) -> str:
    contract = json.JSONDecoder().raw_decode(prompt)[0]["contract"]
    unit_id = contract["closure_units"][0]["unit_id"]
    return json.dumps(
        {
            "summary": "Demo provider leaks into production.",
            "reviewed_input_ids": [item["input_id"] for item in contract["runtime_inputs"]],
            "findings": [
                {
                    "severity": "blocker",
                    "category": "demo_isolation",
                    "unit_ids": [unit_id],
                    "issue": "The demo provider is wired into the production entrypoint.",
                    "expected_change": "Use the demo provider only in verification.",
                }
            ],
        }
    )


def closure_code_frozen_conflict(prompt: str) -> str:
    payload = json.loads(closure_code_blocker(prompt))
    payload["findings"][0]["category"] = "frozen_contract_conflict"
    return json.dumps(payload)


def closure_blocker_review(prompt: str) -> str:
    contract = json.JSONDecoder().raw_decode(prompt)[0]["contract"]
    input_ids = [item["input_id"] for item in contract["runtime_inputs"]]
    return json.dumps(
        {
            "summary": "Provider is not credible.",
            "reviewed_input_ids": input_ids,
            "findings": [
                {
                    "severity": "blocker",
                    "category": "provider_validity",
                    "input_ids": [input_ids[0]],
                    "refs": ["PRE-1"],
                    "issue": "The demo provider returns nothing usable.",
                    "expected_change": "The provider returns seeded records.",
                }
            ],
        }
    )


class CountingQaLlm:
    """Count provider calls; serve queued responses, then the fake QA generator."""

    def __init__(self, responses: list[str] | None = None):
        self.calls = 0
        self.prompts: list[str] = []
        self.responses = list(responses or [])

    def complete(self, *, system: str, prompt: str) -> str:
        del system
        self.calls += 1
        self.prompts.append(prompt)
        # The fake generator only understands the first-attempt prompt, not the repair prompt.
        return self.responses.pop(0) if self.responses else fake_qa_generated_tests(self.prompts[0])


class MissingRequirementQaLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "incomplete coverage",
                "test_files": [
                    {
                        "path": "apps/api/src/V2Acceptance.test.ts",
                        "content": 'import { describe, expect, it } from "vitest";',
                        "scenarios": ["one"],
                    }
                ],
                "scenarios": [
                    {
                        "name": "one",
                        "expected_result": "one",
                        "test_file": "apps/api/src/V2Acceptance.test.ts",
                        "risk": "",
                        "requirement_ids": ["REQ-1"],
                    }
                ],
                "risks": [],
            }
        )


class SequenceVerificationRunner:
    def __init__(self, statuses: list[str]):
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def run(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
        verification_policy=None,
        qa_policy=None,
    ) -> VerificationResult:
        del work_packet, verification_policy, qa_policy
        status = self.statuses.pop(0) if self.statuses else "passed"
        selected = tuple(str(path) for path in (required_test_files or []))
        self.calls.append(status)
        reason_by_status = {
            "passed": "Test command completed successfully.",
            "failed": "QA acceptance failed in fake runner.",
            "skipped": "QA acceptance was skipped in fake runner.",
        }
        return VerificationResult(
            status=status,
            command="fake verification runner",
            cwd=str(target_path),
            exit_code=0 if status in {"passed", "skipped"} else 1,
            duration_seconds=0.01,
            stdout="All tests passed" if status == "passed" else "",
            stderr="qa acceptance failed" if status == "failed" else "",
            reason=reason_by_status.get(status, "Unknown fake runner status."),
            selected_test_files=selected,
            verified_test_files=selected if status == "passed" else (),
        )


class EnvironmentFailureVerificationRunner:
    def run(self, *, target_path: Path, **kwargs) -> VerificationResult:
        del kwargs
        return VerificationResult(
            status="failed",
            command="npm run typecheck",
            cwd=str(target_path),
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr="'tsc' is not recognized as an internal or external command",
            reason="Verification stage exited with code 1: typecheck",
            failure_kind="environment",
        )


class WorkPacketTypeScriptAstExtractor:
    """Cung cấp AST evidence theo packet cho journey, không phụ thuộc npm install."""

    def __init__(self, work_packet: dict[str, object]) -> None:
        self.work_packet = work_packet

    def extract(self, *, target_path, source_paths, policy, run_id=None):
        del target_path, run_id
        symbols_by_file: dict[str, list[dict[str, object]]] = {
            str(path): [] for path in source_paths
        }
        contract = self.work_packet.get("implementation_signature_contract")
        entries = contract.get("entries", []) if isinstance(contract, dict) else []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("source_path") or "")
            if path not in symbols_by_file:
                continue
            symbol = dict(raw)
            symbol.setdefault("name", str(raw.get("qualified_name") or "").split(".")[-1])
            symbol.setdefault("kind", str(raw.get("symbol_kind") or ""))
            symbols_by_file[path].append(symbol)
        mappings = self.work_packet.get("design_mappings_by_file")
        if isinstance(mappings, dict):
            for path, items in mappings.items():
                if path not in symbols_by_file or not isinstance(items, list):
                    continue
                known = {
                    str(item.get("qualified_name") or "")
                    for item in symbols_by_file[path]
                }
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    target = str(item.get("planned_code_target") or "")
                    _, separator, name = target.partition("#")
                    if not separator or not name or name in known:
                        continue
                    symbols_by_file[path].append(
                        {"name": name.split(".")[-1], "qualified_name": name, "kind": "class"}
                    )
        return TypeScriptAstEvidence(
            extractor_id=policy.symbol_extractor_id,
            profile=policy.metadata(),
            files=tuple(
                {"path": path, "symbols": symbols}
                for path, symbols in symbols_by_file.items()
            ),
        )


class DeveloperExecutionStateTest(unittest.TestCase):
    def test_fresh_progress_clears_stale_pending_step_review(self) -> None:
        state = {
            "developer_execution": {
                "executor": "tool_loop",
                "mode": "developer_act",
                "repair_iteration": 0,
                "artifact_version": 1,
                "pending_step_review": {"call_id": "stale-call"},
            }
        }

        execution = PhaseFourWorkflow._developer_execution_state(
            state,
            executor="tool_loop",
            mode="developer_act",
            repair_iteration=0,
            session_iteration=1,
            artifact_version=1,
            progress={"completed_items": 2, "remaining_items": 5},
            status="blocked",
        )

        self.assertNotIn("pending_step_review", execution)


class PhaseFourTest(unittest.TestCase):
    ISOLATED_REPOSITORY_TESTS = {
        "test_canonical_checkout_change_does_not_redirect_run_workspace",
        "test_code_trace_normalizes_boolean_and_notification_sequence_mappings",
        "test_verification_runner_adds_sdlc_java_home_to_path",
        "test_verification_runner_executes_required_qa_test_class",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.git_template = GitRepositoryTemplate(populate_typescript_target)
        cls.runtime_template_temp = tempfile.TemporaryDirectory()
        template_root = Path(cls.runtime_template_temp.name)
        topology = cls.git_template.materialize(template_root)
        cls.shared_topology = topology
        runtime_root = template_root / ".runtime"
        settings = load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": "2",
                "SDLC_IMPLEMENTATION_REPAIR_LIMIT": "2",
                "SDLC_PROJECT_PROFILE": "",
                "SDLC_TARGET_REPO_PATH": str(topology.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(topology.remote.resolve()),
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))
        cls.phase2_run_id = str(approved["run_id"])
        cls.phase2_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        phase3 = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=cls.phase2_run_id)
        cls.phase3_run_id = str(phase3["run_id"])
        workspace_ref = Path(str(phase3["latest_artifacts"]["run_workspace"]))
        if not workspace_ref.is_absolute():
            workspace_ref = Path(__file__).resolve().parents[1] / workspace_ref
        workspace_payload = json.loads(workspace_ref.read_text(encoding="utf-8"))
        cls.phase3_template_canonical = str(workspace_payload["canonical_repo"])
        cls.phase3_template_remote = str(workspace_payload["remote"])
        cls.phase3_template_worktree = str(workspace_payload["worktree_path"])
        cls.phase3_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        ready = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=cls.phase3_run_id)
        cls.ready_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        workflow = PhaseFourWorkflow(settings)
        packet_ref = Path(str(ready["latest_artifacts"]["developer_work_packet"]))
        if not packet_ref.is_absolute():
            packet_ref = Path(__file__).resolve().parents[1] / packet_ref
        packet = json.loads(packet_ref.read_text(encoding="utf-8"))
        workflow.code_trace_validator.symbol_extractor = WorkPacketTypeScriptAstExtractor(packet)
        written = workflow.continue_run(run_id=cls.phase3_run_id)
        cls.written_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        cls.written_worktree_template = DirectoryOverlayTemplate(Path(workspace_payload["worktree_path"]))
        tested = workflow.continue_run(run_id=cls.phase3_run_id)
        cls.tested_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        traced = workflow.continue_run(run_id=cls.phase3_run_id)
        cls.traced_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        if [ready["status"], written["status"], tested["status"], traced["status"]] != [
            "implementation_ready",
            "implementation_written",
            "implementation_tested",
            "implementation_traced",
        ]:
            raise AssertionError(
                "Phase 4 runtime templates did not reach the expected checkpoints: "
                + json.dumps(
                    [ready["status"], written["status"], tested["status"], traced],
                    ensure_ascii=False,
                    default=str,
                )
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.traced_runtime_template.close()
        cls.tested_runtime_template.close()
        cls.written_worktree_template.close()
        cls.written_runtime_template.close()
        cls.ready_runtime_template.close()
        cls.phase3_runtime_template.close()
        cls.phase2_runtime_template.close()
        cls.runtime_template_temp.cleanup()
        cls.git_template.close()

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        if self._testMethodName in self.ISOLATED_REPOSITORY_TESTS:
            topology = self.git_template.materialize(Path(self.temp_dir.name))
        else:
            topology = self.shared_topology
        self.remote_path = topology.remote
        self.remote_url = str(self.remote_path.resolve())
        self.target_repo = topology.canonical
        suffix = hashlib.sha256(self._testMethodName.encode("utf-8")).hexdigest()[:8]
        self.run_id = f"run_20261020_000000_{suffix}"
        self.settings = self._settings()
        # Contract Git identity đã có suite workspace riêng và các journey Phase 1-4.
        # Nhóm này chỉ cần Git thật cho file/worktree, không cần lặp toàn bộ probe.
        validation_patch = patch.object(RunWorkspaceManager, "_validate_git", autospec=True, return_value=None)
        validation_patch.start()
        self.addCleanup(validation_patch.stop)
        creation_patch = patch.object(
            RunWorkspaceManager,
            "_create_locked",
            autospec=True,
            side_effect=lambda manager, *, run_id, state: create_fast_run_workspace(
                manager,
                run_id=run_id,
                state=state,
            ),
        )
        creation_patch.start()
        self.addCleanup(creation_patch.stop)
        ast_patch = patch.object(
            TypeScriptAstExtractor,
            "extract",
            autospec=True,
            side_effect=self._extract_typescript_ast_from_work_packet,
        )
        ast_patch.start()
        self.addCleanup(ast_patch.stop)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _extract_typescript_ast_from_work_packet(
        self,
        extractor,
        *,
        target_path,
        source_paths,
        policy,
        run_id=None,
    ):
        del extractor
        state = PhaseOneWorkflow(self.settings).store.read_state(str(run_id))
        packet_ref = Path(str(state["latest_artifacts"]["developer_work_packet"]))
        if not packet_ref.is_absolute():
            packet_ref = self.repo_root / packet_ref
        packet = json.loads(packet_ref.read_text(encoding="utf-8"))
        return WorkPacketTypeScriptAstExtractor(packet).extract(
            target_path=target_path,
            source_paths=source_paths,
            policy=policy,
            run_id=run_id,
        )

    def test_verification_feedback_uses_current_diagnostic_and_keeps_validator_objective_as_lineage(self) -> None:
        objective = {
            "objective_id": "repair:approved-contract",
            "source": "validator_code_trace",
            "status": "open",
            "canonical_keys": ["contract:EditFormView.showConfirmation"],
            "contract_findings": [
                {
                    "code_ref": "src/main/java/EditFormView.java#showConfirmation",
                    "expected_signature": {"name": "showConfirmation", "parameters": [], "return_type": "void"},
                }
            ],
        }
        result = VerificationResult(
            status="failed",
            command="javac",
            cwd=str(self.target_repo),
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr="src/test/java/FlowTest.java:12: error: FakeView does not override showConfirmation()",
            reason="Compilation failed.",
        )

        feedback = _verification_repair_feedback(
            run_id="run_lineage",
            state={"source_ref": "jira_issue:SDLC-3"},
            work_packet={"issue_key": "SDLC-3", "output_contract": {"allowed_paths": ["src/main/java/EditFormView.java", "src/test/java/FlowTest.java"]}},
            result=result,
            report_path="test_report_v2.md",
            target_repo=str(self.target_repo),
            branch="run/run_lineage",
            feedback_version=2,
            repair_iteration=1,
            prior_feedback={
                "version": "implementation_repair_feedback_v1",
                "feedback_source": "validator_code_trace",
                "repair_objective": objective,
                "repair_lineage": {"root_feedback_artifact": "feedback_v1.json"},
            },
            prior_feedback_ref="feedback_v1.json",
        )

        self.assertEqual(feedback["repair_objective"]["source"], "verification")
        self.assertIn("FakeView does not override", feedback["verification"]["stderr"])
        self.assertEqual(feedback["repair_lineage"]["objective_id"], feedback["repair_objective"]["objective_id"])
        self.assertEqual(feedback["repair_lineage"]["previous_objective_id"], "repair:approved-contract")
        self.assertEqual(feedback["repair_lineage"]["root_feedback_artifact"], "feedback_v1.json")
        self.assertIn("audit lineage only", feedback["required_action"])

    def test_verification_feedback_refreshes_diagnostic_without_reviving_legacy_validator_objective(self) -> None:
        old_diagnostic = "FlowTest.java:12: old compiler diagnostic"
        current_diagnostic = "Controller.java:52: current compiler diagnostic"
        result = VerificationResult(
            status="failed",
            command="javac",
            cwd=str(self.target_repo),
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr=current_diagnostic,
            reason="Compilation failed.",
        )
        legacy_root = {
            "version": "implementation_repair_feedback_v21",
            "feedback_source": "validator_code_trace",
            "missing_links": [
                {
                    "canonical_key": "contract:EditFormView.showConfirmation",
                    "blocking": True,
                    "owner": "developer",
                    "code_ref": "src/main/java/EditFormView.java#showConfirmation",
                    "expected_signature": {"name": "showConfirmation", "parameters": [], "return_type": "void"},
                }
            ],
        }
        prior = {
            "version": "implementation_repair_feedback_v24",
            "feedback_source": "verification",
            "repair_objective": {
                "objective_id": "repair:stale-verification",
                "source": "verification",
                "diagnostic_anchor": {"stderr": old_diagnostic},
            },
            "repair_lineage": {"root_feedback_artifact": "feedback_v21.json"},
        }

        feedback = _verification_repair_feedback(
            run_id="run_legacy_lineage",
            state={"source_ref": "jira_issue:SDLC-3"},
            work_packet={
                "issue_key": "SDLC-3",
                "output_contract": {
                    "allowed_paths": ["src/main/java/EditFormView.java", "src/main/java/Controller.java"]
                },
            },
            result=result,
            report_path="test_report_v25.md",
            target_repo=str(self.target_repo),
            branch="run/run_legacy_lineage",
            feedback_version=25,
            repair_iteration=20,
            prior_feedback=prior,
            prior_feedback_ref="feedback_v24.json",
            root_feedback=legacy_root,
            root_feedback_ref="feedback_v21.json",
        )

        objective = feedback["repair_objective"]
        self.assertEqual(objective["source"], "verification")
        self.assertEqual(feedback["verification"]["stderr"], current_diagnostic)
        self.assertNotIn(old_diagnostic, json.dumps(feedback, ensure_ascii=False))
        self.assertEqual(feedback["repair_lineage"]["root_feedback_artifact"], "feedback_v21.json")
        self.assertEqual(feedback["repair_lineage"]["previous_objective_id"], "repair:stale-verification")


    def test_verification_attempt_count_ignores_phase4_artifact_version_gaps(self) -> None:
        workflow = PhaseFourWorkflow(self.settings)
        artifact_dir = workflow.store.run_dir("run_attempt_count") / "artifacts"
        artifact_dir.mkdir(parents=True)
        for name in (
            "test_report_v1.md",
            "test_report_v5.md",
            "test_report_v6.md",
            "code_change_report_v12.md",
        ):
            (artifact_dir / name).write_text("report\n", encoding="utf-8")

        self.assertEqual(_verification_attempt_count(workflow.store, "run_attempt_count"), 3)

    def test_continue_run_resumes_blocked_repair_selection_policy_drift(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_repair_item_scope_mismatch"
        state = {
            "run_id": run_id,
            "status": "blocked",
            "current_phase": "implementation_repair",
            "last_error": {"error_code": "developer_input_drift"},
            "developer_execution": {"mode": "implementation_repair"},
            "latest_artifacts": {
                "implementation_repair_feedback": "feedback.json",
                "developer_repair_execution_manifest": "manifest.json",
                "developer_repair_execution_journal": "journal.json",
            },
            "allowed_next_actions": ["get_status", "cancel_run"],
        }
        resumed = {"run_id": run_id, "status": "implementation_executing"}

        with (
            patch.object(workflow.workspace_manager, "locked_workspace", return_value=contextlib.nullcontext()),
            patch.object(workflow.store, "read_state", return_value=state),
            patch.object(workflow, "_continue_implementation_repair", return_value=resumed) as repair,
        ):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response, resumed)
        repair.assert_called_once_with(state=state, run_id=run_id)

    def test_continue_run_resumes_blocked_initial_developer_scope_mismatch(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_initial_item_scope_mismatch"
        state = {
            "run_id": run_id,
            "status": "blocked",
            "current_phase": "implementation_writing",
            "last_error": {"error_code": "developer_act_out_of_scope"},
            "developer_execution": {"mode": "developer_act"},
            "latest_artifacts": {
                "developer_work_packet": "packet.json",
                "developer_execution_manifest": "manifest.json",
                "developer_execution_journal": "journal.json",
            },
            "allowed_next_actions": ["get_status", "cancel_run"],
        }
        resumed = {"run_id": run_id, "status": "implementation_executing"}

        with (
            patch.object(workflow.workspace_manager, "locked_workspace", return_value=contextlib.nullcontext()),
            patch.object(workflow.store, "read_state", return_value=state),
            patch.object(workflow, "_continue_developer_act", return_value=resumed) as developer_act,
        ):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response, resumed)
        developer_act.assert_called_once_with(state=state, run_id=run_id)

    def test_resume_repair_uses_packet_bound_to_execution_artifact_version(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_bound_repair_packet"
        current = workflow.store.write_json_artifact(
            run_id,
            "developer_work_packet_v7.json",
            {"version": "developer_work_packet_v7"},
        )
        bound = workflow.store.write_json_artifact(
            run_id,
            "developer_work_packet_v8.json",
            {"version": "developer_work_packet_v8"},
        )

        resolved = workflow._bound_repair_work_packet_path(
            run_id=run_id,
            current_packet_path=workflow.store.relative(current),
            artifact_version=8,
            resume_tool_loop=True,
        )

        self.assertEqual(resolved, workflow.store.relative(bound))
        bound.unlink()
        with self.assertRaises(HarnessError) as caught:
            workflow._bound_repair_work_packet_path(
                run_id=run_id,
                current_packet_path=workflow.store.relative(current),
                artifact_version=8,
                resume_tool_loop=True,
            )
        self.assertEqual(caught.exception.error_code, "developer_input_drift")

    def test_repair_restores_approved_units_missing_from_scoped_packet(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_restore_repair_units"
        structured = workflow.store.write_json_artifact(
            run_id,
            "implementation_plan_structured_v2.json",
            {"work_units": [{"unit_id": "WU-1"}, {"unit_id": "WU-2"}]},
        )
        scoped_packet = {
            "source_ref": "fixture:UC-1",
            "issue_key": "UC-1",
            "execution_units": [{"unit_id": "WU-2"}],
            "approved_plan": {},
        }
        restored_packet = {
            **scoped_packet,
            "execution_units": [{"unit_id": "WU-1"}, {"unit_id": "WU-2"}],
        }
        workspace = type("Workspace", (), {"to_dict": lambda self: {}})()

        with (
            patch.object(workflow.workspace_manager, "resolve", return_value=workspace),
            patch(
                "sdlc_harness.workflows.phase4_implementation._planning_policy_for_state",
                return_value=object(),
            ),
            patch(
                "sdlc_harness.workflows.phase4_implementation._verification_policy_for_state",
                return_value=object(),
            ),
            patch(
                "sdlc_harness.workflows.phase4_implementation.build_developer_work_packet",
                return_value=restored_packet,
            ) as rebuild,
        ):
            result = workflow._restore_missing_repair_units(
                state={"source_ref": "fixture:UC-1"},
                run_id=run_id,
                artifact_paths={
                    "implementation_plan_structured": workflow.store.relative(structured),
                },
                work_packet=scoped_packet,
                repair_feedback={
                    "repair_objective": {
                        "contract_findings": [{"unit_ids": ["WU-1", "WU-2"]}],
                    }
                },
                target_path=Path("target"),
            )

        self.assertEqual(
            [unit["unit_id"] for unit in result["execution_units"]],
            ["WU-1", "WU-2"],
        )
        rebuild.assert_called_once()

    def test_developer_loop_accepts_harness_materialized_runtime_config_as_existing_dirty_state(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_harness_owned_dirty_path"
        result = DeveloperExecutionResult(
            summary="done",
            files=[],
            manifest_path="manifest.json",
            journal_path="journal.json",
            progress={"status": "completed"},
        )
        receipts = {
            "materialized": {"status": "materialized", "path": ".env.example", "changed_paths": [".env.example"]},
            # A later repair round finds the file already materialized: the Harness still owns it.
            "unchanged": {"status": "unchanged", "path": ".env.example", "changed_paths": []},
        }
        for version, (label, fields) in enumerate(receipts.items(), start=1):
            with self.subTest(receipt=label):
                receipt = workflow.store.write_json_artifact(
                    run_id,
                    f"runtime_config_execution_v{version}.json",
                    {"schema_version": "runtime_config_execution_v1", **fields},
                )
                with (
                    patch.object(workflow.developer_tool_loop, "execute", return_value=result) as execute,
                    patch(
                        "sdlc_harness.workflows.phase4_implementation._verification_policy_for_state",
                        return_value=object(),
                    ),
                ):
                    workflow._execute_developer_tool_loop(
                        state={"run_id": run_id},
                        run_id=run_id,
                        work_packet={},
                        target_path=Path("target"),
                        latest_artifacts={
                            "runtime_config_execution": workflow.store.relative(receipt),
                        },
                        mode="implementation_repair",
                        current_phase="implementation_repair",
                        repair_iteration=1,
                        artifact_version=1,
                        manifest_key="developer_repair_execution_manifest",
                        journal_key="developer_repair_execution_journal",
                        repair_feedback={"repair_objective": {}},
                    )

                self.assertEqual(execute.call_args.kwargs["expected_dirty_paths"], [".env.example"])

    def test_legacy_repair_state_uses_next_unused_session_ordinal(self) -> None:
        workflow = PhaseFourWorkflow(self._settings())
        run_id = "run_next_repair_session"
        execution_root = workflow.store.run_dir(run_id) / "developer_execution"
        (execution_root / "implementation_repair_v1").mkdir(parents=True)
        (execution_root / "implementation_repair_v5").mkdir()
        (execution_root / "qa_repair_v9").mkdir()

        ordinal = workflow._next_repair_session_iteration(
            run_id=run_id,
            repair_scope="implementation",
            minimum=2,
        )

        self.assertEqual(ordinal, 6)

    def _settings(self, **extra_overrides: str):
        overrides = {
            "SDLC_LLM_MODE": "fake",
            "SDLC_SYNC_MODE": "mock",
            "SDLC_DESIGN_REPAIR_LIMIT": "2",
            "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": "2",
            "SDLC_IMPLEMENTATION_REPAIR_LIMIT": "2",
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            **extra_overrides,
        }
        return load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides=overrides,
        )

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8")

    def _git_output(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _create_phase3_run(self) -> dict[str, object]:
        return self._materialize_stage(self.phase3_runtime_template)

    def _materialize_stage(
        self,
        runtime_template: RuntimeDirectoryTemplate,
        *,
        with_written_files: bool = False,
    ) -> dict[str, object]:
        manager = RunWorkspaceManager(self.settings)
        worktree_path = manager.expected_worktree_path(self.run_id)
        runtime_template.materialize(
            self.runtime_root,
            replacements={
                self.phase3_run_id: self.run_id,
                self.phase3_run_id[-8:]: self.run_id[-8:],
                self.phase3_template_canonical: str(self.target_repo.resolve()),
                self.phase3_template_remote: self.remote_url,
                self.phase3_template_worktree: str(worktree_path),
            },
        )
        state = manager.store.read_state(self.run_id)
        create_fast_run_workspace(manager, run_id=self.run_id, state=state)
        (worktree_path / ".codegraph").mkdir(exist_ok=True)
        if with_written_files:
            self.written_worktree_template.apply(worktree_path)
        return PhaseOneWorkflow(self.settings).get_status(run_id=self.run_id)

    def _create_phase4_ready_run(self) -> dict[str, object]:
        return self._materialize_stage(self.ready_runtime_template)

    def test_continue_run_rejects_llm_mode_drift_before_developer_execution(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(run_id)
        state["llm"] = {"mode": "deepseek", "model": "deepseek-flash"}
        store.write_state(state)

        with self.assertRaises(HarnessError) as raised:
            PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)

        self.assertEqual(raised.exception.error_code, "run_llm_mode_mismatch")
        after = store.read_state(run_id)
        self.assertEqual(after["status"], "implementation_ready")
        self.assertNotIn("developer_execution", after)

    def _create_phase4_written_run(self) -> dict[str, object]:
        return self._materialize_stage(self.written_runtime_template, with_written_files=True)

    def _create_phase4_tested_run(self) -> dict[str, object]:
        return self._materialize_stage(self.tested_runtime_template, with_written_files=True)

    def _create_phase4_traced_run(self) -> dict[str, object]:
        return self._materialize_stage(self.traced_runtime_template, with_written_files=True)

    def _workspace_path(self, run: dict[str, object]) -> Path:
        artifact = self.repo_root / str(run["latest_artifacts"]["run_workspace"])
        return Path(json.loads(artifact.read_text(encoding="utf-8"))["worktree_path"])

    def _mark_run_as_historical_java(self, run_id: str) -> None:
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(run_id)
        state.pop("profile_snapshot", None)
        store.write_state(state)

    def test_approve_implementation_plan_creates_developer_work_packet(self) -> None:
        phase3 = self._create_phase3_run()

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_preparing")
        self.assertEqual(response["status"], "implementation_ready", response)
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("developer_work_packet", response["latest_artifacts"])
        self.assertIn("Workspace/branch", response["summary_for_user"])
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), "")

        packet_path = self.repo_root / str(response["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["version"], "developer_work_packet_v3")
        self.assertEqual(packet["schema_version"], "developer_work_packet_profiled_v3")
        self.assertEqual(packet["run_id"], phase3["run_id"])
        self.assertEqual(packet["approved_plan"]["implementation_plan"], phase3["latest_artifacts"]["implementation_plan"])
        self.assertTrue(packet["implementation_steps"])
        self.assertTrue(packet["execution_units"])
        structured_path = self.repo_root / str(
            phase3["latest_artifacts"]["implementation_plan_structured"]
        )
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                (item["unit_id"], item["order"], item["depends_on"])
                for item in packet["execution_units"]
            ],
            [
                (item["unit_id"], item["order"], item["depends_on"])
                for item in structured["work_units"]
            ],
        )
        packet_units = {item["unit_id"]: item for item in packet["execution_units"]}
        for source_unit in structured["work_units"]:
            projected_unit = packet_units[source_unit["unit_id"]]
            for field in (
                "planned_code_targets",
                "migration_impact",
                "runtime_bindings",
                "boundary_contracts",
            ):
                default = {} if field == "migration_impact" else []
                self.assertEqual(projected_unit[field], source_unit.get(field, default))
        self.assertTrue(packet["proposed_files"])
        self.assertTrue(packet["current_files"])
        self.assertEqual(packet["run_workspace"]["workspace_mode"], "git_worktree_v1")
        self.assertEqual(packet["run_workspace"]["branch"], response["workspace"]["branch"])
        self.assertEqual(packet["output_contract"]["format"], "changed_file_blocks_v1")
        self.assertTrue(packet["output_contract"]["changed_files_only"])
        self.assertTrue(packet["output_contract"]["allowed_paths"])
        grouped = packet["design_mappings_by_file"]
        self.assertTrue(any(path.startswith(("apps/api/src/", "packages/db/src/")) for path in grouped))
        self.assertTrue(any(item["exists"] for item in packet["proposed_files"]))

    def test_clean_pre_session_plan_v4_packet_upgrades_once_without_code_mutation(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet.pop("execution_units")
        packet["version"] = "developer_work_packet_v2"
        packet["schema_version"] = "developer_work_packet_profiled_v2"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = self._git_output(self.target_repo, "status", "--porcelain")
        workflow = PhaseFourWorkflow(self.settings)
        state = workflow.store.read_state(run_id)

        upgraded, upgraded_path = workflow._upgrade_unit_aware_work_packet(
            state=state,
            run_id=run_id,
            work_packet=packet,
        )

        self.assertTrue(upgraded_path)
        self.assertEqual(upgraded["version"], "developer_work_packet_v3")
        self.assertEqual(len(upgraded["execution_units"]), len(
            json.loads((self.repo_root / state["approved_artifacts"]["implementation_plan_structured"]).read_text(encoding="utf-8"))["work_units"]
        ))
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), before)
        unchanged, second_path = workflow._upgrade_unit_aware_work_packet(
            state=state,
            run_id=run_id,
            work_packet=upgraded,
        )
        self.assertIs(unchanged, upgraded)
        self.assertEqual(second_path, "")

    def test_clean_pre_session_plan_v4_packet_repairs_lossy_execution_units(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["execution_units"][0].pop("boundary_contracts")
        before = self._git_output(self.target_repo, "status", "--porcelain")
        workflow = PhaseFourWorkflow(self.settings)
        state = workflow.store.read_state(run_id)

        upgraded, upgraded_path = workflow._upgrade_unit_aware_work_packet(
            state=state,
            run_id=run_id,
            work_packet=packet,
        )

        self.assertTrue(upgraded_path)
        self.assertIn("boundary_contracts", upgraded["execution_units"][0])
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), before)

    def test_paused_session_can_upgrade_missing_read_context_contract(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        for unit in packet["execution_units"]:
            unit.pop("context_paths", None)
            unit.pop("contract_anchor_paths", None)
        expected_write_paths = {
            unit["unit_id"]: list(unit["write_paths"])
            for unit in packet["execution_units"]
        }
        workflow = PhaseFourWorkflow(self.settings)
        state = workflow.store.read_state(run_id)
        state["latest_artifacts"]["developer_execution_manifest"] = (
            f".runtime/runs/{run_id}/developer_execution/act_v1/manifest.json"
        )

        upgraded, upgraded_path = workflow._upgrade_unit_aware_work_packet(
            state=state,
            run_id=run_id,
            work_packet=packet,
        )

        self.assertTrue(upgraded_path)
        self.assertTrue(all("context_paths" in unit for unit in upgraded["execution_units"]))
        self.assertTrue(all("contract_anchor_paths" in unit for unit in upgraded["execution_units"]))
        self.assertEqual(
            {
                unit["unit_id"]: unit["write_paths"]
                for unit in upgraded["execution_units"]
            },
            expected_write_paths,
        )

    def test_approve_implementation_plan_rejects_plan_changed_after_review(self) -> None:
        phase3 = self._create_phase3_run()
        structured_path = self.repo_root / str(
            phase3["latest_artifacts"]["implementation_plan_structured"]
        )
        structured_path.write_text(
            structured_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(HarnessError) as context:
            CheckpointApprovalWorkflow(self.settings).approve_checkpoint(
                run_id=str(phase3["run_id"])
            )

        self.assertEqual(
            context.exception.error_code,
            "implementation_plan_review_binding_invalid",
        )

    def test_profiled_qa_legacy_java_path_block_can_retry_after_policy_fix(self) -> None:
        state = {
            "status": "blocked",
            "current_phase": "qa_testing",
            "profile_snapshot": {"project_profile_id": "typescript_fullstack_v1"},
            "last_error": {
                "error_code": "developer_act_unsafe_path",
                "message": (
                    "Unsafe or unsupported Developer.act path: "
                    "packages/contracts/src/CulturalHeritage.ts"
                ),
            },
        }

        self.assertTrue(PhaseFourWorkflow._qa_generation_retry_allowed(state))
        state.pop("profile_snapshot")
        self.assertFalse(PhaseFourWorkflow._qa_generation_retry_allowed(state))

    def test_v2_code_trace_emits_one_mapping_per_stable_requirement(self) -> None:
        source_path = "apps/api/src/CulturalHeritage.ts"
        test_path = "apps/api/src/CulturalHeritage.test.ts"
        packet = {
            "run_id": "run-v2",
            "issue_key": "SDLC-2",
            "requirement_contract": {"version": "v2", "revision": 3},
            "canonical_requirements": [{"id": "REQ-1", "content": "Keep heritage data."}],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-1",
                    "content": "Keep heritage data.",
                    "design_targets": ["CulturalHeritage"],
                    "design_element_ids": ["class:CulturalHeritage"],
                    "planned_code_targets": [source_path],
                    "test_intents": ["Verify heritage data."],
                }
            ],
            "design_mappings_by_file": {
                source_path: [
                    {
                        "design_element_id": "class:CulturalHeritage",
                        "design_element_type": "class",
                        "design_element_name": "CulturalHeritage",
                        "planned_code_target": source_path,
                    }
                ]
            },
            "proposed_files": [{"path": test_path}],
        }

        result = CodeTraceValidator(
            CapturingCodeTraceLlm(),
            symbol_extractor=WorkPacketTypeScriptAstExtractor(packet),
        ).validate(
            target_path=self.target_repo,
            work_packet=packet,
            code_trace_policy=build_code_trace_policy(
                planning_policy=build_planning_path_policy(
                    project=get_project_profile("typescript_fullstack_v1"),
                    language=get_language_profile("typescript_v1"),
                ),
                symbol_extractor_id="typescript_compiler_ast_v1",
            ),
        )

        payload = result.to_dict()
        self.assertTrue(result.ok)
        self.assertEqual(payload["mode"], "scenario_boundary_code_validation_v1")
        self.assertEqual(payload["requirement_contract"]["revision"], 3)
        self.assertEqual([item["requirement_ref"] for item in payload["mappings"]], ["REQ-1"])
        self.assertEqual(payload["stats"]["requirements_covered"], 1)

    def test_v2_qa_requires_exact_stable_requirement_coverage(self) -> None:
        context = {
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [
                {"id": "REQ-1", "content": "First"},
                {"id": "REQ-2", "content": "Second"},
            ],
        }
        qa_policy = build_qa_test_policy(
            planning_policy=build_planning_path_policy(
                project=get_project_profile("typescript_fullstack_v1"),
                language=get_language_profile("typescript_v1"),
            )
        )

        with self.assertRaises(HarnessError) as caught:
            QaTesterAgent(MissingRequirementQaLlm()).generate_tests(
                context=context,
                qa_policy=qa_policy,
            )

        self.assertEqual(caught.exception.error_code, "qa_requirement_coverage_invalid")

        generated = QaTesterAgent().generate_tests(context=context, qa_policy=qa_policy)
        covered = {
            requirement_id
            for scenario in generated.scenarios
            for requirement_id in scenario["requirement_ids"]
        }
        self.assertEqual(covered, {"REQ-1", "REQ-2"})

    def test_v2_pr_body_reports_contract_and_stable_id_coverage(self) -> None:
        body = _code_review_pr_body(
            state={
                "quality_reports": {
                    "implementation_traceability": {
                        "requirement_contract_version": "v2",
                        "requirement_revision": 4,
                        "stats": {"mapping_count": 2, "requirement_count": 2, "requirements_covered": 2},
                        "llm_confidence": 0.9,
                    },
                    "qa_acceptance": {
                        "scenario_count": 1,
                        "requirement_ids": ["REQ-1", "REQ-2"],
                    },
                }
            },
            run_id="run-v2",
            issue_key="SDLC-2",
            artifact_paths={"requirement_design_code_trace": "trace.json"},
            qa_generated={
                "scenarios": [
                    {"name": "canonical flow", "requirement_ids": ["REQ-1", "REQ-2"]}
                ],
                "risks": [],
            },
        )

        self.assertIn("Requirement contract: `v2` revision `4`", body)
        self.assertIn("Canonical requirement coverage: 2/2", body)
        self.assertIn("canonical flow [REQ-1, REQ-2]", body)

    def test_cli_approve_checkpoint_rejects_legacy_implementation_plan(self) -> None:
        phase3 = self._create_phase3_run()
        self._mark_run_as_historical_java(str(phase3["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "approve-checkpoint",
                    "--run-id",
                    str(phase3["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], phase3["run_id"])

    def test_phase4_rejects_non_implementation_plan_checkpoint(self) -> None:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_missing_structured_plan_is_rejected(self) -> None:
        phase3 = self._create_phase3_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(phase3["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("implementation_plan_structured")
        state["latest_artifacts"] = latest
        store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_implementation_plan_not_found")

    def test_target_repo_dirty_blocks_without_writing_packet(self) -> None:
        phase3 = self._create_phase3_run()
        (self._workspace_path(phase3) / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")
        self.assertNotIn("developer_work_packet", response["latest_artifacts"])

    def test_continue_run_applies_developer_act_code_changes(self) -> None:
        ready = self._create_phase4_ready_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_writing")
        self.assertEqual(response["status"], "implementation_written", response)
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("code_change_report", response["latest_artifacts"])
        workspace_path = self._workspace_path(response)
        self.assertEqual(
            self._git_output(workspace_path, "rev-parse", "--abbrev-ref", "HEAD"),
            response["workspace"]["branch"],
        )
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), "")
        status = self._git_output(workspace_path, "status", "--porcelain")
        self.assertIn("CulturalHeritage.ts", status)
        self.assertIn("DeleteCulturalHeritageFlow.test.ts", status)

        service_path = (
            workspace_path
            / "apps"
            / "api"
            / "src"
            / "CulturalHeritage.ts"
        )
        self.assertIn("execute", service_path.read_text(encoding="utf-8"))
        report_path = self.repo_root / str(response["latest_artifacts"]["code_change_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("Code Change Report V1", report)
        self.assertIn("Tests: not run in Slice 4.2", report)

    def test_continue_run_uses_tool_loop_for_initial_developer_act(self) -> None:
        ready = self._create_phase4_ready_run()
        workflow = PhaseFourWorkflow(self.settings)

        response = workflow.continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "implementation_written", response)
        self.assertEqual(response["developer_execution"]["schema_version"], "developer_execution_state_v2")
        self.assertEqual(
            response["developer_execution"]["contract_version"],
            "developer_execution_units_v1",
        )
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["developer_execution"]["mode"], "developer_act")
        self.assertEqual(response["developer_execution"]["status"], "completed")
        self.assertIn("developer_execution_manifest", response["latest_artifacts"])
        self.assertIn("developer_execution_journal", response["latest_artifacts"])
        self.assertIn("developer_execution_session", response["latest_artifacts"])
        manifest_path = self.repo_root / str(response["latest_artifacts"]["developer_execution_manifest"])
        journal_path = self.repo_root / str(response["latest_artifacts"]["developer_execution_journal"])
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["version"], "developer_execution_manifest_v8")
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["status"], "completed")
        report_path = self.repo_root / str(response["latest_artifacts"]["code_change_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("Execution mode: `tool_loop`", report)
        self.assertIn("Before SHA-256", report)

    def test_paused_tool_loop_resume_keeps_bound_executor_after_config_flip(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        first = PhaseFourWorkflow(self._settings())
        paused_error = DeveloperExecutionError(
            "developer_action_limit_exceeded",
            "pause for retry",
            run_id=run_id,
            retriable=True,
            manifest_path=f".runtime/runs/{run_id}/developer_execution/act_v1/manifest.json",
            journal_path=f".runtime/runs/{run_id}/developer_execution/act_v1/journal.json",
            progress={"total_items": 1, "completed_items": 0, "remaining_items": 1},
        )
        with patch.object(first.developer_tool_loop, "execute", side_effect=paused_error):
            paused = first.continue_run(run_id=run_id)

        self.assertEqual(paused["status"], "implementation_paused")
        self.assertEqual(paused["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(paused["developer_execution"]["status"], "paused")
        second = PhaseFourWorkflow(self._settings())
        completed = DeveloperExecutionResult(
            summary="resumed tool-loop",
            files=[],
            manifest_path=str(paused["latest_artifacts"]["developer_execution_manifest"]),
            journal_path=str(paused["latest_artifacts"]["developer_execution_journal"]),
            progress={"total_items": 1, "completed_items": 1, "remaining_items": 0},
        )
        with patch.object(second.developer_tool_loop, "execute", return_value=completed) as execute:
            response = second.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["developer_execution"]["status"], "completed")
        execute.assert_called_once()

    def test_paused_developer_scope_recovery_preserves_worktree_and_starts_act_v2(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        plan_path = self.repo_root / str(
            ready["latest_artifacts"]["implementation_plan_structured"]
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for unit in plan["work_units"]:
            unit["planned_code_targets"] = [
                target
                for target in unit["planned_code_targets"]
                if target.get("path") != "packages/db/src/index.ts"
            ]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        workflow = PhaseFourWorkflow(self._settings())
        paused_error = DeveloperExecutionError(
            "developer_step_review_required",
            "pause legacy scope",
            run_id=run_id,
            retriable=True,
            manifest_path=f".runtime/runs/{run_id}/developer_execution/act_v1/manifest.json",
            journal_path=f".runtime/runs/{run_id}/developer_execution/act_v1/journal.json",
            progress={
                "total_items": 2,
                "completed_items": 1,
                "remaining_items": 1,
                "pending_step_review": {"call_id": "legacy-call"},
            },
        )
        with patch.object(workflow.developer_tool_loop, "execute", side_effect=paused_error):
            paused = workflow.continue_run(run_id=run_id)

        amended_units = json.loads(json.dumps(plan["work_units"]))
        amended_units[0]["planned_code_targets"].append(
            {"path": "packages/db/src/index.ts", "action": "create_or_update"}
        )
        previous_packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        amended_packet = json.loads(previous_packet_path.read_text(encoding="utf-8"))
        amended_packet["output_contract"]["allowed_paths"].append("packages/db/src/index.ts")
        amended_packet["proposed_files"].append(
            {"path": "packages/db/src/index.ts", "action": "create_or_update"}
        )
        amended_packet["execution_units"][0]["write_paths"].append("packages/db/src/index.ts")
        projections = {
            key: plan[key]
            for key in (
                "implementation_steps",
                "proposed_files",
                "design_mappings",
                "requirement_coverage",
                "test_intents",
            )
        }
        with (
            patch(
                "sdlc_harness.workflows.phase4_implementation.merge_work_unit_updates",
                return_value=amended_units,
            ),
            patch(
                "sdlc_harness.workflows.phase4_implementation.canonicalize_work_unit_dependencies",
                return_value=amended_units,
            ),
            patch(
                "sdlc_harness.workflows.phase4_implementation.component_plan_projections",
                return_value=projections,
            ),
            patch(
                "sdlc_harness.workflows.phase4_implementation.build_developer_work_packet",
                return_value=amended_packet,
            ),
        ):
            recovered = PhaseFourWorkflow(self._settings()).recover_developer_scope(run_id=run_id)

        self.assertEqual(paused["status"], "implementation_paused")
        self.assertEqual(recovered["status"], "implementation_ready")
        self.assertEqual(recovered["developer_execution"]["session_id"], "developer_act-v2")
        self.assertEqual(recovered["developer_execution"]["status"], "pending")
        self.assertIn("developer_scope_amendment", recovered["latest_artifacts"])
        self.assertNotIn("developer_execution_manifest", recovered["latest_artifacts"])
        packet_path = self.repo_root / str(recovered["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        owners = [
            unit["unit_id"]
            for unit in packet["execution_units"]
            if "packages/db/src/index.ts" in unit["write_paths"]
        ]
        self.assertEqual(len(owners), 1)

        completed = DeveloperExecutionResult(
            summary="scope-recovered tool-loop",
            files=[],
            manifest_path=f".runtime/runs/{run_id}/developer_execution/act_v2/manifest.json",
            journal_path=f".runtime/runs/{run_id}/developer_execution/act_v2/journal.json",
            progress={"total_items": 2, "completed_items": 2, "remaining_items": 0},
        )
        resumed = PhaseFourWorkflow(self._settings())
        with patch.object(resumed.developer_tool_loop, "execute", return_value=completed) as execute:
            response = resumed.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(execute.call_args.kwargs["session_iteration"], 2)

    def test_runtime_config_recovery_reopens_only_the_owner_unit(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        plan_path = self.repo_root / str(
            ready["latest_artifacts"]["implementation_plan_structured"]
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        owner = plan["work_units"][0]
        owner_id = str(owner["unit_id"])
        read_by = str(owner["planned_code_targets"][0]["path"])
        plan["runtime_config_impact"] = {
            "status": "required",
            "reason": "The approved external endpoint must be configurable.",
            "changes": [
                {
                    "kind": "env_var",
                    "unit_id": owner_id,
                    "name": "ETOUR_BASE_URL",
                    "default": "http://127.0.0.1:8090",
                    "read_by": read_by,
                    "purpose": "Configure the external ETOUR endpoint.",
                }
            ],
        }
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self._workspace_path(ready) / "legacy-generated.txt").write_text(
            "preserve me\n",
            encoding="utf-8",
        )

        recovered = PhaseFourWorkflow(self._settings()).recover_runtime_config(
            run_id=run_id
        )

        self.assertEqual(recovered["status"], "implementation_ready")
        self.assertEqual(recovered["developer_execution"]["pending_unit_ids"], [owner_id])
        self.assertIn("runtime_config_recovery", recovered["latest_artifacts"])
        self.assertEqual(
            PhaseOneWorkflow(self.settings)
            .store.read_state(run_id)["developer_expected_dirty_paths"],
            ["legacy-generated.txt"],
        )
        packet_path = self.repo_root / str(
            recovered["latest_artifacts"]["developer_work_packet"]
        )
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [unit["unit_id"] for unit in packet["execution_units"]],
            [owner_id],
        )
        self.assertEqual(
            packet["execution_units"][0]["runtime_config_changes"],
            plan["runtime_config_impact"]["changes"],
        )
        self.assertFalse(
            packet["execution_units"][0]["test_intent_mutation_required"]
        )

    def test_paused_repair_resume_reuses_frozen_packet_and_artifact_version(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        run_id = str(repair_needed["run_id"])
        first = PhaseFourWorkflow(self._settings())
        paused_error = DeveloperExecutionError(
            "developer_action_limit_exceeded",
            "pause repair for retry",
            run_id=run_id,
            retriable=True,
            manifest_path=f".runtime/runs/{run_id}/developer_execution/implementation_repair_v1/manifest.json",
            journal_path=f".runtime/runs/{run_id}/developer_execution/implementation_repair_v1/journal.json",
            progress={"total_items": 1, "completed_items": 0, "remaining_items": 1},
        )
        with patch.object(first.developer_tool_loop, "execute", side_effect=paused_error):
            paused = first.continue_run(run_id=run_id)

        frozen_packet = str(paused["latest_artifacts"]["developer_work_packet"])
        self.assertIn("developer_work_packet_v2.json", frozen_packet)
        self.assertEqual(paused["implementation_repair_iterations"], 0)
        self.assertEqual(paused["implementation_repair_total_iterations"], 1)
        self.assertEqual(paused["developer_execution"]["artifact_version"], 2)
        second = PhaseFourWorkflow(self._settings())
        completed = DeveloperExecutionResult(
            summary="repair session resumed",
            files=[],
            manifest_path=str(paused["latest_artifacts"]["developer_repair_execution_manifest"]),
            journal_path=str(paused["latest_artifacts"]["developer_repair_execution_journal"]),
            progress={"total_items": 1, "completed_items": 1, "remaining_items": 0},
        )
        with patch.object(second.developer_tool_loop, "execute", return_value=completed) as execute:
            response = second.continue_run(run_id=run_id)

        execute.assert_called_once()
        resumed_packet = execute.call_args.kwargs["work_packet"]
        self.assertEqual(resumed_packet["artifact_ref"], "developer_work_packet_v2")
        self.assertEqual(response["latest_artifacts"]["developer_work_packet"], frozen_packet)
        self.assertEqual(response["developer_execution"]["artifact_version"], 2)
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["implementation_repair_total_iterations"], 1)
        self.assertEqual(execute.call_args.kwargs["repair_iteration"], 1)
        self.assertEqual(execute.call_args.kwargs["session_iteration"], 1)

    def test_non_retriable_initial_tool_loop_failure_blocks(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        workflow = PhaseFourWorkflow(self._settings())
        blocked_error = DeveloperExecutionError(
            "developer_workspace_drift",
            "workspace drift",
            run_id=run_id,
            retriable=False,
            progress={"total_items": 1, "completed_items": 0, "remaining_items": 1},
        )

        with patch.object(workflow.developer_tool_loop, "execute", side_effect=blocked_error):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertEqual(response["developer_execution"]["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_workspace_drift")

    def test_plain_tool_loop_contract_failure_is_non_retriable(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        workflow = PhaseFourWorkflow(self._settings())

        with patch.object(
            workflow.developer_tool_loop,
            "execute",
            side_effect=HarnessError(
                "developer_tool_unsafe_path",
                "Unsafe expected dirty path.",
                run_id=run_id,
            ),
        ):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["developer_execution"]["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_tool_unsafe_path")

    def test_llm_transport_failure_pauses_tool_loop_and_legacy_state_can_resume(self) -> None:
        ready = self._create_phase4_ready_run()
        run_id = str(ready["run_id"])
        first = PhaseFourWorkflow(self._settings())

        with patch.object(
            first.developer_tool_loop,
            "execute",
            side_effect=HarnessError(
                "llm_transport_failed",
                "LLM request failed: timed out",
                run_id=run_id,
            ),
        ):
            paused = first.continue_run(run_id=run_id)

        self.assertEqual(paused["status"], "implementation_paused")
        self.assertIn("continue_run", paused["allowed_next_actions"])
        self.assertEqual(paused["developer_execution"]["status"], "paused")

        store = PhaseOneWorkflow(self.settings).store
        legacy = store.read_state(run_id)
        legacy["status"] = "blocked"
        legacy["allowed_next_actions"] = ["get_status", "cancel_run"]
        legacy["last_error"] = HarnessError(
            "requirement_refiner_failed",
            "LLM request failed: legacy timeout",
            run_id=run_id,
        ).to_response()
        execution = dict(legacy["developer_execution"])
        execution["status"] = "blocked"
        legacy["developer_execution"] = execution
        store.write_state(legacy)

        status = PhaseOneWorkflow(self.settings).get_status(run_id=run_id)
        self.assertIn("continue_run", status["allowed_next_actions"])
        second = PhaseFourWorkflow(self._settings())
        completed = DeveloperExecutionResult(
            summary="resumed after transport failure",
            files=[],
            manifest_path="",
            journal_path="",
            progress={"total_items": 1, "completed_items": 1, "remaining_items": 0},
        )
        with patch.object(second.developer_tool_loop, "execute", return_value=completed) as execute:
            response = second.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        execute.assert_called_once()

    def _retired_legacy_repair_internal_failure_does_not_switch_to_tool_loop(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self._settings())

        with patch.object(workflow.developer, "repair", side_effect=RuntimeError("unexpected legacy failure")):
            response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertEqual(response["last_error"]["error_code"], "developer_execution_internal_error")
        self.assertNotEqual(response.get("developer_execution", {}).get("executor"), "tool_loop")

    def test_canonical_checkout_change_does_not_redirect_run_workspace(self) -> None:
        ready = self._create_phase4_ready_run()
        self._git(self.target_repo, "checkout", "-b", "manual-review")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(self._git_output(self.target_repo, "branch", "--show-current"), "manual-review")
        workspace_path = self._workspace_path(response)
        self.assertEqual(
            self._git_output(workspace_path, "branch", "--show-current"),
            response["workspace"]["branch"],
        )

    def test_legacy_phase4_run_without_workspace_is_not_auto_adopted(self) -> None:
        phase3 = self._create_phase3_run()
        workspace_artifact = self.repo_root / str(phase3["latest_artifacts"]["run_workspace"])
        workspace_artifact.unlink()

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "run_workspace_missing")

    def test_tampered_work_packet_workspace_fails_closed(self) -> None:
        ready = self._create_phase4_ready_run()
        packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["run_workspace"]["branch"] = "feature/wrong-run"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "run_workspace_mismatch")

    def test_cli_continue_run_rejects_legacy_developer_act(self) -> None:
        ready = self._create_phase4_ready_run()
        self._mark_run_as_historical_java(str(ready["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(ready["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--qa-repair-limit",
                    "3",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], ready["run_id"])

    def test_continue_run_runs_verification_after_implementation_written(self) -> None:
        written = self._create_phase4_written_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_tested")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("test_report", response["latest_artifacts"])
        report_path = self.repo_root / str(response["latest_artifacts"]["test_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("Test Report V1", report)
        self.assertIn("Status: `passed`", report)
        self.assertIn('["npm", "run", "test:unit"]', report)

    def test_continue_run_runs_code_trace_after_implementation_tested(self) -> None:
        tested = self._create_phase4_tested_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced")
        self.assertEqual(
            response["allowed_next_actions"],
            ["continue_run", "revalidate_implementation", "get_status", "cancel_run"],
        )
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_traceability"]["ok"])
        trace_path = self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(trace["version"], "requirement_design_code_trace_v1")
        self.assertTrue(trace["ok"])
        self.assertTrue(trace["mappings"])
        self.assertFalse(trace["missing_links"])
        self.assertGreater(trace["stats"]["test_file_count"], 0)
        self.assertEqual(trace["mode"], "scenario_boundary_code_validation_v1")
        self.assertTrue(trace["llm_required"])
        self.assertTrue(trace["llm_review"]["ok"])
        self.assertTrue(trace["llm_review"]["review_context"]["code_snippets_included"])
        self.assertIn("implementation_validation_ledger", response["latest_artifacts"])
        ledger_path = self.repo_root / str(response["latest_artifacts"]["implementation_validation_ledger"])
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["version"], "implementation_validation_ledger_v1")
        self.assertEqual(ledger["snapshot_fingerprint"], trace["snapshot_fingerprint"])

    def test_code_trace_llm_receives_source_and_test_snippets(self) -> None:
        tested = self._create_phase4_tested_run()
        llm = CapturingCodeTraceLlm()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(llm)

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["status"], "implementation_traced")
        self.assertIn('"code_snippets"', llm.prompt)
        self.assertIn("CulturalHeritage", llm.prompt)
        self.assertIn("DeleteCulturalHeritageFlow.test.ts", llm.prompt)
        self.assertIn("execute", llm.prompt)
        payloads = [json.loads(prompt) for prompt in llm.prompts]
        snippets = {
            item["path"]: item
            for payload in payloads
            for item in payload["code_snippets"]
        }
        flow_test = next(path for path in snippets if path.endswith("DeleteCulturalHeritageFlow.test.ts"))
        self.assertFalse(snippets[flow_test]["content_truncated"])
        self.assertEqual(snippets[flow_test]["chars_included"], snippets[flow_test]["chars_total"])
        self.assertTrue(any(item["content_truncated"] is False for item in snippets.values()))

    def test_code_trace_llm_accepts_prose_wrapped_json(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(ProseWrappedCodeTraceLlm())

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["status"], "implementation_traced")
        self.assertTrue(response["quality_reports"]["implementation_traceability"]["ok"])

    def test_code_validation_step_review_pauses_input_and_output_then_resumes(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_VALIDATION_STEP_REVIEW_ENABLED="1")
        tested = self._create_phase4_tested_run()

        input_pending = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        self.assertEqual(input_pending["status"], "implementation_validation_step_review_required")
        self.assertEqual(input_pending["pending_step_review"]["review_stage"], "input")

        output_pending = PhaseFourWorkflow(
            self.settings,
            code_validation_step_review={
                "enabled": True,
                "call_id": input_pending["pending_step_review"]["call_id"],
                "decision": "approve",
            },
        ).continue_code_validation_step_review(run_id=str(tested["run_id"]))
        self.assertEqual(output_pending["status"], "implementation_validation_step_review_required")
        self.assertEqual(output_pending["pending_step_review"]["review_stage"], "output")

        traced = PhaseFourWorkflow(
            self.settings,
            code_validation_step_review={
                "enabled": True,
                "call_id": output_pending["pending_step_review"]["call_id"],
                "decision": "approve",
            },
        ).continue_code_validation_step_review(run_id=str(tested["run_id"]))
        self.assertEqual(traced["status"], "implementation_traced")

    def test_code_validation_step_review_after_developer_resumes_validation_not_developer(self) -> None:
        workflow = PhaseFourWorkflow(self.settings)
        state = {
            "run_id": "run_repair_validation_pause",
            "status": "implementation_executing",
            "current_phase": "implementation_repair",
        }

        workflow._pause_for_code_validation_step_review(
            state=state,
            run_id="run_repair_validation_pause",
            progress={"call_id": "code-validation-scenario-SC-1-0001-a01-input", "review_stage": "input"},
        )

        self.assertEqual(
            state["implementation_validation_resume"],
            {"status": "implementation_tested", "current_phase": "implementation_testing"},
        )

    def test_continue_run_runs_qa_after_implementation_traced(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        recorder = RecordingQaTester()
        workflow.qa_tester = recorder
        workflow.verification_runner = SequenceVerificationRunner(["passed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "qa_tested", response)
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("qa_generated_tests", response["latest_artifacts"])
        self.assertIn("qa_test_execution", response["latest_artifacts"])
        self.assertIn("qa_test_report", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["qa_acceptance"]["ok"])
        report_path = self.repo_root / str(response["latest_artifacts"]["qa_test_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("QA Test Report V1", report)
        self.assertIn("Status: `pass`", report)
        self.assertIn("Generated Test Execution", report)
        self.assertIn("Packet mode:", report)
        self.assertIn("Any truncated:", report)
        self.assertTrue(
            (
                self._workspace_path(response)
                / "apps"
                / "api"
                / "src"
                / "RecordingQaAcceptance.test.ts"
            ).exists()
        )
        self.assertEqual(recorder.context["packet_mode"], "trace_dev_style_compact_qa_packet_v2")
        self.assertIn("accepted_requirement", recorder.context)
        self.assertIn("code_under_test", recorder.context)
        self.assertIn("verification", recorder.context)
        self.assertIn("traceability", recorder.context)
        self.assertIn("artifact_references", recorder.context)
        self.assertNotIn("artifacts", recorder.context)
        self.assertNotIn("content", recorder.context["artifact_references"])
        self.assertGreaterEqual(len(recorder.context["code_under_test"]["files"]), 1)
        self.assertEqual(
            recorder.context["context_budget"]["omitted_full_artifacts"],
            ["design_artifact", "implementation_plan", "requirement_design_code_trace"],
        )

    def test_continue_run_moves_to_qa_repair_needed_when_qa_fails(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_repair")
        self.assertEqual(response["status"], "qa_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertEqual(response["qa_repair_iterations"], 0)
        self.assertIn("qa_generated_tests", response["latest_artifacts"])
        self.assertIn("qa_test_execution", response["latest_artifacts"])
        self.assertIn("qa_test_report", response["latest_artifacts"])
        self.assertIn("qa_repair_feedback", response["latest_artifacts"])
        self.assertFalse(response["quality_reports"]["qa_acceptance"]["ok"])
        self.assertTrue(response["quality_reports"]["qa_repair"]["needed"])
        feedback_path = self.repo_root / str(response["latest_artifacts"]["qa_repair_feedback"])
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        self.assertEqual(feedback["version"], "qa_repair_feedback_v1")
        self.assertEqual(feedback["repair_scope"], "code_or_test")
        self.assertTrue(feedback["failing_scenarios"])

    def test_continue_run_blocks_when_qa_fails_with_zero_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_QA_REPAIR_LIMIT="0")
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_repair_limit_exceeded")
        self.assertEqual(response["qa_repair_iterations"], 0)
        self.assertNotIn("qa_repair_feedback", response["latest_artifacts"])

    def test_continue_run_from_qa_repair_needed_repairs_and_retests_to_pass(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "qa_tested")
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["qa_acceptance"]["ok"])
        self.assertFalse(response["quality_reports"]["qa_repair"]["needed"])
        self.assertIn("code_change_report_v2.md", response["latest_artifacts"]["code_change_report"])
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("requirement_design_code_trace_v2.json", response["latest_artifacts"]["requirement_design_code_trace"])
        self.assertIn("qa_generated_tests_v2.json", response["latest_artifacts"]["qa_generated_tests"])

    def test_repairable_code_trace_failure_after_qa_repair_returns_to_implementation_repair(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        qa_repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.continue_run(run_id=str(qa_repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed", response)
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "revalidate_implementation", "get_status", "cancel_run"])
        self.assertEqual(response["implementation_repair_iterations"], 0)
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertIsNone(response["last_error"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_repair"]["needed"])

    def test_qa_repair_uses_tool_loop_and_records_durable_artifacts(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(
            self._settings()
        )
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "qa_tested", response)
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["developer_execution"]["mode"], "qa_repair")
        self.assertEqual(response["developer_execution"]["status"], "completed")
        self.assertIn("developer_qa_repair_execution_manifest", response["latest_artifacts"])
        self.assertIn("developer_qa_repair_execution_journal", response["latest_artifacts"])
        self.assertNotIn("developer_repair_raw_response_v2", json.dumps(response["latest_artifacts"]))

    def test_continue_run_from_qa_repair_needed_blocks_when_qa_still_fails_at_limit(self) -> None:
        self.settings = self._settings(SDLC_QA_REPAIR_LIMIT="1")
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["passed", "failed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_repair_limit_exceeded")
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["qa_repair"]["limit_exceeded"])
    def test_continue_run_blocks_and_records_raw_response_when_qa_json_is_invalid(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = QaTesterAgent(InvalidQaLlm())

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_tester_llm_failed")
        self.assertIn("qa_tester_raw_response", response["latest_artifacts"])
        raw_path = self.repo_root / str(response["latest_artifacts"]["qa_tester_raw_response"])
        self.assertIn("cut off before scenarios", raw_path.read_text(encoding="utf-8"))

    def _qa_review_workflow(self, llm: "CountingQaLlm", **review: object) -> PhaseFourWorkflow:
        workflow = PhaseFourWorkflow(
            self.settings,
            qa_step_review={"enabled": True, "review_input": False, "review_output": True, **review},
        )
        workflow.qa_tester = QaTesterAgent(llm)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        return workflow

    def _set_packet_runtime_surface(self, run: dict[str, object], runnable_status: str) -> None:
        packet_path = self.repo_root / str(run["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["execution_units"][0].setdefault("surface_contracts", []).append(
            {
                "design_node_id": "VDN-GATE-SCREEN",
                "surface": "screen",
                "surface_contract": {"runtime_surface": {"runnable_status": runnable_status, "required_inputs": []}},
            }
        )
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_qa_step_review_pauses_input_before_provider_then_replays_approved_output(self) -> None:
        traced = self._create_phase4_traced_run()
        run_id = str(traced["run_id"])
        llm = CountingQaLlm()

        input_pending = self._qa_review_workflow(llm, review_input=True).continue_run(run_id=run_id)
        self.assertEqual(input_pending["status"], "qa_step_review_required", input_pending)
        self.assertEqual(input_pending["pending_step_review"]["review_stage"], "input")
        self.assertEqual(input_pending["pending_step_review"]["call_id"], "qa-generation-0001-a01-input")
        self.assertIn("qa_execution/step_review/v1/", input_pending["pending_step_review"]["exchange_path"].replace("\\", "/"))
        self.assertEqual(llm.calls, 0)
        self.assertNotIn("qa_generated_tests", input_pending["latest_artifacts"])
        with self.assertRaises(HarnessError) as not_resumable:
            PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(not_resumable.exception.error_code, "run_not_continuable")

        output_pending = self._qa_review_workflow(
            llm,
            review_input=True,
            call_id=input_pending["pending_step_review"]["call_id"],
            decision="approve",
        ).continue_qa_step_review(run_id=run_id)
        self.assertEqual(output_pending["status"], "qa_step_review_required", output_pending)
        self.assertEqual(output_pending["pending_step_review"]["review_stage"], "output")
        self.assertEqual(llm.calls, 1)

        replay_llm = CountingQaLlm()
        tested = self._qa_review_workflow(
            replay_llm,
            call_id=output_pending["pending_step_review"]["call_id"],
            decision="approve",
        ).continue_qa_step_review(run_id=run_id)
        self.assertEqual(tested["status"], "qa_tested", tested)
        self.assertEqual(replay_llm.calls, 0)
        self.assertIsNone(tested["pending_step_review"])
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertIsNone(state["qa_resume"])

    def test_qa_step_review_second_agent_attempt_is_a_separate_ordinal(self) -> None:
        traced = self._create_phase4_traced_run()
        run_id = str(traced["run_id"])
        llm = CountingQaLlm(responses=[InvalidQaLlm().complete(system="", prompt="")])

        first = self._qa_review_workflow(llm).continue_run(run_id=run_id)
        self.assertEqual(first["pending_step_review"]["call_id"], "qa-generation-0001-a01")
        second = self._qa_review_workflow(
            llm,
            call_id="qa-generation-0001-a01",
            decision="approve",
        ).continue_qa_step_review(run_id=run_id)
        self.assertEqual(second["status"], "qa_step_review_required", second)
        self.assertEqual(second["pending_step_review"]["call_id"], "qa-generation-0002-a01")
        self.assertEqual(llm.calls, 2)

        tested = self._qa_review_workflow(
            llm,
            call_id="qa-generation-0002-a01",
            decision="approve",
        ).continue_qa_step_review(run_id=run_id)
        self.assertEqual(tested["status"], "qa_tested", tested)
        self.assertEqual(llm.calls, 2)

    def test_qa_step_review_after_qa_repair_resumes_qa_not_developer(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))
        run_id = str(repair_needed["run_id"])
        feedback_path = str(repair_needed["latest_artifacts"]["qa_repair_feedback"])

        llm = CountingQaLlm()
        workflow = self._qa_review_workflow(llm)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        paused = workflow.continue_run(run_id=run_id)
        self.assertEqual(paused["status"], "qa_step_review_required", paused)
        self.assertEqual(paused["qa_repair_iterations"], 1)
        self.assertIn("code_change_report_v2.md", paused["latest_artifacts"]["code_change_report"])
        self.assertIn("requirement_design_code_trace_v2.json", paused["latest_artifacts"]["requirement_design_code_trace"])

        resume = self._qa_review_workflow(
            llm,
            call_id=paused["pending_step_review"]["call_id"],
            decision="approve",
        )
        resume.developer_tool_loop = None  # Any Developer call would fail loudly.
        tested = resume.continue_qa_step_review(run_id=run_id)

        self.assertEqual(tested["status"], "qa_tested", tested)
        self.assertEqual(tested["qa_repair_iterations"], 1)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(resume.verification_runner.calls, ["passed"])
        self.assertIn("code_change_report_v2.md", tested["latest_artifacts"]["code_change_report"])
        self.assertEqual(tested["quality_reports"]["qa_repair"]["repaired_from"], feedback_path)
        generated = json.loads(
            (self.repo_root / str(tested["latest_artifacts"]["qa_generated_tests"])).read_text(encoding="utf-8")
        )
        self.assertEqual(generated["repaired_from"], feedback_path)

    def test_qa_requires_runtime_closure_for_unbound_runtime_surface(self) -> None:
        traced = self._create_phase4_traced_run()
        run_id = str(traced["run_id"])
        self._set_packet_runtime_surface(traced, "externally_blocked")
        llm = CountingQaLlm()

        response = self._qa_review_workflow(llm, review_input=True).continue_run(run_id=run_id)

        self.assertEqual(response["status"], "blocked", response)
        self.assertEqual(response["last_error"]["error_code"], "qa_requires_runtime_closure")
        self.assertIn("VDN-GATE-SCREEN", response["last_error"]["message"])
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertEqual(llm.calls, 0)
        self.assertNotIn("qa_generated_tests", response["latest_artifacts"])
        self.assertFalse((self.store_run_dir(run_id) / "qa_execution").exists())

    def test_qa_runs_when_runtime_surfaces_are_bound(self) -> None:
        bound = self._create_phase4_traced_run()
        self._set_packet_runtime_surface(bound, "bound")
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        self.assertEqual(workflow.continue_run(run_id=str(bound["run_id"]))["status"], "qa_tested")

    def test_qa_runs_for_unbound_runtime_surface_after_runtime_closure(self) -> None:
        closed = self._create_phase4_traced_run()
        self._set_packet_runtime_surface(closed, "externally_blocked")
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(closed["run_id"]))
        # Gate only: no pinned mode, so the dispatcher keeps the QA path and the gate sees runtime_closed.
        state["runtime_closure"] = {"status": "runtime_closed"}
        store.write_state(state)
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        self.assertEqual(workflow.continue_run(run_id=str(closed["run_id"]))["status"], "qa_tested")

    def store_run_dir(self, run_id: str) -> Path:
        return PhaseOneWorkflow(self.settings).store.run_dir(run_id)

    # ---------------------------------------------------------------- L5.3.3 runtime closure

    def _create_closure_run(self) -> str:
        """A traced run whose frozen Design leaves one external input and one Plan env var to close."""

        traced = self._create_phase4_traced_run()
        worktree = self._workspace_path(traced)
        files = {
            "apps/web/src/ClosureEntry.tsx": 'import { ClosurePanel } from "./ClosurePanel";\nexport function ClosureEntry() {\n  return ClosurePanel();\n}\n',
            "apps/web/src/ClosurePanel.tsx": "export function ClosurePanel() {\n  return null;\n}\n",
            "apps/api/src/feed-client.ts": 'export const feedBaseUrl = process.env.FEED_BASE_URL ?? "http://127.0.0.1:9100";\n',
        }
        for relative, content in files.items():
            (worktree / relative).parent.mkdir(parents=True, exist_ok=True)
            (worktree / relative).write_text(content, encoding="utf-8")
        packet_path = self.repo_root / str(traced["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        unit = packet["execution_units"][0]
        entry, panel = "apps/web/src/ClosureEntry.tsx#ClosureEntry", "apps/web/src/ClosurePanel.tsx#ClosurePanel"
        unit.setdefault("design_node_ids", []).append("VDN-closure-panel")
        unit.setdefault("runtime_bindings", []).append(
            {
                "design_node_id": "VDN-closure-panel",
                "runtime_kind": "ui_component",
                "target": {"component_id": "web", "path": "apps/web/src/ClosurePanel.tsx", "symbol": "ClosurePanel"},
                "attachment": {"owner_path": "apps/web/src/ClosureEntry.tsx", "owner_symbol": "ClosureEntry", "relation": "rendered_by"},
            }
        )
        unit.setdefault("surface_contracts", []).append(
            {
                "design_node_id": "VDN-closure-panel",
                "surface": "ui",
                "surface_contract": {
                    "runtime_surface": {
                        "schema_version": "runtime_surface_binding_v1",
                        "entrypoint": entry,
                        "composition_path": [entry, panel],
                        "required_inputs": [
                            {
                                "name": "recordFeed",
                                "source": "external_use_case",
                                "binding": "PRE-1",
                                "provided_by": entry,
                                "requirement_refs": ["PRE-1"],
                            }
                        ],
                        "runnable_status": "externally_blocked",
                    }
                },
            }
        )
        packet["runtime_config_impact"] = {
            "status": "required",
            "reason": "feed URL",
            "changes": [
                {
                    "kind": "env_var",
                    "unit_id": unit["unit_id"],
                    "name": "FEED_BASE_URL",
                    "default": "http://127.0.0.1:9100",
                    "read_by": "apps/api/src/feed-client.ts",
                    "purpose": "feed base URL",
                }
            ],
        }
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(traced["run_id"])

    def _closure_workflow(self, *, planner=None, validator=None, **review) -> PhaseFourWorkflow:
        workflow = PhaseFourWorkflow(
            self.settings,
            runtime_closure_step_review={"enabled": True, **review} if review else None,
        )
        if planner is not None:
            workflow.closure_planner = RuntimeClosurePlanner(planner)
        if validator is not None:
            workflow.closure_validator = RuntimeClosureValidator(validator)
        return workflow

    def _closure_contract_exists(self, run_id: str) -> bool:
        return any((self.store_run_dir(run_id) / "artifacts").glob("runtime_closure_contract_v*.json"))

    def test_runtime_closure_off_keeps_qa_path_then_enforce_recovers_qa_gate_block(self) -> None:
        run_id = self._create_closure_run()

        blocked = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(blocked["last_error"]["error_code"], "qa_requires_runtime_closure", blocked)
        self.assertNotIn("runtime_closure", PhaseOneWorkflow(self.settings).store.read_state(run_id))
        self.assertNotIn("continue_run", blocked["allowed_next_actions"])

        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        waiting = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(waiting["status"], "waiting_runtime_closure_review", waiting)

    def test_runtime_closure_enforce_pins_run_and_reaches_owner_checkpoint(self) -> None:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()

        waiting = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)

        self.assertEqual(waiting["status"], "waiting_runtime_closure_review", waiting)
        self.assertEqual(waiting["current_phase"], "runtime_closure")
        self.assertEqual(waiting["allowed_next_actions"], ["approve_checkpoint", "get_status", "cancel_run"])
        checkpoint = waiting["pending_checkpoint"]
        self.assertEqual(checkpoint["type"], "runtime_closure_review")
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        pin = state["runtime_closure"]
        self.assertEqual(
            {key: pin[key] for key in ("mode", "contract_version", "verification_policy_version", "planning_attempt", "status")},
            {
                "mode": "enforce",
                "contract_version": "runtime_closure_contract_v1",
                "verification_policy_version": "runtime_closure_stage_policy_v1",
                "planning_attempt": 1,
                "status": "waiting_review",
            },
        )
        for key in (
            "runtime_closure_context",
            "runtime_closure_contract",
            "runtime_closure_contract_markdown",
            "runtime_closure_contract_review",
        ):
            self.assertTrue((self.repo_root / waiting["latest_artifacts"][key]).is_file(), key)
        contract = json.loads((self.repo_root / waiting["latest_artifacts"]["runtime_closure_contract"]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(item["name"] for item in contract["runtime_inputs"]), ["FEED_BASE_URL", "recordFeed"])
        self.assertIn("apps/web/src/closure-provider-1.ts", contract["new_write_targets"])
        markdown = (self.repo_root / waiting["latest_artifacts"]["runtime_closure_contract_markdown"]).read_text(encoding="utf-8")
        self.assertIn("New write targets outside the frozen Plan", markdown)
        # The owner approves literal probe request values at this checkpoint, so the Markdown shows them.
        self.assertIn(", headers `", markdown)
        self.assertIn(", json_body `", markdown)
        self.assertEqual(waiting["quality_reports"]["runtime_closure_contract"]["runnable_status"], "fixture_bound")

        with self.assertRaises(HarnessError) as wrong:
            CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id, checkpoint_id="checkpoint_other")
        self.assertEqual(wrong.exception.error_code, "checkpoint_not_current")

        ready = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(
            run_id=run_id, checkpoint_id=checkpoint["checkpoint_id"]
        )
        self.assertEqual(ready["status"], "runtime_closure_ready", ready)
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["runtime_closure"]["status"], "approved")
        self.assertEqual(state["runtime_closure"]["approved_contract"], checkpoint["contract_path"])

    def _approved_closure_run(self) -> str:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()
        waiting = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        CheckpointApprovalWorkflow(self.settings).approve_checkpoint(
            run_id=run_id, checkpoint_id=waiting["pending_checkpoint"]["checkpoint_id"]
        )
        return run_id

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_runtime_closure_act_runs_developer_in_its_own_session(self) -> None:
        run_id = self._approved_closure_run()
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        frozen_packet_ref = state["latest_artifacts"]["developer_work_packet"]
        frozen_packet_path = self.repo_root / frozen_packet_ref
        frozen_packet_hash = self._sha256(frozen_packet_path)
        act_manifest = self.store_run_dir(run_id) / "developer_execution" / "act_v1" / "manifest.json"
        act_manifest_hash = self._sha256(act_manifest) if act_manifest.exists() else ""
        worktree = Path(json.loads((self.repo_root / state["latest_artifacts"]["run_workspace"]).read_text(encoding="utf-8"))["worktree_path"])

        written = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)

        self.assertEqual(written["status"], "runtime_closure_written", written)
        self.assertEqual(written["current_phase"], "runtime_closure")
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["developer_execution"]["mode"], "runtime_closure")
        self.assertEqual(state["developer_execution"]["status"], "completed")
        self.assertEqual(state["runtime_closure"]["status"], "written")
        # Frozen inputs stay byte-identical; the closure has its own packet and session.
        self.assertEqual(state["latest_artifacts"]["developer_work_packet"], frozen_packet_ref)
        self.assertEqual(self._sha256(frozen_packet_path), frozen_packet_hash)
        if act_manifest_hash:
            self.assertEqual(self._sha256(act_manifest), act_manifest_hash)
        session_dir = self.store_run_dir(run_id) / "developer_execution" / "runtime_closure_v1"
        self.assertTrue((session_dir / "manifest.json").is_file())
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "runtime_closure")
        packet = json.loads((self.repo_root / state["latest_artifacts"]["runtime_closure_work_packet"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["mode"], "runtime_closure")
        units = {unit["component_id"]: unit for unit in packet["execution_units"]}
        self.assertEqual(sorted(units), ["e2e", "web"])
        web_context, e2e_context = units["web"]["closure_context"], units["e2e"]["closure_context"]
        self.assertEqual([item["name"] for item in web_context["resolves"]], ["recordFeed"])
        self.assertEqual([item["name"] for item in e2e_context["resolves"]], ["FEED_BASE_URL"])
        self.assertEqual(web_context["stand_ins"], [])
        self.assertEqual(e2e_context["stand_ins"][0]["module"], "tests/e2e/support/closure-stand-in-2.ts")
        self.assertEqual(e2e_context["env_providers"], [{"env_key": "FEED_BASE_URL", "read_by": "apps/api/src/feed-client.ts", "default": "http://127.0.0.1:9100"}])
        self.assertTrue(set(web_context["frozen_baseline_paths"]) and "apps/web/src/closure-provider-1.ts" not in web_context["frozen_baseline_paths"])
        # Frozen-Plan worktree changes outside the closure scope are hash-bound baselines.
        frozen_dirty = {item["path"] for item in manifest["expected_dirty_files"]}
        self.assertTrue(frozen_dirty)
        self.assertFalse(frozen_dirty & set(packet["output_contract"]["allowed_paths"]))
        self.assertTrue((worktree / "apps/web/src/closure-provider-1.ts").is_file())
        self.assertTrue((worktree / "tests/e2e/support/closure-stand-in-2.ts").is_file())
        # Runtime config receipt is published for the closure packet (Plan env + closure env).
        receipt = json.loads((self.repo_root / state["latest_artifacts"]["runtime_config_execution"]).read_text(encoding="utf-8"))
        self.assertIn(f"_v{state['runtime_closure']['artifact_version']}.json", state["latest_artifacts"]["runtime_config_execution"])
        self.assertIn("FEED_BASE_URL", (worktree / ".env.example").read_text(encoding="utf-8"))
        self.assertEqual(receipt["run_id"], run_id)
        self.assertTrue(state["runtime_closure"]["frozen_packet_sha256"])
        self.assertEqual(state["runtime_closure"]["repair_iterations"], 0)

    def test_runtime_closure_prompt_scope_write_scope_and_frozen_baselines(self) -> None:
        from sdlc_harness.agents.developer.execution import (
            DeveloperExecutionError,
            _action_prompt,
            _safe_write_target,
            _unit_mandatory_obligations,
        )

        run_id = self._approved_closure_run()
        written = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(written["status"], "runtime_closure_written", written)
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        worktree = Path(json.loads((self.repo_root / state["latest_artifacts"]["run_workspace"]).read_text(encoding="utf-8"))["worktree_path"])
        session_dir = self.store_run_dir(run_id) / "developer_execution" / "runtime_closure_v1"
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        items = {item["component_id"]: item for item in manifest["items"]}

        # Each unit prompt carries its own closure context and nothing of the other unit's.
        web_prompt = _action_prompt(
            item=items["web"],
            manifest=manifest,
            history=[],
            target_path=worktree,
            max_write_chars=18000,
            force_incremental=False,
            action_index=1,
            max_actions=24,
            mutated=False,
            context_window_tokens=128000,
        )
        self.assertIn('"runtime_closure"', web_prompt)
        self.assertIn("recordFeed", web_prompt)
        self.assertIn("apps/web/src/closure-provider-1.ts#closureProvider1", web_prompt)
        self.assertNotIn("closure-stand-in-2.ts", web_prompt)
        self.assertNotIn("startClosureStandIn2", web_prompt)
        # Non-closure Developer items keep their exact obligations: no closure key is added.
        act_manifest = self.store_run_dir(run_id) / "developer_execution" / "act_v1" / "manifest.json"
        for item in json.loads(act_manifest.read_text(encoding="utf-8"))["items"]:
            self.assertNotIn("closure_context", item)
            self.assertNotIn("runtime_closure", _unit_mandatory_obligations(item))

        # Writes stay inside the unit's approved closure paths.
        frozen_plan_path = next(path for path in items["web"]["closure_context"]["frozen_baseline_paths"])
        for path in (frozen_plan_path, "package.json", "apps/web/src/unlisted.ts"):
            with self.assertRaises(HarnessError) as outside:
                _safe_write_target(worktree, path, items["web"]["paths"], run_id=run_id)
            self.assertEqual(outside.exception.error_code, "developer_act_out_of_scope")

        # A frozen-Plan file outside the closure scope is a hash-bound baseline: editing it is drift.
        baseline = next(item["path"] for item in manifest["expected_dirty_files"] if item["path"] != ".env.example")
        (worktree / baseline).write_text((worktree / baseline).read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8")
        packet = json.loads((self.repo_root / state["latest_artifacts"]["runtime_closure_work_packet"]).read_text(encoding="utf-8"))
        with self.assertRaises(DeveloperExecutionError) as drift:
            PhaseFourWorkflow(self.settings).developer_tool_loop.execute(
                run_id=run_id,
                work_packet=packet,
                target_path=worktree,
                session_iteration=1,
                expected_dirty_paths=state["runtime_closure"]["expected_dirty_paths"],
                session_scope="runtime_closure",
            )
        self.assertEqual(drift.exception.error_code, "developer_workspace_drift")

    # ---------------------------------------------------------------- L5.3.6 post-execution

    def _written_closure_run(self, *, outcomes=None, validator=None, **settings) -> tuple[str, ClosureVerificationRunnerStub]:
        if settings:
            self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce", **settings)
        run_id = self._approved_closure_run() if not settings else self._approved_closure_run_with_current_settings()
        written = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(written["status"], "runtime_closure_written", written)
        return run_id, ClosureVerificationRunnerStub(outcomes)

    def _approved_closure_run_with_current_settings(self) -> str:
        run_id = self._create_closure_run()
        waiting = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        CheckpointApprovalWorkflow(self.settings).approve_checkpoint(
            run_id=run_id, checkpoint_id=waiting["pending_checkpoint"]["checkpoint_id"]
        )
        return run_id

    def _closure_step(self, run_id: str, runner: ClosureVerificationRunnerStub, *, validator=None) -> dict[str, object]:
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = runner
        if validator is not None:
            workflow.closure_validator = RuntimeClosureValidator(validator)
        return workflow.continue_run(run_id=run_id)

    def _closure_worktree(self, run_id: str) -> Path:
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        return Path(json.loads((self.repo_root / state["latest_artifacts"]["run_workspace"]).read_text(encoding="utf-8"))["worktree_path"])

    def test_runtime_closure_fake_run_goes_from_written_to_runtime_closed_then_qa(self) -> None:
        run_id, runner = self._written_closure_run()
        validator = ScriptedClosureLlm(default=fake_runtime_closure_review)

        verified = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(verified["status"], "runtime_closure_verified", verified)
        self.assertEqual(validator.calls, 0)  # no Validator while the mechanical steps run
        call = runner.closure_calls[0]
        self.assertEqual(call["stage_ids"][-2:], ["runtime_probe", "cleanup"])
        self.assertEqual(len(call["runtime_probe"].probe_ids), 1)
        self.assertEqual(call["runtime_probe"].stand_in_env_keys, ("FEED_BASE_URL",))
        spec = (self._closure_worktree(run_id) / "tests/e2e/runtime-closure.probe.spec.ts").read_text(encoding="utf-8")
        self.assertIn('test("RP-1"', spec)
        # The owner accepted this contract-review warning at the checkpoint; the code review must see it.
        review_file = self.repo_root / verified["latest_artifacts"]["runtime_closure_contract_review"]
        accepted = {"severity": "warning", "category": "probe_evidence", "issue": "accepted at checkpoint"}
        review_file.write_text(
            json.dumps({**json.loads(review_file.read_text(encoding="utf-8")), "warnings": [accepted]}), encoding="utf-8"
        )

        closed = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(closed["status"], "runtime_closed", closed)
        self.assertEqual(validator.calls, 1)
        self.assertIn('"prompt_kind": "Runtime Closure Code Review v1"', validator.prompts[0])
        code_prompt = json.loads(validator.prompts[0])
        self.assertEqual(code_prompt["accepted_contract_warnings"], [accepted])
        self.assertTrue(code_prompt["execution_facts"])
        self.assertIn("Never raise one of them", "\n".join(code_prompt["rules"]))
        report = closed["quality_reports"]["runtime_closure"]
        self.assertEqual((report["runnable_status"], report["blocked_scenario_ids"]), ("fixture_bound", []))
        markdown = (self.repo_root / closed["latest_artifacts"]["runtime_closure_report"]).read_text(encoding="utf-8")
        self.assertIn("## Probes by scenario", markdown)
        self.assertIn("| RP-1 |", markdown)
        for key in ("runtime_closure_verification", "runtime_closure_regression", "runtime_closure_code_review"):
            self.assertTrue((self.repo_root / closed["latest_artifacts"][key]).is_file(), key)
        regression = json.loads((self.repo_root / closed["latest_artifacts"]["runtime_closure_regression"]).read_text(encoding="utf-8"))
        self.assertTrue(regression["ok"])
        publication = PhaseFourWorkflow(self.settings)._runtime_closure_publication_paths(
            state=PhaseOneWorkflow(self.settings).store.read_state(run_id), run_id=run_id
        )
        self.assertIn("apps/web/src/closure-provider-1.ts", publication)
        self.assertIn("tests/e2e/runtime-closure.probe.spec.ts", publication)

        qa = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(qa["status"], "qa_tested", qa)

    def test_runtime_closure_environment_failure_blocks_without_counting_then_retries(self) -> None:
        run_id, runner = self._written_closure_run(outcomes=["env", "passed"])

        blocked = self._closure_step(run_id, runner)
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_environment_failed")
        self.assertIn("continue_run", blocked["allowed_next_actions"])
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["runtime_closure"]["repair_iterations"], 0)
        self.assertNotIn("runtime_closure_repair_feedback", state["latest_artifacts"])

        verified = self._closure_step(run_id, runner)
        self.assertEqual(verified["status"], "runtime_closure_verified", verified)

    def test_runtime_closure_policy_failure_blocks_as_harness_fault(self) -> None:
        run_id, runner = self._written_closure_run(outcomes=["policy"])
        blocked = self._closure_step(run_id, runner)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_verification_policy_failed", blocked)
        self.assertEqual(blocked["allowed_next_actions"], ["get_status", "cancel_run"])

    def test_runtime_closure_probe_failure_repairs_routed_units_and_reruns_from_g14(self) -> None:
        run_id, runner = self._written_closure_run(outcomes=["probe_fail", "passed"])
        validator = ScriptedClosureLlm(default=fake_runtime_closure_review)

        needed = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(needed["status"], "runtime_closure_repair_needed", needed)
        self.assertEqual(validator.calls, 0)
        feedback = json.loads((self.repo_root / needed["latest_artifacts"]["runtime_closure_repair_feedback"]).read_text(encoding="utf-8"))
        self.assertEqual(feedback["feedback_source"], "runtime_closure_verification")
        failure = feedback["probe_failures"][0]
        for field in ("probe_id", "scenario_ids", "service", "method", "path", "expected_status", "actual_status", "expected_min_items", "actual_items", "response_body_excerpt", "proves_input_ids"):
            self.assertIn(field, failure)
        finding = feedback["repair_objective"]["contract_findings"][0]
        self.assertEqual(finding["routing_rule"], 1)
        contract = json.loads((self.repo_root / needed["latest_artifacts"]["runtime_closure_contract"]).read_text(encoding="utf-8"))
        resolving = sorted(
            unit["unit_id"] for unit in contract["closure_units"] if set(unit["resolves_input_ids"]) & set(failure["proves_input_ids"])
        )
        self.assertEqual(finding["unit_ids"], resolving)

        written = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(written["status"], "runtime_closure_written", written)
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["runtime_closure"]["repair_iterations"], 1)
        self.assertTrue((self.store_run_dir(run_id) / "developer_execution" / "runtime_closure_repair_v1").is_dir())

        verified = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(verified["status"], "runtime_closure_verified", verified)
        self.assertEqual(len(runner.closure_calls), 2)
        self.assertEqual(validator.calls, 0)

    def test_runtime_closure_shared_repair_counter_hits_the_limit(self) -> None:
        run_id, runner = self._written_closure_run(outcomes=["probe_fail", "probe_fail"], SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")

        self.assertEqual(self._closure_step(run_id, runner)["status"], "runtime_closure_repair_needed")
        self.assertEqual(self._closure_step(run_id, runner)["status"], "runtime_closure_written")
        blocked = self._closure_step(run_id, runner)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_repair_limit_exceeded", blocked)

    def test_runtime_closure_code_review_blocker_repairs_and_frozen_conflict_blocks(self) -> None:
        run_id, runner = self._written_closure_run()
        validator = ScriptedClosureLlm([closure_code_blocker, closure_code_frozen_conflict])

        self.assertEqual(self._closure_step(run_id, runner, validator=validator)["status"], "runtime_closure_verified")
        needed = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(needed["status"], "runtime_closure_repair_needed", needed)
        feedback = json.loads((self.repo_root / needed["latest_artifacts"]["runtime_closure_repair_feedback"]).read_text(encoding="utf-8"))
        self.assertEqual(feedback["feedback_source"], "runtime_closure_code_review")
        self.assertEqual(feedback["repair_objective"]["contract_findings"][0]["routing_rule"], 3)

        self.assertEqual(self._closure_step(run_id, runner, validator=validator)["status"], "runtime_closure_written")
        self.assertEqual(self._closure_step(run_id, runner, validator=validator)["status"], "runtime_closure_verified")
        blocked = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_frozen_contract_conflict", blocked)
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["runtime_closure"]["repair_iterations"], 1)  # the conflict is not routed to repair

    def test_runtime_closure_g14_missing_provider_repairs_and_stray_file_blocks(self) -> None:
        run_id, runner = self._written_closure_run()
        worktree = self._closure_worktree(run_id)

        (worktree / "apps/web/src/closure-provider-1.ts").write_text("export {};\n", encoding="utf-8")
        needed = self._closure_step(run_id, runner)
        self.assertEqual(needed["status"], "runtime_closure_repair_needed", needed)
        self.assertEqual(runner.closure_calls, [])  # G14 runs before any verification stage
        feedback = json.loads((self.repo_root / needed["latest_artifacts"]["runtime_closure_repair_feedback"]).read_text(encoding="utf-8"))
        self.assertEqual(feedback["g14_issues"][0]["check"], "provider_missing")

        self.assertEqual(self._closure_step(run_id, runner)["status"], "runtime_closure_written")
        (worktree / "apps/api/src/stray-closure-file.ts").write_text("export const stray = 1;\n", encoding="utf-8")
        blocked = self._closure_step(run_id, runner)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_scope_conflict", blocked)

    def test_runtime_closure_code_review_contract_breaks_twice_then_retries_review(self) -> None:
        run_id, runner = self._written_closure_run()
        wrong_ids = json.dumps({"summary": "x", "reviewed_input_ids": ["RI-000000000000"], "findings": []})
        validator = ScriptedClosureLlm([wrong_ids, wrong_ids], default=fake_runtime_closure_review)

        self.assertEqual(self._closure_step(run_id, runner, validator=validator)["status"], "runtime_closure_verified")
        blocked = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_inconclusive", blocked)
        self.assertEqual(validator.calls, 2)  # one exact-error retry, then fail closed
        self.assertIn("continue_run", blocked["allowed_next_actions"])
        self.assertNotIn("runtime_closure_repair_feedback", blocked["latest_artifacts"])

        closed = self._closure_step(run_id, runner, validator=validator)
        self.assertEqual(closed["status"], "runtime_closed", closed)
        self.assertEqual(len(runner.closure_calls), 1)  # the retry resumed review, not verification

    def test_runtime_closure_code_review_step_review_resumes_the_review(self) -> None:
        run_id, runner = self._written_closure_run()
        self.assertEqual(self._closure_step(run_id, runner)["status"], "runtime_closure_verified")
        validator = ScriptedClosureLlm(default=fake_runtime_closure_review)

        def workflow(**review):
            flow = PhaseFourWorkflow(self.settings, runtime_closure_step_review={"enabled": True, "review_input": False, "review_output": True, **review})
            flow.verification_runner = runner
            flow.closure_validator = RuntimeClosureValidator(validator)
            # Regression has its own (Code Validator) review; keep it off to isolate the closure review pause.
            flow.code_validation_step_review = {**flow.code_validation_step_review, "enabled": False}
            return flow

        paused = workflow().continue_run(run_id=run_id)
        self.assertEqual(paused["status"], "runtime_closure_step_review_required", paused)
        call_id = paused["pending_step_review"]["call_id"]
        self.assertTrue(call_id.startswith("runtime-closure-code-review-"))
        closed = workflow(call_id=call_id, decision="approve").continue_runtime_closure_step_review(run_id=run_id)
        self.assertEqual(closed["status"], "runtime_closed", closed)
        self.assertEqual(validator.calls, 1)  # the approved output was replayed

    def test_runtime_closure_new_file_recorded_by_the_session_is_an_approved_scope_extension(self) -> None:
        run_id, runner = self._written_closure_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(run_id)
        packet = json.loads((self.repo_root / state["latest_artifacts"]["runtime_closure_work_packet"]).read_text(encoding="utf-8"))
        web_unit = next(unit for unit in packet["execution_units"] if unit["component_id"] == "web")
        self.assertEqual(web_unit["new_file_policy"]["roots"], ["apps/web/src"])
        self.assertEqual(web_unit["closure_context"]["new_file_roots"], ["apps/web/src"])

        # Simulate a Developer write that created a new file under its component root (D11 b).
        journal_ref = state["latest_artifacts"]["runtime_closure_execution_journal"]
        journal_file = self.repo_root / journal_ref
        journal = json.loads(journal_file.read_text(encoding="utf-8"))
        journal["scope_extensions"] = [{"path": "apps/web/src/session-helper.ts", "item_id": web_unit["unit_id"], "rule": "D11b"}]
        journal_file.write_text(json.dumps(journal), encoding="utf-8")
        (self._closure_worktree(run_id) / "apps/web/src/session-helper.ts").write_text("export const helper = 1;\n", encoding="utf-8")
        workflow = PhaseFourWorkflow(self.settings)
        state["runtime_closure"] = workflow._with_session_scope_extensions(state["runtime_closure"], journal_ref=journal_ref, run_id=run_id)
        store.write_state(state)
        self.assertEqual(
            [(item["path"], item["rule"]) for item in state["runtime_closure"]["scope_extensions"]],
            [("apps/web/src/session-helper.ts", "D11b")],
        )

        verified = self._closure_step(run_id, runner)  # G14 accepts the recorded extension
        self.assertEqual(verified["status"], "runtime_closure_verified", verified)
        publication = workflow._runtime_closure_publication_paths(state={**store.read_state(run_id), "runtime_closure": {**store.read_state(run_id)["runtime_closure"], "status": "runtime_closed"}}, run_id=run_id)
        self.assertIn("apps/web/src/session-helper.ts", publication)

    def test_runtime_closure_webserver_start_failure_is_a_composition_defect(self) -> None:
        run_id, runner = self._written_closure_run(outcomes=["webserver"])
        needed = self._closure_step(run_id, runner)
        self.assertEqual(needed["status"], "runtime_closure_repair_needed", needed)
        feedback = json.loads((self.repo_root / needed["latest_artifacts"]["runtime_closure_repair_feedback"]).read_text(encoding="utf-8"))
        finding = feedback["repair_objective"]["contract_findings"][0]
        self.assertEqual(finding["routing_rule"], 5)
        self.assertEqual(feedback["verification"]["stage_results"][-2]["stage_id"], "runtime_probe")

    def test_runtime_closure_act_transport_failure_resumes_the_closure_session(self) -> None:
        run_id = self._approved_closure_run()
        workflow = PhaseFourWorkflow(self.settings)
        original = workflow.developer_tool_loop.execute
        calls: list[dict[str, object]] = []

        def failing_execute(**kwargs):
            calls.append(kwargs)
            raise HarnessError("llm_transport_failed", "LLM request failed: connection reset")

        workflow.developer_tool_loop.execute = failing_execute
        paused = workflow.continue_run(run_id=run_id)
        self.assertEqual(paused["status"], "implementation_paused", paused)
        self.assertEqual(paused["current_phase"], "runtime_closure")
        self.assertEqual(calls[0]["session_scope"], "runtime_closure")
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["developer_execution"]["mode"], "runtime_closure")
        packet_ref = state["runtime_closure"]["work_packet"]
        del original

        resumed = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        self.assertEqual(resumed["status"], "runtime_closure_written", resumed)
        state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
        self.assertEqual(state["runtime_closure"]["work_packet"], packet_ref)  # the packet is not rebuilt
        self.assertFalse((self.store_run_dir(run_id) / "developer_execution" / "runtime_closure_v2").exists())

    def test_runtime_closure_inconclusive_outputs_fail_closed_and_retry_on_a_new_attempt(self) -> None:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()
        invalid_contract = json.dumps({"resolutions": "not a list", "closure_units": [], "runtime_probes": []})
        planner = ScriptedClosureLlm(
            [
                "",
                '{"resolutions": [',
                ('{"resolutions": []}', "length"),
                HarnessError("llm_transport_failed", "connection reset"),
                invalid_contract,
                invalid_contract,
            ],
            default=fake_runtime_closure_plan,
        )
        for expected_attempt in range(1, 6):
            response = self._closure_workflow(planner=planner).continue_run(run_id=run_id)
            self.assertEqual(response["status"], "blocked", response)
            self.assertEqual(response["last_error"]["error_code"], "runtime_closure_inconclusive", response)
            self.assertIsNone(response["pending_checkpoint"])
            self.assertIn("continue_run", response["allowed_next_actions"])
            self.assertFalse(self._closure_contract_exists(run_id))
            state = PhaseOneWorkflow(self.settings).store.read_state(run_id)
            self.assertEqual(state["runtime_closure"]["planning_attempt"], expected_attempt)
        self.assertEqual(planner.calls, 6)  # the contract-invalid output got exactly one retry

        validator = ScriptedClosureLlm([""], default=fake_runtime_closure_review)
        blocked = self._closure_workflow(planner=planner, validator=validator).continue_run(run_id=run_id)
        self.assertEqual(blocked["last_error"]["error_code"], "runtime_closure_inconclusive", blocked)
        self.assertFalse(self._closure_contract_exists(run_id))

        waiting = self._closure_workflow(planner=planner, validator=validator).continue_run(run_id=run_id)
        self.assertEqual(waiting["status"], "waiting_runtime_closure_review", waiting)
        self.assertTrue(self._closure_contract_exists(run_id))

    def test_runtime_closure_validator_blocker_gets_one_planner_patch_round(self) -> None:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()
        planner = ScriptedClosureLlm(default=fake_runtime_closure_plan)
        validator = ScriptedClosureLlm([closure_blocker_review], default=fake_runtime_closure_review)

        waiting = self._closure_workflow(planner=planner, validator=validator).continue_run(run_id=run_id)

        self.assertEqual(waiting["status"], "waiting_runtime_closure_review", waiting)
        self.assertEqual((planner.calls, validator.calls), (2, 2))
        self.assertNotIn('"previous_round"', planner.prompts[0])
        self.assertIn('"previous_round"', planner.prompts[1])
        self.assertIn("The demo provider returns nothing usable.", planner.prompts[1])
        self.assertIn("previous_blocking_findings", validator.prompts[1])
        review = json.loads((self.repo_root / waiting["latest_artifacts"]["runtime_closure_contract_review"]).read_text(encoding="utf-8"))
        self.assertEqual(len(review["rounds"]), 2)

    def test_runtime_closure_unsupported_config_kind_blocks_before_checkpoint(self) -> None:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()

        def with_compose(prompt: str) -> str:
            output = json.loads(fake_runtime_closure_plan(prompt))
            output["runtime_config_changes"] = [
                {"kind": "compose_service", "unit_key": "web-closure", "service": "feed", "image": "feed:1.0.0", "healthcheck": "true", "purpose": "feed"}
            ]
            return json.dumps(output)

        validator = ScriptedClosureLlm(default=fake_runtime_closure_review)
        response = self._closure_workflow(planner=ScriptedClosureLlm(default=with_compose), validator=validator).continue_run(run_id=run_id)

        self.assertEqual(response["status"], "blocked", response)
        self.assertEqual(response["last_error"]["error_code"], "runtime_closure_config_unsupported")
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(validator.calls, 0)
        self.assertFalse(self._closure_contract_exists(run_id))

    def test_runtime_closure_step_review_pauses_each_call_and_replays_approved_ones(self) -> None:
        self.settings = self._settings(SDLC_RUNTIME_CLOSURE_MODE="enforce")
        run_id = self._create_closure_run()
        planner = ScriptedClosureLlm(default=fake_runtime_closure_plan)
        validator = ScriptedClosureLlm(default=fake_runtime_closure_review)
        review = {"review_input": True, "review_output": True}

        paused = self._closure_workflow(planner=planner, validator=validator, **review).continue_run(run_id=run_id)
        expected = [
            ("runtime-closure-planner-0001-a01-input", "input", 0, 0),
            ("runtime-closure-planner-0001-a01", "output", 1, 0),
            ("runtime-closure-validator-0001-a01-input", "input", 1, 0),
            ("runtime-closure-validator-0001-a01", "output", 1, 1),
        ]
        for call_id, stage, planner_calls, validator_calls in expected:
            self.assertEqual(paused["status"], "runtime_closure_step_review_required", paused)
            self.assertEqual((paused["pending_step_review"]["call_id"], paused["pending_step_review"]["review_stage"]), (call_id, stage))
            self.assertEqual((planner.calls, validator.calls), (planner_calls, validator_calls))
            self.assertFalse(self._closure_contract_exists(run_id))
            paused = self._closure_workflow(
                planner=planner, validator=validator, call_id=call_id, decision="approve"
            ).continue_runtime_closure_step_review(run_id=run_id)

        self.assertEqual(paused["status"], "waiting_runtime_closure_review", paused)
        self.assertEqual((planner.calls, validator.calls), (1, 1))
        exchange_dir = self.store_run_dir(run_id) / "runtime_closure_execution" / "step_review" / "attempt_1"
        self.assertTrue((exchange_dir / "planner" / "exchange_runtime-closure-planner-0001-a01.json").is_file())
        self.assertTrue((exchange_dir / "validator" / "exchange_runtime-closure-validator-0001-a01.json").is_file())

    def _retired_java_code_trace_normalizes_boolean_and_notification_sequence_mappings(self) -> None:
        source_dir = self.target_repo / "src" / "main" / "java" / "com" / "sdlc" / "etour" / "heritage"
        test_dir = self.target_repo / "src" / "test" / "java" / "com" / "sdlc" / "etour" / "heritage"
        controller_path = "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageController.java"
        connection_path = "src/main/java/com/sdlc/etour/heritage/ETOURConnectionService.java"
        notification_path = "src/main/java/com/sdlc/etour/heritage/NotificationService.java"
        test_path = "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"
        (source_dir / "DeleteCulturalHeritageController.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class DeleteCulturalHeritageController { void confirmDeletion() {} }\n",
            encoding="utf-8",
        )
        (source_dir / "ETOURConnectionService.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class ETOURConnectionService {}\n",
            encoding="utf-8",
        )
        (source_dir / "NotificationService.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class NotificationService { void notifyError(String message) {} }\n",
            encoding="utf-8",
        )
        (test_dir / "DeleteCulturalHeritageFlowTest.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class DeleteCulturalHeritageFlowTest {}\n",
            encoding="utf-8",
        )
        work_packet = {
            "run_id": "test-run",
            "issue_key": "SDLC-1",
            "design_mappings_by_file": {
                controller_path: [
                    {
                        "design_element_id": "sequence:sequence_diagram_1:16",
                        "design_element_type": "sequence_message",
                        "design_element_name": "EC->DC: true",
                        "planned_code_target": f"{controller_path}#true",
                    }
                ],
                connection_path: [
                    {
                        "design_element_id": "sequence:sequence_diagram_1:22",
                        "design_element_type": "sequence_message",
                        "design_element_name": 'DC->NS: notifyError("Connection to ETOUR server interrupted")',
                        "planned_code_target": f"{connection_path}#notifyError",
                    }
                ],
            },
            "proposed_files": [{"path": test_path}],
        }
        llm = CapturingCodeTraceLlm()

        result = CodeTraceValidator(llm).validate(target_path=self.target_repo, work_packet=work_packet)

        self.assertTrue(result.ok)
        payload = json.loads(llm.prompt.split("\n\n", 1)[1])
        boolean_mapping = next(
            item for item in payload["static_trace_mappings"] if item["design_element_id"] == "sequence:sequence_diagram_1:16"
        )
        notification_mapping = next(
            item for item in payload["static_trace_mappings"] if item["design_element_id"] == "sequence:sequence_diagram_1:22"
        )
        self.assertEqual(boolean_mapping["trace_policy"], "boolean_response")
        self.assertFalse(boolean_mapping["requires_direct_symbol"])
        self.assertEqual(boolean_mapping["symbol"], "")
        self.assertEqual(notification_mapping["trace_policy"], "semantic_side_effect")
        self.assertEqual(notification_mapping["source_file"], notification_path)
        self.assertEqual(notification_mapping["symbol"], "notifyError")
        self.assertTrue(notification_mapping["symbol_found"])

    def test_cli_continue_run_rejects_legacy_code_trace(self) -> None:
        tested = self._create_phase4_tested_run()
        self._mark_run_as_historical_java(str(tested["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(tested["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], tested["run_id"])

    def test_cli_continue_run_rejects_legacy_verification(self) -> None:
        written = self._create_phase4_written_run()
        self._mark_run_as_historical_java(str(written["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(written["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], written["run_id"])

    def test_continue_run_marks_repair_needed_when_code_trace_missing_test_file(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_repair"]["needed"])
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(trace["ok"])
        self.assertTrue(any(item["type"] == "missing_test_file" for item in trace["missing_links"]))
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(feedback["version"], "implementation_repair_feedback_v1")
        self.assertEqual(feedback["status"], "implementation_repair_needed")
        self.assertEqual(feedback["repair_iteration"], 0)
        self.assertEqual(feedback["repair_scope"], "code_or_test")
        self.assertEqual(feedback["trace_artifact"], response["latest_artifacts"]["requirement_design_code_trace"])
        self.assertTrue(any(item["type"] == "missing_test_file" for item in feedback["missing_links"]))

    def test_continue_run_marks_repair_needed_when_llm_code_trace_finds_semantic_gap(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        state = workflow.store.read_state(str(tested["run_id"]))
        state["developer_execution"] = {
            "executor": "tool_loop",
            "mode": "implementation_repair",
            "status": "blocked",
            "session_id": "implementation_repair-v5",
            "repair_iteration": 1,
            "session_iteration": 5,
            "artifact_version": 9,
        }
        workflow.store.write_state(state)

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["developer_execution"]["status"], "superseded")
        self.assertEqual(
            response["developer_execution"]["superseded_reason"],
            "new_validator_repair_objective",
        )
        self.assertFalse(
            workflow._tool_loop_session_active(
                response,
                mode="implementation_repair",
            )
        )
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(trace["ok"])
        self.assertTrue(trace["llm_required"])
        self.assertFalse(trace["llm_review"]["ok"])
        self.assertTrue(any(item["type"] == "llm_semantic_gap" for item in trace["missing_links"]))
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            feedback["llm_review"]["summary"],
            "Validator reported 1 actionable blocking link(s). "
            "Approved behavior is not covered by code/test evidence.",
        )
        self.assertTrue(feedback["developer_constraints"]["allowed_paths"])

    def test_continue_run_from_repair_needed_repairs_code_then_retraces(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced", response)
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["quality_reports"]["implementation_repair"]["needed"], False)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["repair_iterations"], 1)
        self.assertTrue(test_file.exists())
        self.assertIn("developer_work_packet_v2.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("code_change_report_v2.md", response["latest_artifacts"]["code_change_report"])
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn(
            "requirement_design_code_trace_v2.json",
            response["latest_artifacts"]["requirement_design_code_trace"],
        )
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(trace["ok"])
        self.assertEqual(trace["repair_iteration"], 1)
        self.assertEqual(trace["repaired_from"], repair_needed["latest_artifacts"]["implementation_repair_feedback"])
        report = (self.repo_root / str(response["latest_artifacts"]["code_change_report"])).read_text(encoding="utf-8")
        self.assertIn("Developer work packet:", report)
        self.assertIn("developer_work_packet_v2.json", report)

    def test_implementation_repair_uses_tool_loop_and_records_durable_artifacts(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        workflow = PhaseFourWorkflow(
            self._settings()
        )
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "implementation_traced", response)
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["developer_execution"]["executor"], "tool_loop")
        self.assertEqual(response["developer_execution"]["mode"], "implementation_repair")
        self.assertEqual(response["developer_execution"]["status"], "completed")
        self.assertIn("developer_repair_execution_manifest", response["latest_artifacts"])
        self.assertIn("developer_repair_execution_journal", response["latest_artifacts"])
        report_path = self.repo_root / str(response["latest_artifacts"]["code_change_report"])
        self.assertIn("Execution mode: `tool_loop`", report_path.read_text(encoding="utf-8"))

    def _retired_batch_repair_refreshes_work_packet_from_current_target_repo(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        old_packet_path = self.repo_root / str(repair_needed["latest_artifacts"]["developer_work_packet"])
        old_packet = json.loads(old_packet_path.read_text(encoding="utf-8"))
        old_service = next(
            item
            for item in old_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageService.java")
        )
        self.assertTrue(old_service["exists"])
        self.assertNotIn("isDeleted", old_service["content"])

        workflow = PhaseFourWorkflow(self.settings)
        developer = CapturingOutOfScopeRepairDeveloper()
        workflow.developer = developer
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertIsNotNone(developer.work_packet)
        refreshed_packet = developer.work_packet or {}
        self.assertEqual(refreshed_packet["version"], "developer_work_packet_v2")
        refreshed_service = next(
            item
            for item in refreshed_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageService.java")
        )
        self.assertTrue(refreshed_service["exists"])
        self.assertIn("isDeleted", refreshed_service["content"])
        refreshed_test = next(
            item
            for item in refreshed_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageFlow.test.ts")
        )
        self.assertFalse(refreshed_test["exists"])
        self.assertIn("developer_work_packet_v2.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("developer_repair_raw_response_v2.txt", response["latest_artifacts"]["developer_repair_raw_response"])
        raw_path = self.repo_root / str(response["latest_artifacts"]["developer_repair_raw_response"])
        self.assertIn("raw repair response", raw_path.read_text(encoding="utf-8"))
        packet_path = self.repo_root / str(response["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["artifact_ref"], "developer_work_packet_v2")
        self.assertEqual(packet["version"], "developer_work_packet_v2")

    def test_continue_run_from_repair_needed_respects_zero_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="0")
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertNotIn("code_change_report_v2.md", str(response["latest_artifacts"].get("code_change_report", "")))

    def _retired_batch_repair_blocks_out_of_scope_output(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.developer = OutOfScopeRepairDeveloper()

        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertFalse(
            (
                self._workspace_path(response)
                / "src"
                / "main"
                / "java"
                / "com"
                / "sdlc"
                / "etour"
                / "heritage"
                / "UnexpectedRepair.java"
            ).exists()
        )

    def test_continue_run_from_repair_needed_requeues_when_repair_verification_fails(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
            / "apps"
            / "api"
            / "src"
            / "DeleteCulturalHeritageFlow.test.ts"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["failed"])

        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["implementation_repair_total_iterations"], 1)
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("implementation_repair_feedback_v2.json", response["latest_artifacts"]["implementation_repair_feedback"])
        self.assertEqual(response["quality_reports"]["implementation_repair"]["feedback_source"], "verification")
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", report)
        self.assertIn("qa acceptance failed", report)

    def test_continue_run_from_repair_needed_blocks_when_trace_still_fails_at_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["implementation_repair"]["limit_exceeded"])
        self.assertIn("requirement_design_code_trace_v2.json", response["latest_artifacts"]["requirement_design_code_trace"])

    def test_revalidate_blocked_limit_reopens_repair_when_new_limit_allows(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(blocked["implementation_repair_iterations"], 1)

        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="2")
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.revalidate_implementation(run_id=str(blocked["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(
            response["allowed_next_actions"],
            ["continue_run", "revalidate_implementation", "get_status", "cancel_run"],
        )
        self.assertIn("developer_work_packet_v3.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("test_report_v3.md", response["latest_artifacts"]["test_report"])
        self.assertIn(
            "requirement_design_code_trace_v3.json",
            response["latest_artifacts"]["requirement_design_code_trace"],
        )
        self.assertIn(
            "implementation_repair_feedback_v3.json",
            response["latest_artifacts"]["implementation_repair_feedback"],
        )
        self.assertTrue(response["quality_reports"]["implementation_repair"]["revalidated"])
        self.assertFalse(response["quality_reports"]["implementation_repair"].get("limit_exceeded", False))

    def test_environment_failure_does_not_consume_developer_repair_budget(self) -> None:
        written = self._create_phase4_written_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = EnvironmentFailureVerificationRunner()

        response = workflow.continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "verification_environment_failed")
        self.assertEqual(response["implementation_repair_iterations"], 0)
        self.assertNotIn("implementation_repair_feedback", response["latest_artifacts"])
        self.assertIn("revalidate_implementation", response["allowed_next_actions"])

    def test_revalidate_blocked_limit_opens_new_bounded_repair_cycle(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))
        self.assertEqual(blocked["implementation_repair_iterations"], 1)

        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.revalidate_implementation(run_id=str(blocked["run_id"]))

        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertEqual(response["implementation_repair_iterations"], 0)
        self.assertEqual(response["implementation_repair_cycle"], 1)
        self.assertEqual(response["implementation_repair_total_iterations"], 1)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["repair_iterations"], 0)
        self.assertTrue(response["quality_reports"]["implementation_repair"]["repair_cycle_reopened"])
        self.assertEqual(response["quality_reports"]["implementation_repair"]["previous_repair_iterations"], 1)
        self.assertIsNone(response["last_error"])
        self.assertIn("continue_run", response["allowed_next_actions"])

    def test_revalidate_blocked_limit_can_pass_without_more_repair(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="2")
        response = PhaseFourWorkflow(self.settings).revalidate_implementation(run_id=str(blocked["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced", response)
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["needed"], False)
        self.assertTrue(response["quality_reports"]["implementation_repair"]["revalidated"])
        self.assertIn("developer_work_packet_v3.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("requirement_design_code_trace_v3.json", response["latest_artifacts"]["requirement_design_code_trace"])

    def test_revalidate_implementation_rejects_non_revalidatable_blocked_state(self) -> None:
        written = self._create_phase4_written_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(written["run_id"]))
        state.update(
            {
                "status": "blocked",
                "current_phase": "implementation_testing",
                "last_error": HarnessError("workspace_dirty", "workspace changed", run_id=str(written["run_id"])).to_response(),
            }
        )
        store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).revalidate_implementation(run_id=str(written["run_id"]))

        self.assertEqual(ctx.exception.error_code, "implementation_revalidate_not_allowed")

    def test_cli_revalidate_implementation_rejects_legacy_run(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))
        self._mark_run_as_historical_java(str(blocked["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "revalidate-implementation",
                    "--run-id",
                    str(blocked["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--implementation-repair-limit",
                    "2",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], blocked["run_id"])

    def test_continue_run_blocks_mapping_issue_without_developer_repair(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(TraceMappingIssueCodeTraceLlm())

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "validator_evidence_or_mapping_issue")
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertNotIn("implementation_repair_feedback", response["latest_artifacts"])

    def test_continue_run_blocks_when_code_trace_missing_test_report(self) -> None:
        tested = self._create_phase4_tested_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(tested["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("test_report")
        state["latest_artifacts"] = latest
        store.write_state(state)

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "approved_implementation_plan_not_found")

    def test_continue_run_routes_actionable_verification_failure_to_repair(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "fake-vitest.cjs").write_text(
            "process.stderr.write('unit failed\\n');\nprocess.exit(1);\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(
            response["allowed_next_actions"],
            ["continue_run", "revalidate_implementation", "get_status", "cancel_run"],
        )
        self.assertEqual(response["implementation_repair_iterations"], 0)
        self.assertIn("test_report", response["latest_artifacts"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", report)
        self.assertIn("unit failed", report)
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(feedback["feedback_source"], "verification")
        self.assertEqual(feedback["verification_artifact"], response["latest_artifacts"]["test_report"])
        self.assertIn("unit failed", feedback["verification"]["stderr"])

    def _retired_legacy_blocked_test_failure_resume(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Error 'legacy unit failed'\nexit 1\n",
            encoding="utf-8",
        )
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(written["run_id"]))
        latest = dict(state["latest_artifacts"])
        feedback_path = self.repo_root / str(latest.pop("implementation_repair_feedback"))
        feedback_path.unlink()
        quality_reports = dict(state.get("quality_reports") or {})
        quality_reports.pop("verification", None)
        quality_reports.pop("implementation_repair", None)
        state.update(
            {
                "current_phase": "implementation_testing",
                "status": "blocked",
                "latest_artifacts": latest,
                "quality_reports": quality_reports,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(
                    "test_failed",
                    "Test command exited with code 1.",
                    run_id=str(written["run_id"]),
                ).to_response(),
            }
        )
        store.write_state(state)

        status = PhaseOneWorkflow(self.settings).get_status(run_id=str(written["run_id"]))
        self.assertIn("continue_run", status["allowed_next_actions"])

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("implementation_repair_feedback_v2.json", response["latest_artifacts"]["implementation_repair_feedback"])
        self.assertTrue(repair_needed["quality_reports"]["verification"]["repairable"])

    def _retired_java_verification_skip_behavior(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Host \"JDK compiler 'javac' was not found, so compile/runtime tests were skipped.\"\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_skipped")
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])
        self.assertIn("test_report", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `skipped`", report)

    def test_verification_timeout_stays_blocked_and_can_be_revalidated(self) -> None:
        written = self._create_phase4_written_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        original_run = workflow.verification_runner.run

        def timed_out_run(**kwargs: object) -> VerificationResult:
            result = original_run(**kwargs)
            return VerificationResult(
                **{
                    **result.to_dict(),
                    "reason": "Test command timed out after 120s.",
                    "timed_out": True,
                }
            )

        workflow.verification_runner.run = timed_out_run  # type: ignore[method-assign]

        response = workflow.continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_failed")
        self.assertFalse(response["quality_reports"]["verification"]["repairable"])
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])

    def test_verification_failure_blocks_after_shared_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        written = self._create_phase4_written_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(written["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])

    def _retired_verification_runner_adds_sdlc_java_home_to_path(self) -> None:
        fake_jdk = Path(self.temp_dir.name) / "fake-jdk"
        java_bin = fake_jdk / "bin"
        java_bin.mkdir(parents=True)
        javac = java_bin / ("javac.exe" if os.name == "nt" else "javac")
        javac.write_text("", encoding="utf-8")
        javac.chmod(0o755)
        (self.target_repo / "scripts" / "test.ps1").write_text(
            (
                "if (-not (Get-Command javac -ErrorAction SilentlyContinue)) {\n"
                "  Write-Host \"JDK compiler 'javac' was not found, so compile/runtime tests were skipped.\"\n"
                "  exit 0\n"
                "}\n"
                "Write-Host 'javac visible'\n"
            ),
            encoding="utf-8",
        )
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        work_packet = {
            "test_intents": [
                {"command": f'"{powershell}" -ExecutionPolicy Bypass -File scripts/test.ps1'},
            ],
        }

        with patch.dict(os.environ, {"SDLC_JAVA_HOME": str(fake_jdk), "PATH": ""}, clear=False):
            result = VerificationRunner(timeout_seconds=10).run(target_path=self.target_repo, work_packet=work_packet)

        self.assertEqual(result.status, "passed")
        self.assertIn("javac visible", result.stdout)

    def test_verification_runner_rejects_profiled_packet_without_snapshot_policy(self) -> None:
        work_packet = {
            "verification_stage_ids": ["typecheck", "unit"],
            "test_intents": [{"stage_id": "unit"}],
        }

        with (
            patch("sdlc_harness.verification.runner._run_bounded") as subprocess_run,
            self.assertRaises(HarnessError) as caught,
        ):
            VerificationRunner(timeout_seconds=10).run(
                target_path=self.target_repo,
                work_packet=work_packet,
            )

        self.assertEqual(caught.exception.error_code, "verification_stage_policy_invalid")
        subprocess_run.assert_not_called()

    def _retired_verification_runner_executes_required_java_qa_test_class(self) -> None:
        if not shutil.which("javac") or not shutil.which("java"):
            self.skipTest("JDK is required for the QA generated-test execution regression test.")
        qa_path = "src/test/java/com/sdlc/etour/heritage/RequiredQaAcceptanceTest.java"
        qa_file = self.target_repo / qa_path
        qa_file.write_text(
            (
                "package com.sdlc.etour.heritage;\n"
                "public final class RequiredQaAcceptanceTest {\n"
                "  public static void main(String[] args) {\n"
                "    throw new IllegalStateException(\"qa generated failure\");\n"
                "  }\n"
                "}\n"
            ),
            encoding="utf-8",
        )
        (self.target_repo / "scripts" / "test.ps1").write_text(
            (
                "$ErrorActionPreference = \"Stop\"\n"
                "$sources = Get-ChildItem -LiteralPath \"src\" -Filter *.java -Recurse | "
                "ForEach-Object { Resolve-Path -Relative -LiteralPath $_.FullName }\n"
                "New-Item -ItemType Directory -Force -Path build | Out-Null\n"
                "javac -encoding UTF-8 -d build @sources\n"
                "Write-Host 'baseline compile passed'\n"
            ),
            encoding="utf-8",
        )
        work_packet = {"test_intents": [{"command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1"}]}

        result = VerificationRunner(timeout_seconds=30).run(
            target_path=self.target_repo,
            work_packet=work_packet,
            required_test_files=[qa_path],
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("RequiredQaAcceptanceTest", result.reason)
        self.assertIn("qa generated failure", result.stderr)

    def _retired_verification_runner_rejects_java_qa_without_bytecode(self) -> None:
        qa_path = "src/test/java/com/sdlc/etour/heritage/RequiredQaAcceptanceTest.java"
        work_packet = {
            "test_intents": [
                {"command": "powershell -NoProfile -Command \"Write-Host 'placeholder passed'\""},
            ],
        }

        result = VerificationRunner(timeout_seconds=10).run(
            target_path=self.target_repo,
            work_packet=work_packet,
            required_test_files=[qa_path],
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("did not produce Java bytecode", result.reason)
        self.assertEqual(
            result.discovered_test_entrypoints,
            ("com.sdlc.etour.heritage.RequiredQaAcceptanceTest",),
        )

    def test_continue_run_rejects_non_ready_state(self) -> None:
        phase3 = self._create_phase3_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).continue_run(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "run_not_continuable")

    def test_continue_run_blocks_dirty_target_repo(self) -> None:
        ready = self._create_phase4_ready_run()
        (self._workspace_path(ready) / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_continue_run_blocks_missing_developer_work_packet(self) -> None:
        ready = self._create_phase4_ready_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(ready["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("developer_work_packet")
        state["latest_artifacts"] = latest
        store.write_state(state)

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "approved_implementation_plan_not_found")
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def _retired_batch_continue_run_blocks_out_of_scope_developer_output(self) -> None:
        ready = self._create_phase4_ready_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.developer = OutOfScopeDeveloper()

        response = workflow.continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertFalse(
            (
                self._workspace_path(response)
                / "src"
                / "main"
                / "java"
                / "com"
                / "sdlc"
                / "etour"
                / "heritage"
                / "Unexpected.java"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()

