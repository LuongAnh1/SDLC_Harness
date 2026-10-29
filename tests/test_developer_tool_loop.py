"""Kiểm thử tập trung cho vòng action Developer bền vững của Phase 4."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from sdlc_harness.agents.developer.execution import (
    DeveloperExecutionError,
    DeveloperToolLoopExecutor,
    _action_memory,
    _action_prompt,
    _build_manifest,
    _action_policy_error,
    _current_mutated_sources,
    _database_entrypoint_journal_reopen_ids,
    _diagnostic_repeat_count,
    _execution_phase,
    _merge_context_only_manifest,
    _merge_database_entrypoint_scope_manifest,
    _new_journal,
    _new_test_context_policy_error,
    _parse_action,
    _prior_attempt_feedback,
    _prepare_patch_text,
    _reopen_database_entrypoint_items,
    _selected_repair_paths,
    _step_review_start_ordinal,
    _timeout_diagnostic_completion_error,
    _timeout_diagnostic_action_limit,
    _timeout_diagnostic_state,
    _verification_diagnostic_fingerprint,
    _verification_repair_state,
    developer_execution_retry_allowed,
)
from sdlc_harness.agents.developer.repair_context import build_repair_closure
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.profiles import VerificationStagePolicy
from sdlc_harness.storage.runtime import RuntimeStore


class ScriptedLlm:
    def __init__(self, responses: list[LlmCompletion]):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.systems.append(str(kwargs.get("system") or ""))
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.responses:
            raise AssertionError("Scripted LLM ran out of responses")
        return self.responses.pop(0)


def action(
    name: str,
    arguments: dict[str, object],
    *,
    finish_reason: str = "stop",
    summary: str = "",
) -> LlmCompletion:
    payload: dict[str, object] = {"action": name, "arguments": arguments}
    if summary:
        payload["summary"] = summary
    return LlmCompletion(
        content=json.dumps(payload),
        finish_reason=finish_reason,
        usage={"completion_tokens": 12},
    )


class DeveloperToolLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "target"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.runtime = self.root / ".runtime"
        self.store = RuntimeStore(repo_root=self.root, runtime_root=self.runtime)
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTION_RETRIES": "0",
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def work_packet(self) -> dict[str, object]:
        paths = ["src/Main.java", "src/MainTest.java"]
        return {
            "schema_version": "developer_work_packet_profiled_v1",
            "planning_profile": {
                "project_profile_id": "typescript_fullstack_v1",
                "language_profile_id": "typescript_v1",
                "policy_version": "planning_path_policy_v1",
            },
            "issue_key": "SDLC-2",
            "approved_plan": {"summary": "Implement and test one behavior."},
            "effective_design_contract": {"classes": ["Main"]},
            "requirement_contract": {"version": "v2"},
            "canonical_requirements": [{"id": "REQ-1", "content": "The behavior must be implemented."}],
            "proposed_files": [
                {"path": path, "operation": "create", "purpose": "Cover REQ-1."}
                for path in paths
            ],
            "implementation_steps": [
                {"order": index, "title": path, "description": "Implement REQ-1.", "files": [path]}
                for index, path in enumerate(paths, start=1)
            ],
            "requirement_coverage_by_file": {
                path: [{"requirement_id": "REQ-1"}] for path in paths
            },
            "design_mappings_by_file": {
                path: [{"design_element_id": "class:Main", "planned_code_target": path}]
                for path in paths
            },
            "output_contract": {"allowed_paths": paths},
            "run_workspace": {"base_sha": "base-sha"},
        }

    def one_file_packet(self) -> dict[str, object]:
        packet = self.work_packet()
        packet["proposed_files"] = packet["proposed_files"][:1]
        packet["implementation_steps"] = packet["implementation_steps"][:1]
        packet["requirement_coverage_by_file"] = {
            "src/Main.java": [{"requirement_id": "REQ-1"}],
        }
        packet["design_mappings_by_file"] = {
            "src/Main.java": [{"design_element_id": "class:Main", "planned_code_target": "src/Main.java"}],
        }
        packet["output_contract"] = {"allowed_paths": ["src/Main.java"]}
        return packet

    def executor(
        self,
        llm: ScriptedLlm,
        *,
        step_review: dict[str, object] | None = None,
    ) -> DeveloperToolLoopExecutor:
        return DeveloperToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=llm,
            step_review=step_review,
        )

    def test_action_budget_reserves_required_stages_before_completion(self) -> None:
        self.assertEqual(
            _execution_phase(
                history=[],
                mutated=True,
                action_index=22,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
            ),
            "verification_required",
        )
        self.assertIsNone(
            _action_policy_error(
                action={"action": "verification.run_stage", "arguments": {"stage_id": "typecheck"}},
                history=[],
                seen_observations=set(),
                mutated=True,
                action_index=24,
                max_actions=24,
                pending_stage_ids=["typecheck"],
            )
        )

    def test_failed_stage_requires_mutation_before_verification_rerun(self) -> None:
        self.assertEqual(
            _execution_phase(
                history=[],
                mutated=True,
                action_index=20,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
                failed_stage_ids=["typecheck"],
            ),
            "verification_repair_required",
        )
        rejected = _action_policy_error(
            action={"action": "verification.run_stage", "arguments": {"stage_id": "typecheck"}},
            history=[],
            seen_observations=set(),
            mutated=True,
            action_index=20,
            max_actions=24,
            pending_stage_ids=["typecheck", "unit"],
            failed_stage_ids=["typecheck"],
        )
        self.assertEqual(rejected["error"], "verification_repair_required")
        self.assertIsNone(
            _action_policy_error(
                action={"action": "repo.write_file", "arguments": {"path": "src/Main.java"}},
                history=[],
                seen_observations=set(),
                mutated=True,
                action_index=20,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
                failed_stage_ids=["typecheck"],
            )
        )
        self.assertIsNone(
            _action_policy_error(
                action={"action": "repo.read", "arguments": {"path": "package.json"}},
                history=[],
                seen_observations=set(),
                mutated=True,
                action_index=20,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
                failed_stage_ids=["typecheck"],
            )
        )
        self.assertIsNone(
            _action_policy_error(
                action={"action": "verification.run_stage", "arguments": {"stage_id": "typecheck"}},
                history=[],
                seen_observations=set(),
                mutated=True,
                action_index=24,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
                failed_stage_ids=["typecheck"],
                repair_mutated_stage_ids=["typecheck"],
            )
        )
        ready = _action_policy_error(
            action={
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.java", "content": "class Main { int next; }"},
            },
            history=[],
            seen_observations=set(),
            mutated=True,
            action_index=21,
            max_actions=24,
            pending_stage_ids=["typecheck", "unit"],
            failed_stage_ids=["typecheck"],
            repair_mutated_stage_ids=["typecheck"],
            repair_verification_ready_stage_ids=["typecheck"],
        )
        self.assertIsNone(ready)
        repeated = "class Main {}"
        ready_over_duplicate = _action_policy_error(
            action={
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.java", "content": repeated},
            },
            history=[
                {
                    "action": "repo.write_file",
                    "arguments": {
                        "path": "src/Main.java",
                        "content_chars": len(repeated),
                        "content_sha256": hashlib.sha256(repeated.encode("utf-8")).hexdigest(),
                    },
                    "result": {"ok": False, "error": "no_effect_mutation"},
                }
            ],
            seen_observations=set(),
            mutated=True,
            action_index=22,
            max_actions=24,
            pending_stage_ids=["typecheck", "unit"],
            failed_stage_ids=["typecheck"],
            repair_mutated_stage_ids=["typecheck"],
            repair_verification_ready_stage_ids=["typecheck"],
        )
        self.assertEqual(ready_over_duplicate["error"], "verification_repair_ready")
        self.assertEqual(ready_over_duplicate["repeat_count"], 2)
        blocked_observation = _action_policy_error(
            action={"action": "repo.read", "arguments": {"path": "src/Main.java"}},
            history=[],
            seen_observations=set(),
            mutated=True,
            action_index=22,
            max_actions=24,
            pending_stage_ids=["typecheck", "unit"],
            failed_stage_ids=["typecheck"],
            repair_mutated_stage_ids=["typecheck"],
            repair_verification_ready_stage_ids=["typecheck"],
        )
        self.assertEqual(blocked_observation["error"], "verification_repair_ready")
        forced_verification = _action_policy_error(
            action={
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.java", "content": "class Main { int later; }"},
            },
            history=[],
            seen_observations=set(),
            mutated=True,
            action_index=23,
            max_actions=24,
            pending_stage_ids=["typecheck", "unit"],
            failed_stage_ids=["typecheck"],
            repair_mutated_stage_ids=["typecheck"],
            repair_verification_ready_stage_ids=["typecheck"],
            repair_verification_forced_stage_ids=["typecheck"],
        )
        self.assertEqual(forced_verification["error"], "verification_repair_ready")
        self.assertIsNone(
            _action_policy_error(
                action={"action": "verification.run_stage", "arguments": {"stage_id": "typecheck"}},
                history=[],
                seen_observations=set(),
                mutated=True,
                action_index=22,
                max_actions=24,
                pending_stage_ids=["typecheck", "unit"],
                failed_stage_ids=["typecheck"],
                repair_mutated_stage_ids=["typecheck"],
                repair_verification_ready_stage_ids=["typecheck"],
            )
        )

    def test_exact_no_effect_mutation_is_rejected_but_new_content_is_allowed(self) -> None:
        repeated = "class Main {}"
        history = [
            {
                "action": "repo.write_file",
                "arguments": {
                    "path": "src/Main.java",
                    "content_chars": len(repeated),
                    "content_sha256": hashlib.sha256(repeated.encode("utf-8")).hexdigest(),
                },
                "result": {"ok": False, "error": "no_effect_mutation"},
            }
        ]

        rejected = _action_policy_error(
            action={
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.java", "content": repeated},
            },
            history=history,
            seen_observations=set(),
            mutated=False,
            action_index=2,
            max_actions=24,
        )
        self.assertEqual(rejected["error"], "duplicate_no_effect_mutation")
        self.assertIsNone(
            _action_policy_error(
                action={
                    "action": "repo.write_file",
                    "arguments": {"path": "src/Main.java", "content": "class Main { int value; }"},
                },
                history=history,
                seen_observations=set(),
                mutated=False,
                action_index=3,
                max_actions=24,
            )
        )

    def test_unlocated_timeout_requires_test_instrumentation_then_verification(self) -> None:
        history = [
            {
                "action": "verification.run_stage",
                "arguments": {"stage_id": "unit"},
                "result": {
                    "ok": False,
                    "stage_id": "unit",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "Vitest test failure: hangs: Error: Test timed out in 5000ms.",
                    "selected_test_files": ["src/Main.test.ts"],
                },
            }
        ]

        self.assertEqual(_timeout_diagnostic_state(history)["status"], "instrumentation_required")
        guessed_fix = _action_policy_error(
            action={
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.ts", "content": "export const guessed = true;"},
            },
            history=history,
            seen_observations=set(),
            mutated=True,
            action_index=2,
            max_actions=24,
            failed_stage_ids=["unit"],
        )
        self.assertEqual(guessed_fix["error"], "timeout_diagnostic_required")

        instrumentation = {
            "action": "repo.apply_patch",
            "arguments": {
                "path": "src/Main.test.ts",
                "patch": (
                    "@@ -1 +1,3 @@\n"
                    "+// SDLC_DIAGNOSTIC_INSTRUMENTATION\n"
                    "+throw new Error('SDLC_DIAGNOSTIC_TIMEOUT:createApp');\n"
                ),
            },
        }
        self.assertIsNone(
            _action_policy_error(
                action=instrumentation,
                history=history,
                seen_observations=set(),
                mutated=True,
                action_index=2,
                max_actions=24,
                failed_stage_ids=["unit"],
            )
        )

        history.append(
            {
                "action": "repo.apply_patch",
                "arguments": {"path": "src/Main.test.ts"},
                "result": {
                    "ok": True,
                    "path": "src/Main.test.ts",
                    "timeout_diagnostic_instrumentation": True,
                },
            }
        )
        self.assertEqual(_timeout_diagnostic_state(history)["status"], "verification_required")
        blocked_second_write = _action_policy_error(
            action=instrumentation,
            history=history,
            seen_observations=set(),
            mutated=True,
            action_index=3,
            max_actions=24,
            failed_stage_ids=["unit"],
            repair_mutated_stage_ids=["unit"],
        )
        self.assertEqual(
            blocked_second_write["error"],
            "timeout_diagnostic_verification_required",
        )
        self.assertIsNone(
            _action_policy_error(
                action={"action": "verification.run_stage", "arguments": {"stage_id": "unit"}},
                history=history,
                seen_observations=set(),
                mutated=True,
                action_index=3,
                max_actions=24,
                failed_stage_ids=["unit"],
                repair_mutated_stage_ids=["unit"],
            )
        )

    def test_timeout_diagnostic_markers_block_completion(self) -> None:
        target = self.repo / "src/Main.test.ts"
        target.parent.mkdir(parents=True)
        target.write_text(
            "// SDLC_DIAGNOSTIC_INSTRUMENTATION\n"
            "throw new Error('SDLC_DIAGNOSTIC_TIMEOUT:createApp');\n",
            encoding="utf-8",
        )

        blocked = _timeout_diagnostic_completion_error(
            target_path=self.repo,
            item_paths=["src/Main.test.ts"],
        )

        self.assertEqual(blocked["error"], "timeout_diagnostic_cleanup_required")
        self.assertEqual(blocked["paths"], ["src/Main.test.ts"])

    def test_timeout_diagnostic_reserves_cleanup_and_clean_verification_actions(self) -> None:
        history = [
            {
                "action": "repo.apply_patch",
                "result": {
                    "ok": True,
                    "path": "src/Main.test.ts",
                    "timeout_diagnostic_instrumentation": True,
                },
            }
        ]

        max_actions = _timeout_diagnostic_action_limit(
            configured_max_actions=24,
            timeout_action_reserve=6,
            history=history,
        )

        self.assertEqual(max_actions, 30)
        self.assertIsNone(
            _action_policy_error(
                action={
                    "action": "repo.write_file",
                    "arguments": {"path": "src/Main.test.ts", "content": "clean test\n"},
                },
                history=history,
                seen_observations=set(),
                mutated=True,
                action_index=24,
                max_actions=max_actions,
            )
        )

    def test_timeout_diagnostic_does_not_expand_normal_action_limit(self) -> None:
        self.assertEqual(
            _timeout_diagnostic_action_limit(
                configured_max_actions=24,
                timeout_action_reserve=6,
                history=[],
            ),
            24,
        )

    def test_step_review_new_attempt_continues_after_approved_history(self) -> None:
        session_path = self.repo / "step_review_session_v1.json"
        session_path.write_text(
            json.dumps(
                {
                    "calls": [
                        {"ordinal": 23, "status": "approved"},
                        {"ordinal": 24, "status": "approved"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            _step_review_start_ordinal(
                session_path=session_path,
                requested_cursor=0,
                run_id="run-test",
            ),
            24,
        )

    def test_step_review_pending_exchange_keeps_resume_cursor(self) -> None:
        session_path = self.repo / "step_review_session_v1.json"
        session_path.write_text(
            json.dumps(
                {
                    "calls": [
                        {"ordinal": 24, "status": "approved"},
                        {"ordinal": 25, "status": "pending"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            _step_review_start_ordinal(
                session_path=session_path,
                requested_cursor=24,
                run_id="run-test",
            ),
            24,
        )

    def test_patch_hunk_count_mismatch_is_rejected_before_git_apply(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            _prepare_patch_text(
                "@@ -1 +1,2 @@\n original\n+added\n+silently-truncated-before-fix\n",
                item_path="src/Main.java",
                run_id="run-patch-count",
            )

        self.assertEqual(caught.exception.error_code, "developer_patch_invalid")
        self.assertIn("line counts", caught.exception.message)

    def test_patch_hunk_count_match_is_accepted(self) -> None:
        prepared = _prepare_patch_text(
            "@@ -1 +1,2 @@\n original\n+added\n",
            item_path="src/Main.java",
            run_id="run-patch-count",
        )

        self.assertTrue(prepared.startswith("--- a/src/Main.java\n+++ b/src/Main.java\n@@ -1 +1,2 @@"))

    def test_new_typescript_test_requires_repository_context_before_first_write(self) -> None:
        action_row = {
            "action": "repo.write_file",
            "arguments": {"path": "src/Main.test.ts", "expected_sha256": "", "content": "test('x', () => {});"},
        }
        manifest = {"version": "developer_execution_manifest_v8"}

        rejected = _new_test_context_policy_error(
            action=action_row,
            history=[],
            manifest=manifest,
            target_path=self.repo,
            item_paths=["src/Main.test.ts"],
        )

        self.assertEqual(rejected["error"], "developer_test_context_required")
        self.assertIn("instanceof", rejected["instruction"])

        history = [
            {
                "action": "repo.read",
                "arguments": {"path": "src/Existing.test.ts"},
                "result": {"ok": True, "path": "src/Existing.test.ts", "content": "test('existing', () => {});"},
            }
        ]
        self.assertIsNone(
            _new_test_context_policy_error(
                action=action_row,
                history=history,
                manifest=manifest,
                target_path=self.repo,
                item_paths=["src/Main.test.ts"],
            )
        )

        empty_search = [
            {
                "action": "repo.search",
                "arguments": {"query": "vi.mock"},
                "result": {"ok": True, "hits": [], "truncated": False},
            }
        ]
        rejected_after_empty_search = _new_test_context_policy_error(
            action=action_row,
            history=empty_search,
            manifest=manifest,
            target_path=self.repo,
            item_paths=["src/Main.test.ts"],
        )
        self.assertEqual(rejected_after_empty_search["error"], "developer_test_context_required")

    def test_action_prompt_exposes_unsatisfied_test_authoring_policy(self) -> None:
        prompt = _action_prompt(
            item={"id": "WU-TEST", "paths": ["src/Main.test.ts"], "required_stage_ids": []},
            manifest={"version": "developer_execution_manifest_v8", "mode": "act", "base_sha": "base"},
            history=[],
            target_path=self.repo,
            max_write_chars=18000,
            force_incremental=False,
            action_index=1,
            max_actions=24,
            mutated=False,
            context_window_tokens=128000,
        )
        context = json.loads(prompt.split("Execution context:\n", 1)[1])

        self.assertFalse(context["test_authoring_policy"]["repository_evidence_satisfied"])
        self.assertEqual(context["test_authoring_policy"]["new_test_targets"], ["src/Main.test.ts"])
        self.assertIn("rules", context["test_authoring_policy"])
        self.assertIn("shape-only fake", prompt)

    def test_repair_episode_survives_mutations_until_the_failed_stage_passes(self) -> None:
        failed = {
            "stage_id": "typecheck",
            "status": "failed",
            "failure_kind": "implementation",
            "reason": "tsc failed",
            "stage_results": [
                {
                    "stage_id": "typecheck",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "tsc failed",
                    "stdout": "src/Main.ts(1,1): error TS1: broken",
                    "stderr": "",
                }
            ],
        }
        fingerprint = _verification_diagnostic_fingerprint(failed)
        history = [
            {"action": "verification.run_stage", "result": failed},
            {
                "action": "repo.write_file",
                "result": {"ok": True, "path": "src/Main.ts"},
            },
        ]

        state = _verification_repair_state(history)

        self.assertEqual(state["typecheck"]["diagnostic_fingerprint"], fingerprint)
        self.assertTrue(state["typecheck"]["mutated_since_failure"])
        self.assertEqual(state["typecheck"]["changed_paths"], ["src/Main.ts"])
        history.append(
            {
                "action": "repo.write_file",
                "result": {"ok": False, "error": "no_effect_mutation", "path": "src/Main.ts"},
            }
        )
        self.assertTrue(_verification_repair_state(history)["typecheck"]["verification_ready"])
        history.append(
            {
                "action": "repo.write_file",
                "result": {"ok": False, "error": "verification_repair_ready", "path": "src/Main.ts"},
            }
        )
        self.assertTrue(_verification_repair_state(history)["typecheck"]["verification_forced"])
        history.append(
            {
                "action": "verification.run_stage",
                "result": {"stage_id": "typecheck", "status": "passed", "failure_kind": ""},
            }
        )
        self.assertEqual(_verification_repair_state(history), {})

    def test_upstream_dependency_completion_forces_stale_stage_rerun(self) -> None:
        history = [
            {
                "action": "verification.run_stage",
                "result": {
                    "stage_id": "typecheck",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "DB barrel export is missing.",
                },
            },
            {
                "action": "upstream_dependency_completed",
                "result": {
                    "ok": True,
                    "dependency_id": "WU-DB",
                    "changed_paths": ["packages/db/src/index.ts"],
                    "stale_stage_ids": ["typecheck"],
                },
            },
        ]

        state = _verification_repair_state(history)

        self.assertTrue(state["typecheck"]["verification_ready"])
        self.assertTrue(state["typecheck"]["verification_forced"])
        self.assertEqual(state["typecheck"]["upstream_dependency_id"], "WU-DB")
        self.assertEqual(state["typecheck"]["upstream_changed_paths"], ["packages/db/src/index.ts"])

    def unit_packet(self) -> dict[str, object]:
        packet = self.work_packet()
        packet["execution_units"] = [
            {
                "unit_id": "WU-001",
                "order": 1,
                "title": "Implement behavior",
                "objective": "Implement the approved behavior.",
                "depends_on": [],
                "write_paths": ["src/Main.java"],
                "requirement_refs": ["REQ-1"],
                "acceptance_refs": ["AC-1"],
                "design_node_ids": ["class:Main"],
                "design_edge_ids": ["edge:Main-to-store"],
                "planned_code_targets": [
                    {"path": "src/Main.java", "symbol": "Main.save", "action": "create_or_update"}
                ],
                "migration_impact": {
                    "status": "required",
                    "design_intent": "persist the approved category field",
                    "reason": "The approved boundary requires category persistence.",
                    "targets": ["src/Main.java"],
                },
                "runtime_bindings": [
                    {
                        "design_node_id": "class:Main",
                        "runtime_kind": "service",
                        "target": {"path": "src/Main.java", "symbol": "Main.save"},
                        "attachment": {"owner_path": "src/Main.java", "owner_symbol": "Main"},
                    }
                ],
                "boundary_contracts": [
                    {
                        "design_edge_id": "edge:Main-to-store",
                        "input_mapping": {"category": "category"},
                        "provider_operation": "Main.save",
                    }
                ],
                "required_stage_ids": [],
                "test_target_paths": [],
                "input_digest": "sha256:unit-1",
            },
            {
                "unit_id": "WU-002",
                "order": 2,
                "title": "Verify behavior",
                "objective": "Add the approved test.",
                "depends_on": ["WU-001"],
                "write_paths": ["src/MainTest.java"],
                "requirement_refs": ["REQ-1"],
                "acceptance_refs": ["AC-1"],
                "design_node_ids": ["class:Main"],
                "required_stage_ids": [],
                "test_target_paths": [],
                "input_digest": "sha256:unit-2",
            },
        ]
        return packet

    def test_unit_aware_execution_uses_plan_units_and_durable_session(self) -> None:
        llm = ScriptedLlm(
            [
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "implementation complete",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
                action(
                    "repo.write_file",
                    {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"},
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "test complete",
                        "path_dispositions": [
                            {"path": "src/MainTest.java", "status": "mutated", "reason": "Added test."}
                        ],
                    },
                ),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_units",
            work_packet=self.unit_packet(),
            target_path=self.repo,
        )

        manifest = json.loads((self.root / result.manifest_path).read_text(encoding="utf-8"))
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        session = json.loads((self.root / result.session_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "developer_execution_manifest_v8")
        self.assertEqual([item["id"] for item in manifest["items"]], ["WU-001", "WU-002"])
        self.assertEqual(journal["version"], "developer_execution_journal_v4")
        self.assertEqual(session["contract_version"], "developer_execution_units_v1")
        self.assertEqual(result.progress["completed_unit_ids"], ["WU-001", "WU-002"])
        self.assertIn("repo.write_file", llm.prompts[0])
        self.assertIn("execution.complete_unit", llm.prompts[0])
        self.assertIn("The action is not executed until the Harness returns its tool result", llm.systems[0])
        self.assertIn("end the response immediately", llm.prompts[0])
        self.assertIn("Treat authoritative_sources as the single verified current", llm.prompts[0])
        source_unit = self.unit_packet()["execution_units"][0]
        execution_context = json.loads(llm.prompts[0].split("Execution context:\n", 1)[1])
        prompt_contract = execution_context["execution_unit_contract"]
        for field in (
            "planned_code_targets",
            "migration_impact",
            "runtime_bindings",
            "boundary_contracts",
        ):
            self.assertEqual(manifest["items"][0][field], source_unit[field])
            self.assertEqual(prompt_contract[field], source_unit[field])
        self.assertNotIn("boundary_contracts", execution_context["execution_task"])

    def test_unit_context_paths_are_read_only_and_visible_in_prompt(self) -> None:
        packet = self.unit_packet()
        unit = dict(packet["execution_units"][1])
        unit["depends_on"] = []
        unit["context_paths"] = ["src/MainTest.java", "src/Main.java"]
        unit["contract_anchor_paths"] = ["src/Main.java"]
        packet["execution_units"] = [unit]
        source = self.repo / "src/Main.java"
        source.parent.mkdir(parents=True)
        source.write_text("class Main {}\n", encoding="utf-8")
        llm = ScriptedLlm(
            [
                action("repo.read", {"path": "src/Main.java"}),
                action(
                    "repo.write_file",
                    {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"},
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "test complete",
                        "path_dispositions": [
                            {"path": "src/MainTest.java", "status": "mutated", "reason": "Added test."}
                        ],
                    },
                ),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_read_only_unit_context",
            work_packet=packet,
            target_path=self.repo,
        )

        prompt_context = json.loads(llm.prompts[0].split("Execution context:\n", 1)[1])
        self.assertEqual(
            prompt_context["execution_unit_contract"]["read_context_paths"],
            ["src/MainTest.java", "src/Main.java"],
        )
        self.assertEqual(
            prompt_context["execution_unit_contract"]["write_paths"],
            ["src/MainTest.java"],
        )
        self.assertEqual(result.progress["completed_unit_ids"], ["WU-002"])

    def test_unit_aware_execution_allows_fully_verified_unchanged_unit(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        target = self.repo / "src" / "Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        llm = ScriptedLlm(
            [
                action("repo.read", {"path": "src/Main.java"}),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "Existing implementation is already current.",
                        "path_dispositions": [
                            {
                                "path": "src/Main.java",
                                "status": "verified_unchanged",
                                "reason": "Fresh inspection confirmed the approved implementation.",
                            }
                        ],
                    },
                ),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_unit_verified_unchanged",
            work_packet=packet,
            target_path=self.repo,
        )

        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(result.progress["completed_units"], 1)
        self.assertEqual(journal["items"][0]["mutated_paths"], [])
        self.assertTrue(journal["items"][0]["no_change_required"])

    def test_unit_manifest_excludes_later_signature_from_shared_file(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        packet["execution_units"][0]["signature_entry_ids"] = ["SIG-CURRENT"]
        packet["canonical_requirements"].append(
            {"id": "REQ-LATER", "content": "A later unit owns the query behavior."}
        )
        packet["requirement_coverage_by_file"]["src/Main.java"].append(
            {"requirement_id": "REQ-LATER"}
        )
        packet["implementation_steps"] = [
            {
                "order": 1,
                "title": "Implement behavior",
                "description": "Implement the current unit.",
                "files": ["src/Main.java"],
            },
            {
                "order": 6,
                "title": "Query later",
                "description": "Implement a later unit.",
                "files": ["src/Main.java"],
            },
        ]
        packet["design_mappings_by_file"]["src/Main.java"] = [
            {
                "design_element_id": "class:Main",
                "planned_code_target": "src/Main.java#Main.save",
            },
            {
                "design_element_id": "method:Main.queryLater",
                "planned_code_target": "src/Main.java#Main.queryLater",
            },
        ]
        packet["implementation_signature_contract"] = {
            "entries": [
                {
                    "contract_id": "SIG-CURRENT",
                    "source_path": "src/Main.java",
                    "qualified_name": "Main.save",
                },
                {
                    "contract_id": "SIG-LATER",
                    "source_path": "src/Main.java",
                    "qualified_name": "Main.queryLater",
                },
            ]
        }

        manifest = _build_manifest(
            run_id="run-shared-signature",
            work_packet=packet,
            target_path=self.repo,
            mode="act",
            repair_feedback=None,
            repair_iteration=0,
        )
        file_context = manifest["items"][0]["files"][0]
        signatures = file_context["approved_signatures"]

        self.assertEqual(
            [signature["contract_id"] for signature in signatures],
            ["SIG-CURRENT"],
        )
        self.assertEqual(file_context["requirement_ids"], ["REQ-1"])
        self.assertEqual(
            [row["design_element_id"] for row in file_context["design_mappings"]],
            ["class:Main"],
        )
        self.assertEqual(
            [row["order"] for row in file_context["implementation_steps"]],
            [1],
        )

    def test_step_review_pauses_before_each_developer_action_and_replays_approved_output(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        llm = ScriptedLlm(
            [
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                    summary="The approved file is absent; create the minimal Main implementation.",
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "implementation complete",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
            ]
        )

        with self.assertRaises(DeveloperExecutionError) as first_pause:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(first_pause.exception.error_code, "developer_step_review_required")
        first_pending = first_pause.exception.progress["pending_step_review"]
        self.assertFalse((self.repo / "src/Main.java").exists())
        first_exchange = json.loads(
            (self.root / str(first_pending["exchange_path"])).read_text(encoding="utf-8")
        )
        self.assertIn("Developer Tool Action v2", first_exchange["prompt"])
        self.assertEqual(first_exchange["parsed_output"]["action"], "repo.write_file")

        with self.assertRaises(DeveloperExecutionError) as second_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": first_pending["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review",
                work_packet=packet,
                target_path=self.repo,
            )

        second_pending = second_pause.exception.progress["pending_step_review"]
        self.assertNotEqual(second_pending["call_id"], first_pending["call_id"])
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")
        second_exchange = json.loads(
            (self.root / str(second_pending["exchange_path"])).read_text(encoding="utf-8")
        )
        second_context = json.loads(second_exchange["prompt"].split("Execution context:\n", 1)[1])
        self.assertEqual(
            second_context["working_memory"]["diagnostic_breadcrumbs"],
            [
                {
                    "action_index": 1,
                    "action": "repo.write_file",
                    "path": "src/Main.java",
                    "summary": "The approved file is absent; create the minimal Main implementation.",
                    "outcome": "ok",
                }
            ],
        )

        completed = self.executor(
            llm,
            step_review={
                "enabled": True,
                "call_id": second_pending["call_id"],
                "decision": "approve",
            },
        ).execute(
            run_id="run-step-review",
            work_packet=packet,
            target_path=self.repo,
        )

        self.assertEqual(completed.progress["completed_unit_ids"], ["WU-001"])
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")
        self.assertEqual(len(llm.prompts), 2)
        action_files = sorted(
            path.name
            for path in (
                self.runtime
                / "runs"
                / "run-step-review"
                / "developer_execution"
                / "act_v1"
                / "actions"
            ).glob("*.json")
        )
        self.assertEqual(
            action_files,
            [
                "wu-001_a001_001.json",
                "wu-001_a001_002.json",
            ],
        )

    def test_transport_failure_preserves_approved_actions_for_same_attempt_resume(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]

        class FailsOnceAfterWrite(ScriptedLlm):
            def __init__(self) -> None:
                super().__init__(
                    [
                        action(
                            "repo.write_file",
                            {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                        )
                    ]
                )
                self.fail_next = True

            def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
                if self.responses:
                    return super().complete_with_metadata(**kwargs)
                if self.fail_next:
                    self.fail_next = False
                    raise HarnessError("llm_transport_failed", "LLM request failed: incomplete response")
                return action(
                    "execution.complete_unit",
                    {
                        "summary": "implementation complete",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                )

        llm = FailsOnceAfterWrite()
        with self.assertRaises(DeveloperExecutionError) as first_pause:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review-transport",
                work_packet=packet,
                target_path=self.repo,
            )
        pending = first_pause.exception.progress["pending_step_review"]

        with self.assertRaises(DeveloperExecutionError) as transport_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": pending["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review-transport",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(transport_pause.exception.error_code, "llm_transport_failed")
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")
        journal = json.loads((self.root / transport_pause.exception.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["items"][0]["status"], "review_required")
        self.assertEqual(journal["items"][0]["attempts"], 1)

        with self.assertRaises(DeveloperExecutionError) as resumed:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review-transport",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(resumed.exception.error_code, "developer_step_review_required")
        self.assertTrue(resumed.exception.progress["pending_step_review"]["call_id"].endswith("-0002-a01"))
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")

    def test_step_review_prompt_drift_preserves_approved_worktree_for_rejection(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        llm = ScriptedLlm(
            [
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "implementation complete",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
            ]
        )

        with self.assertRaises(DeveloperExecutionError) as first_pause:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review-drift",
                work_packet=packet,
                target_path=self.repo,
            )
        first_pending = first_pause.exception.progress["pending_step_review"]

        with self.assertRaises(DeveloperExecutionError) as second_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": first_pending["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review-drift",
                work_packet=packet,
                target_path=self.repo,
            )
        second_pending = second_pause.exception.progress["pending_step_review"]
        working_state_path = (
            self.runtime
            / "runs"
            / "run-step-review-drift"
            / "developer_execution"
            / "act_v1"
            / "units"
            / "WU-001"
            / "working_state.json"
        )
        working_state = json.loads(working_state_path.read_text(encoding="utf-8"))
        working_state["history"].append(
            {"action": "rejected", "error": "policy_changed", "instruction": "Regenerate safely."}
        )
        working_state_path.write_text(json.dumps(working_state), encoding="utf-8")

        with self.assertRaises(DeveloperExecutionError) as drift:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": second_pending["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review-drift",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(drift.exception.error_code, "developer_step_review_drift")
        self.assertTrue(drift.exception.retriable)
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")
        journal = json.loads((self.root / drift.exception.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["items"][0]["status"], "review_required")

    def test_operator_recovery_restores_only_hash_bound_approved_mutations(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        llm = ScriptedLlm(
            [
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                ),
                action("repo.read", {"path": "src/Main.java"}),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "implementation complete",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
            ]
        )
        executor = self.executor(llm, step_review={"enabled": True})

        with self.assertRaises(DeveloperExecutionError) as first_pause:
            executor.execute(run_id="run-step-review-recover", work_packet=packet, target_path=self.repo)
        first_pending = first_pause.exception.progress["pending_step_review"]
        with self.assertRaises(DeveloperExecutionError) as second_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": first_pending["call_id"],
                    "decision": "approve",
                },
            ).execute(run_id="run-step-review-recover", work_packet=packet, target_path=self.repo)
        second_pending = second_pause.exception.progress["pending_step_review"]
        with self.assertRaises(DeveloperExecutionError) as third_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": second_pending["call_id"],
                    "decision": "approve",
                },
            ).execute(run_id="run-step-review-recover", work_packet=packet, target_path=self.repo)
        third_pending = third_pause.exception.progress["pending_step_review"]
        journal_path = self.runtime / "runs/run-step-review-recover/developer_execution/act_v1/journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        actions = journal["items"][0]["actions"]
        journal["items"][0]["attempt_history"] = [{"attempt": 1, "actions": actions[:1]}]
        journal["items"][0]["actions"] = actions[1:]
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (self.repo / "src/Main.java").unlink()

        recovery = executor.recover_approved_step_review_state(
            run_id="run-step-review-recover",
            work_packet=packet,
            target_path=self.repo,
            item_id="WU-001",
            pending_call_id=third_pending["call_id"],
        )

        self.assertEqual(recovery["restored_paths"], ["src/Main.java"])
        self.assertEqual((self.repo / "src/Main.java").read_text(encoding="utf-8"), "class Main {}\n")
        working_state = json.loads(
            (
                self.runtime
                / "runs/run-step-review-recover/developer_execution/act_v1/units/WU-001/working_state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(working_state["next_action_index"], 3)
        self.assertEqual(working_state["review_cursor"], 2)

    def test_interrupted_running_step_review_resumes_durable_working_state(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        llm = ScriptedLlm(
            [
                action("repo.read", {"path": "src/Main.java"}),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "ready",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "verified_unchanged", "reason": "Current."}
                        ],
                    },
                ),
            ]
        )
        target = self.repo / "src/Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")

        with self.assertRaises(DeveloperExecutionError) as first_pause:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review-interrupted",
                work_packet=packet,
                target_path=self.repo,
            )
        with self.assertRaises(DeveloperExecutionError) as second_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": first_pause.exception.progress["pending_step_review"]["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review-interrupted",
                work_packet=packet,
                target_path=self.repo,
            )

        journal_path = self.runtime / "runs/run-step-review-interrupted/developer_execution/act_v1/journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "running"
        journal["items"][0]["status"] = "running"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        with self.assertRaises(DeveloperExecutionError) as resumed:
            self.executor(llm, step_review={"enabled": True}).execute(
                run_id="run-step-review-interrupted",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(resumed.exception.error_code, "developer_step_review_required")
        self.assertEqual(
            resumed.exception.progress["pending_step_review"]["call_id"],
            second_pause.exception.progress["pending_step_review"]["call_id"],
        )
        recovered = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["items"][0]["attempts"], 1)
        self.assertEqual(recovered["items"][0]["recovery_mode"], "durable_working_state")
        self.assertEqual(recovered["items"][0].get("attempt_history", []), [])

    def test_operator_recovery_restores_observation_only_history(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [packet["execution_units"][0]]
        llm = ScriptedLlm(
            [
                action("repo.read", {"path": "src/Main.java"}),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "ready",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "verified_unchanged", "reason": "Current."}
                        ],
                    },
                ),
            ]
        )
        target = self.repo / "src/Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        executor = self.executor(llm, step_review={"enabled": True})

        with self.assertRaises(DeveloperExecutionError) as first_pause:
            executor.execute(
                run_id="run-step-review-observation-recover",
                work_packet=packet,
                target_path=self.repo,
            )
        with self.assertRaises(DeveloperExecutionError) as second_pause:
            self.executor(
                llm,
                step_review={
                    "enabled": True,
                    "call_id": first_pause.exception.progress["pending_step_review"]["call_id"],
                    "decision": "approve",
                },
            ).execute(
                run_id="run-step-review-observation-recover",
                work_packet=packet,
                target_path=self.repo,
            )

        journal_path = (
            self.runtime / "runs/run-step-review-observation-recover/developer_execution/act_v1/journal.json"
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        actions = journal["items"][0]["actions"]
        journal["items"][0]["attempt_history"] = [{"attempt": 1, "actions": actions}]
        journal["items"][0]["actions"] = []
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        recovery = executor.recover_approved_step_review_state(
            run_id="run-step-review-observation-recover",
            work_packet=packet,
            target_path=self.repo,
            item_id="WU-001",
            pending_call_id=second_pause.exception.progress["pending_step_review"]["call_id"],
        )

        self.assertEqual(recovery["restored_paths"], [])
        working_state = json.loads(
            (
                self.runtime
                / "runs/run-step-review-observation-recover/developer_execution/act_v1/units/WU-001/working_state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(working_state["next_action_index"], 2)
        self.assertEqual(working_state["review_cursor"], 1)
        self.assertEqual(working_state["history"][0]["action"], "repo.read")
        self.assertEqual(working_state["mutated_paths"], [])

    def test_unit_completion_requires_current_verification_evidence(self) -> None:
        packet = self.unit_packet()
        packet["execution_units"] = [dict(packet["execution_units"][0])]
        packet["execution_units"][0]["required_stage_ids"] = ["typecheck"]
        calls: list[str] = []

        class StageRunner:
            def run_stage(self, **kwargs: object) -> SimpleNamespace:
                calls.append(str(kwargs["stage_id"]))
                return SimpleNamespace(
                    status="passed",
                    failure_kind="",
                    reason="passed",
                    selected_test_files=(),
                    verified_test_files=(),
                    stage_results=(),
                )

        llm = ScriptedLlm(
            [
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                ),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "too early",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
                action("verification.run_stage", {"stage_id": "typecheck"}),
                action(
                    "execution.complete_unit",
                    {
                        "summary": "verified",
                        "path_dispositions": [
                            {"path": "src/Main.java", "status": "mutated", "reason": "Implemented."}
                        ],
                    },
                ),
            ]
        )
        executor = DeveloperToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=llm,
            verification_runner=StageRunner(),
        )

        result = executor.execute(
            run_id="run_unit_verification",
            work_packet=packet,
            target_path=self.repo,
            verification_policy=VerificationStagePolicy(
                project_profile_id="typescript_fullstack_v1",
                policy_version="verification_stage_policy_v1",
                stage_ids=("typecheck",),
            ),
        )

        self.assertEqual(calls, ["typecheck"])
        self.assertEqual(result.progress["completed_unit_ids"], ["WU-001"])
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        evidence = journal["items"][0]["verification_evidence"][0]
        self.assertEqual(evidence["report_ref"], "units/WU-001/verification/typecheck.json")
        self.assertTrue(str(evidence["report_digest"]).startswith("sha256:"))

    def test_identical_verification_failures_stall_after_three_repair_batches(self) -> None:
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM": "7",
            },
        )
        packet = self.unit_packet()
        packet["execution_units"] = [dict(packet["execution_units"][0])]
        packet["execution_units"][0]["required_stage_ids"] = ["typecheck"]

        class FailedStage:
            def to_dict(self) -> dict[str, object]:
                return {
                    "stage_id": "typecheck",
                    "status": "failed",
                    "argv": ["npm", "run", "typecheck"],
                    "cwd": ".",
                    "timeout_seconds": 120,
                    "exit_code": 2,
                    "duration_seconds": 0.1,
                    "stdout": "src/Main.java(1,1): error TS1: broken",
                    "stderr": "",
                    "reason": "tsc failed",
                    "failure_kind": "implementation",
                    "timed_out": False,
                    "selected_test_files": [],
                    "verified_test_files": [],
                }

        class RepeatedFailureRunner:
            def run_stage(self, **kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    status="failed",
                    failure_kind="implementation",
                    reason="tsc failed",
                    selected_test_files=(),
                    verified_test_files=(),
                    stage_results=(FailedStage(),),
                )

        contents = ["class Main { int value = 1; }", "class Main { int value = 2; }", "class Main { int value = 3; }"]
        hashes = [
            hashlib.sha256((content.rstrip() + os.linesep).encode("utf-8")).hexdigest()
            for content in contents
        ]
        llm = ScriptedLlm(
            [
                action("repo.write_file", {"path": "src/Main.java", "expected_sha256": "", "content": contents[0]}),
                action("verification.run_stage", {"stage_id": "typecheck"}),
                action("repo.write_file", {"path": "src/Main.java", "expected_sha256": hashes[0], "content": contents[1]}),
                action("verification.run_stage", {"stage_id": "typecheck"}),
                action("repo.write_file", {"path": "src/Main.java", "expected_sha256": hashes[1], "content": contents[2]}),
                action("verification.run_stage", {"stage_id": "typecheck"}),
            ]
        )
        executor = DeveloperToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=llm,
            verification_runner=RepeatedFailureRunner(),
        )

        with self.assertRaises(DeveloperExecutionError) as caught:
            executor.execute(
                run_id="run_repair_stalled",
                work_packet=packet,
                target_path=self.repo,
                verification_policy=VerificationStagePolicy(
                    project_profile_id="typescript_fullstack_v1",
                    policy_version="verification_stage_policy_v1",
                    stage_ids=("typecheck",),
                ),
            )

        self.assertEqual(caught.exception.error_code, "developer_repair_stalled")
        self.assertFalse((self.repo / "src" / "Main.java").exists())
        self.assertIn("active_failures", llm.prompts[2])
        self.assertIn("authoritative_sources", llm.prompts[2])

    def test_active_repair_reports_budget_exhaustion_instead_of_forcing_verification(self) -> None:
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM": "3",
            },
        )
        packet = self.unit_packet()
        packet["execution_units"] = [dict(packet["execution_units"][0])]
        packet["execution_units"][0]["required_stage_ids"] = ["typecheck"]

        class FailedRunner:
            def run_stage(self, **kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    status="failed",
                    failure_kind="implementation",
                    reason="typecheck failed",
                    selected_test_files=(),
                    verified_test_files=(),
                    stage_results=(),
                )

        first = "class Main { int value = 1; }"
        second = "class Main { int value = 2; }"
        first_hash = hashlib.sha256((first + os.linesep).encode("utf-8")).hexdigest()
        llm = ScriptedLlm(
            [
                action("repo.write_file", {"path": "src/Main.java", "expected_sha256": "", "content": first}),
                action("verification.run_stage", {"stage_id": "typecheck"}),
                action(
                    "repo.write_file",
                    {"path": "src/Main.java", "expected_sha256": first_hash, "content": second},
                ),
            ]
        )
        executor = DeveloperToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=llm,
            verification_runner=FailedRunner(),
        )

        with self.assertRaises(DeveloperExecutionError) as caught:
            executor.execute(
                run_id="run_repair_budget",
                work_packet=packet,
                target_path=self.repo,
                verification_policy=VerificationStagePolicy(
                    project_profile_id="typescript_fullstack_v1",
                    policy_version="verification_stage_policy_v1",
                    stage_ids=("typecheck",),
                ),
            )

        self.assertEqual(caught.exception.error_code, "developer_repair_budget_exhausted")
        self.assertFalse((self.repo / "src" / "Main.java").exists())

    def test_unit_budget_exhaustion_blocks_with_failure_artifact(self) -> None:
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM": "1",
            },
        )
        packet = self.unit_packet()
        packet["execution_units"] = [dict(packet["execution_units"][0])]

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(
                ScriptedLlm(
                    [action("write_file", {"path": "src/Main.java", "content": "class Main {}"})]
                )
            ).execute(
                run_id="run_unit_budget",
                work_packet=packet,
                target_path=self.repo,
            )

        error = caught.exception
        self.assertFalse(error.retriable)
        self.assertEqual(error.error_code, "developer_action_limit_exceeded")
        self.assertTrue(error.failure_path)
        failure = json.loads((self.root / error.failure_path).read_text(encoding="utf-8"))
        self.assertEqual(failure["version"], "developer_failure_classification_v1")
        self.assertEqual(failure["class"], "implementation_defect")
        self.assertEqual(failure["unit_id"], "WU-001")

    def test_truncated_execution_unit_rolls_back_and_can_resume(self) -> None:
        first_llm = ScriptedLlm(
            [
                action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                LlmCompletion(content="", finish_reason="length"),
            ]
        )

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(first_llm).execute(
                run_id="run_resume",
                work_packet=self.work_packet(),
                target_path=self.repo,
            )

        self.assertTrue(caught.exception.retriable)
        self.assertEqual(caught.exception.error_code, "developer_action_truncated")
        self.assertFalse((self.repo / "src" / "Main.java").exists())
        self.assertFalse((self.repo / "src" / "MainTest.java").exists())

        resumed_llm = ScriptedLlm(
            [
                action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                action("write_file", {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"}),
                action("complete", {"summary": "coherent slice implemented"}),
            ]
        )
        result = self.executor(resumed_llm).execute(
            run_id="run_resume",
            work_packet=self.work_packet(),
            target_path=self.repo,
        )

        self.assertEqual(result.progress["completed_items"], 1)
        self.assertTrue((self.repo / "src" / "Main.java").exists())
        self.assertTrue((self.repo / "src" / "MainTest.java").exists())

    def test_expected_dirty_baseline_is_read_only_and_hash_bound(self) -> None:
        qa_path = self.repo / "qa" / "Generated.test.ts"
        qa_path.parent.mkdir(parents=True)
        qa_path.write_text("test('baseline', () => {});\n", encoding="utf-8")
        llm = ScriptedLlm(
            [
                action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                action("complete", {"summary": "implementation written"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_expected_dirty",
            work_packet=self.one_file_packet(),
            target_path=self.repo,
            expected_dirty_paths=["qa/Generated.test.ts"],
        )

        self.assertEqual(result.progress["completed_items"], 1)
        self.assertIn("before_sha256", result.files[0])
        qa_path.write_text("test('tampered', () => {});\n", encoding="utf-8")
        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(ScriptedLlm([])).execute(
                run_id="run_expected_dirty",
                work_packet=self.one_file_packet(),
                target_path=self.repo,
                expected_dirty_paths=["qa/Generated.test.ts"],
            )

        self.assertEqual(caught.exception.error_code, "developer_workspace_drift")
        self.assertFalse(caught.exception.retriable)

        with self.assertRaises(HarnessError) as unsafe:
            self.executor(ScriptedLlm([])).execute(
                run_id="run_unsafe_dirty",
                work_packet=self.one_file_packet(),
                target_path=self.repo,
                expected_dirty_paths=["/outside/repo.test.ts"],
            )
        self.assertEqual(unsafe.exception.error_code, "developer_tool_unsafe_path")

    def test_write_action_cannot_escape_current_work_item(self) -> None:
        llm = ScriptedLlm(
            [
                action("write_file", {"path": "../escape.java", "expected_sha256": "", "content": "bad"}),
                action("write_file", {"path": "../escape.java", "expected_sha256": "", "content": "bad"}),
                action("write_file", {"path": "../escape.java", "expected_sha256": "", "content": "bad"}),
            ]
        )

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(llm).execute(
                run_id="run_scope",
                work_packet=self.work_packet(),
                target_path=self.repo,
            )

        self.assertFalse(caught.exception.retriable)
        self.assertEqual(caught.exception.error_code, "developer_act_out_of_scope")
        self.assertFalse((self.root / "escape.java").exists())

    def test_out_of_scope_write_returns_feedback_and_can_recover_inside_unit(self) -> None:
        settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTION_RETRIES": "2",
            },
        )
        llm = ScriptedLlm(
            [
                action("write_file", {"path": "src/Extra.java", "expected_sha256": "", "content": "bad"}),
                action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                action(
                    "write_file",
                    {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"},
                ),
                action("complete", {"summary": "reworked within approved paths"}),
            ]
        )

        result = DeveloperToolLoopExecutor(
            settings=settings,
            store=self.store,
            llm_client=llm,
        ).execute(
            run_id="run_scope_recovery",
            work_packet=self.work_packet(),
            target_path=self.repo,
        )

        self.assertEqual(result.progress["completed_items"], 1)
        self.assertFalse((self.repo / "src" / "Extra.java").exists())
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        rejected = journal["items"][0]["actions"][0]["tool_result"]
        self.assertEqual(rejected["error"], "developer_act_out_of_scope")
        self.assertIn("src/Main.java", rejected["approved_paths"])

    def test_multiple_actions_with_reasoning_are_rejected_with_specific_feedback(self) -> None:
        raw = (
            '{"action":"read_file","arguments":{"path":"src/Main.java"}}\n'
            "I should edit it now.\n"
            '{"action":"write_file","arguments":{"path":"src/Main.java","content":"class Main {}"}}'
        )

        with self.assertRaisesRegex(
            HarnessError,
            "contained 2 JSON actions",
        ):
            _parse_action(raw)

    def test_thinking_prefix_uses_only_the_final_action(self) -> None:
        raw = (
            '{"action":"repo.read","arguments":{"path":"packages/db/src/cultural-heritage.ts"}}\n'
            "Wait — that source is already in working memory.\n"
            '<｜end▁of▁thinking｜>{"action":"repo.read","arguments":'
            '{"path":"packages/db/package.json"},"summary":"Check package exports"}'
        )

        parsed = _parse_action(raw)

        self.assertEqual(parsed["action"], "repo.read")
        self.assertEqual(parsed["arguments"], {"path": "packages/db/package.json"})

    def test_equivalent_actions_with_different_summaries_are_collapsed(self) -> None:
        raw = (
            '{"action":"repo.read","arguments":{"path":"src/Main.java"},"summary":"Inspect source"}'
            '{"action":"repo.read","arguments":{"path":"src/Main.java"},"summary":"Read source"}'
        )

        parsed = _parse_action(raw)

        self.assertEqual(parsed["action"], "repo.read")
        self.assertEqual(parsed["arguments"], {"path": "src/Main.java"})
        self.assertEqual(parsed["normalization"], "collapsed_2_identical_json_objects")

    def test_actions_with_different_arguments_are_not_collapsed(self) -> None:
        raw = (
            '{"action":"repo.read","arguments":{"path":"src/Main.java"}}'
            '{"action":"repo.read","arguments":{"path":"src/Other.java"}}'
        )

        with self.assertRaisesRegex(HarnessError, "contained 2 JSON actions"):
            _parse_action(raw)

    def test_fullstack_database_sources_are_readable(self) -> None:
        from sdlc_harness.agents.developer.execution import _read_path_allowed

        schema = self.repo / "packages" / "db" / "prisma" / "schema.prisma"
        migration = self.repo / "packages" / "db" / "prisma" / "migrations" / "migration.sql"
        schema.parent.mkdir(parents=True)
        migration.parent.mkdir(parents=True)
        schema.write_text("model Example {}\n", encoding="utf-8")
        migration.write_text("CREATE TABLE example ();\n", encoding="utf-8")

        self.assertTrue(_read_path_allowed(self.repo, schema))
        self.assertTrue(_read_path_allowed(self.repo, migration))

    def test_unit_read_content_stays_in_working_memory_after_recent_window(self) -> None:
        source = "export const value = 42;"
        history = [
            {
                "action": "repo.read",
                "arguments": {"path": "src/Main.ts"},
                "result": {
                    "ok": True,
                    "path": "src/Main.ts",
                    "start_line": 1,
                    "end_line": 1,
                    "content": source,
                },
            },
            *[
                {
                    "action": "repo.list",
                    "arguments": {"prefix": f"src/{index}"},
                    "result": {"ok": True, "files": []},
                }
                for index in range(5)
            ],
        ]

        memory = _action_memory(
            history,
            target_item_paths=["src/Main.ts"],
            max_evidence_chars=1000,
        )

        self.assertEqual(
            memory["inspected_sources"],
            [
                {
                    "path": "src/Main.ts",
                    "start_line": 1,
                    "end_line": 1,
                    "content": source,
                    "verified_at_action": 1,
                }
            ],
        )

    def test_action_memory_soft_caps_ledger_without_dropping_mutation_or_verification_state(self) -> None:
        history = [
            {
                "action": "repo.search",
                "arguments": {"query": f"symbol_{index}"},
                "result": {"ok": True, "matches": []},
                "summary": f"Inspected source group {index}.",
                "raw_completion": "private model reasoning must not enter working memory",
            }
            for index in range(20)
        ]
        history.extend(
            [
                {
                    "action": "repo.write_file",
                    "arguments": {"path": "src/Main.ts"},
                    "result": {"ok": True, "path": "src/Main.ts", "sha256": "sha256:current"},
                    "summary": "Updated the current implementation.",
                },
                {
                    "action": "verification.run_stage",
                    "arguments": {"stage_id": "typecheck"},
                    "result": {"ok": True, "stage_id": "typecheck", "status": "passed"},
                    "summary": "Typecheck passed.",
                },
            ]
        )

        memory = _action_memory(history, target_item_paths=["src/Main.ts"], max_evidence_chars=1000)

        self.assertEqual(memory["projection"]["history_count"], 22)
        self.assertEqual(memory["projection"]["action_ledger_total"], 22)
        self.assertEqual(memory["projection"]["action_ledger_retained"], 16)
        self.assertFalse(memory["projection"]["raw_completion_included"])
        self.assertIn("repo.write_file", [row["action"] for row in memory["action_ledger"]])
        self.assertIn("verification.run_stage", [row["action"] for row in memory["action_ledger"]])
        self.assertEqual(memory["mutation_state"][0]["path"], "src/Main.ts")
        self.assertEqual(memory["verification_state"][0]["result"]["status"], "passed")
        self.assertNotIn("private model reasoning", json.dumps(memory))

    def test_action_memory_invalidates_stale_evidence_conclusion_after_mutation(self) -> None:
        history = [
            {
                "action": "repo.read",
                "arguments": {"path": "src/Main.ts"},
                "result": {"ok": True, "path": "src/Main.ts", "content": "old"},
                "summary": "Old implementation has no guard.",
            },
            {
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.ts"},
                "result": {"ok": True, "path": "src/Main.ts", "sha256": "sha256:new"},
            },
            {
                "action": "repo.read",
                "arguments": {"path": "src/Main.ts"},
                "result": {"ok": True, "path": "src/Main.ts", "content": "new"},
                "summary": "Current implementation contains the guard.",
            },
        ]

        memory = _action_memory(history, target_item_paths=["src/Main.ts"], max_evidence_chars=1000)

        conclusions = memory["evidence_conclusions"]
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["summary"], "Current implementation contains the guard.")
        self.assertNotIn("Old implementation", json.dumps(memory))

    def test_signature_findings_survive_working_memory_and_resume(self) -> None:
        findings = [{"path": "src/Main.ts", "message": "Expected method save(id)."}]
        feedback = _prior_attempt_feedback(
            {
                "actions": [
                    {
                        "completion_gate": {
                            "status": "rejected",
                            "reason": "approved_signature_mismatch",
                            "findings": findings,
                        }
                    }
                ]
            }
        )

        self.assertIsNotNone(feedback)
        memory = _action_memory([feedback], target_item_paths=["src/Main.ts"], max_evidence_chars=1000)
        self.assertEqual(memory["recent_results"][0]["result"]["findings"], findings)

    def test_archived_verification_failure_survives_resume_with_stall_feedback(self) -> None:
        feedback = _prior_attempt_feedback(
            {
                "actions": [],
                "attempt_history": [
                    {
                        "attempt": 1,
                        "actions": [
                            {
                                "tool_action": "verification.run_stage",
                                "tool_result": {
                                    "status": "failed",
                                    "failure_kind": "implementation",
                                    "stage_id": "unit",
                                    "reason": "expected undefined to be true",
                                },
                            },
                            {
                                "tool_action": "repo.write_file",
                                "tool_result": {"ok": False, "error": "no_effect_mutation"},
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(feedback["error"], "previous_verification_repair_stalled")
        self.assertEqual(feedback["failed_stage_id"], "unit")
        self.assertEqual(feedback["previous_no_effect_mutations"], 1)
        self.assertIn("expected undefined", feedback["diagnostic"])

    def test_current_sources_refresh_successful_mutation_content_from_worktree(self) -> None:
        source = self.repo / "src" / "Main.ts"
        source.parent.mkdir(parents=True)
        source.write_text("export const current = 2;\n", encoding="utf-8")
        history = [
            {
                "action": "repo.write_file",
                "arguments": {"path": "src/Main.ts", "content_chars": 26},
                "result": {"ok": True, "path": "src/Main.ts"},
            }
        ]

        current = _current_mutated_sources(
            history,
            target_path=self.repo,
            target_item_paths=["src/Main.ts"],
            max_evidence_chars=1000,
        )

        self.assertEqual(current[0]["path"], "src/Main.ts")
        self.assertEqual(current[0]["content"], "export const current = 2;\n")
        self.assertFalse(current[0]["truncated"])

    def test_action_prompt_hides_stale_inspected_source_after_mutation(self) -> None:
        source = self.repo / "src" / "Main.ts"
        source.parent.mkdir(parents=True)
        source.write_text("export const current = 2;\n", encoding="utf-8")
        history = [
            {
                "action": "read_file",
                "arguments": {"path": "src/Main.ts"},
                "result": {
                    "ok": True,
                    "path": "src/Main.ts",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "export const current = 1;\n",
                },
            },
            {
                "action": "write_file",
                "arguments": {"path": "src/Main.ts"},
                "result": {"ok": True, "path": "src/Main.ts"},
            },
        ]

        prompt = _action_prompt(
            item={"paths": ["src/Main.ts"]},
            manifest={"mode": "act", "base_sha": "base"},
            history=history,
            target_path=self.repo,
            max_write_chars=18000,
            force_incremental=False,
            action_index=3,
            max_actions=24,
            mutated=True,
            context_window_tokens=128000,
        )
        context = json.loads(prompt.split("Execution context:\n", 1)[1])
        memory = context["working_memory"]

        self.assertNotIn("inspected_sources", memory)
        self.assertNotIn("current_sources", memory)
        self.assertEqual(memory["authoritative_sources"][0]["path"], "src/Main.ts")
        self.assertEqual(memory["authoritative_sources"][0]["content"], "export const current = 2;\n")
        self.assertEqual(memory["authoritative_sources"][0]["origin"], "worktree_after_mutation")

    def test_repair_prompt_prioritizes_compact_failure_and_cached_sources(self) -> None:
        source = self.repo / "src" / "Main.ts"
        source.parent.mkdir(parents=True)
        source.write_text("export const current = 1;\n", encoding="utf-8")
        history = [
            {
                "action": "repo.read",
                "arguments": {"path": "src/Main.ts"},
                "result": {
                    "ok": True,
                    "path": "src/Main.ts",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "1: export const current = 1;",
                },
            },
            {
                "action": "verification.run_stage",
                "arguments": {"stage_id": "unit"},
                "result": {
                    "ok": False,
                    "stage_id": "unit",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "Vitest test failure: keeps the contract: Error: Test timed out in 5000ms.",
                    "stage_results": [
                        {
                            "stage_id": "unit",
                            "status": "failed",
                            "stdout": (
                                "src/Main.ts(41,41): error TS2503: Cannot find namespace 'JSX'.\n"
                                + "x" * 5000
                            ),
                            "stderr": "",
                        }
                    ],
                    "diagnostic_fingerprint": "sha256:test",
                    "diagnostic_repeat_count": 1,
                    "selected_test_files": ["src/Main.ts"],
                    "report_ref": "units/WU-001/verification/unit.json",
                },
            },
        ]

        prompt = _action_prompt(
            item={
                "id": "WU-001",
                "title": "Repair API",
                "objective": "Repair the failing unit test.",
                "paths": ["src/Main.ts"],
                "files": [{"path": "src/Main.ts", "design_mappings": [{"large": "noise"}]}],
                "required_stage_ids": ["unit"],
            },
            manifest={"version": "developer_execution_manifest_v8", "mode": "act", "base_sha": "base"},
            history=history,
            target_path=self.repo,
            max_write_chars=18000,
            force_incremental=False,
            action_index=3,
            max_actions=24,
            mutated=False,
            context_window_tokens=128000,
        )
        context = json.loads(prompt.split("Execution context:\n", 1)[1])

        self.assertEqual(context["active_focus"]["phase"], "verification_repair_required")
        self.assertEqual(context["active_failures"][0]["failing_test"], "keeps the contract")
        self.assertIn("src/Main.ts(41,41): error TS2503", context["active_failures"][0]["diagnostic_excerpt"])
        self.assertLessEqual(len(context["active_failures"][0]["diagnostic_excerpt"]), 4000)
        self.assertTrue(context["active_failures"][0]["diagnostic_excerpt"].endswith("...[truncated]"))
        self.assertNotIn("stage_results", context["active_failures"][0])
        self.assertNotIn("files", context["execution_task"])
        self.assertEqual(
            context["working_memory"]["authoritative_sources"][0]["origin"],
            "verified_current_read",
        )
        self.assertEqual(
            context["working_memory"]["authoritative_sources"][0]["verified_at_action"],
            1,
        )
        self.assertNotIn("rules", context["test_authoring_policy"])
        self.assertNotIn(
            "verification.run_stage",
            [row["action"] for row in context["working_memory"]["recent_results"]],
        )
        self.assertEqual(
            context["timeout_diagnostic_protocol"]["status"],
            "instrumentation_required",
        )
        self.assertEqual(
            context["timeout_diagnostic_protocol"]["failure_prefix"],
            "SDLC_DIAGNOSTIC_TIMEOUT:",
        )
        self.assertIn("Do not guess at a production fix", prompt)

    def test_action_prompt_allows_only_verification_after_rejected_no_effect_repair(self) -> None:
        source = self.repo / "src" / "Main.ts"
        source.parent.mkdir(parents=True)
        source.write_text("export const current = 2;\n", encoding="utf-8")
        history = [
            {
                "action": "verification.run_stage",
                "result": {
                    "stage_id": "unit",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "unit failed",
                },
            },
            {"action": "repo.write_file", "result": {"ok": True, "path": "src/Main.ts"}},
            {
                "action": "repo.write_file",
                "result": {"ok": False, "error": "no_effect_mutation", "path": "src/Main.ts"},
            },
            {
                "action": "repo.write_file",
                "result": {"ok": False, "error": "verification_repair_ready", "path": "src/Main.ts"},
            },
        ]

        prompt = _action_prompt(
            item={"id": "WU-001", "paths": ["src/Main.ts"], "required_stage_ids": ["unit"]},
            manifest={"version": "developer_execution_manifest_v8", "mode": "act", "base_sha": "base"},
            history=history,
            target_path=self.repo,
            max_write_chars=18000,
            force_incremental=False,
            action_index=5,
            max_actions=24,
            mutated=True,
            context_window_tokens=128000,
        )
        context = json.loads(prompt.split("Execution context:\n", 1)[1])

        self.assertIn('"action": "verification.run_stage"', prompt)
        self.assertNotIn("- repo.write_file:", prompt)
        self.assertEqual(context["action_budget"]["verification_forced_stage_ids"], ["unit"])

    def test_identical_verification_diagnostics_have_a_stable_repeat_count(self) -> None:
        failed = {
            "stage_id": "typecheck",
            "status": "failed",
            "failure_kind": "implementation",
            "reason": "tsc failed",
            "stage_results": [
                {
                    "stage_id": "typecheck",
                    "status": "failed",
                    "failure_kind": "implementation",
                    "reason": "tsc failed",
                    "stdout": "src/Main.ts(1,1): error TS1: broken",
                    "stderr": "",
                }
            ],
        }
        fingerprint = _verification_diagnostic_fingerprint(failed)
        history = [
            {"action": "verification.run_stage", "result": dict(failed)},
            {"action": "repo.write_file", "result": {"ok": True, "path": "src/Main.ts"}},
            {"action": "verification.run_stage", "result": dict(failed)},
        ]

        self.assertEqual(_diagnostic_repeat_count(history, fingerprint=fingerprint), 2)

    def test_invalid_action_retry_count_resets_after_a_valid_action(self) -> None:
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_MAX_ACTION_RETRIES": "1",
            },
        )
        packet = self.work_packet()
        packet["proposed_files"] = packet["proposed_files"][:1]
        packet["implementation_steps"] = packet["implementation_steps"][:1]
        packet["requirement_coverage_by_file"] = {"src/Main.java": [{"requirement_id": "REQ-1"}]}
        packet["design_mappings_by_file"] = {
            "src/Main.java": [{"design_element_id": "class:Main", "planned_code_target": "src/Main.java"}],
        }
        packet["output_contract"] = {"allowed_paths": ["src/Main.java"]}
        malformed = LlmCompletion(
            content=(
                '{"action":"read_file","arguments":{"path":"src/Main.java"}}\n'
                "Then edit it.\n"
                '{"action":"write_file","arguments":{"path":"src/Main.java","content":"class Main {}"}}'
            ),
            finish_reason="stop",
        )
        llm = ScriptedLlm(
            [
                malformed,
                action("read_file", {"path": "src/Main.java"}),
                malformed,
                action(
                    "write_file",
                    {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                ),
                action("complete", {"summary": "Main implemented"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_invalid_retry_reset",
            work_packet=packet,
            target_path=self.repo,
        )

        self.assertEqual(result.progress["completed_items"], 1)
        self.assertIn("contained 2 JSON actions", llm.prompts[1])
        self.assertIn("Return exactly one JSON action object", llm.prompts[1])

    def test_repair_execution_unit_can_update_any_approved_path(self) -> None:
        main_path = self.repo / "src" / "Main.java"
        main_path.parent.mkdir(parents=True)
        main_path.write_text("class Main {}\n", encoding="utf-8")
        feedback = {
            "feedback_source": "verification",
            "verification": {"stderr": "src/MainTest.java:1: error: missing coverage"},
        }
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Main.java"}),
                action("write_file", {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"}),
                action("complete", {"summary": "test repaired"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_cross_item_recovery",
            work_packet=self.work_packet(),
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        self.assertEqual(result.progress["completed_items"], 1)
        self.assertEqual(main_path.read_text(encoding="utf-8"), "class Main {}\n")
        self.assertTrue((self.repo / "src" / "MainTest.java").exists())

    def test_repair_resume_accepts_persisted_scope_when_only_selection_policy_changed(self) -> None:
        run_id = "run_selection_policy_migration"
        packet = self.work_packet()
        feedback = {
            "feedback_source": "verification",
            "verification": {"stderr": "src/MainTest.java:12: error: cannot call main.run()"},
        }
        session_dir = self.runtime / "runs" / run_id / "developer_execution" / "implementation_repair_v1"
        session_dir.mkdir(parents=True)
        current = _build_manifest(
            run_id=run_id,
            work_packet=packet,
            target_path=self.repo,
            mode="implementation_repair",
            repair_feedback=feedback,
            repair_iteration=1,
        )
        persisted = json.loads(json.dumps(current))
        persisted["version"] = "developer_execution_manifest_v4"
        persisted["input_hash"] = "sha256:legacy-v4-selection-policy"
        persisted["items"][0]["context_paths"] = ["src/MainTest.java"]
        persisted["items"][0]["repair_closure"]["context_paths"] = ["src/MainTest.java"]
        self.assertNotEqual(persisted["input_hash"], current["input_hash"])
        (session_dir / "manifest.json").write_text(json.dumps(persisted), encoding="utf-8")
        (session_dir / "journal.json").write_text(
            json.dumps(_new_journal(run_id=run_id, manifest=persisted, mode="implementation_repair", repair_iteration=1)),
            encoding="utf-8",
        )
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Main.java"}),
                action("write_file", {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"}),
                action("complete", {"summary": "test repaired"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id=run_id,
            work_packet=packet,
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        self.assertEqual(result.progress["completed_items"], 1)
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["migrations"][0]["code"], "repair_selection_policy_migrated")
        self.assertEqual(journal["migrations"][0]["current_item_paths"], ["src/Main.java", "src/MainTest.java"])

    def test_repair_resume_still_blocks_real_approved_input_drift(self) -> None:
        run_id = "run_real_input_drift"
        packet = self.work_packet()
        feedback = {
            "feedback_source": "verification",
            "verification": {"stderr": "src/MainTest.java:12: error: missing coverage"},
        }
        session_dir = self.runtime / "runs" / run_id / "developer_execution" / "implementation_repair_v1"
        session_dir.mkdir(parents=True)
        manifest = _build_manifest(
            run_id=run_id,
            work_packet=packet,
            target_path=self.repo,
            mode="implementation_repair",
            repair_feedback=feedback,
            repair_iteration=1,
        )
        (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        changed_packet = self.work_packet()
        changed_packet["approved_plan"] = {"summary": "Changed after approval."}

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(ScriptedLlm([])).execute(
                run_id=run_id,
                work_packet=changed_packet,
                target_path=self.repo,
                repair_feedback=feedback,
                repair_iteration=1,
            )

        self.assertEqual(caught.exception.error_code, "developer_input_drift")
        self.assertFalse(caught.exception.retriable)

    def test_context_only_manifest_upgrade_preserves_write_baselines(self) -> None:
        packet = self.unit_packet()
        existing = _build_manifest(
            run_id="run_context_upgrade",
            work_packet=packet,
            target_path=self.repo,
            mode="act",
            repair_feedback=None,
            repair_iteration=0,
        )
        candidate_packet = self.unit_packet()
        candidate_packet["execution_units"][1]["context_paths"] = [
            "src/MainTest.java",
            "src/Main.java",
        ]
        candidate_packet["execution_units"][1]["contract_anchor_paths"] = ["src/Main.java"]
        candidate_packet["execution_units"][1]["input_digest"] = "sha256:unit-2-context"
        candidate = _build_manifest(
            run_id="run_context_upgrade",
            work_packet=candidate_packet,
            target_path=self.repo,
            mode="act",
            repair_feedback=None,
            repair_iteration=0,
        )

        merged = _merge_context_only_manifest(existing, candidate)

        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["input_hash"], candidate["input_hash"])
        self.assertEqual(merged["items"][1]["context_paths"], ["src/MainTest.java", "src/Main.java"])
        self.assertEqual(merged["items"][1]["contract_anchor_paths"], ["src/Main.java"])
        self.assertEqual(
            merged["items"][0]["files"][0]["initial_sha256"],
            existing["items"][0]["files"][0]["initial_sha256"],
        )
        changed = json.loads(json.dumps(candidate))
        changed["items"][1]["paths"] = ["src/Other.java"]
        self.assertIsNone(_merge_context_only_manifest(existing, changed))

    def test_database_entrypoint_scope_upgrade_reopens_only_completed_provider(self) -> None:
        barrel = "packages/db/src/index.ts"
        query = "packages/db/src/query.ts"
        for path, content in ((barrel, "export {}\n"), (query, "export const query = 1\n")):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        packet = self.unit_packet()
        packet["proposed_files"] = [
            {"path": barrel, "operation": "update", "purpose": "Publish DB exports."},
            {"path": query, "operation": "update", "purpose": "Implement query."},
        ]
        packet["implementation_steps"] = [
            {"order": 1, "title": "DB base", "description": "Base export.", "files": [barrel]},
            {"order": 2, "title": "DB query", "description": "Query provider.", "files": [query]},
        ]
        packet["requirement_coverage_by_file"] = {
            barrel: [{"requirement_id": "REQ-1"}],
            query: [{"requirement_id": "REQ-1"}],
        }
        packet["design_mappings_by_file"] = {barrel: [], query: []}
        packet["output_contract"] = {"allowed_paths": [barrel, query]}
        packet["execution_units"] = [
            {
                "unit_id": "WU-DB-BASE",
                "order": 1,
                "title": "DB base",
                "objective": "Publish base exports.",
                "component_id": "database",
                "surfaces": ["data"],
                "depends_on": [],
                "write_paths": [barrel],
                "requirement_refs": ["REQ-1"],
                "acceptance_refs": ["AC-1"],
                "design_node_ids": [],
                "planned_code_targets": [{"path": barrel, "action": "create_or_update"}],
                "required_stage_ids": [],
                "test_target_paths": [],
                "input_digest": "sha256:db-base",
            },
            {
                "unit_id": "WU-DB-QUERY",
                "order": 2,
                "title": "DB query",
                "objective": "Implement the query provider.",
                "component_id": "database",
                "surfaces": ["data"],
                "depends_on": ["WU-DB-BASE"],
                "write_paths": [query],
                "requirement_refs": ["REQ-1"],
                "acceptance_refs": ["AC-1"],
                "design_node_ids": [],
                "planned_code_targets": [{"path": query, "action": "create_or_update"}],
                "required_stage_ids": [],
                "test_target_paths": [],
                "input_digest": "sha256:db-query",
            },
        ]
        existing = _build_manifest(
            run_id="run_db_scope",
            work_packet=packet,
            target_path=self.repo,
            mode="act",
            repair_feedback=None,
            repair_iteration=0,
        )
        candidate_packet = json.loads(json.dumps(packet))
        candidate_unit = candidate_packet["execution_units"][1]
        candidate_unit["write_paths"].append(barrel)
        candidate_unit["planned_code_targets"].append(
            {"path": barrel, "action": "create_or_update"}
        )
        candidate_unit["input_digest"] = "sha256:db-query-with-entrypoint"
        candidate = _build_manifest(
            run_id="run_db_scope",
            work_packet=candidate_packet,
            target_path=self.repo,
            mode="act",
            repair_feedback=None,
            repair_iteration=0,
        )

        upgrade = _merge_database_entrypoint_scope_manifest(existing, candidate)

        self.assertIsNotNone(upgrade)
        assert upgrade is not None
        merged, reopened = upgrade
        self.assertEqual(reopened, ["WU-DB-QUERY"])
        self.assertEqual(merged["items"][0], existing["items"][0])
        self.assertIn(barrel, merged["items"][1]["paths"])

        journal = _new_journal(
            run_id="run_db_scope",
            manifest=existing,
            mode="act",
            repair_iteration=0,
        )
        completed = journal["items"][1]
        completed.update(
            {
                "status": "completed",
                "completed_at": "2026-10-30T00:00:00Z",
                "summary": "Query implemented.",
                "after_sha256_by_path": {query: "old-query-hash"},
                "mutated_paths": [query],
            }
        )
        self.assertEqual(
            _database_entrypoint_journal_reopen_ids(manifest=merged, journal=journal),
            ["WU-DB-QUERY"],
        )
        session_dir = self.store.run_dir("run_db_scope") / "developer_execution" / "act_v1"
        snapshot = session_dir / "snapshots" / "WU-DB-QUERY.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps({"files": []}), encoding="utf-8")
        completion = session_dir / "units" / "WU-DB-QUERY" / "completion.json"
        completion.parent.mkdir(parents=True, exist_ok=True)
        completion.write_text(json.dumps({"unit_id": "WU-DB-QUERY"}), encoding="utf-8")

        _reopen_database_entrypoint_items(
            target_path=self.repo,
            session_dir=session_dir,
            manifest=merged,
            journal=journal,
            item_ids=reopened,
        )

        reopened_entry = journal["items"][1]
        self.assertEqual(reopened_entry["status"], "pending")
        self.assertEqual(reopened_entry["scope_reopen_history"][0]["summary"], "Query implemented.")
        self.assertEqual(journal["items"][0]["status"], "pending")
        self.assertTrue(
            (session_dir / "units" / "WU-DB-QUERY" / "completion_before_scope_reopen_1.json").is_file()
        )
        refreshed_snapshot = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual({row["path"] for row in refreshed_snapshot["files"]}, {barrel, query})
        journal["items"][0].update(
            {
                "status": "completed",
                "after_sha256_by_path": {barrel: "stale-provider-hash"},
            }
        )
        self.executor(ScriptedLlm([]))._validate_workspace(
            run_id="run_db_scope",
            target_path=self.repo,
            manifest=merged,
            journal=journal,
            manifest_path="manifest.json",
            journal_path="journal.json",
        )

    def test_no_change_completion_is_rejected_outside_repair_mode(self) -> None:
        packet = self.work_packet()
        packet["proposed_files"] = packet["proposed_files"][:1]
        packet["implementation_steps"] = packet["implementation_steps"][:1]
        packet["requirement_coverage_by_file"] = {"src/Main.java": [{"requirement_id": "REQ-1"}]}
        packet["design_mappings_by_file"] = {
            "src/Main.java": [{"design_element_id": "class:Main", "planned_code_target": "src/Main.java"}],
        }
        packet["output_contract"] = {"allowed_paths": ["src/Main.java"]}
        target = self.repo / "src" / "Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Main.java"}),
                action("complete", {"summary": "skip", "no_change_required": True}),
                action(
                    "write_file",
                    {
                        "path": "src/Main.java",
                        "expected_sha256": expected_hash,
                        "content": "class Main { int value; }",
                    },
                ),
                action("complete", {"summary": "implemented"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_act_requires_mutation",
            work_packet=packet,
            target_path=self.repo,
        )

        self.assertFalse(result.files[0]["no_change_required"])
        self.assertIn("successful mutation", llm.prompts[2])
        self.assertIn("int value", target.read_text(encoding="utf-8"))

    def test_repair_scopes_use_separate_durable_sessions(self) -> None:
        packet = self.work_packet()
        implementation = self.executor(
            ScriptedLlm(
                [
                    action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                    action("complete", {"summary": "implementation repair"}),
                ]
            )
        ).execute(
            run_id="run_repair_scopes",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback={"path": "src/Main.java", "message": "fix implementation"},
            repair_iteration=1,
            repair_scope="implementation",
        )
        qa = self.executor(
            ScriptedLlm(
                [
                    action(
                        "write_file",
                        {
                            "path": "src/MainTest.java",
                            "expected_sha256": "",
                            "content": "class MainTest {}",
                        },
                    ),
                    action("complete", {"summary": "qa repair"}),
                ]
            )
        ).execute(
            run_id="run_repair_scopes",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback={"path": "src/MainTest.java", "message": "fix qa"},
            repair_iteration=1,
            repair_scope="qa",
        )

        self.assertIn("implementation_repair_v1", implementation.manifest_path)
        self.assertIn("qa_repair_v1", qa.manifest_path)
        self.assertNotEqual(implementation.manifest_path, qa.manifest_path)

    def test_repair_session_uses_cumulative_ordinal_without_changing_logical_iteration(self) -> None:
        result = self.executor(
            ScriptedLlm(
                [
                    action(
                        "write_file",
                        {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"},
                    ),
                    action("complete", {"summary": "new repair cycle"}),
                ]
            )
        ).execute(
            run_id="run_repair_cycle_two",
            work_packet=self.work_packet(),
            target_path=self.repo,
            repair_feedback={"path": "src/Main.java", "message": "fix implementation"},
            repair_iteration=1,
            session_iteration=5,
        )

        self.assertIn("implementation_repair_v5", result.manifest_path)
        manifest = json.loads((self.root / result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["repair_iteration"], 1)

    def test_verification_feedback_does_not_treat_incidental_type_mentions_as_repair_targets(self) -> None:
        packet = self.work_packet()
        selected = _selected_repair_paths(
            packet,
            {
                "feedback_source": "verification",
                "verification": {
                    "stderr": ".\\SRC\\MAINTEST.java:12: error: cannot call main.run()",
                },
            },
        )

        self.assertEqual(selected, {"src/MainTest.java"})

    def test_verification_feedback_includes_high_signal_compiler_owner_symbols(self) -> None:
        selected = _selected_repair_paths(
            self.work_packet(),
            {
                "feedback_source": "verification",
                "verification": {
                    "stderr": ".\\src\\MainTest.java:12: error: cannot inherit from final Main",
                },
            },
        )

        self.assertEqual(selected, {"src/Main.java", "src/MainTest.java"})

    def test_blocked_repair_scope_mismatch_has_safe_resume_migration(self) -> None:
        state = {
            "status": "blocked",
            "current_phase": "implementation_repair",
            "last_error": {"error_code": "developer_act_out_of_scope"},
            "developer_execution": {"mode": "implementation_repair"},
            "latest_artifacts": {
                "implementation_repair_feedback": "feedback.json",
                "developer_repair_execution_manifest": "manifest.json",
                "developer_repair_execution_journal": "journal.json",
            },
        }

        self.assertTrue(developer_execution_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_input_drift"}
        self.assertTrue(developer_execution_retry_allowed(state))
        state["latest_artifacts"].pop("developer_repair_execution_journal")
        self.assertFalse(developer_execution_retry_allowed(state))

    def test_blocked_initial_developer_scope_mismatch_can_resume_same_session(self) -> None:
        state = {
            "status": "blocked",
            "current_phase": "implementation_writing",
            "last_error": {"error_code": "developer_act_out_of_scope"},
            "developer_execution": {"mode": "developer_act"},
            "latest_artifacts": {
                "developer_work_packet": "packet.json",
                "developer_execution_manifest": "manifest.json",
                "developer_execution_journal": "journal.json",
            },
        }

        self.assertTrue(developer_execution_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_step_review_drift"}
        self.assertTrue(developer_execution_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_action_limit_exceeded"}
        self.assertTrue(developer_execution_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_repair_budget_exhausted"}
        self.assertTrue(developer_execution_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_repair_stalled"}
        self.assertTrue(developer_execution_retry_allowed(state))
        state["latest_artifacts"].pop("developer_execution_journal")
        self.assertFalse(developer_execution_retry_allowed(state))

    def test_repair_prompt_contains_structured_verification_feedback(self) -> None:
        feedback = {
            "feedback_source": "verification",
            "verification": {"stderr": "src/Main.java:1: error: broken contract"},
        }
        llm = ScriptedLlm(
            [
                action("write_file", {"path": "src/Main.java", "expected_sha256": "", "content": "class Main {}"}),
                action("complete", {"summary": "verification repair"}),
                action("write_file", {"path": "src/MainTest.java", "expected_sha256": "", "content": "class MainTest {}"}),
                action("complete", {"summary": "related test repair"}),
            ]
        )

        self.executor(llm).execute(
            run_id="run_verification_feedback",
            work_packet=self.work_packet(),
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        self.assertIn("broken contract", llm.prompts[0])
        manifest = json.loads(
            (
                self.runtime
                / "runs"
                / "run_verification_feedback"
                / "developer_execution"
                / "implementation_repair_v1"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["repair_feedback"]["feedback_source"], "verification")

    def test_manifest_purpose_falls_back_to_proposed_file_reason(self) -> None:
        packet = self.one_file_packet()
        packet["proposed_files"] = [
            {"path": "src/Main.java", "operation": "update", "reason": "Repair the compile failure."},
        ]

        manifest = _build_manifest(
            run_id="run_reason_fallback",
            work_packet=packet,
            target_path=self.repo,
            mode="implementation_repair",
            repair_feedback={"feedback_source": "verification", "verification": {"stderr": "src/Main.java"}},
            repair_iteration=1,
        )

        self.assertEqual(manifest["items"][0]["files"][0]["purpose"], "Repair the compile failure.")

    def test_verification_repair_uses_dependencies_as_context_without_forcing_mutation(self) -> None:
        paths = ["src/EditFormView.java", "src/Controller.java", "src/FlowTest.java", "src/Unrelated.java"]
        packet = self.work_packet()
        packet["output_contract"] = {"allowed_paths": paths}
        packet["proposed_files"] = [{"path": path, "operation": "update", "purpose": "repair"} for path in paths]
        packet["implementation_steps"] = [{"order": 1, "files": paths, "description": "repair contract"}]
        packet["requirement_coverage_by_file"] = {path: [{"requirement_id": "REQ-1"}] for path in paths}
        packet["design_mappings_by_file"] = {path: [] for path in paths}
        contents = {
            "src/EditFormView.java": "interface EditFormView { void showConfirmation(); }\n",
            "src/Controller.java": "class Controller { void run(EditFormView view) { view.showConfirmation(); } }\n",
            "src/FlowTest.java": "class FlowTest { class FakeView implements EditFormView { public void showConfirmation(Object value) {} } }\n",
            "src/Unrelated.java": "class Unrelated {}\n",
        }
        for path, content in contents.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        interface_before = (self.repo / "src/EditFormView.java").read_text(encoding="utf-8")
        controller_before = (self.repo / "src/Controller.java").read_text(encoding="utf-8")
        test_hash = hashlib.sha256((self.repo / "src/FlowTest.java").read_bytes()).hexdigest()
        feedback = {
            "feedback_source": "verification",
            "repair_objective": {
                "objective_id": "repair:contract",
                "source": "validator_code_trace",
                "status": "open",
                "canonical_keys": ["contract:showConfirmation"],
                "contract_findings": [
                    {
                        "canonical_key": "contract:showConfirmation",
                        "code_ref": "src/EditFormView.java#showConfirmation",
                        "expected_signature": {"name": "showConfirmation", "parameters": [], "return_type": "void"},
                    }
                ],
            },
            "verification": {
                "stderr": "src/FlowTest.java:1: error: FakeView does not override showConfirmation() in EditFormView"
            },
        }
        llm = ScriptedLlm(
            [
                action(
                    "write_file",
                    {
                        "path": "src/FlowTest.java",
                        "expected_sha256": test_hash,
                        "content": "class FlowTest { class FakeView implements EditFormView { public void showConfirmation() {} } }",
                    },
                ),
                action("complete", {"summary": "updated only the failing test double"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_contract_context",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        manifest = json.loads((self.root / result.manifest_path).read_text(encoding="utf-8"))
        item = manifest["items"][0]
        self.assertEqual(set(item["context_paths"]), set(contents))
        self.assertEqual(item["direct_repair_paths"], ["src/FlowTest.java"])
        self.assertEqual(
            set(item["diagnostic_paths"]),
            {"src/EditFormView.java", "src/Controller.java", "src/FlowTest.java"},
        )
        self.assertEqual(item["contract_anchor_paths"], ["src/EditFormView.java"])
        self.assertIn("src/Unrelated.java", item["context_paths"])
        self.assertNotIn("src/Unrelated.java", item["repair_closure"]["repair_hints"]["path_reasons"])
        self.assertEqual((self.repo / "src/EditFormView.java").read_text(encoding="utf-8"), interface_before)
        self.assertEqual((self.repo / "src/Controller.java").read_text(encoding="utf-8"), controller_before)
        self.assertEqual([row["path"] for row in result.files], ["src/FlowTest.java"])
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(len(journal["items"][0]["actions"]), 2)
        self.assertEqual(journal["items"][0]["unresolved_paths"], [])

    def test_verification_repair_can_modify_related_approved_file_outside_diagnostic_paths(self) -> None:
        packet = self.one_file_packet()
        packet["proposed_files"].append({"path": "src/Caller.java", "purpose": "Call Main"})
        packet["output_contract"]["allowed_paths"].append("src/Caller.java")
        main = self.repo / "src/Main.java"
        caller = self.repo / "src/Caller.java"
        main.parent.mkdir(parents=True)
        main.write_text("class Main { void run() {} }\n", encoding="utf-8")
        caller.write_text("class Caller { void call() { new Main().oldRun(); } }\n", encoding="utf-8")
        caller_hash = hashlib.sha256(caller.read_bytes()).hexdigest()
        feedback = {
            "feedback_source": "verification",
            "verification": {"stderr": "src/Main.java:1: error: cannot find symbol oldRun"},
        }
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Caller.java"}),
                action(
                    "write_file",
                    {
                        "path": "src/Caller.java",
                        "expected_sha256": caller_hash,
                        "content": "class Caller { void call() { new Main().run(); } }",
                    },
                ),
                action("complete", {"summary": "aligned the related caller"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_related_approved_path",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        manifest = json.loads((self.root / result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "developer_execution_manifest_v7")
        self.assertIn("src/Caller.java", manifest["items"][0]["context_paths"])
        self.assertIn("new Main().run()", caller.read_text(encoding="utf-8"))
        self.assertIn("workspace_allowed_paths", llm.prompts[0])

    def test_profiled_repair_context_discovers_related_typescript_source_by_symbol(self) -> None:
        paths = ["src/Handler.ts", "src/Adapter.ts", "config/schema.json"]
        packet = self.work_packet()
        packet["output_contract"] = {
            "allowed_paths": paths,
            "path_policy": {"language_profile_id": "typescript_v1"},
        }
        contents = {
            "src/Handler.ts": "export function handle(value: string) { return adapt(value); }\n",
            "src/Adapter.ts": "export function adapt(value: string): string { return value; }\n",
            "config/schema.json": '{"description":"adapt must remain stable"}\n',
        }
        for path, content in contents.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        closure = build_repair_closure(
            work_packet=packet,
            feedback={
                "feedback_source": "verification",
                "verification": {"stderr": "src/Handler.ts:1: cannot find name adapt"},
                "repair_objective": {
                    "contract_findings": [
                        {
                            "code_ref": "src/Handler.ts#handle",
                            "expected_signature": {"name": "adapt"},
                        }
                    ]
                },
            },
            target_path=self.repo,
        )

        self.assertEqual(closure["context_paths"], paths)
        self.assertIn("src/Adapter.ts", closure["diagnostic_paths"])
        self.assertIn("symbol_reference", closure["path_reasons"]["src/Adapter.ts"])
        self.assertNotIn("config/schema.json", closure["diagnostic_paths"])


    def test_same_hash_write_is_not_counted_as_repair_progress(self) -> None:
        packet = self.one_file_packet()
        target = self.repo / "src/Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        initial_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        feedback = {"feedback_source": "verification", "verification": {"stderr": "src/Main.java:1: error"}}
        llm = ScriptedLlm(
            [
                action(
                    "write_file",
                    {"path": "src/Main.java", "expected_sha256": initial_hash, "content": "class Main {}"},
                ),
                action(
                    "write_file",
                    {"path": "src/Main.java", "expected_sha256": initial_hash, "content": "class Main { int value; }"},
                ),
                action("complete", {"summary": "real change applied"}),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_no_effect_write",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback=feedback,
            repair_iteration=1,
        )

        self.assertIn("no_effect_mutation", llm.prompts[1])
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["items"][0]["no_effect_actions"], 1)
        self.assertIn("int value", target.read_text(encoding="utf-8"))

    def test_three_identical_no_effect_writes_stop_the_loop(self) -> None:
        packet = self.one_file_packet()
        target = self.repo / "src/Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        initial_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        repeated = action(
            "write_file",
            {"path": "src/Main.java", "expected_sha256": initial_hash, "content": "class Main {}"},
        )
        llm = ScriptedLlm([repeated, repeated, repeated])

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(llm).execute(
                run_id="run_repeated_no_effect_write",
                work_packet=packet,
                target_path=self.repo,
                repair_feedback={"feedback_source": "verification"},
                repair_iteration=1,
            )

        self.assertEqual(caught.exception.error_code, "developer_repair_stalled")
        self.assertEqual(len(llm.prompts), 3)

    def test_three_duplicate_reads_stop_even_when_the_model_adds_the_full_range(self) -> None:
        packet = self.one_file_packet()
        target = self.repo / "src" / "Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        repeated = action(
            "read_file",
            {"path": "src/Main.java", "start_line": 1, "end_line": 1},
        )
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Main.java"}),
                repeated,
                repeated,
                repeated,
            ]
        )

        with self.assertRaises(DeveloperExecutionError) as caught:
            self.executor(llm).execute(
                run_id="run_repeated_observation",
                work_packet=packet,
                target_path=self.repo,
            )

        self.assertEqual(caught.exception.error_code, "developer_repair_stalled")
        self.assertEqual(len(llm.prompts), 4)
        self.assertIn("authoritative_sources", llm.prompts[-1])

    def test_repair_can_keep_an_inspected_path_with_an_explicit_reason(self) -> None:
        packet = self.one_file_packet()
        target = self.repo / "src/Main.java"
        target.parent.mkdir(parents=True)
        target.write_text("class Main {}\n", encoding="utf-8")
        llm = ScriptedLlm(
            [
                action("read_file", {"path": "src/Main.java"}),
                action(
                    "complete",
                    {
                        "summary": "diagnostic is stale",
                        "no_change_required": True,
                        "unchanged_paths": [
                            {"path": "src/Main.java", "reason": "The reported symbol already exists."}
                        ],
                    },
                ),
            ]
        )

        result = self.executor(llm).execute(
            run_id="run_explicit_unchanged",
            work_packet=packet,
            target_path=self.repo,
            repair_feedback={"feedback_source": "verification", "verification": {"stderr": "src/Main.java"}},
            repair_iteration=1,
        )

        self.assertEqual(result.files, [])
        journal = json.loads((self.root / result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["items"][0]["unchanged_paths"], ["src/Main.java"])
        self.assertTrue(journal["items"][0]["no_change_required"])


if __name__ == "__main__":
    unittest.main()
