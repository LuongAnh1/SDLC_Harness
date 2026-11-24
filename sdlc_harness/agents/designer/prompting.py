"""The Designer prompt: what the model sees for one partition or repair task at one step."""

from __future__ import annotations

import collections
import json

from ...core.coerce import dict_copies as _dicts
from .graph import DELTA_VERSION, GRAPH_VERSION, SURFACE_CONTRACT_VERSION, is_composition_wiring
from .projections import _merged_runtime_states
from .tools import MAX_BATCH_READS
from .ui_context import required_props
from .uiux import RUNTIME_SURFACE_BINDING_VERSION, UI_EXPERIENCE_CONTRACT_V2
from .facts import (
    OBSERVATION_ACTION_LIMIT,
    _dict,
    _feature_guidance_satisfied,
    _required_surface_set,
    _scenarios,
    _strings,
    _ui_guidance_required,
    _uiux_guidance_status,
)


def _active_requirement_slice(
    *,
    requirement_model: dict[str, object],
    active_partition: dict[str, object] | None,
    include_outcome_context: bool = False,
) -> dict[str, object]:
    if active_partition is None:
        return {}
    scenario_id = str(_dict(active_partition.get("partition")).get("scenario_id") or "")
    scenario = next(
        (item for item in _scenarios(requirement_model) if str(item.get("id") or "") == scenario_id),
        None,
    )
    if scenario is None:
        return {}

    surface_work = _dicts(active_partition.get("surface_work"))
    allowed_refs = list(
        dict.fromkeys(
            ref
            for work in surface_work
            for ref in _strings(work.get("requirement_refs"))
        )
    )
    requirement_items: list[dict[str, object]] = []
    collections: list[object] = [
        requirement_model.get("preconditions"),
        requirement_model.get("postconditions"),
        requirement_model.get("business_rules"),
        requirement_model.get("acceptance_criteria"),
        requirement_model.get("quality_requirements"),
        requirement_model.get("assumptions"),
        requirement_model.get("decisions"),
    ]
    collections.extend(_dict(requirement_model.get("flows")).values())
    for item in (row for collection in collections for row in _dicts(collection)):
        if str(item.get("id") or "") in allowed_refs:
            requirement_items.append(item)

    cross_cutting_conditions = _cross_cutting_conditions(requirement_model)

    # A UI partition must stay consistent with acceptance criteria the Requirement allocated only to other surfaces
    # (or to no surface): expose those ids as presentation-only context, never as behavior scope.
    outcome_context: list[dict[str, object]] = []
    if include_outcome_context and any(str(work.get("surface") or "") == "ui" for work in surface_work):
        allocated = {
            ref
            for item in _scenarios(requirement_model)
            for contract in _dict(item.get("surfaces")).values()
            for ref in _strings(_dict(contract).get("requirement_refs"))
        }
        outcome_ids = [
            *(ref for ref in _strings(scenario.get("acceptance_refs")) if ref not in allowed_refs),
            # Postconditions describe the main success end state; branch scenarios end in their own flow outcome.
            *(
                str(item.get("id"))
                for item in _dicts(requirement_model.get("postconditions"))
                if str(scenario.get("kind") or "") == "main"
                and str(item.get("id") or "")
                and str(item.get("id")) not in allocated
            ),
        ]
        by_id = {str(item.get("id") or ""): item for collection in collections for item in _dicts(collection)}
        outcome_context = [
            {"id": ref, "text": str(by_id[ref].get("text") or by_id[ref].get("action") or "")}
            for ref in dict.fromkeys(outcome_ids)
            if ref in by_id
        ]

    return {
        "use_case": requirement_model.get("use_case"),
        "scenario": {
            key: scenario.get(key)
            for key in ("id", "kind", "name", "actor_refs")
            if scenario.get(key) is not None
        },
        "surfaces": [
            {
                "surface": work.get("surface"),
                "requirement_refs": _strings(work.get("requirement_refs")),
                "contract": work.get("contract"),
            }
            for work in surface_work
        ],
        "allowed_requirement_refs": allowed_refs,
        "requirements": requirement_items,
        **({"cross_cutting_conditions": cross_cutting_conditions} if cross_cutting_conditions else {}),
        **({"outcome_context": outcome_context} if outcome_context else {}),
    }


def _cross_cutting_conditions(requirement_model: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            key: item.get(key)
            for key in ("id", "text", "status", "evidence_refs", "open_question_refs")
            if item.get(key) is not None
        }
        for item in _dicts(requirement_model.get("preconditions"))
        if str(item.get("id") or "")
    ]


def _requirement_context_index(requirement_model: dict[str, object]) -> dict[str, object]:
    use_case = _dict(requirement_model.get("use_case"))
    return {
        "schema_version": requirement_model.get("schema_version"),
        "revision": requirement_model.get("revision"),
        "run_id": requirement_model.get("run_id"),
        "use_case": {key: use_case.get(key) for key in ("id", "name") if use_case.get(key) is not None},
        "scenario_index": [
            {
                "id": scenario.get("id"),
                "kind": scenario.get("kind"),
                "required_surfaces": sorted(_required_surface_set(scenario)),
            }
            for scenario in _scenarios(requirement_model)
        ],
    }


def _prompt_review_feedback(
    feedback: str,
    *,
    requirement_model: dict[str, object],
    active_partition: dict[str, object] | None,
) -> str:
    if not feedback or active_partition is None:
        return feedback
    active_scenario_id = str(_dict(active_partition.get("partition")).get("scenario_id") or "")
    sibling_ids = {
        str(scenario.get("id") or "")
        for scenario in _scenarios(requirement_model)
        if str(scenario.get("id") or "") != active_scenario_id
    }
    if any(sibling_id and sibling_id in feedback for sibling_id in sibling_ids):
        return (
            f"The operator rejected the previous action because it mixed sibling-scenario behavior into "
            f"{active_scenario_id}. Regenerate from active_requirement_slice only; scenario_index is navigation-only."
        )
    return feedback


def _repair_source_paths(repair_task: dict[str, object]) -> list[str]:
    paths: list[str] = []
    graph_context = _dict(repair_task.get("graph_context"))
    nodes = _dicts(graph_context.get("writable_nodes"))
    for node in nodes:
        binding = _dict(node.get("runtime_binding"))
        target_path = str(_dict(binding.get("target")).get("path") or "").strip()
        owner_path = str(_dict(binding.get("attachment")).get("owner_path") or "").strip()
        candidates = (
            [owner_path]
            if str(node.get("disposition") or "") == "add"
            else [target_path, owner_path]
        )
        for path in candidates:
            if path and path not in paths:
                paths.append(path)
    return paths


def _repair_source_inspection_prompt(
    *,
    repair_task: dict[str, object],
    observation_limit: int,
    actions_remaining: int | None,
    last_invalid_action: str,
    last_review_feedback: str,
) -> str:
    payload = {
        "mode": "repair_source_inspection_only",
        "repair_target": {
            "target_id": repair_task.get("target_id"),
            "partition": repair_task.get("partition"),
            "remediation_mode": repair_task.get("remediation_mode") or "local_patch",
            "suggested_source_paths": _repair_source_paths(repair_task),
            "read_scope": repair_task.get("read_scope") or {},
        },
        "observation_policy": {
            "limit_before_delta": observation_limit,
            "used_since_delta": 0,
            "must_propose_delta_now": False,
            "source_inspected": False,
            **({"actions_remaining_including_delta": actions_remaining} if actions_remaining is not None else {}),
        },
        "next_context_after_source": "full_exact_repair_feedback",
        "last_invalid_action": last_invalid_action,
        "last_review_feedback": last_review_feedback,
    }
    return (
        "SOURCE INSPECTION ONLY. Do not analyze the finding and do not propose a delta yet. Return exactly one "
        f"repo.read_batch action reading the existing suggested_source_paths (at most {MAX_BATCH_READS}; with one "
        "path, repo.read is the same). If no suggested path exists, return one "
        "codegraph.query action scoped to the supplied component/profile roots. After a successful source inspection, "
        "the Harness will provide the complete unsummarized Validator findings, exact graph context and write scope. "
        "If a prior source/CodeGraph attempt could not establish a required binding and no remaining read can resolve it, "
        "design.report_blocker(target_ref,missing_facts,reason) may stop with a diagnostic and no checkpoint.\n\n"
        f"Allowed actions now: repo.read_batch(requests: 1-{MAX_BATCH_READS} objects {{path,start_line?,end_line?}}); "
        "repo.read(path,start_line?,end_line?); "
        "codegraph.query(operation=explore|symbols|callers|callees|files,query?,symbol?,limit?,depth?,filter?,pattern?).\n"
        "Return exactly one of these JSON shapes, with no type/json_object wrapper and no extra text:\n"
        '{"action":"repo.read_batch","arguments":{"requests":[{"path":"<exact existing suggested_source_path>"},'
        '{"path":"<another one>"}]}}\n'
        '{"action":"repo.read","arguments":{"path":"<exact existing suggested_source_path>"}}\n'
        '{"action":"codegraph.query","arguments":{"operation":"explore","query":"<component-scoped query>"}}\n\n'
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _construction_next_actions(
    *, source_inspected: bool, must_propose: bool
) -> list[str]:
    """The actions a construction call may take now, mirroring the loop's gates."""

    if not source_inspected:
        return ["repo.read_batch", "repo.read", "codegraph.query"]
    if must_propose:
        return ["design.propose_delta", "design.inspect_partition", "design.report_blocker"]
    return [
        "repo.read_batch", "repo.read", "repo.search", "codegraph.query", "landscape.search", "landscape.get_anchor",
        "design.inspect_partition", "design.propose_delta", "design.report_blocker",
    ]


def _designer_prompt(
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    graph: dict[str, object],
    observations: list[dict[str, object]],
    repair_feedback: list[dict[str, object]],
    active_partition: dict[str, object] | None,
    repair_task: dict[str, object] | None,
    observations_since_delta: int,
    last_invalid_action: str,
    observation_limit: int = OBSERVATION_ACTION_LIMIT,
    last_review_feedback: str = "",
    last_rejected_delta: dict[str, object] | None = None,
    active_guidance: list[dict[str, object]] | None = None,
    source_inspected: bool = False,
    actions_remaining: int | None = None,
    design_notes: list[dict[str, object]] | None = None,
) -> str:
    if repair_task is not None and not source_inspected:
        return _repair_source_inspection_prompt(
            repair_task=repair_task,
            observation_limit=observation_limit,
            actions_remaining=actions_remaining,
            last_invalid_action=last_invalid_action,
            last_review_feedback=last_review_feedback,
        )
    graph_contract = str(graph.get("schema_version") or GRAPH_VERSION)
    delta_contract = DELTA_VERSION
    repair_partition = _dict((repair_task or {}).get("partition"))
    repair_scenario_id = str(repair_partition.get("scenario_id") or "")
    prompt_graph = (
        {
            "schema_version": graph.get("schema_version"),
            "revision": graph.get("revision"),
            "artifact_digest": graph.get("artifact_digest"),
            "summary": {
                "partitions": len(_dicts(graph.get("partitions"))),
                "nodes": len(_dicts(graph.get("nodes"))),
                "edges": len(_dicts(graph.get("edges"))),
            },
        }
        if repair_task is not None
        else _construction_graph_view(graph, active_partition=active_partition)
    )
    scenario_matches = _dicts(landscape.get("scenario_matches"))
    if repair_scenario_id:
        scenario_matches = [
            item for item in scenario_matches if str(item.get("scenario_id") or "") == repair_scenario_id
        ]
    elif active_partition is not None:
        # Construction builds only the active scenario's missing surfaces; other entries repeat the same anchors.
        active_scenario_id = str(_dict(active_partition.get("partition")).get("scenario_id") or "")
        missing_surfaces = set(_strings(active_partition.get("missing_surfaces")))
        scenario_matches = [
            item
            for item in scenario_matches
            if str(item.get("scenario_id") or "") == active_scenario_id
            and str(item.get("surface") or "") in missing_surfaces
        ]
    active_requirement_slice = _active_requirement_slice(
        requirement_model=requirement_model,
        active_partition=active_partition,
        include_outcome_context=True,
    )
    prompt_review_feedback = _prompt_review_feedback(
        last_review_feedback,
        requirement_model=requirement_model,
        active_partition=active_partition,
    )
    payload = {
        "graph_contract": graph_contract,
        "delta_contract": delta_contract,
        "landscape_summary": landscape.get("summary"),
        "components": landscape.get("components"),
        "scenario_matches": scenario_matches,
        "graph": prompt_graph,
        "active_partition": active_partition,
        "active_repair_task": repair_task,
        "repair_feedback_delivery": "full_exact" if repair_task is not None else "none",
        "observation_policy": {
            "limit_before_delta": observation_limit,
            "used_since_delta": observations_since_delta,
            "must_propose_delta_now": (active_partition is not None or repair_task is not None) and observations_since_delta >= observation_limit,
            # Computed here so the Designer never re-derives it from three counters (design_v13: 11-20 times a call).
            "counted_observations_left": max(0, observation_limit - observations_since_delta),
            "source_inspected": source_inspected,
            **({"actions_remaining_including_delta": actions_remaining} if actions_remaining is not None else {}),
        },
        "recent_observations": observations,
        "design_notes": design_notes or [],
        "active_uiux_guidance": active_guidance or [],
        "uiux_mode": "enforce",
        "repair_feedback": (
            [] if active_partition is not None or repair_task is not None else repair_feedback
        ),
        "last_invalid_action": last_invalid_action,
        "last_review_feedback": prompt_review_feedback,
    }
    if active_requirement_slice:
        payload["requirement_context"] = _requirement_context_index(requirement_model)
        payload["active_requirement_slice"] = active_requirement_slice
    else:
        payload["requirement"] = requirement_model
    correction = (
        "Your previous action was rejected. Fix this exact error before doing anything else: "
        f"{last_invalid_action} When recent_observations contains the rejected action, "
        "change only the fields or operations needed to fix that rejection; preserve unrelated nodes, edges, "
        "mappings and design decisions. Reconsider topology only if the rejection requires it. A JSON parse "
        "failure may supply only a diagnostic, not the decoded action: do not assume an unavailable action "
        "can be patched or that earlier unsubmitted design choices are recorded. Return the complete corrected action, "
        "not a partial patch. Do not resend the rejected delta unchanged.\n\n"
        if last_invalid_action
        else ""
    )
    rejected_delta = (
        " Your rejected delta follows; change only what the review names and keep every other node, edge and "
        "mapping it contains: " + json.dumps(last_rejected_delta, ensure_ascii=False, sort_keys=True)
        if prompt_review_feedback and last_rejected_delta
        else ""
    )
    review_correction = (
        "The operator rejected your previous proposed action. Address this review before choosing the next action: "
        f"{prompt_review_feedback}{rejected_delta}\n\n"
        if prompt_review_feedback
        else ""
    )
    ui_guidance_required = _ui_guidance_required(
        active_partition=active_partition,
        repair_task=repair_task,
        graph=graph,
    )
    guidance_reused = _feature_guidance_satisfied(graph)
    uiux_status = (
        _uiux_guidance_status(
            active_guidance or [],
            source_inspected=source_inspected,
            delta_required=observations_since_delta >= observation_limit,
        )
        if ui_guidance_required and not guidance_reused
        else {}
    )
    payload["evidence_blocker_policy"] = {
        "action": "design.report_blocker", "requires": "at least one active source/CodeGraph investigation attempt",
        "effect": "stop with design_evidence_insufficient; no checkpoint or extra budget",
    }
    if active_partition is not None and repair_task is None and not uiux_status:
        # A UI partition gets its menu from the UI/UX tool state; every other construction partition gets one here.
        payload["observation_policy"]["allowed_next_actions"] = _construction_next_actions(
            source_inspected=source_inspected,
            must_propose=observations_since_delta >= observation_limit,
        )
    payload["uiux_guidance_status"] = uiux_status
    if ui_guidance_required:
        payload["ui_runtime_context"] = _ui_runtime_prompt_context(
            graph,
            requirement_model=requirement_model,
            allowed_refs=_strings(_dict(active_requirement_slice).get("allowed_requirement_refs")),
        )
        payload["feature_ux_decisions"] = _feature_ux_decisions(graph)
    experience_version = UI_EXPERIENCE_CONTRACT_V2
    runtime_surface_instruction = (
        f"UI contract v2: every UI node's surface_contract.runtime_surface ({RUNTIME_SURFACE_BINDING_VERSION}) must "
        "prove how a user reaches the screen. composition_path starts at ui_runtime_context.entrypoint and ends at the "
        "node's runtime_binding.target, each item written as <repo path>#<symbol>; middle hops are AS-IS components "
        "from ui_runtime_context.composition_chain or runtime targets of other UI nodes, and runtime_binding.attachment "
        "owner_path#owner_symbol must equal the hop just before the target; when that hop is an AS-IS component of "
        "ui_runtime_context.composition_chain, its ref (<repo path>#<symbol>) is a valid owner_ref, so no RLA lookup is "
        "needed. Nodes on the same screen share one screen_id; different screens use different screen_ids. "
        "required_inputs is a required key; use [] only when the target receives no value from a hop before it on "
        "composition_path. "
        "props_status=unknown or required_props=null means unverified, never zero required props: investigate the source "
        "within the existing budget and report a blocker if the binding cannot be established. auth.status=unverified_hint "
        "is only a navigation hint from identifiers, not proof of an authentication provider or missing authentication. "
        "Decide required_inputs by how values enter the target, before classifying their source. Data the target "
        "component obtains by itself (a record it fetches through one of ui_runtime_context.data_clients, or state "
        "it reads from a store) is not a required input: leave it out of required_inputs and model its loading, "
        "ready and error states in runtime_states and transitions; never route it through a hop that does not hold "
        "and pass it. required_inputs lists every value the target receives from a hop before it on composition_path "
        "(records to show or select, a session or auth value passed in, data loaded from another use case), "
        "including every required prop of the AS-IS target component a node modifies or reuses: a required prop is a "
        "required input even when the parent hop that passes it creates the value itself (for example a submit "
        "handler the page defines), and its provided_by is that parent hop. A precondition "
        "does not by itself turn store-read or self-fetched data into a passed input. For each genuinely passed "
        "input, use source=existing_runtime when a provider in ui_runtime_context already supplies it, and then binding "
        "is that provider's ref (<repo path>#<symbol> of the composition_chain hop, component or data client), which "
        "for a value a page creates itself (such as its submit handler) is the page component's ref, never the name "
        "of the function or variable; "
        "new_boundary when this Design adds the provider (binding is that node's VDN-* id); external_use_case when the "
        "approved Requirement says the data comes from another use case, citing the precondition or flow-step id from "
        "ui_runtime_context.dependency_refs whose text states that dependency, and then runnable_status=externally_blocked; "
        "never declare such data as provided by this screen. An input guaranteed by a precondition (for example an "
        "authenticated session passed in) is likewise external_use_case citing that PRE-* id unless ui_runtime_context already "
        "has its provider; demo_fixture only with an accepted D-* decision listed in dependency_refs and "
        "runnable_status=fixture_required; otherwise runnable_status=bound. provided_by is the hop on composition_path "
        "that supplies the input. A hop that already passes the input to its child in the AS-IS code "
        "(ui_runtime_context.composition_chain passes_props) needs no change, and the Harness adds nothing for it. "
        "When an input must newly flow down to the screen, declare it only on the screen node "
        "with provided_by set to the topmost hop that receives it. The Harness itself then adds to your delta a "
        "composition_wiring modify node, and the nonterminal calls edge, for each AS-IS hop from provided_by down to "
        "the screen that does not pass the input yet, but only for hops that active_partition.harness_wiring_operations "
        "lists (with <input names> filled from your required_inputs; with no required_inputs it adds nothing); it "
        "rejects the delta when any other hop would need wiring. These are conditional templates, not applied graph "
        "operations and not a requirement to invent inputs; "
        "do not write those wiring nodes or their internal edges yourself. When it adds wiring, add only the semantic "
        "edge from the final wiring hop to the screen, referencing that hop's temp_ref. A wiring node only forwards a "
        "value; it never creates one. So when a screen needs a new value or handler that its page must create (for "
        "example a second submit action), target that page with the node whose contract describes the new behavior "
        "instead of the shared component the page renders; target the shared component only when the change lies "
        "entirely inside it and its parent already passes every value it needs. An "
        "optional prop with an empty default never counts as a bound input; never invent a dependency that the "
        "Requirement does not state. "
        if ui_guidance_required
        else ""
    )
    uiux_status_instruction = (
        "CURRENT UI/UX TOOL STATE (authoritative; it overrides the general instructions above):\n"
        f"{json.dumps(uiux_status, ensure_ascii=True, separators=(',', ':'))}\n"
        "When required_next_action is present, match its action, mode, and fixed selector exactly; choose only the "
        "query text yourself. When it is null, choose only from allowed_next_actions. A domain query asks for the "
        "observable UX outcome; a stack query asks how React implements that outcome. Evidence-blocker exception: "
        "after an active source/CodeGraph attempt, design.report_blocker may replace this next action when a required "
        "binding fact remains unverifiable; it stops without a Design checkpoint.\n\n"
        if uiux_status
        else ""
    )
    uiux_instruction = (
        (
            "Feature-level UI/UX guidance already exists in this graph: do not call uiux.search_guidance again; "
            "reuse the guidance_refs already cited by UI nodes. "
            if guidance_reused
            else "For this active UI work item, first inspect source, then call uiux.search_guidance twice: "
            "one mode=domain, domain=ux query and one mode=stack, stack=react query. Each query must express one dominant "
            "intent in 2-5 meaningful generic ASCII terms without repository paths, symbols, or project-private text. "
            "Retry the same selector at most once, and only when its previous result is empty or off-topic; otherwise advance "
            "to the next mode shown in CURRENT UI/UX TOOL STATE. "
        )
        + f"Every UI node must include {experience_version} under "
        "surface_contract.experience_contract, and that one object holds all of these fields: screen, "
        "component_structure, feedback[{trigger,response}], accessibility, responsive, focus_flow, focus_policies, "
        "runtime_states, visual_contract, repo_evidence_refs and the successful uiux guidance_refs; none of them goes "
        "directly under surface_contract. Guidance informs UX only; Requirement and "
        "repository source remain authoritative. "
        + (
            "Design the whole screen, not only this partition: reuse the screen-wide fields of feature_ux_decisions "
            "(screen, visual_contract, component layout) for the same screen_id and keep them consistent; a screen "
            "feature_ux_decisions does not name gets a new screen_id and you design its screen-wide fields. "
            "feature_ux_decisions.scenario_focus_policies and runtime_states belong to the scenarios they name: use them "
            "only to keep element names consistent, never copy them into this scenario. Transitions, outcomes, feedback, "
            "focus_policies and runtime_states must implement only active_requirement_slice. active_requirement_slice.outcome_context (if present) lists "
            "this scenario's acceptance criteria that the Requirement assigns to other surfaces. They are reference "
            "only: never put their ids in requirement_refs and do not design their backend mechanism; keep what this "
            "screen shows consistent with them (for example an action that stays unavailable while a request is "
            "pending). This surface's own work is set by allowed_requirement_refs. Mark a runtime state owned by another scenario as {not_applicable:true,reason:"
            "'owned by <scenario id>'} and never add another scenario's behavior (for example its cancel or success "
            "outcome) to this node. Inside experience_contract also fill screen{screen_id,purpose,entry_condition}; "
            "runtime_states for loading, "
            "ready, empty, error and success, each as {presentation,trigger} or {not_applicable:true,reason}; "
            "visual_contract with hierarchy, "
            + "layout as {region,layer,content} items where layer is 'page' for regions in the page flow and "
            "'overlay' for a dialog, modal or popover drawn above the page and for every region rendered inside "
            "it (for example a notice or retry action shown within the dialog); reuse the same region names, "
            "layers and style_target_policy that feature_ux_decisions already set for this screen_id; "
            "typography, color_and_tokens, "
            "focus_appearance, responsive_rules, style_target_policy (reuse_existing only when ui_runtime_context "
            "styles has a stylesheet, otherwise new_stylesheet) and style_target_reason; focus_policies as structured "
            "decisions {event,source_removed,removed_element,primary_target,fallback_target,selection_strategy,"
            "announcement{target,moves_focus}} with one deterministic priority and never targeting the removed "
            "element. primary_target/fallback_target own keyboard focus; announcement.target owns the announced "
            "region. A status/live-region announcement never receives keyboard focus: set announcement.moves_focus=false "
            "and restore focus to a stable surviving primary_target. focus_policies may be [] when no focus policy "
            "applies. Each policy requires event, boolean source_removed, primary_target, selection_strategy and "
            "announcement{target,boolean moves_focus}. removed_element and fallback_target are required only when "
            "source_removed=true; otherwise they may be omitted. Include repo_evidence_refs from ui_runtime_context evidence. "
        )
        + "Never add cancel/close behavior from another scenario; keep each alternative or exception in its own projection. "
        if ui_guidance_required
        else ""
    )
    construction_instruction = (
        "Use active_partition only to build missing surfaces. active_requirement_slice is the authoritative behavior "
        "source for this construction step: contracts, responses, outcomes, and edges may only implement IDs listed "
        "in allowed_requirement_refs. That list holds every scenario item this surface takes part in, which can include "
        "items whose behavior another surface of the scenario implements (for example demo data the data surface "
        "seeds): a node cites only the ids its own behavior implements, and the other surface's nodes carry the rest. "
        "active_requirement_slice.cross_cutting_conditions are approved use-case facts, "
        "not behavior: account for each applicable condition in the Design and cite its id without inventing how the "
        "Requirement is implemented. "
        "graph holds the active scenario in full and graph.index lists every other designed node as one row; before "
        "reusing, attaching to or linking a node that appears only in graph.index, call "
        "design.inspect_partition(scenario_id?,component_id?,node_ids?) to see it in full. "
        "requirement_context.scenario_index is navigation-only; never copy behavior from "
        "a sibling scenario into the active projection. A runtime target may be shared by later projections, but this "
        "projection must declare only the active scenario's behavior. delta_scaffold supplies node shapes and the "
        "partition envelope, not a predetermined architecture or node count: add a node for every further operation "
        "or screen the scenario names. Its sample node is a prototype for that surface, not a required extra node "
        "alongside the concrete nodes you design. It does not contain the ordered behavior path. "
        "Partition completion and scenario completion are different: this delta covers the active missing "
        "surfaces and adds interactions whose endpoints already exist in the graph or this delta. The continuous "
        "path and exactly one final terminal edge are obligations of the completed scenario, not of every "
        "intermediate partition. A partition delta may have no behavior edges when their counterpart endpoints "
        "are all left to later partitions; UI transitions still describe its required observable behavior. "
        "Do not invent navigation or a terminal edge merely to make an intermediate delta look complete. "
        "Add the calls/returns edges that can already connect this projection to the same scenario's nodes. "
        "New edges are ordered after the scenario's existing edges, in add_edge operation order. To place them "
        "between existing edges or move the terminal marker, give each new edge a temp_ref ('tmp:...') and add one "
        "{\"op\":\"order_edges\",\"scenario_id\":\"SC-1\",\"edges\":[...],\"terminal\":\"...\"}: edges lists every "
        "calls/returns/reads/writes edge of the scenario exactly once (VDE-* ids of existing edges, temp_refs of new "
        "ones) in behavioral order, and the optional terminal names the last of them, which becomes the only "
        "terminal=true edge. The Harness renumbers the order; structural edges such as depends_on are not listed, "
        "the Harness places them after the behavior edges. Never remove and re-add an edge only to move it. "
        "The delta that designs a scenario's last required partition is rejected unless, ordered by order, each "
        "behavior edge starts at the participant the previous one reached and the single terminal edge is last; where "
        "the user moves from one screen to the next, that path needs a calls edge from the ui node the user leaves to "
        "the ui node the user opens. The continuity check skips a pair of consecutive edges when either carries a "
        "branch_group; the edges without a branch_group must continue from the participant the previous one reached, "
        "and the terminal edge is the last edge of the scenario. "
        "Every edge endpoint must be an existing VDN-* id or a temp_ref added in this same delta; "
        "when the counterpart surface (for example the API a UI calls) is not designed yet, leave that edge to its later "
        "partition instead of inventing an endpoint. terminal=true belongs only on the edge that reaches the "
        "scenario's final observable outcome; when that outcome is reached through a surface not designed yet "
        "(active_partition.later_partitions lists this scenario's surfaces that later partitions design), keep "
        "every edge of this delta terminal=false and let the later partition add the terminal edge. "
        "A scenario's business_rule belongs to the component whose code makes that decision at run time; "
        "surface_work[].placement or active_partition.placeable_surfaces shows the Harness routing guess, which comes "
        "from the scenario's surface list and is not a decision. When this scenario sends any request the server must "
        "accept or reject, or that creates or changes stored data (for example a save, publish, edit or delete "
        "request), its rule runs in the server component, even when the UI also chooses which lists, labels or actions "
        "to show; then a UI partition adds no place_surface and the server partition designs the rule. Showing or "
        "hiding lists, labels or actions is part of the ui node's contract, never by itself a reason to place the "
        "business_rule in the UI. Only when this scenario sends no such request and another component decides what to "
        "show (for example the UI hides actions the reader may not use on a page it only reads), "
        "add {\"op\":\"place_surface\",\"surface\":\"business_rule\",\"component_id\":\"<that "
        "component>\",\"reason\":\"<where the decision runs>\",\"evidence_refs\":[\"<the ref of that code>\"]} instead of "
        "inventing a call: placed in this delta's component, this delta also adds and covers the business_rule node; "
        "placed elsewhere, drop business_rule from this delta's set_partition_coverage and completion_claims, add no "
        "business_rule node, and that component's partition designs it. "
        "When this scenario runs nothing at all on one of its missing surfaces in any component (for example a request "
        "refused before any database access never touches data), add {\"op\":\"not_exercised\",\"surface\":\"<surface>\","
        "\"reason\":\"<what ends the scenario before it>\",\"evidence_refs\":[\"<the ref of that code>\"]} and set that "
        "surface's coverage to not_exercised instead of a node for code that never runs; its Requirement ids must still "
        "be carried by the scenario's other Design nodes. A surface the scenario reads, writes or decides on is designed "
        "as a node, never declared not exercised. "
        "For an alternative or exception scenario, inspect "
        "surface_work.shared_target_candidates before inventing a new runtime target. "
        "surface_work.other_scenario_targets lists what other scenarios already designed on this surface; when a name "
        "suggests the same decision, read it with design.inspect_partition(node_ids) before choosing, judge it from its "
        "contract, not its name, and share its target if it makes the same decision from the same inputs. "
        "Graph projection identity and "
        "runtime target identity are separate: when the branch starts from or returns to the same interaction owner or "
        "entrypoint as a candidate (for example confirm and cancel in one dialog), add the required branch projection "
        "node and share that candidate's exact runtime target even when its contract only describes the main outcome. "
        "Sharing a runtime target does not choose node.disposition. When target_origin=planned and "
        "candidate_disposition=add, keep disposition=add, leave node.evidence_refs empty, and explain the GAP in "
        "evidence_reason; use reuse, modify, or no_change only for a target grounded in frozen repository evidence. "
        "Write the branch projection's contract only from active_requirement_slice; sharing a runtime target never "
        "copies sibling requirement refs or sibling behavior into the branch contract. Share a candidate's runtime "
        "target when this scenario goes through that same code (the same screen, endpoint or query) or makes the same "
        "decision from the same inputs; a different decision gets its own target. State the reason in one sentence "
        "either way. Nothing rejects a delta for sharing or not sharing, so never choose either only to avoid a "
        "rejection. "
        "For an alternative or exception scenario, add the depends_on edge only when "
        "active_partition.branch_parent_requirement supplies it; null means the branch already has its one canonical "
        "parent. When supplied, connect that branch entry node to its parent main entry node (source branch depends on "
        "target main, class-only), set edge.scenario_id to the branch scenario, and keep the branch interactions in "
        "their own scenario. Never create a cross-scenario edge between two main scenarios; a standalone quality "
        "scenario remains separate. Never inline every branch as unconditional calls in the main success sequence. "
        if active_partition is not None
        else ""
    )
    repair_instruction = (
        "Use active_repair_task for every composition or validator repair. The active_repair_task write_scope is a "
        "strict mutation boundary, but its read_scope is not: use landscape, repo, CodeGraph, and "
        "design.inspect_partition to inspect any related profile component needed to understand the path. If "
        "active_repair_task.write_scope.allow_add_node is false, never use add_node; update a listed writable_node or "
        "repair its edges instead. Never update or remove a read_only_node, and preserve every scenario/partition "
        "outside write_scope. active_repair_task.graph_context.preserved_edges are structural cross-scenario edges: "
        "never remove or re-add them; only edges_in_current_order participate in a repair reorder. "
        "Each finding.remediation_reason is the Validator's exact routing witness. Preserve its causal constraints and "
        "use its named existing nodes and edges to guide the repair, but never treat it as authority to change the "
        "approved Requirement or invent evidence. "
        + (
            "For remediation_mode=partition_rebuild, reconstruct the typed nodes and ordered interactions inside the "
            "exact partition; adding or replacing nodes is allowed by write_scope. "
            if str((repair_task or {}).get("remediation_mode") or "") == "partition_rebuild"
            else "Do not rewrite an entire scenario edge chain when one named edge or node can satisfy the finding. "
        )
        + "When every active repair "
        "finding is boundary_contract_mismatch, submit exactly the delta_scaffold operations: keep each replacement "
        "edge's from, to, kind, and label unchanged, and do not add or reorder unrelated edges. When every active "
        "repair finding is runtime_operation_binding_mismatch, submit exactly the delta_scaffold update_node operation "
        "and preserve every runtime_binding field except the corrected target symbol. Use its exact partition identity "
        "and preserve required_coverage. "
        "When every active repair finding is runtime_boundary_unreachable, submit exactly the delta_scaffold removals; "
        "do not invent an API or try to mutate a node's component identity. The next construction partition will place "
        "the still-required surface in a runtime-feasible component. "
        "For other repairs, preserve required_coverage and add or update a node for every listed surface. In repair mode, assign every edge "
        "to the scenario named by the finding, even when an endpoint is reused from another scenario partition. To "
        "change an existing scenario order, use one order_edges operation listing every behavior edge of the scenario "
        "in behavioral order (the IDs in active_repair_task.edges_in_current_order plus temp_refs of edges this delta "
        "adds); remove an edge only when it is obsolete, using exactly {\"op\":\"remove_edge\",\"edge_id\":\"VDE-*\"}, "
        "and never put an edge object inside remove_edge. "
        if repair_task is not None
        else ""
    )
    uiux_tool_instruction = (
        "uiux.search_guidance(query,mode=domain,domain=ux|icons|react) or "
        "uiux.search_guidance(query,mode=stack,stack=react) (not counted in limit_before_delta); "
        if ui_guidance_required and not guidance_reused
        else ""
    )
    delta_scaffold_instruction = (
        "Start from delta_scaffold as the shape reference and replace every angle-bracket placeholder in the "
        "action you submit. In construction, instantiate its node prototypes for the required operations and "
        "screens; their count is not fixed by the scaffold. In repair, obey the supplied write_scope and any "
        "exact-repair instructions instead.\n\n"
        if active_partition is not None or repair_task is not None
        else ""
    )
    # Fixed text first and volatile state last, so consecutive calls share a long prompt prefix (DeepSeek caches
    # by prefix): the UI/UX state and rejection notices used to lead the prompt and changed on every call.
    return (
        "The volatile state comes last in this prompt: CURRENT_STATE_JSON, then the CURRENT UI/UX TOOL STATE block "
        "when present (authoritative: it overrides the general instructions) and any rejection of your previous "
        "action (fix it before doing anything else).\n\n"
        + uiux_instruction
        + runtime_surface_instruction
        + "Investigate frozen AS-IS evidence, identify GAP, and propose TO-BE one tool action at a time. "
        "Choose the next action before planning its complete artifact. If concrete missing source facts need "
        "observations, issue the reads/searches they need now (the files you can name together in one "
        "repo.read_batch); defer node-by-node design and JSON drafting until the observations return. Earlier private reasoning is not replayed to the next call: only recorded "
        "observations, the graph and your action summaries persist. Give every action a top-level \"summary\" "
        "string next to action and arguments: one to three sentences recording the design conclusions reached so far "
        "(chosen screens and runtime targets, required inputs and the hop that provides each, files still to read) "
        "and why this action is next. DESIGN_NOTES_JSON.design_notes returns your earlier summaries for the active "
        "work item in order; continue from them instead of re-deriving them. They are your own unverified notes, not "
        "evidence: when an observation, the graph or a Harness rejection disagrees, follow that and write the "
        "corrected conclusion in the new summary. Do not repeatedly draft the full design in calls that only read "
        "source. Once evidence and required guidance suffice, submit design.propose_delta. "
        "A GAP is only behavior that active_requirement_slice.allowed_requirement_refs needs and the AS-IS target "
        "lacks; when the AS-IS target already provides all of it, use reuse and describe its current runtime states as "
        "they are. UI/UX guidance shapes how required behavior looks; it never adds behavior the Requirement does not "
        "ask for. "
        "Use only the listed tools. Choose disposition by the runtime target: reuse/modify/no_change require "
        "an existing target grounded in observed repository evidence; use modify when required behavior changes "
        "that target, and reuse when its current behavior already satisfies the slice. A new behavior in an "
        "existing target does not require disposition=add. Use add only for a new runtime target and explain "
        "the GAP in evidence_reason. Every node needs requirement_refs, a concise contract, and a "
        f"{SURFACE_CONTRACT_VERSION} surface_contract matching its surface. "
        "Candidate anchors are AS-IS evidence, not automatic TO-BE targets. For disposition=add, choose a concrete new "
        "profile-owned target path/symbol for the required behavior and attach it to the existing composition owner "
        "that registers or renders it; never bind new domain behavior to an unrelated health, smoke, demo, or test "
        "symbol. The add target must not be identical to its owner or to an existing anchor. Use modify/reuse when the "
        "target itself already exists. An attachment names one owner: owner_ref, owner_path and owner_symbol all come "
        "from that same owner, never from two different ones. A new api route is registered where its sibling routes "
        "are, not by the app root that mounts their router: attach it with relation registered_by to the "
        "surface_work.route_registration_modules entry whose routes it joins, with owner_ref its anchor_id, owner_path "
        "its path and owner_symbol empty (the anchor is the whole file). "
        "repo.read and repo.read_batch only accept existing AS-IS files. When an add target does not exist yet, read its existing composition "
        "owner or use an available CodeGraph call path instead of trying to read the TO-BE target. "
        "Never repeat an identical successful tool action. If a search returns zero matches, either broaden it once "
        "or model the required surface as add using a component/root from the frozen landscape. "
        + construction_instruction
        + repair_instruction
        + "For Graph V2, candidate anchors locate source but do not satisfy investigation. While observation_policy.source_inspected "
        "is false, the next action must be repo.read_batch, repo.read or an available codegraph.query for the active "
        "component; a delta proposed before that is rejected. observation_policy.actions_remaining_including_delta is "
        "the real budget of this target or partition and counts every action, the delta included. "
        "observation_policy.counted_observations_left is how many more counted observations you may make before the "
        "delta; each call of landscape.search, landscape.get_anchor, repo.search, repo.read, repo.read_batch or "
        "codegraph.query counts as one, any other tool is not counted. Read the files you already know you need in one "
        "repo.read_batch instead of one repo.read per call. "
        "Before proposing a node with disposition reuse, modify or no_change, "
        "read its runtime_binding.target.path yourself in this scenario, and likewise the file of the provided_by "
        "hop of every required input you declare: the Harness rejects a delta that designs from an existing file no "
        "repo.read or repo.read_batch of this scenario returned. Each scenario starts without the files earlier "
        "scenarios read; recent_observations shows only this scenario's reads. observation_policy.allowed_next_actions (or the CURRENT UI/UX TOOL "
        "STATE) lists what you may do now; record your conclusions in summary. When the evidence suffices, submit "
        "design.propose_delta, or design.report_blocker if required binding evidence is still missing, before "
        "investigating another partition. Choose add or modify from what the code is and what the Requirement needs, "
        "never to save observations. A repair task proposes its delta directly. source_inspected=true records a "
        "successful source or call-path observation, not proof that every target claim is supported. "
        "CodeGraph symbols/files results are navigation_only and do not mark source inspected; read the located source "
        "or request its call path. Read more source when concrete unresolved claims need it, the files you can name "
        "together in one repo.read_batch. If must_propose_delta_now is true, "
        "the valid next actions are design.propose_delta, design.report_blocker for missing required evidence, "
        "or design.inspect_partition of a node listed only in "
        "graph.index; after a design_target_not_read rejection, reading the paths it lists stays allowed. "
        "Call complete only when active_partition and active_repair_task are null.\n\n"
        "Tools: landscape.search(query?,scenario_id?,surface?,component_id?,kind?); "
        "landscape.get_anchor(anchor_id); repo.search(query,component_id?); "
        "repo.read(path,start_line?,end_line?); "
        f"repo.read_batch(requests) where requests is 1-{MAX_BATCH_READS} objects {{path,start_line?,end_line?}} and "
        "each file comes back as repo.read returns it; "
        "codegraph.query(operation=explore|symbols|callers|callees|impact|files,query?,symbol?,kind?,limit?,depth?,filter?,pattern?); "
        + uiux_tool_instruction
        + "design.inspect_partition(scenario_id?,component_id?,node_ids?); "
        "design.report_blocker(target_ref,missing_facts,reason); "
        "After a source/CodeGraph attempt, if a concrete required binding remains unverifiable, use remaining observations "
        "for that fact or report it with nonempty missing_facts (string array) and a concise reason. Do not invent bindings "
        "or repeat searches; this action stops the run with a diagnostic, consumes existing budget and creates no checkpoint. "
        f"design.propose_delta(delta); complete(). The delta requires schema_version={delta_contract}, "
        "base_graph_digest (copy it from delta_scaffold; the Harness sets it to the digest of the graph this prompt "
        "shows, so never spend effort checking it), partition{use_case_id,scenario_id,component_id}, operations, "
        "completion_claims, and unresolved_items. completion_claims must be objects shaped exactly like "
        "{\"scenario_id\":\"SC-1\",\"surfaces\":[\"ui\",\"api\"]}; allowed surface values are ui, api, "
        "business_rule, data, and interaction. The completion_claims array must never contain bare strings. "
        "Supported operations are set_partition_coverage, "
        "add_node, update_node, remove_node, add_edge, remove_edge, order_edges, place_surface, and not_exercised. An add_node contains "
        "temp_ref, semantic_key, name, kind, surface, disposition, requirement_refs, evidence_refs, "
        "evidence_reason, contract, surface_contract, and for Graph V2 runtime_binding. The node key list is "
        "closed: do not add undeclared node keys, even with null values. Optional schema fields may be omitted "
        "unless their stated condition makes them required; placeholders are explanations, not literal values. "
        "node.evidence_refs accepts only "
        "frozen repository evidence (RLA-* or repo:*) returned by Designer tools or listed in ui_runtime_context "
        "(its evidence_ref values are valid as they are), copied exactly: the #L line is part "
        "of the ref, so never change it or write a new one; never put Requirement provenance E-*, "
        "Requirement IDs, or uiux:* refs there. Reuse/modify/no_change need such a repository evidence ref. For "
        "disposition=add, leave node.evidence_refs empty, explain the GAP in evidence_reason, and keep inspected "
        "composition-owner or entrypoint anchors under runtime_binding.evidence_refs. For a mutation, the complete outer envelope must be "
        f"{{\"action\":\"design.propose_delta\",\"arguments\":{{\"delta\":{{\"schema_version\":\"{delta_contract}\",...}}}},\"summary\":\"...\"}}; "
        f"never use {graph_contract} as the delta schema and never put delta fields directly under arguments. "
        "set_partition_coverage must be exactly {\"op\":\"set_partition_coverage\",\"coverage\":{...}}. "
        "add_node must be exactly {\"op\":\"add_node\",\"node\":{...}}. temp_ref must start with 'tmp:'. "
        "For a delta, use readable multiline JSON with each operation clearly separated. Close the node and its "
        "operation object before starting the next operations array item; indentation does not replace closers. "
        "Node kind is the graph kind supplied by the add_node prototypes in delta_scaffold.operations, not a "
        "landscape anchor kind. Node contract must be "
        "one non-empty string, never an object. surface_contract must keep schema_version exactly "
        "design_surface_contract_v1 and keep surface unchanged. UI requires entrypoint, states, "
        "transitions[{from,action,to,outcome}], validations "
        "(an array of non-empty strings, never objects; it may be empty) and "
        "outcomes. A UI transition outcome is the observable postcondition promised after that transition completes; "
        "it is not a separately ordered runtime event or mechanism. Ordered scenario edges may be the mechanism that "
        "makes it true, so keep the outcome consistent with their mappings rather than treating repeated wording as a "
        "second execution. API requires operation_id, method, path, request_fields[{name,type,location}], responses[{status,outcome}], "
        "authentication, boolean authentication_required, authentication_source "
        "(existing_runtime|external_dependency|new_boundary|none), authentication_requirement_refs, "
        "authentication_evidence_refs, authorization, and boolean authorization_required. Authentication proves identity/session; "
        "authorization is only role/permission policy. Use existing_runtime only with observed repository authentication evidence. "
        "authentication_required=false with a source other than none means optional authentication: callers without a "
        "credential are served, and a present credential identifies the caller; use none only when the operation never "
        "reads the caller's identity. "
        "business_rule requires inputs (an array of non-empty strings, never objects), decision and "
        "outcomes (an array of non-empty strings). data requires entity, operation, "
        "fields[{name,type}], constraints and relations (arrays of non-empty strings, never objects, for example "
        "\"Article.authorUsername -> User.username (many-to-one)\") and migration_intent. "
        "interaction requires steps and outcomes. "
        "Use update_node with changes.surface_contract to enrich an existing node during bundle repair. add_edge must be exactly "
        "{\"op\":\"add_edge\",\"edge\":{\"from\":\"VDN-* or tmp:*\",\"to\":\"VDN-* or tmp:*\","
        "\"kind\":\"calls|reads|writes|returns|contains|realized_by|depends_on\",\"label\":\"specific interaction\","
        "\"scenario_id\":\"SC-*\",\"branch_group\":\"optional-safe-key\",\"guard\":\"optional condition\","
        "\"terminal\":false,\"temp_ref\":\"optional tmp:* name for order_edges\"}}. branch_group and guard must be "
        "supplied together on every consecutive edge belonging "
        "to a conditional error/alternative branch; set terminal=true on the final observable outcome edge. "
        "Multiple interactions between the same endpoints are allowed when labels differ. "
        "For Graph V2, every edge between nodes of different components uses the field boundary_contract; an edge "
        "between two nodes of the same component needs none, and a boundary_contract you add there is checked like any "
        "other; "
        "boundary_contract_v1 is only its schema_version, never the field name. Use exactly "
        "{\"boundary_contract\":{\"schema_version\":\"boundary_contract_v1\","
        "\"consumer_operation\":\"specific caller operation\",\"provider_operation\":\"METHOD /path for an API, "
        "otherwise the specific provider operation\",\"input_mapping\":[{\"from\":\"consumer field\","
        "\"to\":\"provider field\",\"required\":true}],\"auth_mapping\":[],"
        "\"response_mapping\":[{\"from\":\"provider success\",\"to\":\"consumer outcome\","
        "\"required\":true}],\"error_mapping\":[]}}. Map every API request field (required:false when the consumer "
        "may omit it), required authentication "
        "or authorization, success response, and 4xx/5xx response; 4xx/5xx statuses go only in error_mapping, and "
        "response_mapping stays empty when the API declares no success response. This full provider mapping is required on both calls "
        "and returns edges when the provider is an API; a returns edge must not map only the response. The Harness copies "
        "input_mapping onto every new returns edge from its paired calls/reads/writes edge, so on a returns edge write "
        "the response_mapping and error_mapping. On a returns edge, "
        "from is the provider and to is the consumer, so provider_operation always names the from-node operation. "
        "When the provider is a business_rule, input_mapping must map every name in its surface_contract.inputs as a "
        "required target; declare only inputs the caller can actually supply. "
        "For service and persistence_adapter providers, when boundary_contract.provider_operation is a concrete "
        "identifier, runtime_binding.target.symbol must expose that exact callable. A file may host multiple symbols; "
        "never bind a query operation to a create/update/delete callable. "
        "A runtime_binding.attachment with relation called_by names the caller: its owner must be the runtime target "
        "of a node with a calls edge into this node (the route handler or screen that runs the call), never an app "
        "root that only wires modules together. "
        "A boundary_contract describes data mapping but does not make a direct cross-application call executable. "
        "Code in one apps/* application may call a non-API provider in another apps/* application only through an "
        "explicit approved transport surface. Business rules do not imply backend placement: when UI is required and "
        "API/data are not applicable, keep the rule in the UI application or an importable shared package. Never add a "
        "server endpoint merely to repair a scenario whose approved API surface is not_applicable. "
        "Sequence projections are linear within each scenario: only calls/reads/writes/returns become messages. "
        "These are completed-scenario invariants, after all of its required partitions: every scenario with a UI "
        "surface needs actual behavior beyond depends_on, exactly one terminal edge at the "
        "final observable outcome, and a continuous ordered message path. When the scenario names several operations "
        "with the same outcome (for example the same refusal of several different requests), give each operation its "
        "own api_contract node (one method and path each) and its own message path: every operation path except the "
        "last carries a branch_group with a guard naming that operation, and only the last path ends with the terminal "
        "edge. Give branch_group and guard when you add an edge: a later partition cannot change them, so the "
        "partition that adds an edge of a path that is not the last gives it that operation's branch_group, and the "
        "later partitions continue that path with the same branch_group. "
        "Never leave out an operation the scenario names. Likewise a scenario that the actor carries out or "
        "observes on several screens (for example an editor and a list on another page, or the same list rule seen on "
        "two pages) has one ui node per screen, each attached where the AS-IS code renders that screen; a screen whose "
        "code already does what the scenario needs still gets its node, with disposition reuse. When two screens render the same AS-IS component (for "
        "example a create page and an edit page that both render one form component), keep one ui node per screen, "
        "each with its own screen_id and a composition_path through that screen's own page; a node targets its page "
        "when that screen needs a new value or handler the page must create, and the shared component only when the "
        "change lies entirely inside it and every value it needs is already passed to it. "
        "If the requirement requires human confirmation "
        "before persistence, represent that confirmation in the UI surface contract before the outbound mutation call or "
        "in the interaction path before any writes edge. Do not invent a preliminary backend verification round trip unless "
        "the approved requirement explicitly requires it. "
        + delta_scaffold_instruction
        + "Action JSON: {\"action\":\"tool.id\",\"arguments\":{...},\"summary\":\"design conclusions so far and why "
        "this action is next\"}\n\n"
        + _prompt_blocks(payload)
        + "".join(f"\n\n{text.rstrip()}" for text in (uiux_status_instruction, correction, review_correction) if text)
    )


# Ordered from run-stable to volatile; keys keep their names, so instructions such as
# active_partition.surface_work or observation_policy.must_propose_delta_now stay valid.
_PROMPT_BLOCKS = (
    (
        "RUN_CONTEXT_JSON",
        ("graph_contract", "delta_contract", "landscape_summary", "components", "requirement_context", "requirement",
         "uiux_mode", "repair_feedback_delivery"),
    ),
    (
        "PARTITION_CONTEXT_JSON",
        ("active_partition", "active_repair_task", "active_requirement_slice", "scenario_matches", "graph",
         "ui_runtime_context", "feature_ux_decisions", "repair_feedback"),
    ),
    ("OBSERVATIONS_JSON", ("recent_observations",)),
    # Append-only within a work item: after the observations, so a new note never shifts their prefix.
    ("DESIGN_NOTES_JSON", ("design_notes",)),
)


def _prompt_blocks(payload: dict[str, object]) -> str:
    placed = {key for _, keys in _PROMPT_BLOCKS for key in keys}
    blocks = [(label, {key: payload[key] for key in keys if key in payload}) for label, keys in _PROMPT_BLOCKS]
    # Everything else (counters, UI/UX status, last rejection) changes between calls: it goes last.
    blocks.append(("CURRENT_STATE_JSON", {key: value for key, value in payload.items() if key not in placed}))
    return "\n\n".join(
        f"{label}:\n{json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        for label, block in blocks
    )


def designer_prompt_payload(prompt: str) -> dict[str, object]:
    """Merge the labelled JSON blocks of a Designer prompt back into one payload (for tests and run viewers)."""

    payload: dict[str, object] = {}
    decoder = json.JSONDecoder()
    for label, _ in (*_PROMPT_BLOCKS, ("CURRENT_STATE_JSON", ())):
        marker = f"\n{label}:\n"
        start = prompt.find(marker)
        if start >= 0:
            block, _ = decoder.raw_decode(prompt, start + len(marker))
            payload.update(block)
    return payload


def _graph_without_ui_context(graph: dict[str, object]) -> dict[str, object]:
    binding = _dict(graph.get("snapshot_binding"))
    if "ui_repo_context" not in binding:
        return graph
    return {**graph, "snapshot_binding": {key: value for key, value in binding.items() if key != "ui_repo_context"}}


def _scenario_graph_slice(graph: dict[str, object], *, scenario_id: str) -> dict[str, object]:
    """The active scenario in full, every other node as one index row (design.inspect_partition shows it in full).

    Sending every node of every scenario made each prompt grow with the number of designed partitions.
    """

    if not scenario_id:
        return graph
    nodes = _dicts(graph.get("nodes"))
    kept = {str(node.get("id") or "") for node in nodes if str(node.get("scenario_id") or "") == scenario_id}
    # A runtime-bound node of another scenario that an active node is attached to is part of its runtime path.
    kept |= {
        str(_dict(_dict(node.get("runtime_binding")).get("attachment")).get("owner_ref") or "")
        for node in nodes
        if str(node.get("id") or "") in kept
    }
    edges = [
        edge
        for edge in _dicts(graph.get("edges"))
        if str(edge.get("scenario_id") or "") == scenario_id
        or str(edge.get("from") or "") in kept
        or str(edge.get("to") or "") in kept
    ]
    index = [
        {
            "id": node.get("id"),
            "scenario_id": node.get("scenario_id"),
            "component_id": node.get("component_id"),
            "surface": node.get("surface"),
            "kind": node.get("kind"),
            "disposition": node.get("disposition"),
            "name": node.get("name"),
            "target": "{path}#{symbol}".format(
                path=_dict(_dict(node.get("runtime_binding")).get("target")).get("path") or "",
                symbol=_dict(_dict(node.get("runtime_binding")).get("target")).get("symbol") or "",
            ),
        }
        for node in nodes
        if str(node.get("id") or "") not in kept
    ]
    return {
        **graph,
        "nodes": [node for node in nodes if str(node.get("id") or "") in kept],
        "edges": edges,
        "partitions": [
            item for item in _dicts(graph.get("partitions")) if str(item.get("scenario_id") or "") == scenario_id
        ],
        "index": index,
        "index_edge_counts": dict(
            sorted(
                collections.Counter(
                    str(edge.get("scenario_id") or "")
                    for edge in _dicts(graph.get("edges"))
                    if str(edge.get("id") or "") not in {str(item.get("id") or "") for item in edges}
                ).items()
            )
        ),
    }


def _construction_graph_view(
    graph: dict[str, object],
    *,
    active_partition: dict[str, object] | None,
) -> dict[str, object]:
    """Keep active topology visible without sending an unrelated full UX contract to backend partitions."""

    view = _scenario_graph_slice(
        _graph_without_ui_context(graph),
        scenario_id=str(_dict(_dict(active_partition).get("partition")).get("scenario_id") or ""),
    )
    surfaces = {
        str(item.get("surface") or "")
        for item in _dicts(_dict(active_partition).get("surface_work"))
    }
    if "ui" in surfaces:
        return view
    nodes = []
    for node in _dicts(view.get("nodes")):
        if str(node.get("surface") or "") != "ui":
            nodes.append(node)
            continue
        surface = _dict(node.get("surface_contract"))
        surface.pop("experience_contract", None)
        nodes.append({**node, "surface_contract": surface})
    return {**view, "nodes": nodes}


def _ui_runtime_prompt_context(
    graph: dict[str, object],
    *,
    requirement_model: dict[str, object] | None = None,
    allowed_refs: list[str] | None = None,
) -> dict[str, object]:
    binding = _dict(graph.get("snapshot_binding"))
    context = _dict(binding.get("ui_repo_context"))
    known = set(_strings(binding.get("requirement_reference_ids")))
    approved_decisions = set(_strings(binding.get("approved_decision_ids")))
    allowed = set(allowed_refs or [])
    cited = {
        ref
        for node in _dicts(graph.get("nodes"))
        for item in _dicts(_dict(_dict(node.get("surface_contract")).get("runtime_surface")).get("required_inputs"))
        for ref in [str(item.get("binding") or ""), *_strings(item.get("requirement_refs"))]
    }
    model = _dict(requirement_model)
    texts = {
        # Decisions (D-*) carry their content in `answer`.
        str(item.get("id")): str(item.get("text") or item.get("action") or item.get("answer") or "")
        for items in [
            *(_dicts(model.get(key)) for key in ("preconditions", "decisions")),
            *(_dicts(value) for value in _dict(model.get("flows")).values()),
        ]
        for item in items
    }
    return {
        "entrypoint": _dict(context.get("entrypoint")).get("ref"),
        "extraction": context.get("extraction"),
        "uncertainty": context.get("uncertainty", []),
        "composition_chain": [
            {
                "ref": hop.get("ref"),
                # The graph gate accepts this ref as a web attachment owner_ref; say so where the Designer looks
                # (Conduit UC4 H-r1: it hunted an RLA id for NewArticle and then mounted a new editor on App instead).
                **({"owner_ref": hop.get("ref")} if hop.get("renders") else {}),
                "renders": hop.get("renders"),
                "passes_props": hop.get("passes_props"),
                "props_status": _dict(_dict(context.get("components")).get(str(hop.get("ref") or ""))).get("props_status", "legacy"),
                "renders_status": hop.get("renders_status", "legacy"),
                "required_props": (None if _dict(_dict(context.get("components")).get(str(hop.get("ref") or ""))).get("props_status") == "unknown"
                                   else required_props(context, str(hop.get("ref") or ""))),
                "evidence_ref": hop.get("evidence_ref"),
            }
            for hop in _dicts(context.get("composition_chain"))
        ],
        "data_clients": [item.get("ref") for item in _dicts(context.get("data_clients"))],
        "auth": context.get("auth"),
        "styles": context.get("styles"),
        # Preconditions/decisions, the active slice's flow steps, and ids already cited by existing UI runtime inputs
        # (screens reused across scenarios keep their dependency) may justify a runtime input source.
        "dependency_refs": [
            {"id": ref, **({"text": texts[ref]} if texts.get(ref) else {})}
            for ref in sorted(known)
            if ref.startswith("PRE-")
            or (ref.startswith("D-") and ref in approved_decisions)
            or (ref.startswith(("MF-", "AF-", "EF-")) and (ref in allowed or ref in cited))
        ],
    }


def _feature_ux_decisions(graph: dict[str, object]) -> dict[str, object]:
    """Screen-level UX already committed to the graph, derived on demand so resume needs no extra state."""

    screens: dict[str, dict[str, object]] = {}
    for node in _dicts(graph.get("nodes")):
        contract = _dict(node.get("surface_contract"))
        surface = _dict(contract.get("runtime_surface"))
        experience = _dict(contract.get("experience_contract"))
        if str(node.get("surface") or "") != "ui" or not surface or is_composition_wiring(node):
            continue
        screen = screens.setdefault(
            str(surface.get("screen_id") or ""),
            {"nodes": [], "composition_paths": [], "required_inputs": []},
        )
        screen["nodes"].append(f"{node.get('id')} ({node.get('scenario_id')}): {node.get('name')}")
        screen["composition_paths"].append(_strings(surface.get("composition_path")))
        screen["required_inputs"].extend(
            f"{item.get('name')} <- {item.get('source')}" for item in _dicts(surface.get("required_inputs"))
        )
        for key in ("screen", "visual_contract"):
            if experience.get(key) and key not in screen:
                screen[key] = experience[key]
        # Focus policies are scenario behavior: keep their owner so a sibling scenario does not copy them.
        screen.setdefault("scenario_focus_policies", []).extend(
            {"scenario_id": str(node.get("scenario_id") or ""), **policy}
            for policy in _dicts(experience.get("focus_policies"))
        )
        screen.setdefault("_experiences", []).append(experience)
        screen.setdefault("_scenarios", []).append(str(node.get("scenario_id") or ""))
    for screen in screens.values():
        screen["runtime_states"] = _merged_runtime_states(screen.pop("_experiences"), screen.pop("_scenarios"))
    return screens
