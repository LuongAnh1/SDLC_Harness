"""The Developer action prompt: what the model sees for one execution unit at one step."""

from __future__ import annotations

import json
from pathlib import Path
import re

from ...core.coerce import dict_items as _dict_items, dict_value as _dict_value, string_items as _string_items
from .unit_facts import (
    _MAX_BATCH_OBSERVATIONS,
    _MUTATION_ACTIONS,
    _OBSERVATION_ACTIONS,
    _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX,
    _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER,
    _clip_json_value,
    _compact_diagnostic_reason,
    _current_passed_stages,
    _execution_phase,
    _failing_test_name,
    _file_hash,
    _has_successful_target_inspection,
    _has_test_authoring_context,
    _is_typescript_test_path,
    _item_context_paths,
    _item_contract_anchor_paths,
    _item_dependency_paths,
    _item_diagnostic_paths,
    _item_execution_hash_paths,
    _item_files,
    _item_paths,
    _json_hash,
    _package_dependency_statuses,
    _read_path_allowed,
    _required_test_mutation_paths,
    _timeout_diagnostic_cleanup_paths,
    _timeout_diagnostic_state,
    _verification_repair_state,
)


_WORKING_MEMORY_LEDGER_SOFT_LIMIT = 16


_WORKING_MEMORY_RECENT_RESULT_LIMIT = 4


_WORKING_MEMORY_BREADCRUMB_LIMIT = 8


def _action_prompt(
    *,
    item: dict[str, object],
    manifest: dict[str, object],
    history: list[dict[str, object]],
    target_path: Path,
    max_write_chars: int,
    force_incremental: bool,
    action_index: int,
    max_actions: int,
    mutated: bool,
    context_window_tokens: int,
    working_memory_evidence_chars: int | None = None,
    uiux_available: bool = False,
    uiux_guidance: list[dict[str, object]] | None = None,
    visual_profile_required: bool = False,
    app_visual_profile: dict[str, object] | None = None,
    visual_profile_candidates: list[dict[str, object]] | None = None,
    visual_app_scope_id: str = "",
    journal: dict[str, object] | None = None,
) -> str:
    repair_mode = str(manifest.get("mode") or "").endswith("_repair")
    context_repair = str(manifest.get("mode") or "") == "implementation_repair"
    feedback_source = str(_dict_value(manifest.get("repair_feedback")).get("feedback_source") or "")
    unit_repair_context = _developer_repair_context(manifest.get("repair_feedback"), item=item)
    # A Validator or browser-stage repair also runs the dependency closure of the cited units. When no finding is
    # projected onto this unit, nothing authorizes a mutation unless one of its own required stages fails.
    dependency_context_only = (
        repair_mode
        and (
            (context_repair and feedback_source == "validator_code_trace")
            or _repair_feedback_unit_scoped(manifest.get("repair_feedback"))
        )
        and not _dict_items(_dict_value(unit_repair_context.get("repair_objective")).get("contract_findings"))
        and not _dict_items(unit_repair_context.get("missing_links"))
    )
    paths = _item_context_paths(item)
    diagnostic_paths = _item_diagnostic_paths(item)
    anchor_paths = _item_contract_anchor_paths(item)
    handled_paths = _handled_paths(history, required_paths=paths)
    inspected_paths = _inspected_paths(history, required_paths=paths)
    current_hashes = {
        path: _file_hash(target_path / path) for path in _item_execution_hash_paths(item)
    }
    package_dependency_statuses = _package_dependency_statuses(
        target_path=target_path,
        item=item,
        run_id=str(manifest.get("run_id") or ""),
    )
    pending_package_dependency_ids = [
        str(status.get("dependency_id") or "")
        for status in package_dependency_statuses
        if status.get("satisfied") is not True
    ]
    pending_stage_ids = sorted(
        set(_string_items(item.get("required_stage_ids")))
        - _current_passed_stages(history, current_hashes=current_hashes)
    )
    repair_state = _verification_repair_state(history)
    timeout_diagnostic = _timeout_diagnostic_state(history)
    timeout_cleanup_paths = _timeout_diagnostic_cleanup_paths(
        target_path=target_path,
        item_paths=_item_paths(item),
    )
    failed_stage_ids = sorted(repair_state)
    verification_ready_stage_ids = sorted(
        stage_id
        for stage_id, episode in repair_state.items()
        if episode.get("verification_ready") is True
    )
    verification_forced_stage_ids = sorted(
        stage_id
        for stage_id, episode in repair_state.items()
        if episode.get("verification_forced") is True
    )
    no_change_verification_stage_ids = (
        pending_stage_ids
        if repair_mode
        and not mutated
        and not failed_stage_ids
        and bool(paths)
        and set(paths) <= inspected_paths
        else []
    )
    phase = _execution_phase(
        history=history,
        mutated=mutated,
        action_index=action_index,
        max_actions=max_actions,
        pending_stage_ids=pending_stage_ids,
        failed_stage_ids=failed_stage_ids,
    )
    evidence_budget = max(
        8000,
        min(
            int(working_memory_evidence_chars or 96000),
            context_window_tokens * 2,
        ),
    )
    action_memory = _action_memory(
        history,
        target_item_paths=paths,
        max_evidence_chars=evidence_budget,
    )
    current_sources = _current_mutated_sources(
        history,
        target_path=target_path,
        target_item_paths=paths,
        max_evidence_chars=evidence_budget,
    )
    previously_omitted_sources = _dict_items(action_memory.pop("omitted_sources", []))
    authoritative_sources, omitted_sources = _authoritative_sources(
        inspected_sources=_dict_items(action_memory.pop("inspected_sources", [])),
        current_sources=current_sources,
        target_path=target_path,
        max_evidence_chars=evidence_budget,
    )
    action_memory["authoritative_sources"] = authoritative_sources
    if uiux_guidance:
        action_memory["uiux_guidance"] = uiux_guidance
    fully_current_paths = {
        str(source.get("path") or "")
        for source in authoritative_sources
        if source.get("origin") == "worktree_after_mutation"
        and source.get("truncated") is not True
    }
    total_lines_by_path = {
        str(source.get("path") or ""): int(source.get("total_lines") or 0)
        for source in authoritative_sources
        if source.get("total_lines")
    }
    combined_omitted_sources = [
        {
            **source,
            **(
                {"end_line": total_lines_by_path[str(source.get("path") or "")]}
                if source.get("reason") == "repo_read_partial"
                and total_lines_by_path.get(str(source.get("path") or ""))
                else {}
            ),
        }
        for source in [*previously_omitted_sources, *omitted_sources]
        if str(source.get("path") or "") not in fully_current_paths
    ]
    if combined_omitted_sources:
        action_memory["omitted_sources"] = combined_omitted_sources
    if failed_stage_ids:
        action_memory["recent_results"] = [
            row
            for row in _dict_items(action_memory.get("recent_results"))
            if not (
                row.get("action") == "verification.run_stage"
                and _dict_value(row.get("result")).get("status") == "failed"
            )
        ]
    new_test_targets = [
        path
        for path in _item_paths(item)
        if _is_typescript_test_path(path) and not (target_path / path).is_file()
    ]
    test_context_ready = not new_test_targets or _has_test_authoring_context(
        history,
        target_test_path=new_test_targets[0],
    )
    test_authoring_policy: dict[str, object] = {
        "new_test_targets": new_test_targets,
        "repository_evidence_required": bool(new_test_targets),
        "repository_evidence_satisfied": test_context_ready,
        "test_intent_mutation_required": item.get(
            "test_intent_mutation_required", True
        ),
        "required_test_mutation_paths": _required_test_mutation_paths(
            item,
            repair_mode=repair_mode,
        ),
    }
    app_visual_profile = dict(app_visual_profile or {})
    visual_profile_candidates = list(visual_profile_candidates or [])
    visual_profile_pending = visual_profile_required and not app_visual_profile
    unit_action_names = (
        "repo.list|repo.read|repo.read_batch|repo.search|repo.search_batch|repo.read_diff|"
        "codegraph.query|codegraph.query_readonly|"
        + ("uiux.search_guidance|" if uiux_available else "")
        + (
            "uiux.search_visual_profile|uiux.commit_visual_profile|"
            if visual_profile_pending
            else ""
        )
        + "repo.write_file|repo.apply_patch|repo.replace_text|package.ensure_workspace_dependency|"
        "verification.run_stage|execution.complete_unit"
    )
    action_schema = {
        "action": (
            unit_action_names
        ),
        "arguments": {},
        "summary": (
            "short current hypothesis or observation plus the next-action reason; "
            "never claim more than the tool evidence proves"
        ),
    }
    if verification_forced_stage_ids:
        action_schema["action"] = "verification.run_stage"
    if no_change_verification_stage_ids:
        action_schema["action"] = "verification.run_stage"
    if pending_package_dependency_ids:
        action_schema["action"] = "package.ensure_workspace_dependency"
    if visual_profile_pending:
        action_schema["action"] = (
            "uiux.search_visual_profile|uiux.commit_visual_profile"
            if visual_profile_candidates
            else "uiux.search_visual_profile"
        )
    active_failures = [
        _compact_repair_episode(repair_state[stage_id]) for stage_id in failed_stage_ids
    ]
    if new_test_targets:
        test_authoring_policy["rules"] = [
            "Inspect an existing repository test or search/CodeGraph evidence for its test and mocking conventions before creating a new TypeScript test.",
            "Test doubles must satisfy the exact production runtime predicates, including instanceof checks.",
            "Hoisted module-mock factories may reference only values initialized through the test framework's supported hoisting mechanism.",
            "Do not add production props, branches, or initial-state hooks solely to make a test renderer reach otherwise interactive state.",
        ]
    test_authoring_instruction = (
        "Before creating a new TypeScript test, satisfy test_authoring_policy with repository evidence. "
        "Never add production props, branches, or initial-state hooks solely to make a test renderer reach otherwise interactive state. "
        "Test doubles must satisfy exact production predicates such as instanceof; never replace a nominal runtime "
        "check with a shape-only fake. Respect the test framework's module-mock hoisting rules. "
        if new_test_targets
        else ""
    )
    mandatory_obligations = _unit_mandatory_obligations(item)
    shared_finding_progress = _shared_finding_progress(
        item=item,
        repair_context=unit_repair_context,
        journal=journal,
    )
    context = {
        "active_focus": {
            "unit_id": item.get("id"),
            "goal": item.get("objective") or item.get("title"),
            "phase": phase,
            "failed_stage_ids": failed_stage_ids,
            "mandatory_obligations": mandatory_obligations,
            "known_facts": (
                [
                    "Every source in working_memory.authoritative_sources is verified against the current worktree.",
                    "Do not read an authoritative source again unless a successful mutation changed it.",
                ]
                if action_memory["authoritative_sources"]
                else []
            ),
        },
        "execution_task": _focused_execution_task(
            item,
            context_paths=paths,
            active_repair=bool(failed_stage_ids),
        ),
        "execution": {"mode": manifest.get("mode"), "base_sha": manifest.get("base_sha")},
        "dependency_receipts": _completed_dependency_receipts(item=item, manifest=manifest, journal=journal)
        + [
            status
            for status in package_dependency_statuses
            if status.get("satisfied") is True
        ],
        "pending_package_dependencies": [
            status
            for status in package_dependency_statuses
            if status.get("satisfied") is not True
        ],
        "developer_generation_context": manifest.get("developer_generation_context"),
        "repair_feedback": unit_repair_context,
        **({"shared_finding_progress": shared_finding_progress} if shared_finding_progress else {}),
        "repair_progress": {
            "workspace_allowed_paths": _item_paths(item),
            "diagnostic_paths": diagnostic_paths,
            "contract_anchor_paths": anchor_paths,
            "repair_hints": _developer_unit_repair_hints(item),
            "mutated_context_paths": sorted(handled_paths),
            "inspected_context_paths": sorted(inspected_paths),
            "completion_rule": (
                "No Validator finding is assigned to this unit. Complete it unchanged: every unit path needs a "
                "verified_unchanged disposition backed by fresh inspection, and every required stage must pass on "
                "the current hashes. A mutation is allowed only to repair a failing required stage."
                if dependency_context_only
                else "Apply one coherent real mutation, including every related approved file needed to keep the worktree "
                "internally consistent, then call complete so Verification can rerun. Diagnostic paths are hints, "
                "not a mutation boundary. A no-change completion requires inspected evidence and a specific reason."
                if context_repair
                else "Complete after one coherent mutation; the owning quality gate will rerun. If fresh inspection "
                "proves this unit already satisfies its contract, run every required stage on the current hashes, then "
                "complete with every unit path marked verified_unchanged and a specific reason."
            ),
        },
        "target_state": {
            path: {"exists": (target_path / path).is_file(), "sha256": _file_hash(target_path / path)}
            for path in paths
        },
        "action_budget": {
            "current_action": action_index,
            "max_actions": max_actions,
            "remaining_including_current": max_actions - action_index + 1,
            "phase": phase,
            "pending_stage_ids": pending_stage_ids,
            "failed_stage_ids": failed_stage_ids,
            "verification_ready_stage_ids": verification_ready_stage_ids,
            "verification_forced_stage_ids": verification_forced_stage_ids,
            "no_change_verification_stage_ids": no_change_verification_stage_ids,
            "pending_package_dependency_ids": pending_package_dependency_ids,
        },
        "active_failures": active_failures,
        "test_authoring_policy": test_authoring_policy,
        "working_memory": action_memory,
    }
    item_surfaces = {value.strip().lower() for value in _string_items(item.get("surfaces"))}
    if visual_profile_required or (app_visual_profile and "ui" in item_surfaces):
        context["app_visual_profile"] = {
            "app_scope_id": visual_app_scope_id,
            "status": "committed" if app_visual_profile else "required",
            "authority": (
                "Presentation details only. Exact approved Design contracts always win; this profile cannot add "
                "flows, components, dependencies, runtime behavior, or write paths."
            ),
            **({"profile": app_visual_profile} if app_visual_profile else {}),
            **({"candidates": visual_profile_candidates} if visual_profile_candidates else {}),
        }
    qa_acceptance = _developer_read_only_qa_acceptance(item=item, manifest=manifest)
    if qa_acceptance:
        context["read_only_qa_acceptance"] = qa_acceptance
    if timeout_diagnostic or timeout_cleanup_paths:
        context["timeout_diagnostic_protocol"] = {
            "version": "timeout_diagnostic_v1",
            "status": timeout_diagnostic.get("status") if timeout_diagnostic else "cleanup_required",
            "stage_id": timeout_diagnostic.get("stage_id") if timeout_diagnostic else "",
            "test_paths": timeout_diagnostic.get("test_paths") if timeout_diagnostic else timeout_cleanup_paths,
            "source_marker": _TIMEOUT_DIAGNOSTIC_SOURCE_MARKER,
            "failure_prefix": _TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX,
            "cleanup_required_paths": timeout_cleanup_paths,
            "rules": [
                "Treat an unlocated timeout as an evidence gap, not a confirmed root cause.",
                "Instrument bounded suspected await points in the selected test before changing production behavior.",
                "After instrumentation succeeds, immediately rerun the failed verification stage.",
                "Remove all diagnostic instrumentation after the failing await is identified; completion is blocked while markers remain.",
            ],
        }
    extra = (
        "The previous response exceeded the output budget. Use write_file for a small new file or replace_text for "
        "one exact existing block. Do not return a large complete file or multi-hunk patch in one action."
        if force_incremental
        else ""
    )
    phase_instruction = {
        "inspect_or_edit": (
            "Inspect only evidence that is still missing. Do not repeat an observation already recorded in "
            "working_memory.action_ledger. Start editing as soon as the target change is clear."
        ),
        "edit_required": (
            "The inspection budget is exhausted. Your next action MUST be write_file, replace_text, or apply_patch for an "
            "approved execution-unit path. Use working_memory.authoritative_sources; do not read, list, or search again."
        ),
        "edit_or_complete": (
            "At least one mutation succeeded. Continue only when another change is necessary for the same coherent "
            "fix; otherwise call complete and let Verification run."
        ),
        "edit_or_verify": (
            "A mutation succeeded. Finish only the edits needed for this unit, then run every pending verification "
            "stage before proposing completion. Do not call completion while pending_stage_ids is non-empty."
        ),
        "verification_required": (
            "The remaining budget is reserved for pending verification stages plus completion. Your next action "
            "MUST be verification.run_stage for one pending_stage_id."
        ),
        "verification_repair_required": (
            "A verification repair episode is active. Review active_failures plus "
            "working_memory.authoritative_sources, then make all coherent production, caller, and test changes needed. "
            "Use a read/search action only for a different dependency whose evidence is absent from authoritative_sources. "
            "Call verification.run_stage only when the repair batch is "
            "internally consistent and at least one mutation followed the latest failure; do not complete the unit. "
            "When verification_ready_stage_ids is non-empty, rerun one of those stages unless you have a materially "
            "different repair; never resend a payload that already had no effect."
        ),
        "complete_required": "A mutation succeeded and only one action remains. You MUST call complete now.",
    }[phase]
    if dependency_context_only and phase == "inspect_or_edit":
        phase_instruction = (
            "Inspect only unit paths whose current bytes are still missing; do not repeat an observation already "
            "recorded in working_memory.action_ledger. Once every unit path is inspected, run the pending "
            "verification stages."
        )
    if no_change_verification_stage_ids:
        phase_instruction = (
            "Fresh inspection indicates this repair unit may be unchanged. Your next and only allowed action MUST be "
            f"verification.run_stage for one of {no_change_verification_stage_ids}. After all required stages pass "
            "on the current hashes, complete with every unit path marked verified_unchanged and a specific reason."
        )
    if failed_stage_ids:
        work_instruction = (
            "This is an active verification repair, not a fresh implementation pass. The current unit sources have "
            "already been inspected and authoritative_sources is the verified current worktree view. Diagnose the "
            "active failure from that evidence, then make the smallest coherent production or test mutation. Do not "
            "restart inspection of a source already present in authoritative_sources."
        )
    elif not repair_mode:
        work_instruction = (
            "Advance the approved unit one tool action at a time. When exact current bytes are already present, prefer "
            "a bounded mutation over more planning or rereading. Do not predesign or serialize every remaining file; "
            "Verification and Validator assess the unit only after the required edits are coordinated."
        )
    elif feedback_source == "verification":
        work_instruction = (
            "This is a Verification repair pass. The approved requirement, design, and work packet are the contract; "
            "the latest verification field is the current diagnostic and worktree files are the current state. Find "
            "the root cause and make the smallest coherent fix across any workspace_allowed_paths that must change. "
            "Diagnostic and contract-anchor paths are inspection hints only. Keep production code, callers, and tests "
            "consistent; do not regenerate unrelated files. Then call complete for Verification to rerun."
        )
    elif dependency_context_only:
        work_instruction = (
            "Validator assigned no finding to this unit; it runs only as dependency context for the units that "
            "carry the findings. Do not mutate it to anticipate their repair or to tidy existing behavior. Inspect "
            "each unit path once, run every required verification stage on the current hashes, then call complete "
            "with every path disposition verified_unchanged. Mutate only to repair a required stage that fails."
        )
    elif feedback_source == "validator_code_trace":
        work_instruction = (
            "Verification already passed. Implement one coherent bounded slice from Validator's repair_objective. "
            "Make each affected contract match its expected_signature and update only the dependent callers, "
            "implementations, or tests needed for that slice. Context paths may remain unchanged. Then call complete."
        )
    else:
        work_instruction = (
            "Repair the reported quality feedback from the current worktree as one coherent bounded slice. Context "
            "paths may be inspected without modification. Call complete when the mutation is ready for its quality gate."
        )
    if repair_mode and not mutated and not dependency_context_only:
        work_instruction += (
            " If fresh inspection proves this unit already satisfies its contract, run every pending required stage "
            "on the current hashes, then complete with every unit path marked verified_unchanged and a specific reason."
        )
    if timeout_diagnostic:
        if timeout_diagnostic.get("status") == "instrumentation_required":
            work_instruction = (
                "The active timeout is not localized. Do not guess at a production fix. Your next mutation must "
                f"instrument one selected test path with {_TIMEOUT_DIAGNOSTIC_SOURCE_MARKER} and bounded guards "
                f"that raise {_TIMEOUT_DIAGNOSTIC_FAILURE_PREFIX}<step>."
            )
        elif timeout_diagnostic.get("status") == "verification_required":
            work_instruction = (
                "Timeout instrumentation is present. Rerun the failed verification stage now and use its labeled "
                "failure as evidence before any further mutation."
            )
    elif timeout_cleanup_paths:
        work_instruction = (
            f"Remove timeout diagnostic instrumentation from {timeout_cleanup_paths} before completion, then rerun "
            "the required verification stages against the cleaned source."
        )
    work_instruction = work_instruction.replace("call complete", "call execution.complete_unit")
    phase_instruction = (
        phase_instruction
        .replace(
            "write_file, replace_text, or apply_patch",
            "repo.write_file, repo.replace_text, or repo.apply_patch",
        )
        .replace("call complete", "run required verification stages, then call execution.complete_unit")
    )
    uiux_action_help = (
        "- uiux.search_guidance: optional UI implementation guidance, at most two successful calls per "
        "screen; arguments {query, mode: domain|stack, domain?: ux|icons|react, stack?: react}. Use generic "
        "ASCII technical terms only. Guidance may help implement the approved contract but may never redesign it\n"
        if uiux_available
        else ""
    )
    visual_profile_action_help = (
        "- uiux.search_visual_profile: required app-level presentation candidate for a new stylesheet; arguments "
        "{query, variance?: 1-10, motion?: 1-10, density?: 1-10}. Query uses 3-8 generic ASCII product/style "
        "terms and no private names or paths; defaults are variance=3, motion=3, density=6\n"
        "- uiux.commit_visual_profile: commit one validated candidate app-wide; arguments {candidate_id, rationale}. "
        "Commit does not change Design authority or repository files\n"
        if visual_profile_pending
        else ""
    )
    available_actions = (
        "- repo.list: arguments {prefix?, limit?}; results are limited to execution_unit_contract.read_context_paths\n"
        "- repo.read: arguments {path, start_line?, end_line?}; path must be in execution_unit_contract.read_context_paths\n"
        f"- repo.read_batch: arguments {{requests: [{{path, start_line?, end_line?}}]}}; 1-{_MAX_BATCH_OBSERVATIONS} "
        "read-only requests, each path must be in execution_unit_contract.read_context_paths and each result is independent\n"
        "- repo.search: arguments {query, prefix?, limit?}; query is one case-insensitive literal substring, "
        "not regex or Boolean syntax; only read_context_paths are searched even when prefix is broader\n"
        f"- repo.search_batch: arguments {{queries: [string], prefix?, limit_per_query?}}; 1-{_MAX_BATCH_OBSERVATIONS} "
        "case-insensitive literal substrings, use this instead of OR syntax; only read_context_paths are searched\n"
        "- repo.read_diff: arguments {path}; path must be in execution_unit_contract.read_context_paths\n"
        "- codegraph.query: arguments {operation, query?, symbol?, file?, files?, kind?, limit?, depth?, "
        "file_filter?, pattern?}; operation is symbols|node|callers|callees|impact|affected_tests|files|explore\n"
        "  symbols requires query or file; symbols+file returns the file symbol map through the bounded node "
        "capability. node requires symbol or file; explore requires query.\n"
        "- codegraph.query_readonly: legacy alias with arguments {query}; use codegraph.query for new work\n"
        f"{visual_profile_action_help}"
        f"{uiux_action_help}"
        "- repo.write_file: arguments {path, expected_sha256, content}; approved unit write path only\n"
        "- repo.replace_text: arguments {path, expected_sha256, old_text, new_text}; replace one exact unique "
        "block in an existing approved unit path\n"
        "- repo.apply_patch: arguments {path, expected_sha256, patch}; unified diff for an approved unit path\n"
        "- verification.run_stage: arguments {stage_id}; stage_id must be listed in required_stage_ids\n"
        "- execution.complete_unit: arguments {summary, path_dispositions: [{path, status, reason}]}; status is "
        "mutated|verified_unchanged|not_required. Every unit path needs a disposition, non-mutated paths need "
        "fresh repo.read/repo.read_diff evidence, and every required stage must pass against current hashes. "
        + (
            "Every listed required_test_mutation_path must be mutated in this unit; a passing pre-existing test "
            "file is not coverage evidence for a directly affected intent\n"
            if _required_test_mutation_paths(item, repair_mode=repair_mode)
            else "This recovery reuses previously authored test intents. Do not mutate tests merely to recreate "
            "coverage evidence; run the required stages against the current tests instead.\n"
        )
    )
    context["execution_unit_contract"] = {
        "unit_id": item.get("id"),
        "depends_on": item.get("depends_on", []),
        "write_paths": _item_paths(item),
        "read_context_paths": _item_context_paths(item),
        "contract_anchor_paths": _item_contract_anchor_paths(item),
        "requirement_refs": item.get("requirement_refs", []),
        "acceptance_refs": item.get("acceptance_refs", []),
        "design_node_ids": item.get("design_node_ids", []),
        "planned_code_targets": _dict_items(item.get("planned_code_targets")),
        "migration_impact": _dict_value(item.get("migration_impact")),
        "behavior_contract_source": "active_focus.mandatory_obligations",
        "required_stage_ids": item.get("required_stage_ids", []),
        "test_target_paths": item.get("test_target_paths", []),
        "required_test_mutation_paths": _required_test_mutation_paths(
            item,
            repair_mode=repair_mode,
        ),
        "test_intent_mutation_required": item.get(
            "test_intent_mutation_required", True
        ),
        "package_dependencies": _dict_items(item.get("package_dependencies")),
        "package_dependency_statuses": package_dependency_statuses,
        "package_tool_owned_paths": _item_dependency_paths(item),
        "approved_signatures": _item_approved_signatures(item),
        "hard_constraints": [
            "Approved signatures are immutable API-shape constraints, not authorization to implement behavior. They override conflicting prose only for qualified name, parameters, async shape, and return type.",
            "Preserve each qualified_name, parameter type/order/optional/rest shape, async flag, and return_type exactly. Parameter names are evidence, not contract; destructuring is allowed when that shape is preserved.",
            "Treat planned code targets, migration impact, and active_focus.mandatory_obligations as approved execution constraints.",
            "Implement behavior only when active_focus.mandatory_obligations for this unit requires it. A parameter or result type present only in an approved signature may reserve compatibility for another unit; it must not by itself create a branch, side effect, result value, or domain error.",
            "For non-UI work, preserve every exact entity field, operation, relation, constraint, and migration intent in active_focus.mandatory_obligations.approved_non_ui_contracts; never invent convenience fields or sibling API behavior.",
            "Preserve each boundary input/output/error mapping and runtime attachment in active_focus.mandatory_obligations; inspect related approved paths instead of inventing a different contract.",
            "For UI work, implement the approved experience contract including structure, feedback, accessibility, responsive behavior, and focus flow.",
            "For UI work, autoFocus alone does not prove focus containment or post-action focus restoration, and CSS class names alone do not prove responsive behavior without matching style evidence.",
            "Before mutating a boundary consumer, inspect its unread contract_anchor_paths; they are read-only context and never expand write_paths.",
            "Repository reads, diffs, listings and searches are confined to read_context_paths. CodeGraph topology may name a connected out-of-unit path, but it does not grant source access to that path; leave its inspection and repair to the later unit that declares it as context.",
            "Package manifests and lockfiles in package_tool_owned_paths may be changed only by package.ensure_workspace_dependency.",
            "Use compatible control flow or domain errors only when the current unit's mandatory obligations explicitly require that behavior; do not infer behavior from signature shape alone and do not change an approved contract.",
        ],
    }
    forced_stage_ids = verification_forced_stage_ids or no_change_verification_stage_ids
    if forced_stage_ids:
        available_actions = (
            "- verification.run_stage: arguments {stage_id}; stage_id MUST be one of "
            f"{forced_stage_ids}\n"
        )
        if verification_forced_stage_ids:
            phase_instruction = (
                "Harness rejected an exact no-effect mutation. Your next and only allowed action MUST be "
                f"verification.run_stage for one of {verification_forced_stage_ids}."
            )
    if pending_package_dependency_ids:
        available_actions = (
            "- package.ensure_workspace_dependency: arguments {dependency_id}; dependency_id MUST be one of "
            f"{pending_package_dependency_ids}\n"
        )
        phase_instruction = (
            "An approved local workspace dependency is not synchronized between its consumer manifest and "
            "npm lockfile. Your next and only allowed action MUST be package.ensure_workspace_dependency for "
            f"one of {pending_package_dependency_ids}. Do not inspect node_modules or edit package metadata "
            "with repo.write_file."
        )
    if visual_profile_pending:
        available_actions = visual_profile_action_help
        work_instruction = (
            "Establish one app-level presentation profile before reading or changing this new UI. The profile "
            "fills only visual implementation details; it must not reinterpret the approved flow or Design contract."
        )
        phase_instruction = (
            "Call uiux.search_visual_profile when no valid candidate exists. After reviewing a validated candidate, "
            "call uiux.commit_visual_profile with its exact candidate_id. At most two candidates may be evaluated."
        )
    context["next_action"] = {
        "response_scope": (
            "Choose and emit exactly one tool action. Do not plan or draft a later file or action before the Harness "
            "returns this action's result; completing the whole unit is not required in this response."
        ),
        "unit_strategy": work_instruction,
        "instruction": phase_instruction,
        "consistency_boundary": (
            "Coordinate all approved files before verification or completion. An intermediate single-file mutation "
            "may leave related approved edits for later actions."
        ),
        "editing_rule": (
            "For an existing file, prefer repo.replace_text with an exact unique old_text block and its replacement. "
            "Use repo.apply_patch only for one small localized hunk; do not reconstruct the whole file or manually "
            "recalculate later hunk offsets. Use repo.write_file for a small new file or when the complete replacement "
            "safely fits the write budget."
        ),
        "design_rule": (
            "Implement the explicit mandatory obligations without inventing sibling controls, alternate flows, or a "
            "new interaction model. Preserve unrelated observable behavior, including timing, fallback, and error "
            "handling, unless an obligation explicitly changes it. When the contract permits multiple presentations, "
            "choose the smallest compatible extension once and proceed; do not redesign it again without new tool or "
            "verification evidence."
        ),
    }
    signature_contract_ref = (
        "execution_unit_contract.approved_signatures"
    )
    return (
        f"Developer Tool Action {'v2'}\n\n"
        "Return exactly one JSON object matching this shape:\n"
        f"{json.dumps(action_schema, ensure_ascii=False)}\n\n"
        "Available actions:\n"
        f"{available_actions}\n"
        f"A mutation's text payload must stay below {max_write_chars} characters. {extra}\n"
        f"Treat {signature_contract_ref} as immutable API-shape constraints, not behavior authorization. They override "
        "conflicting prose only for qualified name, parameter type/order/optional/rest shape, async flag, and return "
        "type. Parameter names are evidence rather than contract, so destructuring is allowed when the approved shape "
        "is preserved. Implement behavior only from active_focus.mandatory_obligations for this unit; a signature-only "
        "parameter or result reserves compatibility and must not create a branch, side effect, result, or domain error. "
        "Treat active_focus.mandatory_obligations as the single canonical behavior "
        "checklist; execution_task and the short goal are navigation summaries, not competing contracts. "
        "Choose exactly one next action, emit its JSON object, and end the response immediately. Only Harness "
        "tool result fields in working_memory prove execution or source facts. Treat authoritative_sources as the "
        "single verified current "
        "worktree view; each entry includes its freshness and verification action. Do not read the same "
        "path/range again unless a successful mutation changed that file. If working_memory.omitted_sources lists "
        "a path/range, its bytes are not present in this prompt; do not claim they were inspected, and request only "
        "the missing range when it is required. Do not output source code "
        "outside the JSON action. working_memory.agent_hypotheses and summary text in diagnostic_breadcrumbs or "
        "graph_evidence are unverified notes written by a prior model response, not evidence; overturn them whenever "
        "authoritative source bytes or tool-result fields disagree. Use working_memory.graph_evidence to locate "
        "symbols and call paths. Before "
        "changing an existing shared or boundary symbol, obtain node or impact evidence unless fresh seed evidence "
        "already covers it. CodeGraph never replaces repo.read/repo.read_diff for exact current bytes and hashes, "
        "and never expands execution_unit_contract.read_context_paths or write_paths. Do not query or inspect source "
        "for a connected path unless it appears in read_context_paths. "
        "In summary, state the conclusion established by the latest relevant evidence "
        "and why the chosen action follows. Keep it brief; do not include chain-of-thought. "
        f"{test_authoring_instruction}"
        "Follow next_action for the current response. Do not design, serialize, or validate a later file or action "
        "before the Harness returns the current tool result. Do not speculate about outcomes assigned to pending "
        "Verification stages.\n\n"
        f"Execution context:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _repair_feedback_unit_scoped(value: object) -> bool:
    """True when at least one finding or missing link names the exact units that own it."""

    feedback = _dict_value(value)
    rows = [
        *_dict_items(_dict_value(feedback.get("repair_objective")).get("contract_findings")),
        *_dict_items(feedback.get("missing_links")),
    ]
    return any(_string_items(row.get("unit_ids")) for row in rows)


def _developer_repair_context(
    value: object,
    *,
    item: dict[str, object],
) -> dict[str, object]:
    """Keep Validator observations out of the action context sent to Developer."""

    feedback = dict(_dict_value(value))
    developer_constraints = dict(_dict_value(feedback.get("developer_constraints")))
    unit_paths = set(_item_paths(item))
    if "allowed_paths" in developer_constraints:
        developer_constraints["allowed_paths"] = [
            path
            for path in _string_items(developer_constraints.get("allowed_paths"))
            if path in unit_paths
        ]
        feedback["developer_constraints"] = developer_constraints
    unit_id = str(item.get("id") or "")

    def assigned_to_unit(row: dict[str, object]) -> bool:
        unit_ids = _string_items(row.get("unit_ids"))
        return not unit_ids or unit_id in unit_ids

    if str(feedback.get("feedback_source") or "") != "validator_code_trace":
        if not _repair_feedback_unit_scoped(feedback):
            return feedback
        # Browser-stage and closure findings carry exact unit_ids: a dependency-closure unit sees only its own
        # findings, never another unit's (r9 UI Polish repair, 2026-11-16).
        repair_objective = dict(_dict_value(feedback.get("repair_objective")))
        own_findings = [
            dict(finding)
            for finding in _dict_items(repair_objective.get("contract_findings"))
            if assigned_to_unit(finding)
        ]
        repair_objective["contract_findings"] = own_findings
        feedback["repair_objective"] = repair_objective
        if "missing_links" in feedback:
            feedback["missing_links"] = [
                dict(link) for link in _dict_items(feedback.get("missing_links")) if assigned_to_unit(link)
            ]
        assignment = (
            f"The repair feedback assigns {len(own_findings)} blocking finding(s) to unit {unit_id}. "
            "Repair only those findings within this unit's write paths."
            if own_findings
            else f"The repair feedback assigns no finding to unit {unit_id}; preserve it as dependency context."
        )
        feedback["summary"] = f"{assignment} {str(feedback.get('summary') or '').strip()}".strip()
        return feedback

    actionable_links = [
        dict(item)
        for item in _dict_items(feedback.get("missing_links"))
        if item.get("blocking") is not False
        and str(item.get("owner") or "developer").strip().lower() == "developer"
        and str(item.get("recommended_action") or "developer_repair").strip().lower()
        == "developer_repair"
        and assigned_to_unit(item)
    ]
    repair_objective = dict(_dict_value(feedback.get("repair_objective")))
    contract_findings = [
        dict(finding)
        for finding in _dict_items(repair_objective.get("contract_findings"))
        if assigned_to_unit(finding)
    ]
    repair_objective["contract_findings"] = contract_findings
    repair_objective["canonical_keys"] = [
        canonical_key
        for finding in contract_findings
        if (canonical_key := str(finding.get("canonical_key") or ""))
    ]

    messages = [str(item.get("message") or "").strip() for item in actionable_links]
    messages = [message for message in messages if message]
    summary = (
        f"Validator assigned {len(contract_findings)} blocking finding(s) to unit {unit_id}. "
        "Repair only those findings within this unit's write paths."
        if contract_findings
        else f"Validator assigned no blocking finding to unit {unit_id}; preserve it as dependency context."
    )
    if messages:
        summary += " " + " | ".join(messages)

    allowed_keys = {
        "version",
        "run_id",
        "source_ref",
        "issue_key",
        "status",
        "repair_iteration",
        "repair_scope",
        "feedback_source",
        "severity",
        "required_action",
        "target_repo",
        "branch",
        "trace_artifact",
        "developer_constraints",
        "artifact_ref",
        "repair_objective",
        "repair_lineage",
    }
    return {
        **{key: item for key, item in feedback.items() if key in allowed_keys},
        "repair_objective": repair_objective,
        "developer_constraints": developer_constraints,
        "summary": summary,
        "missing_links": actionable_links,
    }


def _developer_unit_repair_hints(item: dict[str, object]) -> dict[str, object]:
    """Project global repair discovery evidence onto the active unit's read scope."""

    hints = _dict_value(_dict_value(item.get("repair_closure")).get("repair_hints"))
    context_paths = set(_item_context_paths(item))
    filtered: dict[str, object] = {}
    for key in ("diagnostic_paths", "contract_anchor_paths"):
        values = [path for path in _string_items(hints.get(key)) if path in context_paths]
        if values:
            filtered[key] = values
    path_reasons = {
        path: reasons
        for path, reasons in _dict_value(hints.get("path_reasons")).items()
        if path in context_paths
    }
    if path_reasons:
        filtered["path_reasons"] = path_reasons
    excerpts = [
        dict(excerpt)
        for excerpt in _dict_items(hints.get("excerpts"))
        if str(excerpt.get("path") or "") in context_paths
    ]
    if excerpts:
        filtered["excerpts"] = excerpts
    return filtered


def _action_memory(
    history: list[dict[str, object]],
    *,
    target_item_paths: list[str],
    max_evidence_chars: int,
) -> dict[str, object]:
    ledger_by_key: dict[str, dict[str, object]] = {}
    agent_hypotheses_by_key: dict[str, dict[str, object]] = {}
    mutation_state_by_path: dict[str, dict[str, object]] = {}
    verification_state_by_stage: dict[str, dict[str, object]] = {}
    graph_evidence: list[dict[str, object]] = []
    for index, row in enumerate(history, start=1):
        arguments = _dict_value(row.get("arguments"))
        result = _dict_value(row.get("result"))
        action = str(row.get("action") or "")
        path = str(arguments.get("path") or result.get("path") or "").replace("\\", "/")
        summary = str(row.get("summary") or "").strip()
        key = json.dumps(
            {
                "action": action,
                "path": path,
                "query": arguments.get("query"),
                "stage_id": arguments.get("stage_id") or result.get("stage_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        previous = ledger_by_key.get(key, {})
        ledger_by_key[key] = {
            "action": action,
            "path": path,
            "query": arguments.get("query"),
            "stage_id": arguments.get("stage_id") or result.get("stage_id"),
            "count": int(previous.get("count") or 0) + 1,
            "last_index": index,
            "ok": result.get("ok"),
            "error": row.get("error") or result.get("error"),
        }
        if action in _MUTATION_ACTIONS and result.get("ok") is True and path:
            for graph_row in graph_evidence:
                if graph_row.get("fresh") is True:
                    graph_row["fresh"] = False
                    graph_row["stale_after_action"] = index
            for evidence_key, evidence in list(agent_hypotheses_by_key.items()):
                if evidence.get("path") == path:
                    agent_hypotheses_by_key.pop(evidence_key, None)
            mutation_state_by_path[path] = {
                key: value
                for key, value in {
                    "path": path,
                    "action": action,
                    "last_index": index,
                    "sha256": result.get("sha256"),
                    "summary": summary[:500],
                }.items()
                if value not in {None, ""}
            }
        if action in {"codegraph.query", "codegraph.query_readonly", "codegraph.seed_context"} and result.get("ok") is True:
            graph_evidence.append(
                {
                    key: value
                    for key, value in {
                        "action": action,
                        "operation": arguments.get("operation") or result.get("operation"),
                        "query": arguments.get("query"),
                        "symbol": arguments.get("symbol"),
                        "file": arguments.get("file"),
                        "files": arguments.get("files"),
                        "snapshot_digest": result.get("snapshot_digest"),
                        "index_synced": result.get("index_synced"),
                        "verified_at_action": index,
                        "fresh": True,
                        "summary": summary[:500],
                        "summary_epistemic_status": "unverified_agent_hypothesis",
                        "evidence": _clip_json_value(result, 4000),
                    }.items()
                    if value is not None and value != "" and value != []
                }
            )
        if action in _OBSERVATION_ACTIONS and summary:
            evidence_key = json.dumps(
                {
                    "action": action,
                    "path": path,
                    "query": arguments.get("query"),
                    "start_line": result.get("start_line"),
                    "end_line": result.get("end_line"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            agent_hypotheses_by_key[evidence_key] = {
                key: value
                for key, value in {
                    "action": action,
                    "path": path,
                    "query": arguments.get("query"),
                    "start_line": result.get("start_line"),
                    "end_line": result.get("end_line"),
                    "verified_at_action": index,
                    "summary": summary[:500],
                    "epistemic_status": "unverified_agent_hypothesis",
                    "outcome": (
                        "ok"
                        if result.get("ok") is True
                        else str(row.get("error") or result.get("error") or "unknown")
                    ),
                }.items()
                if value not in {None, ""}
            }
        if action == "verification.run_stage":
            stage_id = str(arguments.get("stage_id") or result.get("stage_id") or "")
            if stage_id:
                verification_state_by_stage[stage_id] = {
                    "stage_id": stage_id,
                    "last_index": index,
                    "result": _compact_working_result(result),
                }
    complete_ledger = sorted(ledger_by_key.values(), key=lambda row: int(row.get("last_index") or 0))
    sticky_ledger = [
        row
        for row in complete_ledger
        if row.get("action") in {*_MUTATION_ACTIONS, "verification.run_stage"} or row.get("error")
    ]
    remaining_capacity = max(0, _WORKING_MEMORY_LEDGER_SOFT_LIMIT - len(sticky_ledger))
    sticky_ids = {id(row) for row in sticky_ledger}
    non_sticky = [row for row in complete_ledger if id(row) not in sticky_ids]
    recent_non_sticky = non_sticky[-remaining_capacity:] if remaining_capacity else []
    retained_ids = {id(row) for row in [*sticky_ledger, *recent_non_sticky]}
    ledger = [row for row in complete_ledger if id(row) in retained_ids]

    diagnostic_breadcrumbs: list[dict[str, object]] = []
    for index, row in enumerate(history, start=1):
        summary = str(row.get("summary") or "").strip()
        arguments = _dict_value(row.get("arguments"))
        result = _dict_value(row.get("result"))
        action = str(row.get("action") or "")
        path = str(arguments.get("path") or result.get("path") or "").replace("\\", "/")
        if action in _MUTATION_ACTIONS and result.get("ok") is True and path:
            diagnostic_breadcrumbs = [
                breadcrumb
                for breadcrumb in diagnostic_breadcrumbs
                if not (
                    breadcrumb.get("path") == path
                    and breadcrumb.get("action") in _OBSERVATION_ACTIONS
                )
            ]
        if not summary:
            continue
        outcome = (
            "ok"
            if result.get("ok") is True
            else str(row.get("error") or result.get("error") or result.get("status") or "unknown")
        )
        diagnostic_breadcrumbs.append(
            {
                key: value
                for key, value in {
                    "action_index": index,
                    "action": action,
                    "path": path,
                    "stage_id": arguments.get("stage_id") or result.get("stage_id"),
                    "summary": summary[:500],
                    "summary_epistemic_status": "unverified_agent_hypothesis",
                    "outcome": outcome,
                }.items()
                if value is not None and value != ""
            }
        )

    latest_reads: list[dict[str, object]] = []
    seen_ranges: set[tuple[str, object, object]] = set()
    for history_index in range(len(history) - 1, -1, -1):
        row = history[history_index]
        action_name = str(row.get("action") or "")
        if action_name not in {"read_file", "repo.read", "repo.read_batch"}:
            continue
        parent_result = _dict_value(row.get("result"))
        read_results = (
            _dict_items(parent_result.get("results"))
            if action_name == "repo.read_batch"
            else [parent_result]
        )
        for result in reversed(read_results):
            if not result.get("ok") or not isinstance(result.get("content"), str):
                continue
            content = str(result.get("content") or "")
            start_line = int(result.get("start_line") or 1)
            end_line = int(result.get("end_line") or start_line)
            total_lines = int(result.get("total_lines") or end_line)
            truncated = result.get("truncated") is True
            has_more = result.get("has_more") is True
            next_start_line = int(result.get("next_start_line") or 0)
            legacy_clipped = False
            if "truncated" not in result and len(content) >= 12000:
                final_line = content.splitlines()[-1] if content.splitlines() else ""
                match = re.match(r"^(\d+):", final_line)
                if match:
                    end_line = max(start_line - 1, int(match.group(1)) - 1)
                    total_lines = max(total_lines, int(result.get("end_line") or end_line))
                    truncated = True
                    has_more = True
                    next_start_line = end_line + 1
                    legacy_clipped = True
            key = (str(result.get("path") or ""), start_line, end_line)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            candidate = {
                "path": key[0],
                "start_line": key[1],
                "end_line": key[2],
                "content": content,
                "verified_at_action": history_index + 1,
                **({"total_lines": total_lines} if "total_lines" in result or legacy_clipped else {}),
                **({"truncated": True} if truncated else {}),
                **({"has_more": True} if has_more else {}),
                **({"next_start_line": next_start_line} if next_start_line else {}),
            }
            if any(_read_source_covers(newer, candidate) for newer in latest_reads):
                continue
            latest_reads.append(candidate)
    target_order = {path: index for index, path in enumerate(target_item_paths)}
    latest_reads.sort(
        key=lambda row: (
            -int(row.get("verified_at_action") or 0),
            target_order.get(str(row.get("path")), len(target_order)),
        )
    )

    inspected_sources: list[dict[str, object]] = []
    omitted_inspected_sources: list[dict[str, object]] = []
    evidence_chars = 0
    for row in latest_reads:
        content = str(row.get("content") or "")
        remaining = max_evidence_chars - evidence_chars
        if remaining <= 0:
            omitted_inspected_sources.append(_omitted_source_descriptor(row, content))
            continue
        if len(content) > remaining:
            if inspected_sources:
                omitted_inspected_sources.append(_omitted_source_descriptor(row, content))
                continue
            content = content[:remaining]
        inspected_sources.append(
            {
                **row,
                "content": content,
                **(
                    {
                        "truncated": True,
                        "original_content_chars": len(str(row.get("content") or "")),
                    }
                    if len(content) < len(str(row.get("content") or ""))
                    else {}
                ),
            }
        )
        evidence_chars += len(content)

    partial_paths: set[str] = set()
    for row in latest_reads:
        path = str(row.get("path") or "")
        if not path or path in partial_paths or row.get("has_more") is not True:
            continue
        cursor = int(row.get("next_start_line") or int(row.get("end_line") or 0) + 1)
        total_lines = int(row.get("total_lines") or 0)
        for candidate in sorted(
            (source for source in latest_reads if str(source.get("path") or "") == path),
            key=lambda source: int(source.get("start_line") or 1),
        ):
            candidate_start = int(candidate.get("start_line") or 1)
            candidate_end = int(candidate.get("end_line") or candidate_start)
            if candidate_end < cursor:
                continue
            if candidate_start > cursor:
                break
            cursor = max(cursor, candidate_end + 1)
        if not total_lines or cursor <= total_lines:
            omitted_inspected_sources.append(
                {
                    "path": path,
                    "start_line": cursor,
                    **({"end_line": total_lines} if total_lines else {}),
                    "verified_at_action": row.get("verified_at_action"),
                    "reason": "repo_read_partial",
                }
            )
            partial_paths.add(path)

    recent_results: list[dict[str, object]] = []
    for row in history[-_WORKING_MEMORY_RECENT_RESULT_LIMIT:]:
        result = dict(_dict_value(row.get("result")))
        if row.get("error") and not result:
            result = {
                "ok": False,
                "error": row.get("error"),
                "instruction": row.get("instruction"),
            }
            if row.get("findings"):
                result["findings"] = row.get("findings")
        if isinstance(result.get("content"), str):
            result["content_chars"] = len(result["content"])
            result.pop("content", None)
        if row.get("action") == "repo.read_batch":
            result["results"] = [
                {
                    **child,
                    **(
                        {"content_chars": len(str(child.get("content") or ""))}
                        if isinstance(child.get("content"), str)
                        else {}
                    ),
                    "content": None,
                }
                for child in _dict_items(result.get("results"))
            ]
        recent_results.append(
            {
                "action": row.get("action"),
                "arguments": _dict_value(row.get("arguments")),
                "result": _compact_working_result(result),
            }
        )
    memory = {
        "projection": {
            "version": "developer_working_memory_projection_v4",
            "history_count": len(history),
            "action_ledger_total": len(complete_ledger),
            "action_ledger_retained": len(ledger),
            "raw_completion_included": False,
        },
        "action_ledger": ledger,
        "inspected_sources": inspected_sources,
        "recent_results": recent_results,
    }
    if agent_hypotheses_by_key:
        memory["agent_hypotheses"] = list(agent_hypotheses_by_key.values())
    if mutation_state_by_path:
        memory["mutation_state"] = list(mutation_state_by_path.values())
    if verification_state_by_stage:
        memory["verification_state"] = list(verification_state_by_stage.values())
    if graph_evidence:
        prioritized = sorted(
            graph_evidence,
            key=lambda row: (
                str(row.get("operation") or "") in {"impact", "affected_tests"},
                int(row.get("verified_at_action") or 0),
            ),
        )
        memory["graph_evidence"] = prioritized[-8:]
    if diagnostic_breadcrumbs:
        memory["diagnostic_breadcrumbs"] = diagnostic_breadcrumbs[-_WORKING_MEMORY_BREADCRUMB_LIMIT:]
    if omitted_inspected_sources:
        memory["omitted_sources"] = omitted_inspected_sources
    return memory


def _compact_working_result(result: dict[str, object]) -> dict[str, object]:
    """Keep the next decision visible without duplicating full durable reports."""

    if result.get("failure_kind") or result.get("status") == "failed":
        reason = str(result.get("reason") or "")
        return {
            key: value
            for key, value in {
                "ok": result.get("ok"),
                "stage_id": result.get("stage_id"),
                "status": result.get("status"),
                "failure_kind": result.get("failure_kind"),
                "failing_test": _failing_test_name(reason),
                "reason": _compact_diagnostic_reason(reason),
                "diagnostic_fingerprint": result.get("diagnostic_fingerprint"),
                "diagnostic_repeat_count": result.get("diagnostic_repeat_count"),
                "report_ref": result.get("report_ref"),
            }.items()
            if value not in {None, ""}
        }
    return _dict_value(
        _clip_json_value(result, 8000 if result.get("batch_kind") == "search" else 2000)
    )


def _compact_repair_episode(episode: dict[str, object]) -> dict[str, object]:
    diagnostics = _dict_value(episode.get("diagnostics"))
    reason = str(diagnostics.get("reason") or "")
    return {
        key: value
        for key, value in {
            "stage_id": episode.get("stage_id"),
            "failure_kind": diagnostics.get("failure_kind"),
            "failing_test": _failing_test_name(reason),
            "reason": _compact_diagnostic_reason(reason),
            "diagnostic_excerpt": episode.get("diagnostic_excerpt"),
            "diagnostic_fingerprint": episode.get("diagnostic_fingerprint"),
            "diagnostic_repeat_count": episode.get("diagnostic_repeat_count"),
            "mutated_since_failure": episode.get("mutated_since_failure"),
            "verification_ready": episode.get("verification_ready"),
            "changed_paths": episode.get("changed_paths"),
            "selected_test_files": diagnostics.get("selected_test_files"),
            "report_ref": diagnostics.get("report_ref"),
        }.items()
        if value is not None and value != "" and value != []
    }


def _authoritative_sources(
    *,
    inspected_sources: list[dict[str, object]],
    current_sources: list[dict[str, object]],
    target_path: Path,
    max_evidence_chars: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Expose one source view; successful mutations replace cached observations."""

    current_by_path = {str(row.get("path") or ""): row for row in current_sources}
    sources: list[dict[str, object]] = []
    emitted: set[str] = set()
    for inspected in inspected_sources:
        path = str(inspected.get("path") or "")
        current = current_by_path.get(path)
        if current:
            if path in emitted:
                continue
            sources.append(
                {
                    **current,
                    "origin": "worktree_after_mutation",
                    "freshness": "current_worktree_verified",
                    "changed": True,
                }
            )
        else:
            source = (target_path / path).resolve()
            sha256 = _file_hash(source) if source.is_file() else ""
            current_total_lines = (
                len(source.read_text(encoding="utf-8", errors="replace").splitlines())
                if source.is_file()
                and (inspected.get("has_more") is True or inspected.get("truncated") is True)
                else 0
            )
            sources.append(
                {
                    **inspected,
                    "sha256": sha256,
                    **({"total_lines": current_total_lines} if current_total_lines else {}),
                    "origin": "verified_current_read",
                    "freshness": "current_worktree_verified",
                    "changed": False,
                }
            )
        emitted.add(path)
    for path, current in current_by_path.items():
        if path not in emitted:
            sources.append(
                {
                    **current,
                    "origin": "worktree_after_mutation",
                    "freshness": "current_worktree_verified",
                    "changed": True,
                }
            )
    bounded: list[dict[str, object]] = []
    omitted: list[dict[str, object]] = []
    used = 0
    for source in sources:
        content = str(source.get("content") or "")
        remaining = max_evidence_chars - used
        if not content:
            bounded.append(source)
            continue
        if remaining <= 0 or len(content) > remaining:
            omitted.append(
                {
                    key: value
                    for key, value in {
                        "path": source.get("path"),
                        "start_line": source.get("start_line"),
                        "end_line": source.get("end_line"),
                        "sha256": source.get("sha256"),
                        "verified_at_action": source.get("verified_at_action"),
                        "content_chars": len(content),
                        "reason": "working_memory_evidence_budget",
                    }.items()
                    if value not in {None, ""}
                }
            )
            continue
        bounded.append(source)
        used += len(content)
    return bounded, omitted


def _read_source_covers(container: dict[str, object], candidate: dict[str, object]) -> bool:
    """A newer whole/ranged read supersedes an older range of the same file."""

    if str(container.get("path") or "") != str(candidate.get("path") or ""):
        return False
    container_start = int(container.get("start_line") or 1)
    candidate_start = int(candidate.get("start_line") or 1)
    container_end = int(container.get("end_line") or container_start)
    candidate_end = int(candidate.get("end_line") or candidate_start)
    return container_start <= candidate_start and container_end >= candidate_end


def _omitted_source_descriptor(source: dict[str, object], content: str) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "path": source.get("path"),
            "start_line": source.get("start_line"),
            "end_line": source.get("end_line"),
            "sha256": source.get("sha256"),
            "verified_at_action": source.get("verified_at_action"),
            "content_chars": len(content),
            "reason": "working_memory_evidence_budget",
        }.items()
        if value not in {None, ""}
    }


def _current_mutated_sources(
    history: list[dict[str, object]],
    *,
    target_path: Path,
    target_item_paths: list[str],
    max_evidence_chars: int,
) -> list[dict[str, object]]:
    """Read current worktree content for paths changed during this attempt."""

    mutated: dict[str, int] = {}
    for index, row in enumerate(history, start=1):
        if row.get("action") not in _MUTATION_ACTIONS:
            continue
        result = _dict_value(row.get("result"))
        path = str(result.get("path") or "").replace("\\", "/")
        if result.get("ok") is True and path in target_item_paths:
            mutated[path] = index

    sources: list[dict[str, object]] = []
    used = 0
    ordered_paths = sorted(mutated, key=lambda path: mutated[path], reverse=True)
    for path in ordered_paths:
        source = (target_path / path).resolve()
        try:
            source.relative_to(target_path.resolve())
        except ValueError:
            continue
        if not source.is_file() or not _read_path_allowed(target_path, source):
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        remaining = max_evidence_chars - used
        if remaining <= 0:
            break
        truncated = len(content) > remaining
        if truncated:
            if sources:
                continue
            content = content[:remaining]
        sources.append(
            {
                "path": path,
                "sha256": _file_hash(source),
                "content": content,
                "truncated": truncated,
                "verified_at_action": mutated[path],
            }
        )
        used += len(content)
    return sources


def _item_approved_signatures(item: dict[str, object]) -> list[dict[str, object]]:
    signatures: list[dict[str, object]] = []
    seen: set[str] = set()
    for file_item in _item_files(item):
        for signature in _dict_items(file_item.get("approved_signatures")):
            identity = str(signature.get("contract_id") or "") or _json_hash(signature)
            if identity in seen:
                continue
            seen.add(identity)
            signatures.append(signature)
    return signatures


def _developer_read_only_qa_acceptance(
    *,
    item: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, object]:
    intents = _dedupe_prompt_rows(_dict_items(item.get("qa_test_intents")))
    decisions = _dedupe_prompt_rows(_dict_items(manifest.get("approved_runtime_decisions")))
    if not intents and not decisions:
        return {}
    return {
        "authority": (
            "Read-only QA expectations and accepted runtime-fixture decisions. They clarify downstream browser "
            "acceptance but never add Developer write paths, required stages, mutations, or completion obligations."
        ),
        **({"qa_test_intents": intents} if intents else {}),
        **({"accepted_runtime_decisions": decisions} if decisions else {}),
    }


def _unit_mandatory_obligations(item: dict[str, object]) -> dict[str, object]:
    """Promote approved behavior that a short work-unit objective can hide."""

    requirements = _dedupe_prompt_rows(_dict_items(item.get("requirements")))
    test_intents = _dedupe_prompt_rows(_dict_items(item.get("test_intents")))
    ui_obligations = _dedupe_prompt_rows(_dict_items(item.get("ui_obligations")))
    runtime_config_changes = _dedupe_prompt_rows(
        _dict_items(item.get("runtime_config_changes"))
    )
    ui: dict[str, object] = {}
    ui_contracts: list[dict[str, object]] = []
    non_ui_contracts: list[dict[str, object]] = []
    for binding in _dict_items(item.get("surface_contracts")):
        surface = str(binding.get("surface") or "").strip().lower()
        contract = _dict_value(binding.get("surface_contract"))
        if surface != "ui":
            if surface and contract:
                non_ui_contracts.append(
                    {
                        "design_node_id": binding.get("design_node_id"),
                        "surface": surface,
                        "surface_contract": contract,
                    }
                )
            continue
        experience = _dict_value(contract.get("experience_contract"))
        exact_contract = (
            str(experience.get("schema_version") or "") == "ui_experience_contract_v2"
            or bool(contract.get("runtime_surface"))
            or bool(experience.get("focus_policies"))
        )
        if exact_contract:
            ui_contracts.append(
                {
                    "design_node_id": binding.get("design_node_id"),
                    "surface_contract": contract,
                }
            )
            continue
        for field in ("component_structure", "accessibility", "responsive", "focus_flow"):
            values = _string_items(experience.get(field))
            if values:
                ui.setdefault(field, []).extend(values)
        feedback = [
            {
                "trigger": str(row.get("trigger") or ""),
                "response": str(row.get("response") or ""),
            }
            for row in _dict_items(experience.get("feedback"))
            if str(row.get("trigger") or "") or str(row.get("response") or "")
        ]
        if feedback:
            ui.setdefault("feedback", []).extend(feedback)
        for field in ("entrypoint", "states", "transitions", "validations", "outcomes"):
            value = contract.get(field)
            if value:
                ui.setdefault(field, value)

    boundaries = []
    for boundary in _dict_items(item.get("boundary_contracts")):
        boundaries.append(
            {
                "design_edge_id": boundary.get("design_edge_id"),
                "consumer_operation": boundary.get("consumer_operation"),
                "provider_operation": boundary.get("provider_operation"),
                "auth_mapping": boundary.get("auth_mapping", []),
                "input_mapping": boundary.get("input_mapping", []),
                "response_mapping": boundary.get("response_mapping", []),
                "error_mapping": boundary.get("error_mapping", []),
            }
        )

    wiring = []
    for binding in _dict_items(item.get("runtime_bindings")):
        wiring.append(
            {
                "runtime_kind": binding.get("runtime_kind"),
                "target": binding.get("target", {}),
                "attachment": binding.get("attachment", {}),
            }
        )

    return {
        **({"requirements": requirements} if requirements else {}),
        **({"test_intents": test_intents} if test_intents else {}),
        **({"ui_obligations": ui_obligations} if ui_obligations else {}),
        **({"ui": ui} if ui else {}),
        **({"approved_ui_contracts": ui_contracts} if ui_contracts else {}),
        **(
            {"approved_non_ui_contracts": _dedupe_prompt_rows(non_ui_contracts)}
            if non_ui_contracts
            else {}
        ),
        **({"boundaries": boundaries} if boundaries else {}),
        **({"runtime_wiring": wiring} if wiring else {}),
        # Mandatory obligations reach the prompt in act and repair alike, unlike execution_task.
        **(
            {
                "runtime_closure": (
                    _dict_value(item.get("runtime_closure_authority"))
                    or _dict_value(item.get("closure_context"))
                )
            }
            if _dict_value(item.get("runtime_closure_authority"))
            or _dict_value(item.get("closure_context"))
            else {}
        ),
        **(
            {"runtime_configuration": runtime_config_changes}
            if runtime_config_changes
            else {}
        ),
    }


def _dedupe_prompt_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        identity = _json_hash(row)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def _completed_unit_entries(journal: dict[str, object] | None) -> dict[str, dict[str, object]]:
    return {
        str(entry.get("id") or ""): entry
        for entry in _dict_items(_dict_value(journal).get("items"))
        if entry.get("status") == "completed" and str(entry.get("id") or "")
    }


def _unit_completion_outcome(entry: dict[str, object]) -> dict[str, object]:
    """What a unit completed earlier in this session actually changed, as journaled by the Harness."""

    return {
        "mutated_paths": _string_items(entry.get("mutated_paths")),
        "path_statuses": {
            path: str(_dict_value(row).get("status") or "")
            for path, row in _dict_value(entry.get("path_dispositions")).items()
        },
        "completion_summary": str(entry.get("summary") or "")[:800],
    }


def _completed_dependency_receipts(
    *,
    item: dict[str, object],
    manifest: dict[str, object],
    journal: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Describe what a completed dependency owns and, within this session, what it changed."""

    by_id = {
        str(row.get("id") or ""): row
        for row in _dict_items(manifest.get("items"))
        if str(row.get("id") or "")
    }
    completed = _completed_unit_entries(journal)
    receipts: list[dict[str, object]] = []
    for dependency_id in _string_items(item.get("depends_on")):
        dependency = _dict_value(by_id.get(dependency_id))
        receipt: dict[str, object] = {
            "unit_id": dependency_id,
            "status": "completed",
        }
        if dependency:
            receipt.update(
                {
                    "title": dependency.get("title"),
                    "component_id": dependency.get("component_id"),
                    "surfaces": _string_items(dependency.get("surfaces")),
                    "write_paths": _item_paths(dependency),
                    "planned_code_targets": _dict_items(dependency.get("planned_code_targets")),
                }
            )
        if dependency_id in completed:
            receipt.update(_unit_completion_outcome(completed[dependency_id]))
        receipts.append(receipt)
    return receipts


def _shared_finding_progress(
    *,
    item: dict[str, object],
    repair_context: dict[str, object],
    journal: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Findings this unit shares with units that already completed in the current repair session."""

    unit_id = str(item.get("id") or "")
    completed = _completed_unit_entries(journal)
    progress: list[dict[str, object]] = []
    for finding in _dict_items(_dict_value(repair_context.get("repair_objective")).get("contract_findings")):
        shared_ids = [
            other for other in _string_items(finding.get("unit_ids")) if other != unit_id and other in completed
        ]
        if not shared_ids:
            continue
        progress.append(
            {
                "finding_id": str(finding.get("finding_id") or ""),
                "completed_unit_ids": shared_ids,
                "outcomes": {other: _unit_completion_outcome(completed[other]) for other in shared_ids},
                "note": (
                    "The finding text describes the code before these units ran in this session. Judge what is "
                    "still missing from the current bytes, not from the finding text alone."
                ),
            }
        )
    return progress


def _focused_execution_task(
    item: dict[str, object],
    *,
    context_paths: list[str],
    active_repair: bool,
) -> dict[str, object]:
    task = _prompt_execution_task(item, context_paths=context_paths)
    if not active_repair:
        return task
    keep = {
        "id",
        "title",
        "objective",
        "order",
        "scenario_id",
        "component_id",
        "surfaces",
        "depends_on",
        "paths",
        "allowed_paths",
        "requirement_ids",
        "requirement_refs",
        "acceptance_refs",
        "requirements",
        "test_intents",
        "required_stage_ids",
        "test_target_paths",
        "implementation_steps",
        "package_dependencies",
    }
    return {key: value for key, value in task.items() if key in keep}


def _prompt_execution_task(item: dict[str, object], *, context_paths: list[str]) -> dict[str, object]:
    context = set(context_paths)
    steps = [
        row
        for row in _dict_items(item.get("implementation_steps"))
        if context.intersection(path.replace("\\", "/") for path in _string_items(row.get("files")))
    ]
    navigation_fields = {
        "id",
        "title",
        "order",
        "scenario_id",
        "component_id",
        "surfaces",
        "depends_on",
    }
    visible_item = {
        key: value
        for key, value in item.items()
        if key in navigation_fields
    }
    file_summaries = []
    for row in _item_files(item):
        if str(row.get("path") or "") not in context:
            continue
        file_summaries.append(
            {
                key: row.get(key)
                for key in ("path", "operation")
                if row.get(key) not in {None, ""}
            }
        )
    return {
        **visible_item,
        "paths": context_paths,
        "allowed_paths": _item_paths(item),
        "files": file_summaries,
        "implementation_steps": steps,
    }


def _handled_paths(history: list[dict[str, object]], *, required_paths: list[str]) -> set[str]:
    handled: set[str] = set()
    required_folded = {path.casefold(): path for path in required_paths}
    for row in history:
        if row.get("action") not in _MUTATION_ACTIONS or not _dict_value(row.get("result")).get("ok"):
            continue
        path = str(_dict_value(row.get("result")).get("path") or "").replace("\\", "/")
        canonical = required_folded.get(path.casefold())
        if canonical:
            handled.add(canonical)
    return handled


def _inspected_paths(history: list[dict[str, object]], *, required_paths: list[str]) -> set[str]:
    return {path for path in required_paths if _has_successful_target_inspection(history, item_path=path)}
