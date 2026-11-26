"""Kiểm thử creation/adoption/cleanup và isolation Git worktree bằng remote cục bộ.

Suite khóa các bất biến của Slice 4.9A2/A3 và 4.9B: mỗi run có workspace riêng,
base SHA lấy sau khi fetch remote, identity được xác minh fail-closed, hai run
không ghi chéo file và rollback không xóa workspace nếu không chứng minh được nó
còn nguyên trạng thái mới tạo/nhận nuôi. Cleanup dùng PR giả và không chạm GitHub thật.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

from sdlc_harness.application import ApplicationCommand, ApplicationService
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.fake import fake_requirement_model_v2
from sdlc_harness.sources.fixtures import load_use_case
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.agents.requirements.trace_v1 import use_case_segments
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.profiles import build_profile_snapshot, get_language_profile, get_project_profile
from sdlc_harness.repo_context import CodeGraphContextProvider
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workspaces import RunWorkspaceManager, branch_name_for_run
from tests._support import GitRepositoryTemplate


pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _write_profile_target(root: Path) -> None:
    project = get_project_profile("typescript_fullstack_v1")
    language = get_language_profile(project.language_profile_id)
    for required_path in project.required_paths:
        path = root / required_path
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("placeholder\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    for component in project.components:
        for editable_root in component.editable_roots:
            (root / editable_root).mkdir(parents=True, exist_ok=True)
            (root / editable_root / ".gitkeep").write_text("", encoding="utf-8")
    payload = {
        "version": project.target_manifest_schema,
        "project_profile_id": project.profile_id,
        "project_profile_schema": project.schema_version,
        "language_profile_id": language.profile_id,
        "language_profile_schema": language.schema_version,
        "components": [
            {"component_id": component.component_id, "editable_roots": list(component.editable_roots)}
            for component in project.components
        ],
        "protected_paths": list(project.protected_paths),
        "required_paths": list(project.required_paths),
        "verification_stage_ids": list(project.verification_stage_ids),
    }
    manifest = root / ".sdlc" / "target-profile.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _populate_workspace_target(root: Path) -> None:
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    _write_profile_target(root)


class WorkspaceGitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.git_template = GitRepositoryTemplate(_populate_workspace_target)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.git_template.close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.harness_root = self.base / "SDLC_Harness"
        self.harness_root.mkdir()
        topology = self.git_template.materialize(
            self.base,
            canonical_name="target",
            include_seed=True,
        )
        self.remote = topology.remote
        if topology.seed is None:
            raise AssertionError("Git template did not create the requested seed repository.")
        self.seed = topology.seed
        self.canonical = topology.canonical
        self.worktree_root = self.base / "target-worktrees"
        self.runtime_root = self.harness_root / ".runtime"

        self.settings = self._settings(remote=str(self.remote.resolve()))
        self.store = RuntimeStore(repo_root=self.harness_root, runtime_root=self.runtime_root)
        self.manager = RunWorkspaceManager(self.settings)
        self.run_id = "run_20261015_120000_a1b2c3d4"
        self._write_state(self.run_id)

    def _settings(self, *, remote: str):
        return load_settings(
            root=self.harness_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_TARGET_REPO_PATH": str(self.canonical),
                "SDLC_TARGET_WORKTREE_ROOT": str(self.worktree_root),
                "SDLC_TARGET_REPO_REMOTE": remote,
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_IMPLEMENTATION_BRANCH_PREFIX": "feature",
            },
        )

    def _write_state(
        self,
        run_id: str,
        *,
        source_ref: str = "dataset_use_case:etour:UC1",
        profiled: bool = False,
    ) -> None:
        state: dict[str, object] = {
                "run_id": run_id,
                "source_ref": source_ref,
                "dataset": "etour",
                "use_case": "UC1",
                "status": "active",
                "latest_artifacts": {},
        }
        if profiled:
            state["profile_snapshot"] = build_profile_snapshot(
                project_profile_id="typescript_fullstack_v1",
                target_remote=str(self.remote.resolve()),
                target_base_branch="main",
            ).to_dict()
        self.store.write_state(state)

    def _push_remote_change(self) -> str:
        feature = self.seed / "feature.txt"
        feature.write_text("new remote base\n", encoding="utf-8")
        _git(self.seed, "add", feature.name)
        _git(self.seed, "commit", "-m", "advance remote base")
        _git(self.seed, "push", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def _prepare_legacy_run(self) -> tuple[str, str, dict[str, object]]:
        branch = "feature/sdlc-1-implementation"
        _git(self.canonical, "checkout", "-b", branch)
        (self.canonical / "legacy.txt").write_text("legacy code\n", encoding="utf-8")
        _git(self.canonical, "add", "legacy.txt")
        _git(self.canonical, "commit", "-m", "legacy implementation")
        _git(self.canonical, "push", "-u", "origin", branch)
        head_sha = _git(self.canonical, "rev-parse", "HEAD")
        base_sha = _git(self.canonical, "rev-parse", "origin/main")
        publication = {
            "version": "github_code_review_v1",
            "status": "published",
            "repository": "example/target",
            "remote": str(self.remote.resolve()),
            "base_branch": "main",
            "branch": branch,
            "issue_key": "SDLC-1",
            "run_id": self.run_id,
            "commit": {"sha": head_sha},
            "push": {"status": "pushed", "branch": branch},
            "pull_request": {
                "number": 1,
                "url": "https://github.com/example/target/pull/1",
                "isDraft": True,
                "headRefName": branch,
                "baseRefName": "main",
            },
        }
        sync_dir = self.store.run_dir(self.run_id) / "sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        publication_path = sync_dir / "github_code_review_v1.json"
        publication_path.write_text(json.dumps(publication), encoding="utf-8")
        self.store.write_state(
            {
                "ok": True,
                "run_id": self.run_id,
                "source_ref": "jira_issue:SDLC-1",
                "current_phase": "code_review",
                "status": "code_review_ready",
                "latest_artifacts": {"github_code_review": self.store.relative(publication_path)},
                "pending_checkpoint": {"type": "code_review"},
            }
        )
        pull_request = {
            "number": 1,
            "url": "https://github.com/example/target/pull/1",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": branch,
            "headRefOid": head_sha,
            "baseRefName": "main",
            "baseRefOid": base_sha,
        }
        return branch, head_sha, pull_request

    def _publish_workspace(self) -> tuple[object, dict[str, object]]:
        record = self.manager.create_for_run(self.run_id)
        with self.manager.locked_workspace(self.run_id) as locked:
            worktree = Path(locked.worktree_path)
            (worktree / "generated.txt").write_text("generated\n", encoding="utf-8")
            _git(worktree, "add", "generated.txt")
            _git(worktree, "commit", "-m", "generated by run")
            _git(worktree, "push", "-u", "origin", locked.branch)
            record = self.manager.refresh_locked(
                locked,
                pr_number=17,
                pr_url="https://github.com/example/target/pull/17",
            )
        pull_request = {
            "number": 17,
            "url": "https://github.com/example/target/pull/17",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": record.branch,
            "headRefOid": record.head_sha,
            "baseRefName": "main",
            "baseRefOid": record.base_sha,
            "mergedAt": None,
            "mergeCommit": None,
        }
        return record, pull_request

    def _merge_published_workspace(self, record: object, pull_request: dict[str, object]) -> None:
        _git(self.seed, "fetch", "origin")
        _git(self.seed, "merge", "--no-ff", f"origin/{record.branch}", "-m", "merge run")
        _git(self.seed, "push", "origin", "main")
        pull_request.update(
            {
                "state": "MERGED",
                "isDraft": False,
                "mergedAt": "2026-10-15T08:00:00Z",
                "mergeCommit": {"oid": _git(self.seed, "rev-parse", "HEAD")},
            }
        )

    def test_phase1_opencode_markdown_requirements_reach_a_trace_checkpoint(self) -> None:
        settings = dataclass_replace(self.settings, llm_mode="deepseek")
        workflow = PhaseOneWorkflow(settings)
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=Path(__file__).resolve().parents[1])
        with (
            patch("sdlc_harness.workflows.phase1.load_use_case", return_value=source),
            patch.object(RunWorkspaceManager, "create_for_run", side_effect=HarnessError("workspace_fetch_failed", "offline")),
            patch("sdlc_harness.llm.opencode._find_opencode") as find_opencode,
        ):
            paused = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        self.assertEqual(paused["status"], "requirement_paused")
        find_opencode.assert_not_called()
        run_id = paused["run_id"]
        state = self.store.read_state(run_id)
        self.assertEqual(state["requirement_opencode_flow"], "markdown_first_v3")
        self.assertEqual(state["requirement_opencode_reviewer_model"], settings.phase1_opencode_reviewer_model)
        worktree = self.manager.expected_worktree_path(run_id)
        draft_ref = "docs/sdlc-local/requirements-draft.md"
        draft_file = worktree / draft_ref
        segments = use_case_segments(source.text)
        clause_ids = [f"UC-{index}" for index in range(1, len(segments) + 1)]
        framed = (
            "# Requirements\n\n## Use-case clauses\n"
            + "".join(f'- **{ident}** "{segment}"\n' for ident, segment in zip(clause_ids, segments))
            + "\n## Acceptance criteria\n### AC-1 The use case\nThe system does what the use case says.\n"
            + "- API: answers as stated.\n- Web: shows the result.\n- Covers: " + ", ".join(clause_ids) + "\n"
            + "\n## Implied obligations\n- **IMP-1** Keep the readme. Serves: UC-1. Repo: `README.md`\n"
            + "\n## Open questions\n### Q-1 Refusal status\nWhich status answers a refused request?\n- Affects: AC-1\n"
        )
        unframed = framed.replace("- Covers: " + ", ".join(clause_ids) + "\n", "")
        revised = framed + "\n## Review responses\n\n- 1: applied.\n"
        answered = revised + "\nQ-1 answer: HTTP 404 (decided by Q-1)\n"
        # The owner is asked once; the gap the answer opens is settled by the Refiner as its own marked decision.
        agent_decided = answered.replace(
            "- Web: shows the result.\n",
            "- Web: shows the result; an incomplete publish is refused (decided by Q-2, agent decision).\n",
        ) + (
            "\n### Q-2 Incomplete publish\nWhat does publishing an incomplete draft answer?\n"
            "- Disposition: agent_decided\n- Answer: 422 with field errors\n"
            "- Reason: published articles keep the existing validation.\n- Status: answered\n"
        )
        question = {
            "id": "Q-1", "question": "Which status answers a refused request?", "source": "repository_survey",
            "context": "The API answers 404 for missing records today.", "category": "api_contract",
            "proposed_disposition": "human_required", "proposed_blocking": True, "affects": ["AC-1"], "depends_on": [],
            "answer_guidance": None, "answer_mode": "multiple_choice",
            "options": [{"key": "a", "answer": "HTTP 404", "implication": None},
                        {"key": "b", "answer": "HTTP 403", "implication": None}],
            "recommended_option": "a", "suggested_answer": None, "why_it_is_safe": None, "impact": "high",
            "uncertainty": "medium", "reversible": False,
        }
        trace = {
            "schema_version": "requirement_trace_v1",
            "uc_clauses": [
                {"id": ident, "quote": segment, "observable": True, "reason": None, "kind": "main_step", "scenario": "SC-1"}
                for ident, segment in zip(clause_ids, segments)
            ],
            "acceptance_criteria": [
                {"id": "AC-1", "title": "The use case", "text": "The system does what the use case says.",
                 "api": "answers as stated", "web": "shows the result", "covers": clause_ids,
                 "decided_by": ["Q-1", "Q-2"]},
            ],
            "implied_obligations": [
                {"id": "IMP-1", "text": "Keep the readme.", "serves": ["UC-1"], "repo_evidence": ["README.md"],
                 "decided_by": []},
            ],
        }
        run = subprocess.run
        calls: list[dict[str, object]] = []
        sessions = {"refiner": "ses_testrefiner", "reviewer": "ses_testreviewer"}

        def invoke(args, **kwargs):
            if args[0] != "fake-opencode":
                self.assertEqual(args[0], "git")
                return run(args, **kwargs)
            self.assertEqual(Path(kwargs["cwd"]), worktree)
            prompt = kwargs["input"]
            config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            permissions = config["agents"]["sdlc-requirement"]["permissions"]
            editable = [rule["resource"] for rule in permissions if rule["action"] == "edit"]
            self.assertEqual(len(editable), 1)
            self.assertIs(config["mcp"]["servers"]["codegraph"]["codemode"], False)
            self.assertIn({"action": "codegraph*", "resource": "*", "effect": "allow"}, permissions)
            output = editable[0]
            self.assertIn(output, prompt)
            session = args[args.index("--session") + 1] if "--session" in args else None
            model_arg = args[args.index("--model") + 1]
            answer = output
            if "Write the requirements to" in prompt:
                kind, role = "draft", "refiner"
                self.assertEqual(output, draft_ref)
                self.assertIn(source.text.strip(), prompt)
                self.assertFalse(draft_file.exists())
                draft_file.write_text(unframed, encoding="utf-8")
            elif "checked the frame of your requirements" in prompt:
                kind, role = "frame", "refiner"
                self.assertIn("AC-1: add a 'Covers:' line", prompt)
                self.assertEqual(draft_file.read_text(encoding="utf-8"), unframed)
                draft_file.write_text(framed, encoding="utf-8")
            elif "# Review the answered requirements" in prompt:
                kind, role = "answered_review", "reviewer"
                self.assertEqual(model_arg, settings.phase1_opencode_reviewer_model)
                if sum(call["kind"] == "answered_review" for call in calls) == 0:
                    self.assertEqual(draft_file.read_text(encoding="utf-8"), answered)
                    (worktree / output).write_text("1. AC-1: an incomplete publish is open; propose 422.\n", encoding="utf-8")
                else:
                    self.assertEqual(draft_file.read_text(encoding="utf-8"), agent_decided)
                    answer = "OK"
            elif "# Revise the answered requirements" in prompt:
                kind, role = "answered_revise", "refiner"
                self.assertIn("agent_decided", prompt)
                self.assertEqual(draft_file.read_text(encoding="utf-8"), answered)
                draft_file.write_text(agent_decided, encoding="utf-8")
            elif "You are the independent Requirement Reviewer" in prompt:
                kind, role = "review", "reviewer"
                self.assertEqual(model_arg, settings.phase1_opencode_reviewer_model)
                self.assertNotIn(source.text.strip(), prompt)
                if sum(call["kind"] == "review" for call in calls) == 0:
                    self.assertEqual(draft_file.read_text(encoding="utf-8"), framed)
                    (worktree / output).write_text("1. AC-1: make the criterion observable.\n", encoding="utf-8")
                else:
                    self.assertIn("Review responses", draft_file.read_text(encoding="utf-8"))
                    answer = "The document now covers the use case.\n\nOK"
            elif "Revise the requirements" in prompt:
                kind, role = "revise", "refiner"
                # A rejected revision restarts from the reviewed round, and the Refiner is told so.
                self.assertEqual(draft_file.read_text(encoding="utf-8"), framed)
                rerun = any(call["kind"] == "revise" for call in calls)
                self.assertEqual("Your previous edit was rejected and discarded" in prompt, rerun)
                draft_file.write_text(revised, encoding="utf-8")
            elif "Write the open questions file" in prompt:
                kind, role = "questions", "refiner"
                (worktree / output).write_text(json.dumps({"questions": [question]}), encoding="utf-8")
            elif "# Apply the answers" in prompt:
                kind, role = "answers", "refiner"
                self.assertIn("Q-1 (the owner accepted your recommended option): HTTP 404", prompt)
                self.assertEqual(draft_file.read_text(encoding="utf-8"), revised)
                draft_file.write_text(answered, encoding="utf-8")
            elif "Convert the requirements to the trace JSON" in prompt:
                kind, role = "convert", "refiner"
                self.assertIn("docs/sdlc-local/source-", prompt)
                (worktree / output).write_text("{not json", encoding="utf-8")
            else:
                kind, role = "fix", "refiner"
                self.assertIn("failed these Harness checks", prompt)
                self.assertIn("not valid JSON", prompt)
                (worktree / output).write_text(json.dumps(trace), encoding="utf-8")
            if role == "refiner":
                self.assertEqual(model_arg, settings.phase1_opencode_model)
            previous = [call for call in calls if call["role"] == role]
            self.assertEqual(session, sessions[role] if previous else None)
            calls.append({"kind": kind, "role": role})
            events = [
                {"type": "step_start", "sessionID": sessions[role]},
                {"type": "text", "part": {"text": "Working."}},
                {"type": "step_finish", "part": {"reason": "tool-calls"}},
                {"type": "step_start", "sessionID": sessions[role]},
                {"type": "text", "part": {"text": answer}},
                {"type": "step_finish", "part": {"reason": "stop"}},
            ]
            return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

        review_mode = {"enabled": True, "review_input": False, "review_output": True}
        pauses: list[str] = []

        def approve_all(response: dict[str, object]) -> dict[str, object]:
            while response["status"] == "requirement_paused":
                self.assertIn("requirement_execution", response, response.get("last_error"))
                pending = response["requirement_execution"]["step_review"]
                self.assertEqual(pending["review_stage"], "output")
                pauses.append(pending["call_id"])
                reject = pending["call_id"].startswith("requirement-markdown-revise") and pauses[-2:-1] != pauses[-1:]
                reject = reject and not any(call.startswith("requirement-markdown-revise") for call in pauses[:-1])
                decision = {"decision": "reject", "note": "Keep the IDs."} if reject else {"decision": "approve"}
                response = PhaseOneWorkflow(
                    settings, requirement_step_review={**review_mode, "call_id": pending["call_id"], **decision},
                ).continue_requirement_run(run_id=run_id)
            return response

        def prepare_index(provider, target_path):
            (target_path / ".codegraph").mkdir()
            return {}

        with (
            patch("sdlc_harness.llm.opencode._find_opencode", return_value="fake-opencode"),
            patch("sdlc_harness.llm.opencode.subprocess.run", side_effect=invoke),
            patch.object(CodeGraphContextProvider, "prepare_index", autospec=True, side_effect=prepare_index) as index,
        ):
            waiting = approve_all(
                PhaseOneWorkflow(settings, requirement_step_review=review_mode).continue_requirement_run(run_id=run_id)
            )
            self.assertEqual(waiting["status"], "waiting_requirement_clarification")
            self.assertEqual([item["id"] for item in waiting["pending_checkpoint"]["questions"]], ["Q-1"])
            with self.assertRaises(HarnessError):
                PhaseOneWorkflow(settings).answer_clarification(
                    run_id=run_id, question_id="Q-1", answer="a", answered_by="owner",
                )
            response = approve_all(
                PhaseOneWorkflow(settings, requirement_step_review=review_mode).answer_markdown_questions(
                    run_id=run_id, answers={}, answered_by="owner",
                )
            )
            index.assert_called_once()

        self.assertEqual(response["status"], "waiting_requirement_approval")
        self.assertEqual(
            [call_id.rsplit("-", 2)[0] for call_id in pauses],
            [
                "requirement-markdown-draft",
                "requirement-markdown-frame-r2",
                "requirement-markdown-review-r2",
                "requirement-markdown-revise-r3",
                "requirement-markdown-revise-r3",
                "requirement-markdown-review-r3",
                "requirement-markdown-questions-a1",
                "requirement-markdown-answers",
                "requirement-markdown-answered-review-r1",
                "requirement-markdown-answered-revise-r2",
                "requirement-markdown-answered-review-r2",
                "requirement-json-conversion-a1",
                "requirement-json-conversion-a2",
            ],
        )
        self.assertEqual(
            [call["kind"] for call in calls],
            [
                "draft", "frame", "review", "revise", "revise", "review", "questions", "answers",
                "answered_review", "answered_revise", "answered_review", "convert", "fix",
            ],
        )
        checkpoint = response["pending_checkpoint"]
        self.assertEqual(checkpoint["artifact_ref"], "requirement_trace_v1_r1")
        self.assertEqual(checkpoint["counts"]["decisions"], 2)
        artifacts = self.store.read_state(run_id)["latest_artifacts"]
        self.assertNotIn("requirement_model", artifacts)
        record = json.loads((self.harness_root / artifacts["requirement_markdown_flow"]).read_text(encoding="utf-8"))
        self.assertEqual([item["kind"] for item in record["rounds"]], ["draft", "frame_fix", "revise"])
        self.assertEqual([item["kind"] for item in record["answered_rounds"]], ["answers", "revise"])
        stored = json.loads((self.harness_root / artifacts["requirement_trace"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["decisions"][0]["question_id"], "Q-1")
        self.assertEqual(stored["acceptance_criteria"][0]["decision_refs"], ["D-1", "D-2"])
        self.assertEqual([q["status"] for q in stored["questions"]], ["resolved", "agent_decided"])
        self.assertEqual((stored["decisions"][1]["question_id"], stored["decisions"][1]["source"]), ("Q-2", "agent_decided"))
        self.assertEqual(stored["markdown_artifact"], artifacts["requirement_markdown_draft"])
        # Approving the Markdown requirement freezes it and starts the Markdown design (its flow is tested on its own).
        with patch.object(PhaseTwoWorkflow, "_run_hoc_design", return_value={"status": "design_investigating"}) as design:
            CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=run_id)
        design.assert_called_once_with(run_id=run_id)
        started = self.store.read_state(run_id)
        self.assertEqual(started["design_opencode_flow"], "design_markdown_v1")
        self.assertEqual(started["approved_artifacts"]["requirement_trace"], artifacts["requirement_trace"])
        self.assertEqual(started["approved_artifacts"]["requirement_markdown"], artifacts["requirement_markdown_draft"])
        self.assertEqual(started["design_opencode_reviewer_model"], settings.phase1_opencode_reviewer_model)
        status = [line for line in _git(worktree, "status", "--porcelain").splitlines() if ".codegraph" not in line]
        self.assertEqual(status, [])

    def test_create_fetches_remote_and_preserves_canonical_checkout(self) -> None:
        canonical_branch = _git(self.canonical, "branch", "--show-current")
        canonical_head = _git(self.canonical, "rev-parse", "HEAD")
        newest_remote_sha = self._push_remote_change()

        record = self.manager.create_for_run(self.run_id)

        self.assertEqual(record.base_sha, newest_remote_sha)
        self.assertEqual(record.head_sha, newest_remote_sha)
        self.assertEqual(record.provisioning_mode, "created_for_run")
        self.assertEqual(record.branch, "feature/etour-uc1-a1b2c3d4")
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), canonical_branch)
        self.assertEqual(_git(self.canonical, "rev-parse", "HEAD"), canonical_head)
        self.assertEqual(_git(Path(record.worktree_path), "branch", "--show-current"), record.branch)

        state = self.store.read_state(self.run_id)
        self.assertEqual(state["workspace"]["base_sha"], newest_remote_sha)
        self.assertEqual(
            state["latest_artifacts"]["run_workspace"],
            f".runtime/runs/{self.run_id}/artifacts/run_workspace_v1.json",
        )

    def test_create_is_idempotent_and_locked_workspace_resolves_identity(self) -> None:
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(self.run_id)

        self.assertEqual(first, second)
        with self.manager.locked_workspace(self.run_id) as locked:
            self.assertEqual(locked, first)
            self.assertEqual(_git(Path(locked.worktree_path), "rev-parse", "HEAD"), locked.head_sha)

    def test_profiled_workspace_binds_manifest_base_sha_and_component_roots(self) -> None:
        self._write_state(self.run_id, profiled=True)

        record = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        snapshot = state["profile_snapshot"]

        self.assertEqual(snapshot["target_manifest_schema"], "target_profile_manifest_v1")
        self.assertRegex(snapshot["target_manifest_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(snapshot["base_sha"], record.base_sha)
        self.assertEqual(snapshot["resolved_component_roots"]["web"], ["apps/web/src"])
        self.assertEqual(self.manager.resolve(self.run_id), record)

    def test_profiled_workspace_manifest_mismatch_rolls_back_creation(self) -> None:
        self._write_state(self.run_id, profiled=True)
        manifest = self.seed / ".sdlc" / "target-profile.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["project_profile_id"] = "other_profile_v1"
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _git(self.seed, "add", ".sdlc/target-profile.json")
        _git(self.seed, "commit", "-m", "break manifest")
        _git(self.seed, "push", "origin", "main")

        with self.assertRaises(HarnessError) as context:
            self.manager.create_for_run(self.run_id)

        self.assertEqual(
            context.exception.error_code,
            "target_profile_manifest_mismatch",
            context.exception.message,
        )
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())
        self.assertFalse(self.store.read_state(self.run_id)["profile_snapshot"].get("base_sha"))

    def test_profiled_workspace_detects_snapshot_and_manifest_drift(self) -> None:
        self._write_state(self.run_id, profiled=True)
        record = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        state["profile_snapshot"]["target_manifest_digest"] = "sha256:" + "f" * 64
        self.store.write_state(state)

        with self.assertRaises(HarnessError) as snapshot_context:
            self.manager.resolve(self.run_id)
        self.assertEqual(snapshot_context.exception.error_code, "profile_snapshot_mismatch")

        state["profile_snapshot"]["target_manifest_digest"] = "sha256:" + hashlib.sha256(
            (Path(record.worktree_path) / ".sdlc" / "target-profile.json").read_bytes()
        ).hexdigest()
        self.store.write_state(state)
        (Path(record.worktree_path) / ".sdlc" / "target-profile.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(HarnessError) as manifest_context:
            self.manager.resolve(self.run_id)
        self.assertEqual(manifest_context.exception.error_code, "target_profile_manifest_invalid")

    def test_locked_workspace_backfills_valid_pre_binding_profile_snapshot(self) -> None:
        record = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        state["profile_snapshot"] = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote=str(self.remote.resolve()),
            target_base_branch="main",
        ).to_dict()
        self.store.write_state(state)

        with self.manager.locked_workspace(self.run_id) as locked:
            self.assertEqual(locked, record)

        rebound = self.store.read_state(self.run_id)["profile_snapshot"]
        self.assertEqual(rebound["base_sha"], record.base_sha)
        self.assertRegex(rebound["target_manifest_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(rebound["resolved_component_roots"]["web"], ["apps/web/src"])

    def test_adopt_legacy_run_preserves_head_state_and_is_idempotent(self) -> None:
        branch, head_sha, pull_request = self._prepare_legacy_run()
        reader_calls: list[tuple[str, int, Path]] = []

        def read_pr(repository: str, number: int, target_path: Path) -> dict[str, object]:
            reader_calls.append((repository, number, target_path))
            return pull_request

        record, adopted = self.manager.adopt_legacy_run(self.run_id, pull_request_reader=read_pr)

        self.assertTrue(adopted)
        self.assertEqual(record.provisioning_mode, "adopted_legacy")
        self.assertEqual(record.branch, branch)
        self.assertEqual(record.head_sha, head_sha)
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), "main")
        self.assertEqual(_git(Path(record.worktree_path), "branch", "--show-current"), branch)
        self.assertEqual(_git(Path(record.worktree_path), "rev-parse", "HEAD"), head_sha)
        self.assertEqual(reader_calls, [("example/target", 1, self.canonical.resolve())])
        state = self.store.read_state(self.run_id)
        self.assertEqual(state["status"], "code_review_ready")
        self.assertEqual(state["pending_checkpoint"], {"type": "code_review"})
        self.assertEqual(state["workspace"]["head_sha"], head_sha)

        def unexpected_reader(repository: str, number: int, target_path: Path) -> dict[str, object]:
            self.fail("Idempotent adoption must not query GitHub again.")

        repeated, adopted_again = self.manager.adopt_legacy_run(
            self.run_id,
            pull_request_reader=unexpected_reader,
        )
        self.assertFalse(adopted_again)
        self.assertEqual(repeated, record)
        matching = [
            path
            for path, entry in self.manager._worktree_registry(self.canonical, run_id=self.run_id).items()
            if entry.get("branch") == f"refs/heads/{branch}"
        ]
        self.assertEqual(matching, [Path(record.worktree_path).resolve()])

    def test_adopt_legacy_run_rejects_pr_mismatch_without_mutation(self) -> None:
        branch, _, pull_request = self._prepare_legacy_run()
        pull_request["headRefOid"] = "f" * 40

        with self.assertRaises(HarnessError) as ctx:
            self.manager.adopt_legacy_run(
                self.run_id,
                pull_request_reader=lambda repository, number, target_path: pull_request,
            )

        self.assertEqual(ctx.exception.error_code, "workspace_adoption_failed")
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), branch)
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_adopt_legacy_run_rejects_missing_publication_without_mutation(self) -> None:
        self.store.write_state(
            {
                "ok": True,
                "run_id": self.run_id,
                "source_ref": "jira_issue:SDLC-1",
                "current_phase": "code_review",
                "status": "code_review_ready",
                "latest_artifacts": {
                    "github_code_review": self.store.relative(
                        self.store.run_dir(self.run_id) / "sync" / "github_code_review_v1.json"
                    )
                },
                "pending_checkpoint": {"type": "code_review"},
            }
        )

        with self.assertRaises(HarnessError) as ctx:
            self.manager.adopt_legacy_run(
                self.run_id,
                pull_request_reader=lambda repository, number, target_path: {},
            )

        self.assertEqual(ctx.exception.error_code, "workspace_adoption_failed")
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), "main")
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_adopt_legacy_run_rolls_back_when_artifact_write_fails(self) -> None:
        branch, _, pull_request = self._prepare_legacy_run()
        failure = HarnessError("runtime_write_failed", "simulated artifact failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_json_artifact", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.adopt_legacy_run(
                    self.run_id,
                    pull_request_reader=lambda repository, number, target_path: pull_request,
                )

        self.assertEqual(ctx.exception.error_code, "workspace_adoption_failed")
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), branch)
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_adopt_legacy_run_keeps_owned_workspace_when_state_write_fails(self) -> None:
        _, head_sha, pull_request = self._prepare_legacy_run()
        failure = HarnessError("runtime_write_failed", "simulated state failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_state", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.adopt_legacy_run(
                    self.run_id,
                    pull_request_reader=lambda repository, number, target_path: pull_request,
                )

        self.assertEqual(ctx.exception.error_code, "workspace_adoption_failed")
        self.assertTrue(self.manager.artifact_path(self.run_id).is_file())
        self.assertTrue(self.manager.expected_worktree_path(self.run_id).is_dir())
        self.assertEqual(_git(self.manager.expected_worktree_path(self.run_id), "rev-parse", "HEAD"), head_sha)

        recovered, adopted = self.manager.adopt_legacy_run(
            self.run_id,
            pull_request_reader=lambda repository, number, target_path: self.fail(
                "Recovery from an owned workspace must not query GitHub again."
            ),
        )
        self.assertFalse(adopted)
        self.assertEqual(recovered.head_sha, head_sha)
        self.assertEqual(self.store.read_state(self.run_id)["workspace"]["head_sha"], head_sha)

    def test_refresh_locked_records_new_head_and_pull_request_identity(self) -> None:
        record = self.manager.create_for_run(self.run_id)

        with self.manager.locked_workspace(self.run_id) as locked:
            worktree = Path(locked.worktree_path)
            (worktree / "generated.txt").write_text("generated\n", encoding="utf-8")
            _git(worktree, "add", "generated.txt")
            _git(worktree, "commit", "-m", "generated by run")
            refreshed = self.manager.refresh_locked(
                locked,
                pr_number=17,
                pr_url="https://github.com/example/target/pull/17",
            )

        self.assertNotEqual(refreshed.head_sha, record.head_sha)
        self.assertEqual(refreshed.head_sha, _git(Path(refreshed.worktree_path), "rev-parse", "HEAD"))
        self.assertEqual(refreshed.pr_number, 17)
        self.assertEqual(refreshed.pr_url, "https://github.com/example/target/pull/17")
        self.assertEqual(refreshed.status_summary()["head_sha"], refreshed.head_sha)
        self.assertEqual(refreshed.status_summary()["pr_number"], 17)
        self.assertNotIn("worktree_path", refreshed.status_summary())
        self.assertEqual(self.manager.resolve(self.run_id), refreshed)
        state = self.store.read_state(self.run_id)
        self.assertEqual(state["workspace"]["head_sha"], refreshed.head_sha)
        self.assertEqual(state["workspace"]["pr_number"], 17)

    def test_cleanup_rejects_open_pull_request_without_mutation(self) -> None:
        record, pull_request = self._publish_workspace()

        with self.assertRaises(HarnessError) as ctx:
            self.manager.cleanup_run(self.run_id, pull_request_reader=lambda current: pull_request)

        self.assertEqual(ctx.exception.error_code, "workspace_cleanup_not_allowed")
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(self.manager.read(self.run_id).lifecycle, "active")
        self.assertEqual(_git(self.canonical, "rev-parse", record.branch), record.head_sha)

    def test_cleanup_rejects_dirty_cancelled_workspace(self) -> None:
        record, _ = self._publish_workspace()
        state = self.store.read_state(self.run_id)
        state["status"] = "cancelled"
        self.store.write_state(state)
        (Path(record.worktree_path) / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.cleanup_run(
                self.run_id,
                pull_request_reader=lambda current: self.fail("Cancelled cleanup must not query GitHub."),
            )

        self.assertEqual(ctx.exception.error_code, "workspace_dirty")
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(self.manager.read(self.run_id).lifecycle, "active")

    def test_cleanup_cancelled_run_requires_remote_copy(self) -> None:
        record = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        state["status"] = "cancelled"
        self.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            self.manager.cleanup_run(
                self.run_id,
                pull_request_reader=lambda current: self.fail("Cancelled cleanup must not query GitHub."),
            )

        self.assertEqual(ctx.exception.error_code, "workspace_cleanup_not_allowed")
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(self.manager.read(self.run_id).lifecycle, "active")

    def test_cleanup_cancelled_run_removes_only_local_workspace_and_branch(self) -> None:
        record, _ = self._publish_workspace()
        state = self.store.read_state(self.run_id)
        state["status"] = "cancelled"
        self.store.write_state(state)
        codegraph = Path(record.worktree_path) / ".codegraph"
        codegraph.mkdir()
        (codegraph / "index.json").write_text("{}\n", encoding="utf-8")

        cleaned, changed = self.manager.cleanup_run(
            self.run_id,
            pull_request_reader=lambda current: self.fail("Cancelled cleanup must not query GitHub."),
        )

        self.assertTrue(changed)
        self.assertEqual(cleaned.lifecycle, "cleaned")
        self.assertEqual(cleaned.cleanup_reason, "run_cancelled")
        self.assertTrue(cleaned.local_branch_deleted)
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertEqual(_git(self.canonical, "branch", "--list", record.branch), "")
        self.assertEqual(_git(self.canonical, "ls-remote", "--heads", "origin", record.branch).split()[0], record.head_sha)
        self.assertEqual(self.store.read_state(self.run_id)["status"], "cancelled")

    def test_application_cleanup_legacy_run_uses_record_after_target_cutover(self) -> None:
        record, _ = self._publish_workspace()
        state = self.store.read_state(self.run_id)
        state["status"] = "cancelled"
        self.store.write_state(state)
        service = ApplicationService(root=self.harness_root)
        command = ApplicationCommand(
            name="cleanup-run-workspace",
            arguments={"run_id": self.run_id},
            settings_overrides={
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_TARGET_REPO_PATH": str(self.base / "typescript-target"),
                "SDLC_TARGET_WORKTREE_ROOT": str(self.base / "typescript-worktrees"),
                "SDLC_TARGET_REPO_REMOTE": "https://github.com/example/typescript-target.git",
                "SDLC_TARGET_BASE_BRANCH": "main",
            },
            runtime_root=self.runtime_root,
        )

        response = service.execute(command)

        self.assertTrue(response["cleaned"])
        self.assertEqual(response["cleanup_reason"], "run_cancelled")
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertEqual(_git(self.canonical, "branch", "--list", record.branch), "")
        self.assertTrue(_git(self.canonical, "ls-remote", "--heads", "origin", record.branch))

    def test_cleanup_merged_pull_request_is_audited_and_idempotent(self) -> None:
        record, pull_request = self._publish_workspace()
        self._merge_published_workspace(record, pull_request)

        cleaned, changed = self.manager.cleanup_run(
            self.run_id,
            pull_request_reader=lambda current: pull_request,
        )

        self.assertTrue(changed)
        self.assertEqual(cleaned.lifecycle, "cleaned")
        self.assertEqual(cleaned.cleanup_reason, "pr_merged")
        self.assertEqual(cleaned.pr_state_at_cleanup, "MERGED")
        self.assertEqual(cleaned.pr_merged_at, "2026-10-15T08:00:00Z")
        self.assertEqual(cleaned.merge_commit_sha, pull_request["mergeCommit"]["oid"])
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertEqual(_git(self.canonical, "branch", "--list", record.branch), "")
        self.assertTrue(_git(self.canonical, "ls-remote", "--heads", "origin", record.branch))

        repeated, changed_again = self.manager.cleanup_run(
            self.run_id,
            pull_request_reader=lambda current: self.fail("Cleaned retry must not query GitHub."),
        )
        self.assertFalse(changed_again)
        self.assertEqual(repeated, cleaned)

    def test_cleanup_retry_resumes_after_local_branch_delete_failure(self) -> None:
        record, pull_request = self._publish_workspace()
        self._merge_published_workspace(record, pull_request)
        original_git = self.manager._git
        failed_once = False

        def fail_update_ref_once(cwd: Path, *args: str, error_code: str, run_id: str) -> str:
            nonlocal failed_once
            if args[:2] == ("update-ref", "-d") and not failed_once:
                failed_once = True
                raise HarnessError("workspace_cleanup_failed", "simulated ref failure", run_id=run_id)
            return original_git(cwd, *args, error_code=error_code, run_id=run_id)

        with patch.object(self.manager, "_git", side_effect=fail_update_ref_once):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.cleanup_run(self.run_id, pull_request_reader=lambda current: pull_request)

        self.assertEqual(ctx.exception.error_code, "workspace_cleanup_failed")
        self.assertEqual(self.manager.read(self.run_id).lifecycle, "cleanup_pending")
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertEqual(_git(self.canonical, "rev-parse", record.branch), record.head_sha)

        cleaned, changed = self.manager.cleanup_run(
            self.run_id,
            pull_request_reader=lambda current: self.fail("Pending retry must not query GitHub."),
        )
        self.assertTrue(changed)
        self.assertEqual(cleaned.lifecycle, "cleaned")
        self.assertEqual(_git(self.canonical, "branch", "--list", record.branch), "")

    def test_cleanup_does_not_delete_local_branch_that_moved_after_authorization(self) -> None:
        record, pull_request = self._publish_workspace()
        self._merge_published_workspace(record, pull_request)
        original_git = self.manager._git
        moved = False

        def move_branch_after_prune(cwd: Path, *args: str, error_code: str, run_id: str) -> str:
            nonlocal moved
            result = original_git(cwd, *args, error_code=error_code, run_id=run_id)
            if args[:2] == ("worktree", "prune") and not moved:
                moved = True
                _git(self.canonical, "update-ref", f"refs/heads/{record.branch}", record.base_sha)
            return result

        with patch.object(self.manager, "_git", side_effect=move_branch_after_prune):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.cleanup_run(self.run_id, pull_request_reader=lambda current: pull_request)

        self.assertEqual(ctx.exception.error_code, "workspace_cleanup_failed")
        self.assertEqual(self.manager.read(self.run_id).lifecycle, "cleanup_pending")
        self.assertEqual(_git(self.canonical, "rev-parse", record.branch), record.base_sha)

    def test_same_source_uses_distinct_branch_and_worktree_per_run(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run)

        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)

        self.assertNotEqual(first.branch, second.branch)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertTrue(Path(first.worktree_path).is_dir())
        self.assertTrue(Path(second.worktree_path).is_dir())

    def test_interleaved_runs_keep_files_and_branches_isolated(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run, source_ref="dataset_use_case:etour:UC2")
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)

        with self.manager.locked_workspace(self.run_id) as locked:
            (Path(locked.worktree_path) / "first-only.txt").write_text("first\n", encoding="utf-8")
        with self.manager.locked_workspace(second_run) as locked:
            (Path(locked.worktree_path) / "second-only.txt").write_text("second\n", encoding="utf-8")

        first_path = Path(first.worktree_path)
        second_path = Path(second.worktree_path)
        self.assertTrue((first_path / "first-only.txt").exists())
        self.assertFalse((first_path / "second-only.txt").exists())
        self.assertTrue((second_path / "second-only.txt").exists())
        self.assertFalse((second_path / "first-only.txt").exists())
        self.assertEqual(_git(first_path, "branch", "--show-current"), first.branch)
        self.assertEqual(_git(second_path, "branch", "--show-current"), second.branch)
        self.assertFalse((self.canonical / "first-only.txt").exists())
        self.assertFalse((self.canonical / "second-only.txt").exists())

    def test_dirty_workspace_does_not_block_resolving_another_run(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run, source_ref="dataset_use_case:etour:UC2")
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)
        (Path(first.worktree_path) / "dirty-only-in-first.txt").write_text("dirty\n", encoding="utf-8")

        resolved_second = self.manager.resolve(second_run)

        self.assertEqual(resolved_second, second)
        self.assertEqual(_git(Path(second.worktree_path), "status", "--porcelain"), "")

    def test_second_action_for_same_run_is_rejected_while_lock_is_held(self) -> None:
        self.manager.create_for_run(self.run_id)

        with self.manager.run_lock(self.run_id):
            with self.assertRaises(HarnessError) as ctx:
                with self.manager.locked_workspace(self.run_id):
                    pass

        self.assertEqual(ctx.exception.error_code, "run_workspace_locked")

    def test_dirty_canonical_repo_is_rejected_without_side_effects(self) -> None:
        (self.canonical / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_dirty")
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_codegraph_changes_do_not_block_workspace_creation(self) -> None:
        codegraph = self.canonical / ".codegraph"
        codegraph.mkdir()
        (codegraph / "index.json").write_text("{}\n", encoding="utf-8")

        record = self.manager.create_for_run(self.run_id)

        self.assertTrue(Path(record.worktree_path).is_dir())

    def test_remote_mismatch_is_rejected(self) -> None:
        manager = RunWorkspaceManager(self._settings(remote=str(self.base / "other.git")))

        with self.assertRaises(HarnessError) as ctx:
            manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_remote_mismatch")
        self.assertFalse(manager.expected_worktree_path(self.run_id).exists())

    def test_branch_and_path_conflicts_fail_closed(self) -> None:
        branch = branch_name_for_run(
            prefix="feature",
            source_ref="dataset_use_case:etour:UC1",
            issue_key="etour-UC1",
            run_id=self.run_id,
        )
        _git(self.canonical, "branch", branch)
        with self.assertRaises(HarnessError) as branch_ctx:
            self.manager.create_for_run(self.run_id)
        self.assertEqual(branch_ctx.exception.error_code, "workspace_branch_conflict")

        _git(self.canonical, "branch", "-d", branch)
        self.manager.expected_worktree_path(self.run_id).mkdir(parents=True)
        with self.assertRaises(HarnessError) as path_ctx:
            self.manager.create_for_run(self.run_id)
        self.assertEqual(path_ctx.exception.error_code, "workspace_path_invalid")

    def test_resolve_detects_git_identity_drift(self) -> None:
        record = self.manager.create_for_run(self.run_id)
        _git(self.canonical, "worktree", "remove", record.worktree_path)

        with self.assertRaises(HarnessError) as ctx:
            self.manager.resolve(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_path_invalid")

    def test_artifact_persistence_failure_rolls_back_clean_creation(self) -> None:
        branch = "feature/etour-uc1-a1b2c3d4"
        failure = HarnessError("runtime_write_failed", "simulated artifact failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_json_artifact", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "runtime_write_failed")
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())
        refs = _git(self.canonical, "branch", "--list", branch)
        self.assertEqual(refs, "")

    def test_state_persistence_failure_keeps_owned_workspace_for_retry(self) -> None:
        failure = HarnessError("runtime_write_failed", "simulated state failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_state", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "runtime_write_failed")
        self.assertTrue(self.manager.artifact_path(self.run_id).is_file())
        record = self.manager.read(self.run_id)
        self.assertTrue(Path(record.worktree_path).is_dir())

        recovered = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        self.assertEqual(recovered, record)
        self.assertEqual(state["workspace"]["head_sha"], record.head_sha)

    def test_profile_binding_recovers_after_state_persistence_failure(self) -> None:
        self._write_state(self.run_id, profiled=True)
        failure = HarnessError("runtime_write_failed", "simulated state failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_state", side_effect=failure):
            with self.assertRaises(HarnessError) as context:
                self.manager.create_for_run(self.run_id)

        self.assertEqual(context.exception.error_code, "runtime_write_failed")
        record = self.manager.read(self.run_id)
        persisted_before_retry = self.store.read_state(self.run_id)["profile_snapshot"]
        self.assertNotIn("base_sha", persisted_before_retry)

        recovered = self.manager.create_for_run(self.run_id)
        persisted_after_retry = self.store.read_state(self.run_id)["profile_snapshot"]
        self.assertEqual(recovered, record)
        self.assertEqual(persisted_after_retry["base_sha"], record.base_sha)
        self.assertIn("target_manifest_digest", persisted_after_retry)

    def test_tampered_artifact_does_not_resolve(self) -> None:
        self.manager.create_for_run(self.run_id)
        artifact = self.manager.artifact_path(self.run_id)
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["head_sha"] = "f" * 40
        artifact.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.resolve(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_branch_mismatch")


if __name__ == "__main__":
    unittest.main()
