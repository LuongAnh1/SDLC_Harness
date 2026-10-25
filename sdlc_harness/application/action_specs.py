"""Metadata action HTTP dùng chung cho xác thực request và Ops Dashboard."""

from __future__ import annotations

from typing import Any

from ..core.config import Settings


ACTION_SPECS: dict[str, dict[str, Any]] = {
    "answer_clarification": {
        "command": "answer-clarification",
        "required": {"question_id", "answer", "answered_by"},
        "optional": set(),
        "overrides": set(),
        "confirmation": "standard",
    },
    "approve_checkpoint": {
        "command": "approve-checkpoint",
        "required": set(),
        "optional": {"checkpoint_id"},
        "overrides": {
            "design_repair_limit",
            "design_validation_trials",
            "design_llm_call_budget",
            "implementation_plan_repair_limit",
            "implementation_repair_limit",
        },
        "confirmation": "confirm",
    },
    "clarify_more": {
        "command": "clarify-more",
        "required": set(),
        "optional": set(),
        "overrides": set(),
        "confirmation": "standard",
    },
    "continue_run": {
        "command": "continue-run",
        "required": set(),
        "optional": set(),
        "overrides": {"implementation_repair_limit", "qa_repair_limit"},
        "confirmation": "standard",
    },
    "sync_run": {
        "command": "sync-run",
        "required": set(),
        "optional": set(),
        "overrides": set(),
        "confirmation": "standard",
    },
    "revalidate_design": {
        "command": "revalidate-design",
        "required": set(),
        "optional": set(),
        "overrides": {"design_repair_limit", "design_validation_trials", "design_llm_call_budget"},
        "confirmation": "standard",
    },
    "revalidate_requirement": {
        "command": "revalidate-requirement",
        "required": set(),
        "optional": set(),
        "overrides": set(),
        "confirmation": "standard",
    },
    "revalidate_implementation_plan": {
        "command": "revalidate-implementation-plan",
        "required": set(),
        "optional": set(),
        "overrides": {"implementation_plan_repair_limit"},
        "confirmation": "standard",
    },
    "revalidate_implementation": {
        "command": "revalidate-implementation",
        "required": set(),
        "optional": set(),
        "overrides": {"implementation_repair_limit"},
        "confirmation": "standard",
    },
    "adopt_run_workspace": {
        "command": "adopt-run-workspace",
        "required": set(),
        "optional": set(),
        "overrides": set(),
        "confirmation": "confirm",
    },
    "cleanup_run_workspace": {
        "command": "cleanup-run-workspace",
        "required": set(),
        "optional": set(),
        "overrides": set(),
        "confirmation": "type_run_id",
    },
}


OVERRIDE_SETTINGS = {
    "design_repair_limit": ("SDLC_DESIGN_REPAIR_LIMIT", 0),
    "design_validation_trials": ("SDLC_DESIGN_VALIDATION_TRIALS", 1),
    "design_llm_call_budget": ("SDLC_DESIGN_LLM_CALL_BUDGET", 1),
    "implementation_plan_repair_limit": ("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT", 0),
    "implementation_repair_limit": ("SDLC_IMPLEMENTATION_REPAIR_LIMIT", 0),
    "qa_repair_limit": ("SDLC_QA_REPAIR_LIMIT", 0),
}


WORKFLOW_NODES = (
    {"id": "requirement", "label": "Requirement"},
    {"id": "design", "label": "Design"},
    {"id": "plan", "label": "Implementation Plan"},
    {"id": "developer", "label": "Developer"},
    {"id": "verification", "label": "Verification"},
    {"id": "validator", "label": "Validator"},
    {"id": "qa", "label": "QA"},
    {"id": "review", "label": "Draft PR / Review"},
)

PHASE_NODES = {
    "requirement_drafting": "requirement",
    "requirement_clarification": "requirement",
    "waiting_requirement_clarification_continue": "requirement",
    "waiting_requirement_approval": "requirement",
    "design_drafting": "design",
    "design_revalidating": "design",
    "design_validating": "design",
    "design_repairing": "design",
    "design_review": "design",
    "design_experiment": "design",
    "implementation_planning": "plan",
    "implementation_plan_revalidating": "plan",
    "implementation_plan_validating": "plan",
    "implementation_plan_repairing": "plan",
    "implementation_preparing": "developer",
    "implementation_writing": "developer",
    "implementation_repair": "developer",
    "qa_repair": "developer",
    "implementation_testing": "verification",
    "qa_testing": "qa",
    "code_review_publishing": "review",
    "code_review_syncing": "review",
    "code_review": "review",
    "code_review_approval": "review",
    "code_review_approved": "review",
}

STATUS_NODES = {
    "implementation_tested": "validator",
    "implementation_traced": "qa",
}


def public_dashboard_schema(settings: Settings) -> dict[str, object]:
    """Return safe declarative metadata consumed by the browser dashboard."""

    actions: dict[str, object] = {}
    for action, spec in ACTION_SPECS.items():
        actions[action] = {
            "required_arguments": sorted(spec["required"]),
            "optional_arguments": sorted(spec["optional"]),
            "integer_overrides": sorted(spec["overrides"]),
            "confirmation": spec["confirmation"],
        }
    return {
        "ok": True,
        "version": "dashboard_schema_v1",
        "run_sources": {
            "dataset": {"required": ["dataset", "use_case"]},
            "jira": {"required": ["issue_key"]},
        },
        "actions": actions,
        "workflow": {
            "nodes": [dict(node) for node in WORKFLOW_NODES],
            "phase_nodes": dict(PHASE_NODES),
            "status_nodes": dict(STATUS_NODES),
            "repair_defaults": {
                "design": settings.design_repair_limit,
                "plan": settings.implementation_plan_repair_limit,
                "implementation": settings.implementation_repair_limit,
                "qa": settings.qa_repair_limit,
            },
        },
        "refresh_seconds": {"snapshot": 5, "active_run": 3, "operation": 2},
    }
