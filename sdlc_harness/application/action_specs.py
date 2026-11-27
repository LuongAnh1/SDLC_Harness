"""Metadata action HTTP dùng chung cho xác thực request và Ops Dashboard."""

from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..workflows.graph import dashboard_workflow


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
    "challenge_assumption": {
        "command": "challenge-assumption",
        "required": {"assumption_id", "instruction"},
        "optional": set(),
        "overrides": set(),
        "actor_argument": "challenged_by",
        "confirmation": "standard",
    },
    "continue_run": {
        "command": "continue-run",
        "required": set(),
        "optional": {"step_review_call_id", "step_review_decision", "step_review_note"},
        "overrides": {"implementation_repair_limit"},
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
        "overrides": {"design_repair_limit", "design_llm_call_budget"},
        "confirmation": "standard",
    },
    "restart_design": {
        "command": "restart-design",
        "required": set(),
        "optional": set(),
        "overrides": {"design_repair_limit", "design_llm_call_budget"},
        "confirmation": "confirm",
    },
    "restart_plan": {
        "command": "restart-plan",
        "required": {"reason"},
        "optional": set(),
        "overrides": set(),
        "confirmation": "confirm",
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
    "run_until_checkpoint": {
        "command": "run-until-checkpoint",
        "required": set(),
        "optional": {"max_steps"},
        "overrides": set(),
        "confirmation": "standard",
    },
    "set_step_review": {
        "command": "set-step-review",
        "required": {"enabled", "reason"},
        "optional": set(),
        "overrides": set(),
        "confirmation": "confirm",
    },
    "rewind_run": {
        "command": "rewind-run",
        "required": {"node", "reason"},
        "optional": set(),
        "overrides": set(),
        "confirmation": "confirm",
    },
    "adjust_budget": {
        "command": "adjust-budget",
        "required": {"budget_id", "limit", "reason"},
        "optional": set(),
        "overrides": set(),
        "confirmation": "confirm",
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
    "design_llm_call_budget": ("SDLC_DESIGN_LLM_CALL_BUDGET", 1),
    "implementation_plan_repair_limit": ("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT", 0),
    "implementation_repair_limit": ("SDLC_IMPLEMENTATION_REPAIR_LIMIT", 0),
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
            **dashboard_workflow(),
            "repair_defaults": {
                "design": settings.design_repair_limit,
                "plan": settings.implementation_plan_repair_limit,
                "implementation": settings.implementation_repair_limit,
            },
        },
        "refresh_seconds": {"snapshot": 5, "active_run": 3, "operation": 2},
    }
