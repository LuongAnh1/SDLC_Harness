"""The SDLC run workflow as data (G1.1): every run status, how it moves on and who moves it.

A status is a node. A node says which phase and dashboard stage it belongs to, what kind of work comes next,
and how the run leaves it:

- ``continue_routes``: what ``continue_run`` does, first matching route wins; a route's ``when`` names predicate
  methods of the workflow that must all be truthy;
- ``approve``: the owner checkpoint method behind ``approve_checkpoint``;
- ``step_review``: the per-call review pause and the method that resumes it.

Status names are the public contract (Niko, HTTP API, dashboard) and never change here. Only mechanical routing
lives in this table; what an artifact means stays with the agents and Validators.
"""

from __future__ import annotations

from dataclasses import dataclass

PHASE_ONE = "PhaseOneWorkflow"
PHASE_TWO = "PhaseTwoWorkflow"
PHASE_THREE = "PhaseThreeWorkflow"
PHASE_FOUR = "PhaseFourWorkflow"

# What the next step of a node is (the four task types plus the states between them).
KINDS = (
    "deterministic",  # Harness code: verification, capture, gates, publication
    "generate",  # an LLM produces an artifact (Refiner, Designer, planners, QA tester)
    "evaluate",  # an LLM judges an artifact (Validators, Browser QA)
    "agent",  # the Developer tool loop
    "human",  # waits for the owner
    "step_review",  # paused for per-call review
    # Set while work is in progress. Every mutating command holds the run's OS lock, so `continue_run` finding this
    # status means the process that set it died; its resume route (if any) picks the work up again.
    "running",
    "blocked",  # a failure; only retry routes move it
    "terminal",
)


# Kinds whose next step is machine work; unattended mode (U1) only steps through these.
AUTOMATED_KINDS = frozenset({"deterministic", "generate", "evaluate", "agent"})


@dataclass(frozen=True)
class Route:
    handler: str
    when: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepReview:
    workflow: str
    argument: str
    method: str
    # Phase 2 only enables designer review when the caller sends review arguments.
    always_enabled: bool = True


@dataclass(frozen=True)
class Approval:
    workflow: str
    method: str


@dataclass(frozen=True)
class Node:
    status: str
    phase: str
    stage: str
    kind: str
    workflow: str = ""
    continue_routes: tuple[Route, ...] = ()
    approve: Approval | None = None
    step_review: StepReview | None = None


def _phase_four(status: str, stage: str, kind: str, *routes: Route, phase: str = "") -> Node:
    return Node(status=status, phase=phase or stage, stage=stage, kind=kind, workflow=PHASE_FOUR, continue_routes=routes)


def _human(status: str, stage: str, approve: Approval | None, phase: str = "") -> Node:
    return Node(status=status, phase=phase or stage, stage=stage, kind="human", approve=approve)


def _running(status: str, stage: str, phase: str = "", resume: tuple[str, str] = ("", "")) -> Node:
    workflow, handler = resume
    return Node(
        status=status,
        phase=phase or stage,
        stage=stage,
        kind="running",
        workflow=workflow,
        continue_routes=(Route(handler),) if handler else (),
    )


def _step_review(status: str, stage: str, review: StepReview) -> Node:
    return Node(status=status, phase=stage, stage=stage, kind="step_review", workflow=review.workflow, step_review=review)


_PLAN_RESUME = (PHASE_THREE, "continue_implementation_plan_step_review")

# A blocked run moves on only through these retry rules (order matters: the first match wins).
BLOCKED_ROUTES = (
    Route("_resume_runtime_closure_stage", ("_runtime_closure_retry_allowed",)),
    Route("_continue_runtime_closure_repair", ("_closure_developer_retry_allowed", "_runtime_closure_repair_mode")),
    Route("_continue_runtime_closure_act", ("_closure_developer_retry_allowed",)),
    Route("_resume_browser_stage", ("_browser_retry_stage",)),
    Route("_continue_final_verification", ("_final_verification_retry_allowed",)),
    Route("_continue_browser_developer", ("_browser_developer_retry_allowed",)),
    Route("_continue_developer_act", ("_developer_retry_allowed", "_developer_act_mode")),
    Route("_continue_implementation_repair", ("_developer_retry_allowed",)),
    Route("_continue_implementation_repair", ("_developer_transport_retry_allowed", "_implementation_repair_execution_mode")),
    Route("_continue_developer_act", ("_developer_transport_retry_allowed",)),
)

# A Developer session that was interrupted or paused resumes in the loop that owns it.
DEVELOPER_SESSION_ROUTES = (
    Route("_continue_browser_developer", ("_browser_execution_active",)),
    Route("_continue_runtime_closure_repair", ("_runtime_closure_repair_mode",)),
    Route("_continue_runtime_closure_act", ("_runtime_closure_execution_active",)),
    Route("_continue_implementation_repair", ("_implementation_repair_mode",)),
    Route("_continue_developer_act"),
)

NODES: tuple[Node, ...] = (
    # Phase 1: requirement
    Node("requirement_drafting", "requirement_drafting", "requirement", "generate", PHASE_ONE,
         (Route("continue_requirement_run"),)),
    Node("requirement_paused", "requirement_drafting", "requirement", "generate", PHASE_ONE,
         (Route("continue_requirement_run"),)),
    _human("waiting_requirement_clarification", "requirement", None, phase="requirement_clarification"),
    _human("waiting_requirement_clarification_continue", "requirement", None, phase="requirement_clarification"),
    _human("waiting_requirement_approval", "requirement", Approval(PHASE_TWO, "approve_checkpoint"),
           phase="waiting_requirement_approval"),
    Node("failed", "", "", "terminal"),
    # Phase 2: repository discovery and design
    _running("repo_discovery", "design", phase="repo_discovery", resume=(PHASE_TWO, "continue_repo_discovery")),
    Node("repo_discovery_paused", "repo_discovery", "design", "generate", PHASE_TWO,
         (Route("continue_repo_discovery"),)),
    Node("repo_discovery_blocked", "repo_discovery", "design", "blocked"),
    # The Designer session is saved after every action, so an interrupted loop resumes like a transport pause.
    _running("design_investigating", "design", phase="design_investigating", resume=(PHASE_TWO, "continue_design")),
    _running("design_repairing", "design", phase="design_investigating", resume=(PHASE_TWO, "continue_design")),
    _running("design_applying_delta", "design", phase="design_investigating", resume=(PHASE_TWO, "continue_design")),
    _running("design_validating", "design", phase="design_validating", resume=(PHASE_TWO, "revalidate_design")),
    _running("design_restarting", "design", phase="design_restarting", resume=(PHASE_TWO, "restart_design")),
    Node("design_paused", "design_investigating", "design", "step_review", PHASE_TWO,
         step_review=StepReview(PHASE_TWO, "designer_step_review", "continue_design", always_enabled=False)),
    _human("design_human_required", "design", None, phase="design_review"),
    Node("design_blocked", "design_investigating", "design", "blocked"),
    _human("waiting_design_approval", "design", Approval(PHASE_THREE, "approve_design_checkpoint"),
           phase="design_review"),
    # Phase 3: implementation plan
    # An interrupted plan run plans again from the frozen Requirement and Design; approved exchanges replay.
    _running("implementation_planning", "plan", phase="implementation_planning", resume=_PLAN_RESUME),
    _running("implementation_plan_validating", "plan", phase="implementation_plan_validating", resume=_PLAN_RESUME),
    _running("implementation_plan_repairing", "plan", phase="implementation_plan_repairing", resume=_PLAN_RESUME),
    _running("implementation_plan_revalidating", "plan", phase="implementation_plan_revalidating", resume=_PLAN_RESUME),
    _step_review("implementation_plan_step_review_required", "plan",
                 StepReview(PHASE_THREE, "plan_step_review", "continue_implementation_plan_step_review")),
    _human("waiting_implementation_plan_review", "plan",
           Approval(PHASE_FOUR, "approve_implementation_plan_checkpoint"), phase="implementation_planning"),
    # Phase 4: implementation, verification, Code Validator, then the lanes (always on since 2026-11-18)
    _phase_four("implementation_ready", "developer", "agent", Route("_continue_developer_act"),
                phase="implementation_preparing"),
    _phase_four("implementation_executing", "developer", "agent", *DEVELOPER_SESSION_ROUTES,
                phase="implementation_writing"),
    _phase_four("implementation_paused", "developer", "agent", *DEVELOPER_SESSION_ROUTES,
                phase="implementation_writing"),
    _phase_four("implementation_repair_needed", "developer", "agent", Route("_continue_implementation_repair"),
                phase="implementation_repair"),
    _phase_four("implementation_written", "verification", "deterministic", Route("_continue_verification"),
                phase="implementation_testing"),
    _phase_four("implementation_tested", "validator", "evaluate", Route("_continue_code_trace"),
                phase="implementation_testing"),
    _step_review("implementation_validation_step_review_required", "validator",
                 StepReview(PHASE_FOUR, "code_validation_step_review", "continue_code_validation_step_review")),
    _phase_four("implementation_traced", "qa", "generate", Route("_continue_runtime_closure_planning"),
                phase="qa_testing"),
    # Browser QA passed; Documentation Closure starts.
    _phase_four("qa_tested", "qa", "deterministic", Route("_continue_documentation_closure"), phase="qa_testing"),
    # Phase 4: runtime closure (L5.3)
    _human("waiting_runtime_closure_review", "runtime_closure",
           Approval(PHASE_FOUR, "approve_runtime_closure_checkpoint")),
    _step_review("runtime_closure_step_review_required", "runtime_closure",
                 StepReview(PHASE_FOUR, "runtime_closure_step_review", "continue_runtime_closure_step_review")),
    _phase_four("runtime_closure_ready", "runtime_closure", "agent", Route("_continue_runtime_closure_act")),
    _phase_four("runtime_closure_written", "runtime_closure", "deterministic",
                Route("_continue_runtime_closure_verification")),
    _phase_four("runtime_closure_verified", "runtime_closure", "evaluate", Route("_continue_runtime_closure_review")),
    _phase_four("runtime_closure_repair_needed", "runtime_closure", "agent",
                Route("_continue_runtime_closure_repair")),
    _phase_four("runtime_closed", "runtime_closure", "deterministic",
                Route("_continue_browser_readiness")),
    # Phase 4: browser stages (L5.4)
    _step_review("browser_stage_step_review_required", "browser",
                 StepReview(PHASE_FOUR, "browser_stage_step_review", "continue_browser_stage_step_review")),
    _phase_four("browser_readiness_ready", "browser", "agent", Route("_continue_browser_readiness_act"),
                phase="browser_readiness"),
    _phase_four("browser_readiness_written", "browser", "deterministic",
                Route("_continue_browser_readiness_verification"), phase="browser_readiness"),
    _phase_four("browser_readiness_repair_needed", "browser", "agent", Route("_continue_browser_readiness_repair"),
                phase="browser_readiness"),
    _phase_four("browser_ready", "browser", "generate", Route("_continue_browser_journey_planning"),
                phase="browser_readiness"),
    _phase_four("browser_journey_planned", "browser", "deterministic", Route("_continue_browser_baseline_capture"),
                phase="browser_journey"),
    _phase_four("browser_journey_captured", "browser", "evaluate", Route("_continue_browser_journey_review"),
                phase="browser_journey"),
    _phase_four("browser_journey_patch_needed", "browser", "generate", Route("_continue_browser_journey_patch"),
                phase="browser_journey"),
    _phase_four("browser_journey_repair_needed", "browser", "agent", Route("_continue_browser_journey_repair"),
                phase="browser_journey"),
    _phase_four("browser_journey_repair_written", "browser", "deterministic",
                Route("_continue_browser_journey_repair_verification"), phase="browser_journey"),
    _phase_four("browser_baseline_captured", "browser", "generate", Route("_continue_ui_polish_planning"),
                phase="browser_journey"),
    _human("waiting_ui_polish_contract_review", "browser",
           Approval(PHASE_FOUR, "approve_ui_polish_contract_checkpoint"), phase="ui_polish"),
    _phase_four("ui_polish_ready", "browser", "agent", Route("_continue_ui_polish_act"), phase="ui_polish"),
    _phase_four("ui_polish_written", "browser", "deterministic", Route("_continue_ui_polish_verification"),
                phase="ui_polish"),
    _phase_four("ui_polish_verified", "browser", "evaluate", Route("_continue_ui_polish_review"), phase="ui_polish"),
    _phase_four("ui_polish_repair_needed", "browser", "agent", Route("_continue_ui_polish_repair"), phase="ui_polish"),
    _human("waiting_ui_polish_review", "browser", Approval(PHASE_FOUR, "approve_ui_polish_checkpoint"),
           phase="ui_polish"),
    _phase_four("ui_polished", "browser", "evaluate", Route("_continue_browser_qa_review"), phase="ui_polish"),
    _phase_four("browser_qa_journey_patch_needed", "browser", "generate",
                Route("_continue_browser_qa_journey_patch"), phase="browser_qa"),
    _phase_four("browser_qa_repair_needed", "browser", "agent", Route("_continue_browser_qa_repair"),
                phase="browser_qa"),
    _phase_four("browser_qa_written", "browser", "deterministic", Route("_continue_browser_qa_verification"),
                phase="browser_qa"),
    _phase_four("browser_qa_verified", "browser", "evaluate", Route("_continue_browser_qa_review"),
                phase="browser_qa"),
    # Phase 4: Documentation Closure, Final Verification, publication (L5.5)
    _human("waiting_documentation_review", "documentation", Approval(PHASE_FOUR, "approve_documentation_checkpoint"),
           phase="documentation_closure"),
    _phase_four("documentation_approved", "documentation", "deterministic", Route("_continue_final_verification"),
                phase="documentation_closure"),
    _phase_four("final_verification_passed", "final_verification", "deterministic",
                Route("_continue_publication_preview")),
    _human("publication_previewed", "review", Approval(PHASE_FOUR, "approve_publication_checkpoint"),
           phase="code_review_publishing"),
    _phase_four("publication_approved", "review", "deterministic", Route("_continue_approved_publication"),
                phase="code_review_publishing"),
    _phase_four("code_review_publish_failed", "review", "deterministic", Route("_continue_code_review_publish"),
                phase="code_review_publishing"),
    _phase_four("code_review_published", "review", "deterministic", Route("_continue_code_review_sync"),
                phase="code_review_publishing"),
    # Code review sync runs through the sync_run action, not continue_run.
    _running("code_review_syncing", "review", phase="code_review_syncing"),
    Node("code_review_sync_failed", "code_review_syncing", "review", "blocked"),
    _human("code_review_ready", "review", Approval(PHASE_FOUR, "approve_code_review_checkpoint"),
           phase="code_review"),
    _running("code_review_approval_syncing", "review", phase="code_review_approval"),
    Node("code_review_approval_sync_failed", "code_review_approval", "review", "blocked"),
    Node("code_review_approved", "code_review_approved", "review", "terminal"),
    # Any phase
    Node("blocked", "", "", "blocked", PHASE_FOUR, BLOCKED_ROUTES),
)

NODES_BY_STATUS = {node.status: node for node in NODES}

# Statuses where `approve_checkpoint` is refused with a specific error instead of "no checkpoint".
APPROVAL_REFUSALS = {
    "waiting_requirement_clarification": (
        "requirement_approval_blocked",
        "Requirement approval is blocked until all blocking clarification questions are resolved.",
    ),
}

# Dashboard stages in pipeline order; the dashboard falls back to `PHASE_STAGES` for a blocked run.
STAGES = (
    ("requirement", "Requirement"),
    ("design", "Design"),
    ("plan", "Implementation Plan"),
    ("developer", "Developer"),
    ("verification", "Verification"),
    ("validator", "Validator"),
    ("runtime_closure", "Runtime Closure"),
    ("browser", "Browser Stages"),
    ("qa", "QA"),
    ("documentation", "Documentation"),
    ("final_verification", "Final Verification"),
    ("review", "Draft PR / Review"),
)
PHASE_STAGES = {
    "requirement_drafting": "requirement",
    "requirement_clarification": "requirement",
    "waiting_requirement_clarification_continue": "requirement",
    "waiting_requirement_approval": "requirement",
    "repo_discovery": "design",
    "design_drafting": "design",
    "design_investigating": "design",
    "design_revalidating": "design",
    "design_validating": "design",
    "design_repairing": "design",
    "design_restarting": "design",
    "design_review": "design",
    "implementation_planning": "plan",
    "implementation_plan_revalidating": "plan",
    "implementation_plan_validating": "plan",
    "implementation_plan_repairing": "plan",
    "implementation_preparing": "developer",
    "implementation_writing": "developer",
    "implementation_repair": "developer",
    "implementation_testing": "verification",
    "qa_testing": "qa",
    "runtime_closure": "runtime_closure",
    "browser_readiness": "browser",
    "browser_journey": "browser",
    "ui_polish": "browser",
    "browser_qa": "browser",
    "documentation_closure": "documentation",
    "final_verification": "final_verification",
    "code_review_publishing": "review",
    "code_review_syncing": "review",
    "code_review": "review",
    "code_review_approval": "review",
    "code_review_approved": "review",
}


@dataclass(frozen=True)
class RewindTarget:
    """A node `rewind_run` can send a run back to: the stage it resets first and how (G1.4)."""

    stage: str
    reset: str = "restart"  # "restart": back to the stage entry; "rerun": repeat this node inside the stage
    browser_status: str = ""  # `browser_stages.status` the node expects, for browser nodes


# Phase 4 stages that own state, in pipeline order. Rewinding to a stage also restarts every later stage.
REWIND_STAGES = ("journey", "polish", "qa", "documentation", "final_verification", "publication")

REWIND_TARGETS = {
    "browser_ready": RewindTarget("journey", browser_status="browser_ready"),
    "browser_baseline_captured": RewindTarget("polish", browser_status="baseline_captured"),
    "ui_polished": RewindTarget("qa", browser_status="ui_polished"),
    "browser_qa_written": RewindTarget("qa", reset="rerun", browser_status="browser_qa_written"),
    "qa_tested": RewindTarget("documentation"),
    "documentation_approved": RewindTarget("final_verification"),
}


@dataclass(frozen=True)
class RepairBudget:
    """A Phase 4 repair budget: its default limit, where the run counts rounds, the block a spent budget raises."""

    setting: str
    counter: tuple[str, ...]
    error_code: str


# The owner can raise these per run with `adjust_budget` (G1.6); a run blocked by a spent budget then continues
# from the step that hit it. Phase 4 implementation and QA repairs keep their existing owner paths
# (`continue_run` overrides, `revalidate_implementation`).
REPAIR_BUDGETS = {
    # Runtime closure repairs share the implementation repair limit, not its counter.
    "runtime_closure_repair": RepairBudget(
        "implementation_repair_limit", ("runtime_closure", "repair_iterations"), "runtime_closure_repair_limit_exceeded"
    ),
    "browser_readiness_repair": RepairBudget(
        "browser_readiness_repair_limit",
        ("browser_stages", "repair_iterations", "readiness"),
        "browser_readiness_repair_limit_exceeded",
    ),
    "browser_polish_repair": RepairBudget(
        "browser_polish_repair_limit", ("browser_stages", "repair_iterations", "polish"), "ui_polish_repair_limit_exceeded"
    ),
    # Journey code repairs and Browser QA code repairs spend this one shared budget.
    "browser_qa_repair": RepairBudget(
        "browser_qa_repair_limit", ("browser_stages", "repair_iterations", "qa"), "browser_qa_repair_limit_exceeded"
    ),
    "browser_journey_patch": RepairBudget(
        "browser_journey_patch_limit", ("browser_stages", "journey_patch_iterations"), "browser_journey_patch_limit_exceeded"
    ),
}


@dataclass(frozen=True)
class VerificationRoute:
    """Where a failed verification goes (G1.6b): code failures to the lane's repair, the rest blocks the run."""

    repair: str  # route method for an `implementation` failure
    environment_error: str  # retryable block for an `environment` failure
    policy_error: str  # block for any other failure


_BROWSER_QA_VERIFICATION = VerificationRoute(
    "_route_browser_qa_repair", "browser_qa_environment_failed", "browser_qa_verification_policy_failed"
)
VERIFICATION_ROUTES = {
    "runtime_closure_verification": VerificationRoute(
        "_route_closure_repair", "runtime_closure_environment_failed", "runtime_closure_verification_policy_failed"
    ),
    "browser_readiness_verification": VerificationRoute(
        "_route_browser_readiness_repair",
        "browser_readiness_environment_failed",
        "browser_readiness_verification_policy_failed",
    ),
    "browser_journey_verification": VerificationRoute(
        "_route_browser_journey_repair", "browser_baseline_environment_failed", "browser_journey_verification_policy_failed"
    ),
    "ui_polish_verification": VerificationRoute(
        "_route_ui_polish_repair", "ui_polish_environment_failed", "ui_polish_verification_policy_failed"
    ),
    "browser_qa_initial_verification": _BROWSER_QA_VERIFICATION,
    "browser_qa_verification": _BROWSER_QA_VERIFICATION,
}

# Blocking findings of a semantic review, routed by the Validator's category; the first category with findings wins.
# Code before journey: a journey patched around wrong behavior would be patched again after the repair, and the
# review that follows every repair reports the journey defects still open.
REVIEW_BLOCKER_ROUTES = {
    "browser_journey_review": (
        ("implementation", "_route_browser_journey_repair"),
        ("journey_defect", "_route_browser_journey_patch"),
    ),
    "browser_qa_review": (
        ("implementation", "_route_browser_qa_repair"),
        ("journey_defect", "_route_browser_qa_journey_patch"),
    ),
}


# The run's LLM call ceiling in unattended mode (U1.2); `adjust_budget` sets it like a repair budget.
LLM_CALL_BUDGET = "llm_calls"


def node_for(status: str) -> Node | None:
    return NODES_BY_STATUS.get(status)


def continue_handler(workflow: object, state: dict[str, object]) -> str:
    """The handler `continue_run` takes for this state, or "" when the status cannot continue."""

    node = NODES_BY_STATUS.get(str(state.get("status") or ""))
    if node is None:
        return ""
    for route in node.continue_routes:
        if all(getattr(workflow, predicate)(state) for predicate in route.when):
            return route.handler
    return ""


def approval_for(status: str) -> Approval | None:
    node = NODES_BY_STATUS.get(status)
    return node.approve if node is not None else None


def step_review_for(status: str) -> StepReview | None:
    node = NODES_BY_STATUS.get(status)
    return node.step_review if node is not None else None


def dashboard_workflow() -> dict[str, object]:
    """Dashboard nodes and lookups, derived from the graph so new stages never go missing."""

    return {
        "nodes": [{"id": stage_id, "label": label} for stage_id, label in STAGES],
        "phase_nodes": dict(PHASE_STAGES),
        "status_nodes": {node.status: node.stage for node in NODES if node.stage},
    }
