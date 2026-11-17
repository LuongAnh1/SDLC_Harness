"""G1.1/G1.2: the declarative workflow graph and the dispatchers that read it.

The parity tests were written against the hand-written dispatchers before G1.2 replaced them with table lookups
(`PhaseFourWorkflow._continue_run_locked`, `approval.py`, the `continue-run` routing in `application/service.py`);
every handler and leaf predicate is stubbed and every status and predicate combination is checked. The frozen
priority cases keep the replaced if-chain's order explicit.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.agents.tester.qa import QA_STATUSES
from sdlc_harness.application.action_specs import public_dashboard_schema
from sdlc_harness.application.commands import ApplicationCommand
from sdlc_harness.application.service import ApplicationService
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.workflows import graph
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow

PACKAGE = Path(__file__).resolve().parents[1] / "sdlc_harness"
WORKFLOW_CLASSES = {
    graph.PHASE_ONE: PhaseOneWorkflow,
    graph.PHASE_TWO: PhaseTwoWorkflow,
    graph.PHASE_THREE: PhaseThreeWorkflow,
    graph.PHASE_FOUR: PhaseFourWorkflow,
}
# Predicates the Phase 4 dispatcher reads directly; the graph's named predicates are built from these.
INSTANCE_LEAVES = (
    "_browser_execution_active",
    "_runtime_closure_execution_active",
    "_qa_generation_retry_allowed",
    "_runtime_closure_retry_allowed",
    "_browser_retry_stage",
    "_final_verification_retry_allowed",
    "_runtime_closure_enforced",
    "_browser_stages_enforced",
    "_documentation_closure_enforced",
)
MODULE_LEAVES = ("developer_execution_retry_allowed", "developer_transport_retry_allowed")
BLOCKED_LEAVES = (
    "_qa_generation_retry_allowed",
    "_runtime_closure_retry_allowed",
    "_runtime_closure_execution_active",
    "_browser_retry_stage",
    "_final_verification_retry_allowed",
    "_browser_execution_active",
    "developer_execution_retry_allowed",
    "developer_transport_retry_allowed",
)
MODES = ("", "runtime_closure_repair", "qa_repair", "implementation_repair", "developer_act")
PHASES = ("", "qa_repair", "implementation_repair")
RUN_STATE_KEYS = {"allowed_next_actions", "current_phase", "summary_for_user"}


def _run_statuses_written_by_code() -> set[str]:
    """Constant statuses in run-state updates and in `status = ...` assignments of the workflows."""

    def constants(value: ast.AST) -> list[str]:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return [value.value]
        if isinstance(value, ast.IfExp):
            return constants(value.body) + constants(value.orelse)
        return []

    found: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        in_workflows = path.parent.name == "workflows"
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {key.value: value for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)}
                if "status" in keys and RUN_STATE_KEYS & set(keys):
                    found.update(constants(keys["status"]))
            elif in_workflows and isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"status", "resume_status"}:
                    found.update(constants(node.value))
    # A QA result (`pass`/`fail`/`skipped`) is also held in a local named `status`; it is never a run status.
    return found - set(QA_STATUSES)


class WorkflowGraphShapeTest(unittest.TestCase):
    def test_every_run_status_the_code_writes_is_a_graph_node(self) -> None:
        written = _run_statuses_written_by_code()

        self.assertEqual(sorted(written - set(graph.NODES_BY_STATUS)), [])
        self.assertIn("browser_readiness_ready", written)  # from a conditional expression
        self.assertIn("waiting_requirement_approval", written)  # from a `status = ...` assignment

    def test_every_graph_status_appears_in_the_code(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8-sig") for path in PACKAGE.rglob("*.py") if path.name != "graph.py"
        )

        self.assertEqual([node.status for node in graph.NODES if f'"{node.status}"' not in source], [])

    def test_nodes_are_unique_and_well_formed(self) -> None:
        self.assertEqual(len(graph.NODES), len(graph.NODES_BY_STATUS))
        stages = {stage_id for stage_id, _ in graph.STAGES}
        for node in graph.NODES:
            with self.subTest(status=node.status):
                self.assertIn(node.kind, graph.KINDS)
                self.assertTrue(not node.stage or node.stage in stages)
                moves = bool(node.continue_routes or node.approve or node.step_review)
                if node.kind in {"generate", "evaluate", "agent", "deterministic"}:
                    self.assertTrue(node.continue_routes, "work nodes need a continue route")
                if node.kind == "step_review":
                    self.assertIsNotNone(node.step_review)
                if node.kind in {"running", "terminal"}:
                    self.assertFalse(moves)
                if node.continue_routes:
                    self.assertIsNotNone(node.continue_routes[-1])
        self.assertTrue(set(graph.PHASE_STAGES.values()) <= stages)

    def test_every_handler_approval_resume_and_predicate_exists(self) -> None:
        for node in graph.NODES:
            with self.subTest(status=node.status):
                if node.continue_routes:
                    workflow = WORKFLOW_CLASSES[node.workflow]
                    for route in node.continue_routes:
                        self.assertTrue(callable(getattr(workflow, route.handler, None)), route.handler)
                        for predicate in route.when:
                            self.assertTrue(callable(getattr(workflow, predicate, None)), predicate)
                if node.approve:
                    self.assertTrue(callable(getattr(WORKFLOW_CLASSES[node.approve.workflow], node.approve.method, None)))
                if node.step_review:
                    workflow = WORKFLOW_CLASSES[node.step_review.workflow]
                    self.assertTrue(callable(getattr(workflow, node.step_review.method, None)))

    def test_dashboard_workflow_comes_from_the_graph(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workflow = public_dashboard_schema(load_settings(root=Path(root)))["workflow"]

        self.assertEqual([node["id"] for node in workflow["nodes"]], [stage for stage, _ in graph.STAGES])
        for node in graph.NODES:
            if node.stage:
                self.assertEqual(workflow["status_nodes"][node.status], node.stage)
        self.assertEqual(workflow["status_nodes"]["browser_qa_verified"], "browser")
        self.assertEqual(workflow["phase_nodes"]["documentation_closure"], "documentation")


class WorkflowGraphParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = load_settings(
            root=Path(self.temp_dir.name), overrides={"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"}
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------ Phase 4 continue_run

    def _phase_four(self, leaves: dict[str, bool], state: dict[str, object]) -> PhaseFourWorkflow:
        workflow = PhaseFourWorkflow(self.settings)
        handlers = {
            route.handler for node in graph.NODES if node.workflow == graph.PHASE_FOUR for route in node.continue_routes
        }
        for name in handlers:
            setattr(workflow, name, lambda *, state, run_id, _name=name: _name)
        for name in INSTANCE_LEAVES:
            setattr(workflow, name, lambda _state, _value=leaves.get(name, False): _value)
        workflow.store.read_state = lambda run_id: state
        workflow._require_frozen_llm_mode = lambda *args, **kwargs: None
        return workflow

    def _old_choice(self, workflow: PhaseFourWorkflow) -> str:
        try:
            return workflow._continue_run_locked(run_id="run_graph")
        except HarnessError as exc:
            self.assertEqual(exc.error_code, "run_not_continuable")
            return ""

    def _cases(self, node: graph.Node):
        if node.status == "blocked":
            names: tuple[str, ...] = BLOCKED_LEAVES
            shapes = list(itertools.product(MODES, PHASES))
        elif node.status in {"implementation_executing", "implementation_paused"}:
            names = ("_browser_execution_active", "_runtime_closure_execution_active")
            shapes = list(itertools.product(MODES, PHASES))
        else:
            names = tuple(
                sorted({predicate for route in node.continue_routes for predicate in route.when} & set(INSTANCE_LEAVES))
            )
            shapes = [("", "")]
        for values in itertools.product((False, True), repeat=len(names)):
            for mode, phase in shapes:
                yield dict(zip(names, values)), mode, phase

    def test_phase_four_continue_routes_match_the_dispatcher(self) -> None:
        checked = 0
        for node in (*graph.NODES, graph.Node("not_a_status", "", "", "terminal")):
            for leaves, mode, phase in self._cases(node):
                state = {"status": node.status, "current_phase": phase, "developer_execution": {"mode": mode}}
                with patch.multiple(
                    "sdlc_harness.workflows.phase4",
                    developer_execution_retry_allowed=lambda _state, _v=leaves.get(MODULE_LEAVES[0], False): _v,
                    developer_transport_retry_allowed=lambda _state, _v=leaves.get(MODULE_LEAVES[1], False): _v,
                ):
                    old = self._old_choice(self._phase_four(leaves, state))
                    new = graph.continue_handler(self._phase_four(leaves, state), state)
                expected = new if node.workflow == graph.PHASE_FOUR else ""
                self.assertEqual(old, expected, f"{node.status} leaves={leaves} mode={mode!r} phase={phase!r}")
                checked += 1
        self.assertGreater(checked, 3000)

    def test_priority_order_of_the_replaced_if_chain_is_kept(self) -> None:
        # Frozen from the if-chain G1.2 replaced: these orders are behavior, not table layout.
        cases = [
            ("blocked", {"_qa_generation_retry_allowed": True, "_runtime_closure_retry_allowed": True}, "", "",
             "_continue_qa_test"),
            ("blocked", {"_browser_retry_stage": True, "_final_verification_retry_allowed": True}, "", "",
             "_resume_browser_stage"),
            ("blocked", {"_final_verification_retry_allowed": True, "_browser_execution_active": True,
                         "developer_execution_retry_allowed": True}, "", "", "_continue_final_verification"),
            ("blocked", {"_runtime_closure_execution_active": True, "developer_transport_retry_allowed": True},
             "runtime_closure_repair", "", "_continue_runtime_closure_repair"),
            ("blocked", {"developer_execution_retry_allowed": True}, "developer_act", "", "_continue_developer_act"),
            ("blocked", {"developer_execution_retry_allowed": True}, "developer_act", "qa_repair", "_continue_qa_repair"),
            ("blocked", {"developer_execution_retry_allowed": True}, "", "", "_continue_implementation_repair"),
            ("blocked", {"developer_transport_retry_allowed": True}, "implementation_repair", "",
             "_continue_implementation_repair"),
            ("blocked", {"developer_transport_retry_allowed": True}, "", "implementation_repair",
             "_continue_developer_act"),
            ("blocked", {}, "", "", ""),
            ("implementation_paused", {"_browser_execution_active": True}, "runtime_closure_repair", "",
             "_continue_browser_developer"),
            ("implementation_executing", {"_runtime_closure_execution_active": True}, "runtime_closure_repair", "",
             "_continue_runtime_closure_repair"),
            ("implementation_paused", {}, "", "implementation_repair", "_continue_implementation_repair"),
            ("implementation_paused", {}, "", "", "_continue_developer_act"),
            ("qa_tested", {"_documentation_closure_enforced": True}, "", "", "_continue_documentation_closure"),
            ("qa_tested", {}, "", "", "_continue_code_review_publish"),
            ("runtime_closed", {}, "", "", "_continue_qa_test"),
            ("implementation_traced", {"_runtime_closure_enforced": True}, "", "", "_continue_runtime_closure_planning"),
            ("waiting_design_approval", {}, "", "", ""),
        ]
        for status, leaves, mode, phase, expected in cases:
            state = {"status": status, "current_phase": phase, "developer_execution": {"mode": mode}}
            with patch.multiple(
                "sdlc_harness.workflows.phase4",
                developer_execution_retry_allowed=lambda _state, _v=leaves.get(MODULE_LEAVES[0], False): _v,
                developer_transport_retry_allowed=lambda _state, _v=leaves.get(MODULE_LEAVES[1], False): _v,
            ):
                got = self._old_choice(self._phase_four(leaves, state))
            self.assertEqual(got, expected, f"{status} {leaves} mode={mode!r} phase={phase!r}")

    # ------------------------------------------------------------------ approve_checkpoint

    def _recorders(self, calls: list[tuple[str, str]], state: dict[str, object]) -> dict[str, type]:
        def factory(class_name: str) -> type:
            class Recorder:
                def __init__(self, settings, **kwargs) -> None:
                    self.kwargs = kwargs
                    self.store = type("Store", (), {"read_state": staticmethod(lambda run_id: state)})()

                def __getattr__(self, method: str):
                    def call(**kwargs):
                        calls.append((class_name, method, tuple(sorted(self.kwargs))))
                        return {"status": "recorded"}

                    return call

            return Recorder

        return {name: factory(name) for name in WORKFLOW_CLASSES}

    def test_approvals_match_approval_dispatcher(self) -> None:
        for node in (*graph.NODES, graph.Node("not_a_status", "", "", "terminal")):
            calls: list[tuple[str, str, tuple[str, ...]]] = []
            state = {"status": node.status}
            with patch.multiple("sdlc_harness.workflows.approval", **{
                name: recorder for name, recorder in self._recorders(calls, state).items() if name != graph.PHASE_ONE
            }):
                approval = CheckpointApprovalWorkflow(self.settings)
                approval.store.read_state = lambda run_id: state
                try:
                    approval.approve_checkpoint(run_id="run_graph")
                    got: tuple[str, ...] = calls[-1][:2]
                except HarnessError as exc:
                    got = ("error", exc.error_code)
            if node.approve is not None:
                expected: tuple[str, ...] = (node.approve.workflow, node.approve.method)
            elif node.status in graph.APPROVAL_REFUSALS:
                expected = ("error", graph.APPROVAL_REFUSALS[node.status][0])
            else:
                expected = ("error", "checkpoint_not_current")
            self.assertEqual(got, expected, node.status)

    # ------------------------------------------------------------------ continue-run routing in the service

    def test_service_continue_routing_matches_graph(self) -> None:
        service = ApplicationService(root=Path(self.temp_dir.name))
        for node in (*graph.NODES, graph.Node("not_a_status", "", "", "terminal")):
            calls: list[tuple[str, str, tuple[str, ...]]] = []
            state = {"status": node.status}
            with patch.multiple("sdlc_harness.application.service", **self._recorders(calls, state)):
                service._execute_unlocked(
                    ApplicationCommand("continue-run", {"run_id": "run_graph"}), settings=self.settings
                )
            got = calls[-1]
            if node.step_review is not None:
                review = node.step_review
                # The review argument is always passed; Phase 2 passes None when the caller sent no review arguments.
                expected = (review.workflow, review.method, (review.argument,))
            elif node.continue_routes and node.workflow != graph.PHASE_FOUR:
                expected = (node.workflow, node.continue_routes[0].handler, ())
            else:
                expected = (
                    graph.PHASE_FOUR,
                    "continue_until_implementation_checkpoint",
                    (
                        "code_validation_step_review",
                        "developer_step_review",
                        "qa_step_review",
                        "runtime_closure_step_review",
                    ),
                )
            self.assertEqual(got, expected, node.status)


if __name__ == "__main__":
    unittest.main()
