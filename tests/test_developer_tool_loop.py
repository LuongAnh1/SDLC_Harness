"""Kiểm thử tập trung cho vòng action Developer bền vững của Phase 4."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from sdlc_harness.agents.developer.execution import (
    DeveloperExecutionError,
    DeveloperToolLoopExecutor,
    _build_manifest,
    _new_journal,
    _parse_action,
    _selected_repair_paths,
    repair_scope_mismatch_retry_allowed,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.storage.runtime import RuntimeStore


class ScriptedLlm:
    def __init__(self, responses: list[LlmCompletion]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.responses:
            raise AssertionError("Scripted LLM ran out of responses")
        return self.responses.pop(0)


def action(name: str, arguments: dict[str, object], *, finish_reason: str = "stop") -> LlmCompletion:
    return LlmCompletion(
        content=json.dumps({"action": name, "arguments": arguments}),
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
                "SDLC_DEVELOPER_EXECUTION_MODE": "tool_loop",
                "SDLC_DEVELOPER_MAX_ACTION_RETRIES": "0",
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def work_packet(self) -> dict[str, object]:
        paths = ["src/Main.java", "src/MainTest.java"]
        return {
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

    def executor(self, llm: ScriptedLlm) -> DeveloperToolLoopExecutor:
        return DeveloperToolLoopExecutor(settings=self.settings, store=self.store, llm_client=llm)

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

    def test_write_action_cannot_escape_current_work_item(self) -> None:
        llm = ScriptedLlm(
            [action("write_file", {"path": "../escape.java", "expected_sha256": "", "content": "bad"})]
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

    def test_invalid_action_retry_count_resets_after_a_valid_action(self) -> None:
        self.settings = load_settings(
            root=self.root,
            runtime_root=self.runtime,
            overrides={
                "SDLC_LLM_MODE": "deepseek",
                "SDLC_LLM_API_KEY": "test-only",
                "SDLC_DEVELOPER_EXECUTION_MODE": "tool_loop",
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

        self.assertTrue(repair_scope_mismatch_retry_allowed(state))
        state["last_error"] = {"error_code": "developer_input_drift"}
        self.assertTrue(repair_scope_mismatch_retry_allowed(state))
        state["latest_artifacts"].pop("developer_repair_execution_journal")
        self.assertFalse(repair_scope_mismatch_retry_allowed(state))

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
        self.assertEqual(manifest["version"], "developer_execution_manifest_v5")
        self.assertIn("src/Caller.java", manifest["items"][0]["context_paths"])
        self.assertIn("new Main().run()", caller.read_text(encoding="utf-8"))
        self.assertIn("workspace_allowed_paths", llm.prompts[0])


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
