"""Runtime/Integration Closure contract (L5.3).

Harness derives the closure context from frozen typed artifacts, assembles the Closure
Planner output into ``runtime_closure_contract_v1`` and checks the mechanically decidable
gates G1-G13 and G15. Whether a resolution, probe or demo decision makes sense is the
Closure Validator's and the owner's call: nothing here reads domain prose or knows any
use-case vocabulary. Every function is pure; repository facts (symbols, references,
runtime-config source issues) are passed in by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re

from ...core.errors import HarnessError
from ...profiles.planning import STYLESHEET_ROOTS, PlanningPathPolicy
from ...profiles.signature import implementation_signature_contract_digest
# Harness renders this spec from the approved probes; no closure unit may write it.
from ...verification.registry import RUNTIME_PROBE_SPEC_PATH
from ...verification.runner import RUNTIME_PROBE_ANNOTATION_TYPE, RuntimeProbeRun
from ...profiles.runtime_config import (
    canonical_runtime_config_impact,
    runtime_config_impact_issues,
    runtime_config_policy,
)

CONTEXT_SCHEMA_VERSION = "runtime_closure_context_v1"
CONTRACT_SCHEMA_VERSION = "runtime_closure_contract_v1"
CLOSURE_VERIFICATION_POLICY_VERSION = "runtime_closure_stage_policy_v1"
# shortcut: closure stage ownership lives here until L5.3.5 adds the closure policy to profiles/verification.py.
CLOSURE_DEVELOPER_STAGE_IDS = ("typecheck", "unit")
CLOSURE_PROBE_SERVICES = ("api", "web")
BROWSER_TEST_COMPONENT_ID = "e2e"
MAX_PROBE_BODY_EXCERPT_CHARS = 2000

ORIGINS = ("design_required_input", "plan_runtime_config", "closure_declared")
HARNESS_ORIGINS = ("design_required_input", "plan_runtime_config")
DESIGN_SOURCES = ("existing_runtime", "external_use_case", "new_boundary", "demo_fixture")
RESOLUTION_KINDS = ("existing_runtime", "new_boundary", "approved_demo_fixture", "unresolved_external")
# design_source (frozen by Design) -> resolution kinds the closure may choose (section 3 of the plan).
RESOLUTION_COMPATIBILITY: dict[str | None, tuple[str, ...]] = {
    "existing_runtime": ("existing_runtime",),
    "new_boundary": ("new_boundary", "existing_runtime"),
    "demo_fixture": ("approved_demo_fixture",),
    "external_use_case": ("approved_demo_fixture", "unresolved_external"),
    None: RESOLUTION_KINDS,
}
PRODUCTION_RESOLUTION_KINDS = ("existing_runtime", "new_boundary")
PROVIDER_KINDS = ("symbol", "env_var")
CHANGE_REFS = ("plan", "closure")
PROBE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
DEMO_DECISION_SCOPE = "demo_and_verification_only"
BOUND_INPUT_KEYS = (
    "vertical_design_graph_digest",
    "implementation_plan_sha256",
    "code_snapshot_fingerprint",
    "verification_policy_version",
)
CLOSURE_RUNNABLE_STATUSES = ("bound", "fixture_bound", "externally_blocked")

CONTRACT_INVALID = "runtime_closure_contract_invalid"
CONFIG_UNSUPPORTED = "runtime_closure_config_unsupported"

_INPUT_ID = re.compile(r"^RI-[0-9A-F]{12}$")
_UNIT_ID = re.compile(r"^CU-[0-9A-F]{12}$")
_CLOSURE_DECISION_ID = re.compile(r"^CD-[1-9][0-9]*$")
_REQUIREMENT_DECISION_ID = re.compile(r"^D-[A-Za-z0-9][A-Za-z0-9_-]*$")
_PROBE_ID = re.compile(r"^RP-[1-9][0-9]*$")
_STAND_IN_ID = re.compile(r"^VS-[1-9][0-9]*$")
_PLANNER_KEY = re.compile(r"^(?!RI-|CU-)[A-Za-z][A-Za-z0-9_-]{0,31}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_SYMBOL_REF = re.compile(r"^([^#\s]+)#([A-Za-z_$][A-Za-z0-9_$]*)$")
_PROBE_PATH = re.compile(r"^/[^\s?#]*(\?[^\s#]*)?$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_PROBE_HEADER_VALUE_CHARS = 1000
MAX_PROBE_JSON_BODY_CHARS = 4000
# A request body is meaningful only for methods that carry one.
PROBE_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# How Harness executes what a contract declares. The Planner and the Validator see the same facts.
CLOSURE_EXECUTION_FACTS = (
    "During runtime_probe Harness sets each stand-in env_key to a run-scoped loopback URL for the booted services "
    "and the probe process alike, starts each stand-in by calling its start_export with that URL's port before "
    "the probes, and closes it afterwards. A stand-in listens on the port it is given, never on a fixed port or "
    "the production default; the production default stays unchanged.",
    "closure_context.plan_allowed_paths are the files the frozen Plan already writes. A closure unit may also "
    "write other files inside path_policy.component_editable_roots (never protected or Harness-owned paths); "
    "those are listed to the owner as new write targets for approval.",
)
# What the Closure Validator judges. The Planner sees the same list so both work to one standard;
# none of it is a Harness gate.
CLOSURE_REVIEW_DUTIES = (
    "input_completeness: does the journey need a runtime input the contract does not declare?",
    "provider_validity: is each provider a sensible source for its input, not an empty default in disguise?",
    "session_verification: if an input is a session or identity, does the closure verify it server-side at the level "
    "the approved contract requires, instead of trusting a client-supplied value?",
    "probe_evidence: does each probe or test intent actually prove the inputs and scenarios it cites?",
    "demo_isolation: do demo fixtures and stand-ins stay in demo/verification scope and out of the production path?",
    "demo_data_sufficiency: is the demo data enough for every approved scenario that is not blocked?",
    "unresolved_justification: is each unresolved input truly unresolvable, or is the closure avoiding work?",
    "approved_behavior_change: does any unit objective or provider change approved business behavior?",
)

SymbolExists = Callable[[str, str], bool]
# True: the earlier hop imports/references the later one; False: it does not; None: unknown.
SymbolReferences = Callable[[str, str], bool | None]


# --------------------------------------------------------------------------- context


def derive_closure_context(
    *,
    run_id: str,
    work_packet: dict[str, object],
    bound_inputs: dict[str, object],
) -> dict[str, object]:
    """Project the runtime inputs of a frozen work packet into the closure context.

    A required input is in scope when its source is not ``existing_runtime`` or its surface
    is not ``bound``. Inputs repeated on several surfaces of the same consumer collapse into
    one input whose ``scenario_ids`` and ``source_refs`` are the union of those surfaces.
    """

    missing = [key for key in BOUND_INPUT_KEYS if not _text(bound_inputs.get(key))]
    if not run_id or missing:
        raise HarnessError(
            "runtime_closure_context_invalid",
            f"Runtime closure context needs run_id and bound inputs; missing: {missing or ['run_id']}",
            run_id=run_id or None,
        )
    units = _dicts(work_packet.get("execution_units"))
    unit_scenarios = {_text(unit.get("unit_id")): _text(unit.get("scenario_id")) for unit in units}
    node_scenarios: dict[str, set[str]] = {}
    for unit in units:
        for node_id in _texts(unit.get("design_node_ids")):
            if unit_scenarios[_text(unit.get("unit_id"))]:
                node_scenarios.setdefault(node_id, set()).add(unit_scenarios[_text(unit.get("unit_id"))])

    grouped: dict[tuple[str, str, str, str], dict[str, set[object]]] = {}
    for unit in units:
        scenario_id = _text(unit.get("scenario_id"))
        binding_targets = {
            _text(binding.get("design_node_id")): _target_ref(binding.get("target"))
            for binding in _dicts(unit.get("runtime_bindings"))
        }
        for surface in _dicts(unit.get("surface_contracts")):
            runtime = _dict(_dict(surface.get("surface_contract")).get("runtime_surface"))
            if not runtime:
                continue
            node_id = _text(surface.get("design_node_id"))
            path = tuple(_texts(runtime.get("composition_path")))
            consumer = binding_targets.get(node_id) or (path[-1] if path else "")
            surface_bound = _text(runtime.get("runnable_status")) == "bound"
            for required in _dicts(runtime.get("required_inputs")):
                source = _text(required.get("source"))
                if source == "existing_runtime" and surface_bound:
                    continue
                key = (consumer, _text(required.get("name")), source, _text(required.get("provided_by")))
                entry = grouped.setdefault(
                    key,
                    {"node_ids": set(), "refs": set(), "bindings": set(), "scenarios": set(), "paths": set()},
                )
                entry["node_ids"].add(node_id)
                entry["refs"].update(_texts(required.get("requirement_refs")))
                entry["bindings"].add(_text(required.get("binding")))
                entry["paths"].add(path)
                if scenario_id:
                    entry["scenarios"].add(scenario_id)

    runtime_inputs: list[dict[str, object]] = []
    for (consumer, name, source, provided_by), entry in sorted(grouped.items()):
        bindings = {str(value) for value in entry["bindings"] if value}
        refs = {str(value) for value in entry["refs"]}
        runtime_inputs.append(
            {
                "input_id": _digest_id("RI", "design_required_input", consumer, name, source, provided_by),
                "origin": "design_required_input",
                "name": name,
                "design_source": source or None,
                "source_refs": sorted(
                    {str(value) for value in entry["node_ids"] if value}
                    | refs
                    | {value for value in bindings if "#" not in value}
                ),
                "consumer": consumer,
                "provided_by": provided_by,
                "design_decision_refs": sorted(ref for ref in refs | bindings if ref.startswith("D-")),
                "composition_paths": [list(path) for path in sorted(entry["paths"]) if path],
                "scenario_ids": sorted(str(value) for value in entry["scenarios"]),
            }
        )

    plan_env_changes = _plan_env_changes(work_packet)
    for change in sorted(plan_env_changes, key=lambda item: (item["name"], item["read_by"])):
        unit_id = change["unit_id"]
        runtime_inputs.append(
            {
                "input_id": _digest_id("RI", "plan_runtime_config", "env_var", change["name"], change["read_by"]),
                "origin": "plan_runtime_config",
                "name": change["name"],
                "design_source": None,
                "source_refs": [unit_id] if unit_id else [],
                "consumer": change["read_by"],
                "provided_by": "",
                "env": {"env_key": change["name"], "read_by": change["read_by"], "default": change["default"]},
                "scenario_ids": [unit_scenarios[unit_id]] if unit_scenarios.get(unit_id) else [],
            }
        )

    entrypoints = sorted(
        {
            ref
            for unit in units
            for binding in _dicts(unit.get("runtime_bindings"))
            if _text(binding.get("runtime_kind")) == "entrypoint"
            for ref in [_target_ref(binding.get("target"))]
            if ref
        }
    )
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "run_id": run_id,
        "bound_inputs": {key: _text(bound_inputs.get(key)) for key in BOUND_INPUT_KEYS},
        "entrypoints": entrypoints,
        "scenario_ids": sorted({value for value in unit_scenarios.values() if value}),
        "design_node_scenarios": {node: sorted(values) for node, values in sorted(node_scenarios.items())},
        "runtime_inputs": runtime_inputs,
        "plan_env_changes": plan_env_changes,
        "plan_allowed_paths": sorted(set(_texts(_dict(work_packet.get("output_contract")).get("allowed_paths")))),
        "approved_runtime_decision_ids": sorted(
            {_text(item.get("id")) for item in _dicts(work_packet.get("approved_runtime_decisions"))} - {""}
        ),
    }


# --------------------------------------------------------------------------- assembly


def assemble_closure_contract(
    *,
    context: dict[str, object],
    planner_output: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Merge Planner-owned fields into the Harness context; Harness assigns every ID.

    The Planner names its own closure units and closure-declared inputs with short ``key``
    labels and may cite those keys wherever an ID is expected. Harness replaces each key with
    a digest ID so the contract never carries an LLM-invented ID.
    """

    issues: list[dict[str, object]] = []
    output = planner_output if isinstance(planner_output, dict) else {}
    context_inputs = _dicts(context.get("runtime_inputs"))
    known_ids = {_text(item.get("input_id")) for item in context_inputs}

    input_keys: dict[str, str] = {}
    declared: list[dict[str, object]] = []
    for item in _dicts(output.get("closure_declared_inputs")):
        key = _text(item.get("key"))
        name, consumer, provided_by = _text(item.get("name")), _text(item.get("consumer")), _text(item.get("provided_by"))
        input_id = _digest_id("RI", "closure_declared", consumer, name, provided_by)
        if not _PLANNER_KEY.fullmatch(key):
            issues.append(_issue("G1", f"closure_declared_inputs key `{key}` must match {_PLANNER_KEY.pattern}.", input_id))
        elif key in input_keys:
            issues.append(_issue("G1", f"closure_declared_inputs key `{key}` is used twice.", input_id))
        input_keys[key] = input_id
        declared.append(
            {
                "input_id": input_id,
                "origin": "closure_declared",
                "name": name,
                "design_source": None,
                "source_refs": sorted(set(_texts(item.get("source_refs")))),
                "consumer": consumer,
                "provided_by": provided_by,
                "resolution": _resolution(item.get("resolution")),
            }
        )

    def input_ref(value: object) -> str:
        text = _text(value)
        return input_keys.get(text, text)

    resolutions: dict[str, dict[str, object]] = {}
    for item in _dicts(output.get("resolutions")):
        input_id = _text(item.get("input_id"))
        if input_id not in known_ids:
            issues.append(_issue("G2", f"Resolution cites unknown Harness input `{input_id}`.", input_id))
        elif input_id in resolutions:
            issues.append(_issue("G2", f"Input `{input_id}` has more than one resolution.", input_id))
        else:
            resolutions[input_id] = _resolution(item)
    for input_id in sorted(known_ids - set(resolutions)):
        issues.append(_issue("G2", f"Input `{input_id}` has no resolution.", input_id))

    runtime_inputs = [
        {**_public_input(item), "resolution": resolutions.get(_text(item.get("input_id")))}
        for item in context_inputs
    ] + declared

    unit_keys: dict[str, str] = {}
    raw_units = _dicts(output.get("closure_units"))
    for item in raw_units:
        key, component_id = _text(item.get("key")), _text(item.get("component_id"))
        unit_id = _digest_id("CU", component_id, key)
        if not _PLANNER_KEY.fullmatch(key):
            issues.append(_issue("G1", f"closure_units key `{key}` must match {_PLANNER_KEY.pattern}.", unit_id))
        elif key in unit_keys:
            issues.append(_issue("G1", f"closure_units key `{key}` is used twice.", unit_id))
        unit_keys.setdefault(key, unit_id)

    def unit_ref(value: object) -> str:
        text = _text(value)
        return unit_keys.get(text, text)

    closure_units = [
        {
            "unit_id": _digest_id("CU", _text(item.get("component_id")), _text(item.get("key"))),
            "key": _text(item.get("key")),
            "component_id": _text(item.get("component_id")),
            "objective": _text(item.get("objective")),
            "depends_on": [unit_ref(value) for value in _raw_list(item.get("depends_on"))],
            "resolves_input_ids": [input_ref(value) for value in _raw_list(item.get("resolves_input_ids"))],
            "write_paths": _raw_texts(item.get("write_paths")),
            "test_target_paths": _raw_texts(item.get("test_target_paths")),
            "required_stage_ids": _raw_texts(item.get("required_stage_ids")),
            "test_intents": [
                {
                    "intent": _text(intent.get("intent")),
                    "test_path": _text(intent.get("test_path")),
                    "proves_input_ids": [input_ref(value) for value in _raw_list(intent.get("proves_input_ids"))],
                }
                for intent in _dicts(item.get("test_intents"))
            ],
            "context_paths": _raw_texts(item.get("context_paths")),
        }
        for item in raw_units
    ]

    raw_changes = [
        {**item, "unit_id": unit_ref(item.get("unit_key") or item.get("unit_id"))}
        for item in _dicts(output.get("runtime_config_changes"))
    ]
    runtime_config_changes = canonical_runtime_config_impact({"changes": raw_changes})["changes"]

    runtime_probes = [
        {
            "probe_id": _text(item.get("probe_id")),
            "scenario_ids": _raw_texts(item.get("scenario_ids")),
            "service": _text(item.get("service")),
            "method": _text(item.get("method")).upper(),
            "path": _text(item.get("path")),
            "headers_from_input_ids": [input_ref(value) for value in _raw_list(item.get("headers_from_input_ids"))],
            # Literal header values the owner approves at the checkpoint; G1 checks their shape.
            "headers": dict(item["headers"]) if isinstance(item.get("headers"), dict) else item.get("headers") or {},
            # Literal JSON request body (or null), approved by the owner like headers; G1 checks its shape.
            "json_body": item.get("json_body"),
            "expect_status": item.get("expect_status"),
            "expect_json_array_min_items": item.get("expect_json_array_min_items"),
            "proves_input_ids": [input_ref(value) for value in _raw_list(item.get("proves_input_ids"))],
        }
        for item in _dicts(output.get("runtime_probes"))
    ]
    stand_ins = [
        {
            "stand_in_id": _text(item.get("stand_in_id")),
            "for_input_id": input_ref(item.get("for_input_id")),
            "decision_ref": _text(item.get("decision_ref")),
            "module": _text(item.get("module")),
            "start_export": _text(item.get("start_export")),
            "env_key": _text(item.get("env_key")),
        }
        for item in _dicts(output.get("verification_stand_ins"))
    ]
    demo_decisions = [
        {
            "id": _text(item.get("id")),
            "input_ids": [input_ref(value) for value in _raw_list(item.get("input_ids"))],
            "statement": _text(item.get("statement")),
            "scope": _text(item.get("scope")),
        }
        for item in _dicts(output.get("demo_decisions"))
    ]

    contract: dict[str, object] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "run_id": _text(context.get("run_id")),
        "bound_inputs": dict(_dict(context.get("bound_inputs"))),
        "entrypoints": list(_texts(context.get("entrypoints"))),
        "runtime_inputs": runtime_inputs,
        "verification_stand_ins": stand_ins,
        "demo_decisions": demo_decisions,
        "closure_units": closure_units,
        "runtime_config_changes": runtime_config_changes,
        "runtime_probes": runtime_probes,
        "new_write_targets": [],
        "unresolved": [],
    }
    contract["new_write_targets"] = expected_new_write_targets(contract, context=context)
    scenarios_by_input = _input_scenarios(contract, context=context)
    contract["unresolved"] = [
        {
            "input_id": input_ref(item.get("input_id")),
            "reason": _text(item.get("reason")),
            "blocks_scenarios": scenarios_by_input.get(input_ref(item.get("input_id")), []),
        }
        for item in _dicts(output.get("unresolved"))
    ]
    return contract, issues


STAND_IN_START_SIGNATURE = "(port: number) => Promise<{ close(): Promise<void> }>"


def closure_path_policy_view(policy: PlanningPathPolicy) -> dict[str, object]:
    """Path facts the Planner and Validator see; the same facts G5/G9 enforce."""

    roots = {component: list(component_roots) for component, component_roots in policy.component_roots}
    browser_roots = roots.get(BROWSER_TEST_COMPONENT_ID, [])
    return {
        "component_editable_roots": roots,
        "protected_paths": list(policy.protected_paths),
        "browser_test_component_id": BROWSER_TEST_COMPONENT_ID,
        "browser_test_root": browser_roots[0] if browser_roots else "",
        "harness_owned_paths": [RUNTIME_PROBE_SPEC_PATH],
        "test_suffixes": list(policy.language_test_suffixes),
        "stand_in_module_rule": (
            "A verification stand-in module is a non-test `.ts` file under the browser test root, written by a "
            f"`{BROWSER_TEST_COMPONENT_ID}` closure unit, exporting `start_export` with signature "
            f"{STAND_IN_START_SIGNATURE}."
        ),
    }


def planner_input_view(context: dict[str, object]) -> list[dict[str, object]]:
    """Inputs as the Planner sees them: Harness fields plus the scenarios and paths they touch."""

    return [
        {
            **_public_input(item),
            "scenario_ids": list(_texts(item.get("scenario_ids"))),
            **({"composition_paths": item["composition_paths"]} if item.get("composition_paths") else {}),
            **({"design_decision_refs": item["design_decision_refs"]} if item.get("design_decision_refs") else {}),
            **({"env": item["env"]} if item.get("env") else {}),
        }
        for item in _dicts(context.get("runtime_inputs"))
    ]


CLOSURE_WORK_PACKET_MODE = "runtime_closure"
CLOSURE_UNIT_RULES = (
    "Requirement, Design and Plan stay the authority: this unit only provides approved runtime inputs, wiring and "
    "verification support. Never change approved business behavior.",
    "Files in frozen_baseline_paths are approved earlier output: read them, never edit them; an edit is drift.",
    "Implement every provider in `resolves` at its exact `<path>#<symbol>`; a stand-in exports `start_export` with "
    "the exact `start_signature`.",
    "Approved demo decisions apply only to demo and verification; production defaults stay as approved.",
    "When the approved change needs a file that does not exist yet, you may create it under new_file_roots; "
    "Harness records it as a scope extension for the owner. Never edit an existing file outside your approved paths.",
)


def closure_new_file_policies(policy: PlanningPathPolicy) -> dict[str, dict[str, object]]:
    """D11 (b) per component: where a closure unit may create a not-yet-existing file."""

    policies = {}
    for component, roots in policy.component_roots:
        suffixes = {*policy.language_source_suffixes, *policy.language_test_suffixes}
        suffixes.update(suffix for root in roots for suffix in STYLESHEET_ROOTS.get(root, ()))
        policies[component] = {
            "roots": list(roots),
            "suffixes": sorted(suffixes),
            "protected_paths": list(policy.protected_paths),
            "reserved_paths": [RUNTIME_PROBE_SPEC_PATH],
        }
    return policies
# Packet-level fields copied verbatim from the frozen Developer packet.
_FROZEN_PACKET_FIELDS = (
    "run_id",
    "source_ref",
    "issue_key",
    "target_repository",
    "run_workspace",
    "approved_plan",
    "effective_design_contract",
    "requirement_contract",
    "canonical_requirements",
    "approved_runtime_decisions",
)


def _dependency_order(units: list[dict[str, object]]) -> list[dict[str, object]]:
    """Stable topological order: contract order, except a unit waits for the units it depends on.

    Units left in a cycle (G12 rejects that earlier) keep their contract order at the end.
    """

    ids = {_text(unit.get("unit_id")) for unit in units}
    placed: set[str] = set()
    ordered: list[dict[str, object]] = []
    remaining = list(units)
    while remaining:
        ready = next(
            (
                unit
                for unit in remaining
                if all(dep in placed or dep not in ids for dep in _texts(unit.get("depends_on")))
            ),
            None,
        )
        if ready is None:
            return ordered + remaining
        ordered.append(ready)
        placed.add(_text(ready.get("unit_id")))
        remaining.remove(ready)
    return ordered


def build_closure_work_packet(
    *,
    contract: dict[str, object],
    context: dict[str, object],
    frozen_packet: dict[str, object],
    version: int,
    contract_ref: str,
    new_file_policies: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Project an owner-approved closure contract into a Developer packet (frozen packet untouched)."""

    inputs = {_text(item.get("input_id")): item for item in _dicts(contract.get("runtime_inputs"))}
    plan_env = {
        (_text(change.get("name")), _text(change.get("read_by"))): change
        for change in _dicts(context.get("plan_env_changes"))
    }
    closure_changes = _dicts(contract.get("runtime_config_changes"))
    closure_env = {
        (_text(change.get("name")), _text(change.get("read_by"))): change
        for change in closure_changes
        if _text(change.get("kind")) == "env_var"
    }
    # The executor runs units in packet order and needs every dependency done first (G12 ensures no cycle).
    units = _dependency_order(_dicts(contract.get("closure_units")))
    allowed = list(
        dict.fromkeys(
            path
            for unit in units
            for path in [*_texts(unit.get("write_paths")), *_texts(unit.get("test_target_paths"))]
        )
    )
    plan_paths = list(_texts(context.get("plan_allowed_paths")))
    execution_units = []
    for order, unit in enumerate(units, start=1):
        unit_id = _text(unit.get("unit_id"))
        write_paths = list(_texts(unit.get("write_paths")))
        unit_inputs = [inputs[input_id] for input_id in _texts(unit.get("resolves_input_ids")) if input_id in inputs]
        unit_input_ids = {_text(item.get("input_id")) for item in unit_inputs}
        providers = [_dict(_dict(item.get("resolution")).get("provider")) for item in unit_inputs]
        env_providers = []
        for provider in providers:
            if _text(provider.get("kind")) != "env_var":
                continue
            key = (_text(provider.get("env_key")), _text(provider.get("read_by")))
            change = (plan_env if _text(provider.get("change_ref")) == "plan" else closure_env).get(key, {})
            env_providers.append({"env_key": key[0], "read_by": key[1], "default": _text(_dict(change).get("default"))})
        closure_context = {
            "rules": list(CLOSURE_UNIT_RULES),
            "resolves": [
                {
                    "input_id": _text(item.get("input_id")),
                    "name": _text(item.get("name")),
                    "consumer": _text(item.get("consumer")),
                    "provided_by": _text(item.get("provided_by")),
                    "resolution_kind": _text(_dict(item.get("resolution")).get("kind")),
                    "provider": _dict(item.get("resolution")).get("provider"),
                    "reason": _text(_dict(item.get("resolution")).get("reason")),
                }
                for item in unit_inputs
            ],
            # Every unit sees all owner-approved demo values and probes: a supporting unit (for example
            # one preparing demo data) resolves no input but must use the same approved values.
            "approved_decisions": [
                {key: decision.get(key) for key in ("id", "input_ids", "statement", "scope")}
                for decision in _dicts(contract.get("demo_decisions"))
            ],
            "probes": [
                {key: probe.get(key) for key in (
                    "probe_id", "scenario_ids", "service", "method", "path", "headers_from_input_ids",
                    "headers", "json_body", "expect_status", "expect_json_array_min_items",
                )}
                for probe in _dicts(contract.get("runtime_probes"))
            ],
            "stand_ins": [
                {
                    **{key: stand_in.get(key) for key in ("stand_in_id", "module", "start_export", "env_key")},
                    "start_signature": STAND_IN_START_SIGNATURE,
                }
                for stand_in in _dicts(contract.get("verification_stand_ins"))
                if _text(stand_in.get("for_input_id")) in unit_input_ids
            ],
            "env_providers": env_providers,
            "frozen_baseline_paths": [path for path in plan_paths if path not in write_paths],
            "new_file_roots": list(
                _texts(_dict((new_file_policies or {}).get(_text(unit.get("component_id")))).get("roots"))
            ),
        }
        provider_targets = [
            {"path": ref.partition("#")[0], "symbol": ref.partition("#")[2]}
            for provider in providers
            if _text(provider.get("kind")) == "symbol"
            for ref in [_text(provider.get("ref"))]
            if ref.partition("#")[0] in write_paths
        ] + [
            {"path": _text(item.get("module")), "symbol": _text(item.get("start_export"))}
            for item in closure_context["stand_ins"]
            if _text(item.get("module")) in write_paths
        ]
        unit_payload = {
            "unit_id": unit_id,
            "order": order,
            "scenario_id": None,
            "component_id": _text(unit.get("component_id")),
            "surfaces": [],
            "title": f"Runtime closure: {_text(unit.get('key'))}",
            "objective": _text(unit.get("objective")),
            "depends_on": list(_texts(unit.get("depends_on"))),
            "requirement_refs": [],
            "acceptance_refs": [],
            "design_node_ids": [],
            "design_edge_ids": [],
            "planned_code_targets": provider_targets,
            "migration_impact": {},
            "runtime_bindings": [],
            "boundary_contracts": [],
            "surface_contracts": [],
            "ui_obligations": [],
            "write_paths": write_paths,
            "test_target_paths": list(_texts(unit.get("test_target_paths"))),
            "required_stage_ids": list(_texts(unit.get("required_stage_ids"))),
            "signature_entry_ids": [],
            "test_intents": [
                {**intent, "stage_id": "unit"} for intent in _dicts(unit.get("test_intents"))
            ],
            "qa_test_intents": [],
            "repo_evidence_refs": [],
            "package_dependencies": [],
            "runtime_config_changes": [
                change for change in closure_changes if _text(change.get("unit_id")) == unit_id
            ],
            "context_paths": list(dict.fromkeys([*write_paths, *_texts(unit.get("context_paths"))])),
            "contract_anchor_paths": [],
            "closure_context": closure_context,
            **(
                {"new_file_policy": dict((new_file_policies or {})[_text(unit.get("component_id"))])}
                if _text(unit.get("component_id")) in (new_file_policies or {})
                else {}
            ),
        }
        unit_payload["input_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(unit_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        execution_units.append(unit_payload)

    plan_impact = _dict(frozen_packet.get("runtime_config_impact"))
    all_changes = [*_dicts(plan_impact.get("changes")), *closure_changes]
    frozen_output = _dict(frozen_packet.get("output_contract"))
    signature_contract = _scoped_signature_contract(frozen_packet, allowed)
    return {
        **{field: frozen_packet.get(field) for field in _FROZEN_PACKET_FIELDS if field in frozen_packet},
        **({"implementation_signature_contract": signature_contract} if signature_contract else {}),
        "version": f"runtime_closure_work_packet_v{version}",
        "schema_version": frozen_packet.get("schema_version"),
        "mode": CLOSURE_WORK_PACKET_MODE,
        "runtime_closure_contract": contract_ref,
        "runtime_config_impact": {
            "status": "required" if all_changes else "not_required",
            "reason": "Approved Plan changes plus approved runtime closure changes.",
            "changes": all_changes,
        },
        "execution_units": execution_units,
        "implementation_steps": [],
        "proposed_files": [
            {"path": path, "operation": "create_or_update", "purpose": "Approved runtime closure write path."}
            for path in allowed
        ],
        "requirement_coverage": [],
        "requirement_coverage_by_file": {},
        "design_mappings_by_file": {},
        "package_dependency_requirements": [],
        "verification_stage_ids": list(CLOSURE_DEVELOPER_STAGE_IDS),
        # The runner requires a test-intent block; typecheck always proves the closure compiles.
        "test_intents": [
            {"stage_id": "typecheck", "intent": "Closure changes type-check against the frozen workspace."},
            *(intent for unit in execution_units for intent in _dicts(unit.get("test_intents"))),
        ],
        "qa_test_intents": [],
        "output_contract": {**frozen_output, "allowed_paths": allowed},
    }


def _scoped_signature_contract(frozen_packet: dict[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """Approved signatures still bind the Plan files a closure unit edits; the rest are out of scope."""

    frozen = _dict(frozen_packet.get("implementation_signature_contract"))
    if not frozen:
        return {}
    scope = set(allowed)
    contract = {
        **frozen,
        "entries": [
            entry
            for entry in _dicts(frozen.get("entries"))
            if _text(entry.get("source_path")).replace("\\", "/") in scope
        ],
    }
    contract["digest"] = implementation_signature_contract_digest(contract)
    return contract


def extend_closure_work_packet(
    *,
    packet: dict[str, object],
    frozen_packet: dict[str, object],
    extensions: list[dict[str, object]],
    version: int,
) -> dict[str, object]:
    """D11 (a): add the owning Plan unit, scoped to exactly the cited Plan paths, to a closure packet."""

    units = [dict(unit) for unit in _dicts(packet.get("execution_units"))]
    by_id = {_text(unit.get("unit_id")): unit for unit in units}
    plan_units = {_text(unit.get("unit_id")): unit for unit in _dicts(frozen_packet.get("execution_units"))}
    added_paths: list[str] = []
    for extension in extensions:
        unit_id = _text(extension.get("plan_unit_id"))
        plan_unit = plan_units.get(unit_id)
        if plan_unit is None:
            continue
        cited = [path for path in _texts(extension.get("paths")) if path in _texts(plan_unit.get("write_paths"))]
        added_paths.extend(cited)
        if unit_id in by_id:
            existing = by_id[unit_id]
            for field in ("write_paths", "context_paths"):
                existing[field] = list(dict.fromkeys([*_texts(existing.get(field)), *cited]))
            continue
        unit = {
            **plan_unit,
            "write_paths": cited,
            "test_target_paths": [path for path in _texts(plan_unit.get("test_target_paths")) if path in cited],
            "context_paths": cited,
            "contract_anchor_paths": [
                path for path in _texts(plan_unit.get("contract_anchor_paths")) if path in cited
            ],
            "depends_on": [dependency for dependency in _texts(plan_unit.get("depends_on")) if dependency in by_id],
            "runtime_config_changes": [],
            "package_dependencies": [],
        }
        units.append(unit)
        by_id[unit_id] = unit
    allowed = list(dict.fromkeys([*_texts(_dict(packet.get("output_contract")).get("allowed_paths")), *added_paths]))
    frozen_mappings = _dict(frozen_packet.get("design_mappings_by_file"))
    frozen_coverage = _dict(frozen_packet.get("requirement_coverage_by_file"))
    signatures = _scoped_signature_contract(frozen_packet, allowed)
    return {
        **packet,
        "version": f"runtime_closure_work_packet_v{version}",
        "execution_units": units,
        "output_contract": {**_dict(packet.get("output_contract")), "allowed_paths": allowed},
        "proposed_files": [
            *_dicts(packet.get("proposed_files")),
            *[
                {"path": path, "operation": "update", "purpose": "D11 (a): repair a frozen-Plan file cited by a closure diagnostic."}
                for path in added_paths
                if path not in {_text(item.get("path")) for item in _dicts(packet.get("proposed_files"))}
            ],
        ],
        "design_mappings_by_file": {
            **_dict(packet.get("design_mappings_by_file")),
            **{path: frozen_mappings[path] for path in added_paths if path in frozen_mappings},
        },
        "requirement_coverage_by_file": {
            **_dict(packet.get("requirement_coverage_by_file")),
            **{path: frozen_coverage[path] for path in added_paths if path in frozen_coverage},
        },
        **({"implementation_signature_contract": signatures} if signatures else {}),
    }


def route_closure_problems(
    *,
    problems: list[dict[str, object]],
    contract: dict[str, object],
    packet: dict[str, object],
    frozen_packet: dict[str, object],
    planning_policy: PlanningPathPolicy,
    target_path,
) -> dict[str, object]:
    """Pick repair units mechanically, stopping at the first rule that matches (plan section 8).

    1 inputs -> closure units resolving them; 2 cited paths -> unit writing them (closure first,
    then the owning frozen-Plan unit = D11 a); 3 cited unit ids; 4 probe service -> component;
    5 named webServer component, else every closure unit.
    """

    contract_units = _dicts(contract.get("closure_units"))
    closure_ids = {_text(unit.get("unit_id")) for unit in contract_units}
    packet_units = _dicts(packet.get("execution_units"))
    packet_ids = {_text(unit.get("unit_id")) for unit in packet_units}
    path_owner = {path: _text(unit.get("unit_id")) for unit in packet_units for path in _texts(unit.get("write_paths"))}
    plan_units = {_text(unit.get("unit_id")): unit for unit in _dicts(frozen_packet.get("execution_units"))}
    probes = {_text(item.get("probe_id")): item for item in _dicts(contract.get("runtime_probes"))}
    protected = tuple(planning_policy.protected_paths)
    findings: list[dict[str, object]] = []
    extensions: dict[str, set[str]] = {}
    conflicts: list[str] = []

    def is_protected(path: str) -> bool:
        return any(path == item or path.startswith(f"{item}/") for item in protected)

    for index, problem in enumerate(problems, start=1):
        input_ids = set(_texts(problem.get("input_ids")))
        paths = list(dict.fromkeys(_texts(problem.get("paths"))))
        probe = _dict(problem.get("probe")) or next(
            (probes[probe_id] for probe_id in _texts(problem.get("probe_ids")) if probe_id in probes), {}
        )
        for path in paths:
            if is_protected(path):
                conflicts.append(f"`{path}` is protected; only Harness writes it")
        unit_ids: list[str] = []
        rule = 0
        if input_ids:
            unit_ids = sorted(
                _text(unit.get("unit_id"))
                for unit in contract_units
                if input_ids & set(_texts(unit.get("resolves_input_ids")))
            )
            rule = 1 if unit_ids else 0
        if not unit_ids and paths:
            owned = sorted({path_owner[path] for path in paths if path in path_owner})
            if owned:
                unit_ids, rule = owned, 2
            else:
                cited_ids = set(_texts(problem.get("unit_ids")))
                for path in paths:
                    writers = [uid for uid, unit in plan_units.items() if path in _texts(unit.get("write_paths"))]
                    # A path shared by several Plan units goes to the unit the finding cites.
                    owner = next((uid for uid in writers if uid in cited_ids), writers[0] if writers else "")
                    if owner:
                        extensions.setdefault(owner, set()).add(path)
                        if owner not in unit_ids:
                            unit_ids.append(owner)
                        rule = 2
                    elif not is_protected(path) and (target_path / path).is_file():
                        conflicts.append(f"`{path}` exists outside the frozen Plan and the closure contract")
        if not unit_ids:
            cited = _texts(problem.get("unit_ids"))
            closure_cited = [uid for uid in cited if uid in closure_ids or uid in packet_ids]
            plan_cited = [uid for uid in cited if uid in plan_units and uid not in packet_ids]
            if closure_cited:
                unit_ids, rule = sorted(set(closure_cited)), 3
            elif plan_cited:
                for uid in plan_cited:
                    cited_paths = [path for path in paths if path in _texts(plan_units[uid].get("write_paths"))]
                    if cited_paths:
                        extensions.setdefault(uid, set()).update(cited_paths)
                        unit_ids.append(uid)
                    else:
                        conflicts.append(f"Plan unit `{uid}` is cited without an exact path it owns")
                rule = 3
        if not unit_ids and probe:
            unit_ids = sorted(
                _text(unit.get("unit_id"))
                for unit in contract_units
                if _text(unit.get("component_id")) == _text(probe.get("service"))
            )
            rule = 4 if unit_ids else rule
        if not unit_ids:
            names = set(_texts(problem.get("webserver_names")))
            unit_ids = sorted(
                _text(unit.get("unit_id")) for unit in contract_units if _text(unit.get("component_id")) in names
            ) or sorted(closure_ids)
            rule = 5
        findings.append(
            {
                "finding_id": f"CLF-{index}",
                "source": _text(problem.get("source")),
                "unit_ids": sorted(set(unit_ids)),
                "routing_rule": rule,
                "issue": _text(problem.get("issue")),
                "expected_change": _text(problem.get("expected_change")),
                **({"input_ids": sorted(input_ids)} if input_ids else {}),
                **({"probe_id": _text(probe.get("probe_id"))} if probe else {}),
                **({"paths": paths} if paths else {}),
            }
        )
    return {
        "findings": findings,
        "plan_extensions": [
            {"plan_unit_id": unit_id, "paths": sorted(paths)} for unit_id, paths in sorted(extensions.items())
        ],
        "conflicts": sorted(set(conflicts)),
    }


def closure_repair_feedback(
    *,
    run_id: str,
    version: int,
    repair_iteration: int,
    source: str,
    summary: str,
    findings: list[dict[str, object]],
    plan_extensions: list[dict[str, object]],
    allowed_paths: list[str],
    extra: dict[str, object],
) -> dict[str, object]:
    """Repair feedback in the executor's shape: findings carry exact unit_ids for unit-scoped repair."""

    objective_id = "repair:" + hashlib.sha256(
        json.dumps({"source": source, "findings": findings}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    extension_paths = [path for item in plan_extensions for path in _texts(item.get("paths"))]
    return {
        "version": f"runtime_closure_repair_feedback_v{version}",
        "run_id": run_id,
        "status": "runtime_closure_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "feedback_source": source,
        "severity": "repair_required",
        "summary": summary,
        "required_action": (
            "Repair every finding assigned to this unit from the current worktree with the smallest coherent change. "
            "The approved runtime closure contract, Requirement, Design and Plan are the authority; never change "
            "approved business behavior. Harness then reruns G14, closure verification from a clean database, the "
            "Code Validator regression and the closure code review."
        ),
        "repair_objective": {
            "objective_id": objective_id,
            "source": source,
            "status": "open",
            "canonical_keys": [],
            "contract_findings": findings,
            "must_preserve": "Keep every approved closure resolution, probe and Plan behavior that already holds.",
        },
        "plan_extensions": plan_extensions,
        "missing_links": list(extra.get("missing_links") or []) if source == "validator_code_trace" else [],
        "developer_constraints": {"allowed_paths": list(dict.fromkeys([*allowed_paths, *extension_paths]))},
        **{key: value for key, value in extra.items() if key != "missing_links"},
    }


def approved_closure_changes(
    *,
    contract: dict[str, object],
    packet: dict[str, object],
    pin: dict[str, object],
) -> dict[str, object]:
    """Context for Code Validator tasks whose sources the closure changed (never a gate)."""

    changed = {
        *_texts(_dict(packet.get("output_contract")).get("allowed_paths")),
        *(_text(item.get("path")) for item in _dicts(pin.get("scope_extensions"))),
    }
    return {
        "note": (
            "The owner approved these runtime closure changes after the Design froze. A demo or verification "
            "provider at these symbols is an approved change, not a contradiction of an external_use_case input."
        ),
        "demo_decisions": [
            {key: item.get(key) for key in ("id", "statement", "scope", "input_ids")}
            for item in _dicts(contract.get("demo_decisions"))
        ],
        "resolutions": [
            {
                "input_id": _text(item.get("input_id")),
                "name": _text(item.get("name")),
                "kind": _text(_dict(item.get("resolution")).get("kind")),
                "provider": _dict(item.get("resolution")).get("provider"),
            }
            for item in _dicts(contract.get("runtime_inputs"))
        ],
        "new_write_targets": list(_texts(contract.get("new_write_targets"))),
        "changed_paths": sorted(path for path in changed if path),
    }


def runtime_probe_run(contract: dict[str, object]) -> RuntimeProbeRun:
    """The IDs the verification runner needs; argv and env values stay Harness-owned."""

    return RuntimeProbeRun(
        probe_ids=tuple(_text(item.get("probe_id")) for item in _dicts(contract.get("runtime_probes"))),
        stand_in_env_keys=tuple(
            _text(item.get("env_key")) for item in _dicts(contract.get("verification_stand_ins"))
        ),
    )


def runtime_probe_failures(
    contract: dict[str, object],
    probe_results: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Failed probes joined with their approved definition, ready for closure repair feedback."""

    probes = {_text(item.get("probe_id")): item for item in _dicts(contract.get("runtime_probes"))}
    failures = []
    for outcome in probe_results:
        if not isinstance(outcome, dict) or outcome.get("status") == "passed":
            continue
        probe_id = _text(outcome.get("probe_id"))
        probe = probes.get(probe_id, {})
        observation = _dict(outcome.get("observation"))
        failures.append(
            {
                "probe_id": probe_id,
                "scenario_ids": list(_texts(probe.get("scenario_ids"))),
                "service": _text(probe.get("service")),
                "method": _text(probe.get("method")),
                "path": _text(probe.get("path")),
                "expected_status": probe.get("expect_status"),
                "actual_status": observation.get("actual_status"),
                "expected_min_items": probe.get("expect_json_array_min_items"),
                "actual_items": observation.get("actual_items"),
                "response_body_excerpt": str(observation.get("response_body_excerpt") or "")[
                    :MAX_PROBE_BODY_EXCERPT_CHARS
                ],
                "proves_input_ids": list(_texts(probe.get("proves_input_ids"))),
            }
        )
    return failures


def render_runtime_probe_spec(contract: dict[str, object]) -> str:
    """Deterministic request-only Playwright spec for the approved probes and stand-ins."""

    spec_dir = PurePosixPath(RUNTIME_PROBE_SPEC_PATH).parent
    config_import = "../" * len(spec_dir.parts) + "playwright.config"
    stand_ins = _dicts(contract.get("verification_stand_ins"))
    probes = _dicts(contract.get("runtime_probes"))
    lines = [
        "// Generated by SDLC Harness from the approved runtime closure contract. Do not edit by hand.",
        'import { expect, test, type APIRequestContext } from "@playwright/test";',
        f'import config from "{config_import}";',
    ]
    for index, stand_in in enumerate(stand_ins, start=1):
        module = PurePosixPath(_text(stand_in.get("module")))
        relative = posixpath.relpath(str(module.with_suffix("")), str(spec_dir))
        relative = relative if relative.startswith(".") else f"./{relative}"
        lines.append(f'import {{ {_text(stand_in.get("start_export"))} as standIn{index} }} from "{relative}";')
    lines += [
        "",
        "type ProbeServer = { name?: string; url?: string };",
        "type ProbeHandle = { close(): Promise<void> };",
        "type RuntimeProbe = {",
        "  probe_id: string;",
        "  service: string;",
        "  method: string;",
        "  path: string;",
        "  headers: Record<string, string>;",
        "  json_body: Record<string, unknown> | null;",
        "  expect_status: number;",
        "  expect_json_array_min_items: number | null;",
        "};",
        "",
        "const webServers = ([] as ProbeServer[]).concat((config.webServer ?? []) as ProbeServer | ProbeServer[]);",
        "const standIns: ProbeHandle[] = [];",
        "",
        "function serviceOrigin(name: string): string {",
        "  const server = webServers.find((item) => item.name === name);",
        "  if (!server || !server.url) {",
        "    throw new Error(`playwright.config.ts declares no webServer named ${name}`);",
        "  }",
        "  return new URL(server.url).origin;",
        "}",
        "",
        "function standInPort(envKey: string): number {",
        "  const value = process.env[envKey];",
        "  if (!value) {",
        "    throw new Error(`Runtime closure stand-in env ${envKey} is not set`);",
        "  }",
        "  return Number(new URL(value).port);",
        "}",
        "",
        "async function runProbe(request: APIRequestContext, probe: RuntimeProbe): Promise<void> {",
        "  const response = await request.fetch(`${serviceOrigin(probe.service)}${probe.path}`, {",
        "    method: probe.method,",
        "    headers: probe.headers,",
        "    // Playwright serializes an object `data` as JSON and sets content-type application/json.",
        "    data: probe.json_body ?? undefined,",
        "    failOnStatusCode: false,",
        "  });",
        "  const body = await response.text();",
        "  let items: number | null = null;",
        "  try {",
        "    const parsed: unknown = JSON.parse(body);",
        "    items = Array.isArray(parsed) ? parsed.length : null;",
        "  } catch {",
        "    items = null;",
        "  }",
        "  test.info().annotations.push({",
        f'    type: "{RUNTIME_PROBE_ANNOTATION_TYPE}",',
        "    description: JSON.stringify({",
        "      probe_id: probe.probe_id,",
        "      actual_status: response.status(),",
        "      actual_items: items,",
        f"      response_body_excerpt: body.slice(0, {MAX_PROBE_BODY_EXCERPT_CHARS}),",
        "    }),",
        "  });",
        "  expect(response.status()).toBe(probe.expect_status);",
        "  if (probe.expect_json_array_min_items !== null) {",
        "    expect(items ?? -1).toBeGreaterThanOrEqual(probe.expect_json_array_min_items);",
        "  }",
        "}",
    ]
    if stand_ins:
        lines += ["", "test.beforeAll(async () => {"]
        lines += [
            f'  standIns.push(await standIn{index}(standInPort({json.dumps(_text(stand_in.get("env_key")))})));'
            for index, stand_in in enumerate(stand_ins, start=1)
        ]
        lines += [
            "});",
            "",
            "test.afterAll(async () => {",
            "  for (const standIn of standIns.reverse()) {",
            "    await standIn.close();",
            "  }",
            "});",
        ]
    for probe in probes:
        literal = json.dumps(
            {
                "probe_id": _text(probe.get("probe_id")),
                "service": _text(probe.get("service")),
                "method": _text(probe.get("method")),
                "path": _text(probe.get("path")),
                "headers": {str(key): str(value) for key, value in sorted(_dict(probe.get("headers")).items())},
                "json_body": probe.get("json_body") if isinstance(probe.get("json_body"), dict) else None,
                "expect_status": probe.get("expect_status"),
                "expect_json_array_min_items": probe.get("expect_json_array_min_items"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        lines += [
            "",
            f"test({json.dumps(_text(probe.get('probe_id')))}, async ({{ request }}) => {{",
            f"  await runProbe(request, {literal});",
            "});",
        ]
    return "\n".join(lines) + "\n"


def expected_new_write_targets(contract: dict[str, object], *, context: dict[str, object]) -> list[str]:
    """Paths closure units write that the frozen Plan never allowed (owner approves them)."""

    allowed = set(_texts(context.get("plan_allowed_paths")))
    return sorted(
        {
            path
            for unit in _dicts(contract.get("closure_units"))
            for path in [*_texts(unit.get("write_paths")), *_texts(unit.get("test_target_paths"))]
            if path and path not in allowed
        }
    )


# --------------------------------------------------------------------------- rule review


def review_closure_planner_output(
    *,
    context: dict[str, object],
    planner_output: dict[str, object],
    planning_policy: PlanningPathPolicy,
    repo_runtime_config: dict[str, object],
    symbol_exists: SymbolExists,
    references: SymbolReferences | None = None,
    runtime_config_source_issues: Iterable[dict[str, object]] = (),
) -> dict[str, object]:
    """Assemble one Planner output and run the mechanical rule review on the result."""

    contract, assembly_issues = assemble_closure_contract(context=context, planner_output=planner_output)
    review = closure_contract_rule_issues(
        contract,
        context=context,
        planning_policy=planning_policy,
        repo_runtime_config=repo_runtime_config,
        symbol_exists=symbol_exists,
        references=references,
        runtime_config_source_issues=runtime_config_source_issues,
    )
    issues = _dedupe_issues([*assembly_issues, *review["issues"]])
    return {**review, "ok": not issues, "issues": issues, "contract": contract}


def closure_contract_rule_issues(
    contract: dict[str, object],
    *,
    context: dict[str, object],
    planning_policy: PlanningPathPolicy,
    repo_runtime_config: dict[str, object],
    symbol_exists: SymbolExists,
    references: SymbolReferences | None = None,
    runtime_config_source_issues: Iterable[dict[str, object]] = (),
) -> dict[str, object]:
    """Gates G1-G13 and G15 on an assembled contract. Verdicts never depend on prose."""

    checker = _ContractChecker(
        contract=contract if isinstance(contract, dict) else {},
        context=context,
        planning_policy=planning_policy,
        repo_runtime_config=repo_runtime_config,
        symbol_exists=symbol_exists,
        references=references,
        source_issues=list(runtime_config_source_issues),
    )
    issues = _dedupe_issues(checker.run())
    return {
        "ok": not issues,
        "issues": issues,
        "config_unsupported": any(item["error_code"] == CONFIG_UNSUPPORTED for item in issues),
        "reachability_unverified": checker.reachability_unverified,
    }


def closure_runnable_status(contract: dict[str, object]) -> dict[str, object]:
    """Aggregate runnable status: bound, fixture_bound or externally_blocked (+ scenarios)."""

    unresolved = _dicts(contract.get("unresolved"))
    blocked = sorted({scenario for item in unresolved for scenario in _texts(item.get("blocks_scenarios"))})
    kinds = {
        _text(_dict(item.get("resolution")).get("kind")) for item in _dicts(contract.get("runtime_inputs"))
    }
    if unresolved or "unresolved_external" in kinds:
        status = "externally_blocked"
    elif "approved_demo_fixture" in kinds:
        status = "fixture_bound"
    else:
        status = "bound"
    return {"runnable_status": status, "blocked_scenario_ids": blocked}


class _ContractChecker:
    def __init__(
        self,
        *,
        contract: dict[str, object],
        context: dict[str, object],
        planning_policy: PlanningPathPolicy,
        repo_runtime_config: dict[str, object],
        symbol_exists: SymbolExists,
        references: SymbolReferences | None,
        source_issues: list[dict[str, object]],
    ):
        self.contract = contract
        self.context = context
        self.policy = planning_policy
        self.repo_runtime_config = repo_runtime_config
        self.symbol_exists = symbol_exists
        self.references = references
        self.source_issues = source_issues
        self.issues: list[dict[str, object]] = []
        self.reachability_unverified: list[dict[str, object]] = []
        self.inputs = _dicts(contract.get("runtime_inputs"))
        self.inputs_by_id = {_text(item.get("input_id")): item for item in self.inputs}
        self.context_inputs = {_text(item.get("input_id")): item for item in _dicts(context.get("runtime_inputs"))}
        self.units = _dicts(contract.get("closure_units"))
        self.units_by_id = {_text(item.get("unit_id")): item for item in self.units}
        self.probes = _dicts(contract.get("runtime_probes"))
        self.stand_ins = _dicts(contract.get("verification_stand_ins"))
        self.decisions = _dicts(contract.get("demo_decisions"))
        self.decisions_by_id = {_text(item.get("id")): item for item in self.decisions}
        self.unresolved = _dicts(contract.get("unresolved"))
        self.changes = _dicts(contract.get("runtime_config_changes"))
        self.component_roots = {component: tuple(roots) for component, roots in planning_policy.component_roots}
        self.stand_in_modules = {_text(item.get("module")) for item in self.stand_ins}

    def add(self, gate: str, message: str, *refs: str, code: str = CONTRACT_INVALID) -> None:
        self.issues.append(_issue(gate, message, *refs, code=code))

    def run(self) -> list[dict[str, object]]:
        self.g1_schema()
        self.g2_input_set()
        self.g3_compatibility()
        self.g4_existing_providers()
        self.g5_new_provider_ownership()
        self.g6_decisions()
        self.g7_unresolved()
        self.g8_composition()
        self.g9_paths()
        self.g10_stages_and_services()
        self.g11_coverage()
        self.g12_dependencies()
        self.g13_runtime_config()
        self.g15_scenarios()
        return self.issues

    # G1 -- shape, enums and stable unique IDs.
    def g1_schema(self) -> None:
        contract, context = self.contract, self.context
        if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
            self.add("G1", f"schema_version must be `{CONTRACT_SCHEMA_VERSION}`.")
        for field in ("run_id", "bound_inputs", "entrypoints"):
            if contract.get(field) != context.get(field):
                self.add("G1", f"Harness field `{field}` does not match the closure context.")
        for field in (
            "runtime_inputs",
            "verification_stand_ins",
            "demo_decisions",
            "closure_units",
            "runtime_config_changes",
            "runtime_probes",
            "new_write_targets",
            "unresolved",
        ):
            if not isinstance(contract.get(field), list):
                self.add("G1", f"Field `{field}` must be a list.")
        self._unique("G1", "runtime_inputs.input_id", [_text(item.get("input_id")) for item in self.inputs])
        for item in self.inputs:
            input_id = _text(item.get("input_id"))
            if not _INPUT_ID.fullmatch(input_id):
                self.add("G1", f"Input id `{input_id}` must match {_INPUT_ID.pattern}.", input_id)
            if _text(item.get("origin")) not in ORIGINS:
                self.add("G1", f"Input `{input_id}` has unknown origin `{item.get('origin')}`.", input_id)
            if item.get("design_source") is not None and item.get("design_source") not in DESIGN_SOURCES:
                self.add("G1", f"Input `{input_id}` has unknown design_source `{item.get('design_source')}`.", input_id)
            if not _text(item.get("name")) or not _text(item.get("consumer")):
                self.add("G1", f"Input `{input_id}` needs a name and a consumer.", input_id)
            if _text(item.get("origin")) == "closure_declared":
                for field in ("consumer", "provided_by"):
                    if not self._symbol_ref(item.get(field)):
                        self.add("G1", f"Closure-declared input `{input_id}` {field} must be `<path>#<symbol>`.", input_id)
            if isinstance(item.get("resolution"), dict):
                self._resolution_shape(input_id, _dict(item.get("resolution")))

        unit_ids = [_text(item.get("unit_id")) for item in self.units]
        self._unique("G1", "closure_units.unit_id", unit_ids)
        components = set(self.component_roots)
        for unit in self.units:
            unit_id = _text(unit.get("unit_id"))
            if not _UNIT_ID.fullmatch(unit_id):
                self.add("G1", f"Closure unit id `{unit_id}` must match {_UNIT_ID.pattern}.", unit_id)
            elif unit_id != _digest_id("CU", _text(unit.get("component_id")), _text(unit.get("key"))):
                self.add("G1", f"Closure unit id `{unit_id}` was not assigned by Harness.", unit_id)
            if _text(unit.get("component_id")) not in components:
                self.add("G1", f"Closure unit `{unit_id}` has unknown component `{unit.get('component_id')}`.", unit_id)
            if not _text(unit.get("objective")):
                self.add("G1", f"Closure unit `{unit_id}` needs an objective.", unit_id)
            if not _texts(unit.get("write_paths")):
                self.add("G1", f"Closure unit `{unit_id}` needs at least one write path.", unit_id)
            for field in ("depends_on", "resolves_input_ids", "write_paths", "test_target_paths", "required_stage_ids", "context_paths", "test_intents"):
                if not isinstance(unit.get(field), list):
                    self.add("G1", f"Closure unit `{unit_id}` field `{field}` must be a list.", unit_id)
            for input_id in _texts(unit.get("resolves_input_ids")):
                self._known_input("G1", input_id, f"Closure unit `{unit_id}` resolves")
            for intent in _dicts(unit.get("test_intents")):
                if not _text(intent.get("intent")) or not _text(intent.get("test_path")):
                    self.add("G1", f"Closure unit `{unit_id}` has a test intent without intent or test_path.", unit_id)
                for input_id in _texts(intent.get("proves_input_ids")):
                    self._known_input("G1", input_id, f"Closure unit `{unit_id}` test intent proves")

        self._unique("G1", "runtime_probes.probe_id", [_text(item.get("probe_id")) for item in self.probes])
        for probe in self.probes:
            probe_id = _text(probe.get("probe_id"))
            if not _PROBE_ID.fullmatch(probe_id):
                self.add("G1", f"Probe id `{probe_id}` must match {_PROBE_ID.pattern}.", probe_id)
            if not _texts(probe.get("scenario_ids")):
                self.add("G1", f"Probe `{probe_id}` needs scenario_ids.", probe_id)
            if _text(probe.get("method")) not in PROBE_METHODS:
                self.add("G1", f"Probe `{probe_id}` method must be one of {list(PROBE_METHODS)}.", probe_id)
            if not _PROBE_PATH.fullmatch(_text(probe.get("path"))):
                self.add("G1", f"Probe `{probe_id}` path must be a relative request path starting with `/`.", probe_id)
            status = probe.get("expect_status")
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                self.add("G1", f"Probe `{probe_id}` expect_status must be an HTTP status integer.", probe_id)
            minimum = probe.get("expect_json_array_min_items")
            if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0):
                self.add("G1", f"Probe `{probe_id}` expect_json_array_min_items must be null or an integer >= 0.", probe_id)
            headers = probe.get("headers")
            if not isinstance(headers, dict) or any(
                not isinstance(name, str)
                or not _HEADER_NAME.fullmatch(name)
                or not isinstance(value, str)
                or len(value) > MAX_PROBE_HEADER_VALUE_CHARS
                or "\r" in value
                or "\n" in value
                for name, value in headers.items()
            ):
                self.add("G1", f"Probe `{probe_id}` headers must map header names to single-line string values.", probe_id)
            elif _texts(probe.get("headers_from_input_ids")) and not headers:
                self.add("G1", f"Probe `{probe_id}` names headers_from_input_ids but sends no headers.", probe_id)
            body = probe.get("json_body")
            if body is not None:
                if not isinstance(body, dict):
                    self.add("G1", f"Probe `{probe_id}` json_body must be null or a JSON object.", probe_id)
                elif _text(probe.get("method")) not in PROBE_BODY_METHODS:
                    self.add("G1", f"Probe `{probe_id}` json_body needs one of {list(PROBE_BODY_METHODS)}.", probe_id)
                elif len(json.dumps(body, ensure_ascii=False, sort_keys=True)) > MAX_PROBE_JSON_BODY_CHARS:
                    self.add("G1", f"Probe `{probe_id}` json_body exceeds {MAX_PROBE_JSON_BODY_CHARS} characters.", probe_id)
            # A probe may prove only that a scenario boots: runs without closure inputs still probe.
            for field in ("headers_from_input_ids", "proves_input_ids"):
                if not isinstance(probe.get(field), list):
                    self.add("G1", f"Probe `{probe_id}` {field} must be a list.", probe_id)
                for input_id in _texts(probe.get(field)):
                    self._known_input("G1", input_id, f"Probe `{probe_id}` {field}")

        self._unique("G1", "verification_stand_ins.stand_in_id", [_text(item.get("stand_in_id")) for item in self.stand_ins])
        for stand_in in self.stand_ins:
            stand_in_id = _text(stand_in.get("stand_in_id"))
            if not _STAND_IN_ID.fullmatch(stand_in_id):
                self.add("G1", f"Stand-in id `{stand_in_id}` must match {_STAND_IN_ID.pattern}.", stand_in_id)
            if not _IDENTIFIER.fullmatch(_text(stand_in.get("start_export"))):
                self.add("G1", f"Stand-in `{stand_in_id}` start_export must be an identifier.", stand_in_id)
            if not _ENV_KEY.fullmatch(_text(stand_in.get("env_key"))):
                self.add("G1", f"Stand-in `{stand_in_id}` env_key must match {_ENV_KEY.pattern}.", stand_in_id)
            self._known_input("G1", _text(stand_in.get("for_input_id")), f"Stand-in `{stand_in_id}` for_input_id")

        self._unique("G1", "demo_decisions.id", [_text(item.get("id")) for item in self.decisions])
        for decision in self.decisions:
            decision_id = _text(decision.get("id"))
            if not _CLOSURE_DECISION_ID.fullmatch(decision_id):
                self.add("G1", f"Demo decision id `{decision_id}` must match {_CLOSURE_DECISION_ID.pattern}.", decision_id)
            if not _text(decision.get("statement")):
                self.add("G1", f"Demo decision `{decision_id}` needs a statement.", decision_id)
            if _text(decision.get("scope")) != DEMO_DECISION_SCOPE:
                self.add("G1", f"Demo decision `{decision_id}` scope must be `{DEMO_DECISION_SCOPE}`.", decision_id)
            if not _texts(decision.get("input_ids")):
                self.add("G1", f"Demo decision `{decision_id}` must name the inputs it covers.", decision_id)
            for input_id in _texts(decision.get("input_ids")):
                self._known_input("G1", input_id, f"Demo decision `{decision_id}` input_ids")

        self._unique("G1", "unresolved.input_id", [_text(item.get("input_id")) for item in self.unresolved])
        for row in self.unresolved:
            input_id = _text(row.get("input_id"))
            self._known_input("G1", input_id, "Unresolved row")
            if not _text(row.get("reason")):
                self.add("G1", f"Unresolved input `{input_id}` needs a reason.", input_id)

    # G2 -- Harness inputs present exactly, each with one resolution.
    def g2_input_set(self) -> None:
        contract_harness = {
            input_id: item
            for input_id, item in self.inputs_by_id.items()
            if _text(item.get("origin")) in HARNESS_ORIGINS
        }
        for input_id in sorted(set(self.context_inputs) - set(contract_harness)):
            self.add("G2", f"Harness input `{input_id}` is missing from the contract.", input_id)
        for input_id in sorted(set(contract_harness) - set(self.context_inputs)):
            self.add("G2", f"Input `{input_id}` is not a Harness-derived input of this run.", input_id)
        for input_id in sorted(set(contract_harness) & set(self.context_inputs)):
            if _public_input(contract_harness[input_id]) != _public_input(self.context_inputs[input_id]):
                self.add("G2", f"Harness fields of input `{input_id}` were changed.", input_id)
        for item in self.inputs:
            input_id = _text(item.get("input_id"))
            if _text(item.get("origin")) == "closure_declared":
                expected = _digest_id(
                    "RI", "closure_declared", _text(item.get("consumer")), _text(item.get("name")), _text(item.get("provided_by"))
                )
                if input_id != expected:
                    self.add("G2", f"Closure-declared input id `{input_id}` was not assigned by Harness.", input_id)
            if not isinstance(item.get("resolution"), dict):
                self.add("G2", f"Input `{input_id}` must have exactly one resolution.", input_id)

    # G3 -- resolution kind allowed for the frozen design_source.
    def g3_compatibility(self) -> None:
        for input_id, item, resolution in self._resolved_inputs():
            source = item.get("design_source")
            allowed = RESOLUTION_COMPATIBILITY.get(source if isinstance(source, str) else None, ())
            kind = _text(resolution.get("kind"))
            if kind in RESOLUTION_KINDS and kind not in allowed:
                self.add(
                    "G3",
                    f"Input `{input_id}` with design_source `{source}` cannot be resolved as `{kind}`; allowed: {list(allowed)}.",
                    input_id,
                )

    # G4 -- existing providers exist; env providers match exactly one approved env change.
    def g4_existing_providers(self) -> None:
        plan_changes = _dicts(self.context.get("plan_env_changes"))
        closure_env = [item for item in self.changes if _text(item.get("kind")) == "env_var"]
        failing = {
            (_text(item.get("name")), _text(item.get("read_by"))) for item in self.source_issues
        }
        for input_id, item, resolution in self._resolved_inputs():
            provider = _dict(resolution.get("provider"))
            kind = _text(resolution.get("kind"))
            if _text(provider.get("kind")) == "symbol" and kind == "existing_runtime":
                ref = self._symbol_ref(provider.get("ref"))
                if ref and not self.symbol_exists(*ref):
                    self.add("G4", f"Provider `{provider.get('ref')}` of input `{input_id}` does not exist.", input_id)
            if _text(provider.get("kind")) != "env_var":
                continue
            key = (_text(provider.get("env_key")), _text(provider.get("read_by")))
            change_ref = _text(provider.get("change_ref"))
            pool = plan_changes if change_ref == "plan" else closure_env
            matches = [change for change in pool if (_text(change.get("name")), _text(change.get("read_by"))) == key]
            if len(matches) != 1:
                self.add(
                    "G4",
                    f"Env provider {key[0]}@{key[1]} of input `{input_id}` must match exactly one `{change_ref}` env_var change.",
                    input_id,
                )
            env = _dict(self.context_inputs.get(input_id, {}).get("env"))
            if env and key != (_text(env.get("env_key")), _text(env.get("read_by"))):
                self.add("G4", f"Plan config input `{input_id}` must be provided by its own env change.", input_id)
            if change_ref == "plan" and key in failing:
                self.add("G4", f"Approved env change {key[0]}@{key[1]} has runtime config source issues.", input_id)

    # G5 -- new providers are written by a closure unit that resolves the input.
    def g5_new_provider_ownership(self) -> None:
        for input_id, _, resolution in self._resolved_inputs():
            kind = _text(resolution.get("kind"))
            provider = _dict(resolution.get("provider"))
            if kind in ("new_boundary", "approved_demo_fixture") and _text(provider.get("kind")) == "symbol":
                ref = self._symbol_ref(provider.get("ref"))
                owners = [
                    unit
                    for unit in self.units
                    if input_id in _texts(unit.get("resolves_input_ids")) and ref and ref[0] in _texts(unit.get("write_paths"))
                ]
                if not owners:
                    self.add(
                        "G5",
                        f"Provider `{provider.get('ref')}` of input `{input_id}` must be in write_paths of a closure unit "
                        "that resolves the input.",
                        input_id,
                    )
            if _text(provider.get("kind")) == "env_var" and _text(provider.get("change_ref")) == "closure":
                read_by = _text(provider.get("read_by"))
                if not any(read_by in _texts(unit.get("write_paths")) for unit in self.units):
                    self.add("G5", f"Env provider read_by `{read_by}` of input `{input_id}` is not written by any closure unit.", input_id)
        browser_roots = self.component_roots.get(BROWSER_TEST_COMPONENT_ID, ())
        for stand_in in self.stand_ins:
            stand_in_id, module = _text(stand_in.get("stand_in_id")), _text(stand_in.get("module"))
            if not any(
                _text(unit.get("component_id")) == BROWSER_TEST_COMPONENT_ID and module in _texts(unit.get("write_paths"))
                for unit in self.units
            ):
                self.add("G5", f"Stand-in `{stand_in_id}` module must be written by a `{BROWSER_TEST_COMPONENT_ID}` closure unit.", stand_in_id)
            if not self._stand_in_module_path(module, browser_roots):
                self.add(
                    "G5",
                    f"Stand-in `{stand_in_id}` module `{module}` must be a non-test `.ts` file under {list(browser_roots)}.",
                    stand_in_id,
                )
            target = self.inputs_by_id.get(_text(stand_in.get("for_input_id")), {})
            provider = _dict(_dict(target.get("resolution")).get("provider"))
            if _text(provider.get("kind")) != "env_var" or _text(provider.get("env_key")) != _text(stand_in.get("env_key")):
                self.add(
                    "G5",
                    f"Stand-in `{stand_in_id}` env_key must equal the env_var provider of input `{stand_in.get('for_input_id')}`.",
                    stand_in_id,
                )

    # G6 -- demo fixtures cite a decision that exists and covers the input.
    def g6_decisions(self) -> None:
        approved = set(_texts(self.context.get("approved_runtime_decision_ids")))
        for input_id, item, resolution in self._resolved_inputs():
            decision_ref = _text(resolution.get("decision_ref"))
            kind = _text(resolution.get("kind"))
            if kind != "approved_demo_fixture":
                if decision_ref:
                    self.add("G6", f"Only approved_demo_fixture may cite a decision; input `{input_id}` cites `{decision_ref}`.", input_id)
                continue
            source = item.get("design_source")
            if _CLOSURE_DECISION_ID.fullmatch(decision_ref):
                decision = self.decisions_by_id.get(decision_ref)
                if decision is None:
                    self.add("G6", f"Input `{input_id}` cites unknown closure decision `{decision_ref}`.", input_id)
                elif input_id not in _texts(decision.get("input_ids")):
                    self.add("G6", f"Closure decision `{decision_ref}` does not list input `{input_id}`.", input_id)
                if source == "demo_fixture":
                    self.add("G6", f"Design demo fixture input `{input_id}` must cite its own D-* decision.", input_id)
            elif _REQUIREMENT_DECISION_ID.fullmatch(decision_ref):
                if decision_ref not in approved:
                    self.add("G6", f"Input `{input_id}` cites `{decision_ref}`, which is not an approved runtime decision.", input_id)
                design_refs = _texts(self.context_inputs.get(input_id, {}).get("design_decision_refs"))
                if source == "demo_fixture" and decision_ref not in design_refs:
                    self.add("G6", f"Design demo fixture input `{input_id}` must cite its own decision {design_refs}.", input_id)
                if source == "external_use_case":
                    self.add("G6", f"External use-case input `{input_id}` needs a closure decision (CD-*), not `{decision_ref}`.", input_id)
            else:
                self.add("G6", f"approved_demo_fixture input `{input_id}` must cite a CD-* or D-* decision.", input_id)
        for stand_in in self.stand_ins:
            decision_ref = _text(stand_in.get("decision_ref"))
            if decision_ref not in self.decisions_by_id:
                self.add("G6", f"Stand-in `{stand_in.get('stand_in_id')}` must cite a closure decision of this contract.", _text(stand_in.get("stand_in_id")))

    # G7 -- unresolved inputs are listed with Harness-computed blocked scenarios.
    def g7_unresolved(self) -> None:
        rows = {_text(row.get("input_id")): row for row in self.unresolved}
        scenarios = _input_scenarios(self.contract, context=self.context)
        stand_in_targets = {_text(item.get("for_input_id")) for item in self.stand_ins}
        for input_id, _, resolution in self._resolved_inputs():
            if _text(resolution.get("kind")) == "unresolved_external" and input_id not in rows:
                self.add("G7", f"unresolved_external input `{input_id}` must have an unresolved row.", input_id)
        for input_id, row in rows.items():
            item = self.inputs_by_id.get(input_id)
            if item is None:
                continue
            resolution = _dict(item.get("resolution"))
            env_without_stand_in = (
                _text(_dict(resolution.get("provider")).get("kind")) == "env_var" and input_id not in stand_in_targets
            )
            if _text(resolution.get("kind")) != "unresolved_external" and not env_without_stand_in:
                self.add("G7", f"Input `{input_id}` is listed as unresolved but has a resolution that is not unresolved.", input_id)
            if _texts(row.get("blocks_scenarios")) != scenarios.get(input_id, []):
                self.add("G7", f"blocks_scenarios of `{input_id}` must be the Harness projection {scenarios.get(input_id, [])}.", input_id)

    # G8 -- declared composition paths are connected; provided_by precedes the consumer.
    def g8_composition(self) -> None:
        for input_id, context_input in self.context_inputs.items():
            consumer = _text(context_input.get("consumer"))
            provided_by = _text(context_input.get("provided_by"))
            for path in context_input.get("composition_paths") or []:
                hops = _texts(path)
                for hop in hops:
                    ref = self._symbol_ref(hop)
                    if not ref or not self.symbol_exists(*ref):
                        self.add("G8", f"Composition hop `{hop}` of input `{input_id}` does not exist.", input_id)
                if provided_by and consumer in hops and (
                    provided_by not in hops or hops.index(provided_by) >= hops.index(consumer)
                ):
                    self.add("G8", f"provided_by `{provided_by}` of input `{input_id}` must precede its consumer on the path.", input_id)
                for earlier, later in zip(hops, hops[1:]):
                    verdict = self.references(earlier, later) if self.references is not None else None
                    if verdict is False:
                        self.add("G8", f"`{earlier}` does not reference `{later}` on the composition path of `{input_id}`.", input_id)
                    elif verdict is None:
                        entry = {"input_id": input_id, "from": earlier, "to": later}
                        if entry not in self.reachability_unverified:
                            self.reachability_unverified.append(entry)

    # G9 -- write paths are editable, unprotected and new targets are exactly listed.
    def g9_paths(self) -> None:
        protected = tuple(self.policy.protected_paths)
        browser_roots = self.component_roots.get(BROWSER_TEST_COMPONENT_ID, ())
        for unit in self.units:
            unit_id = _text(unit.get("unit_id"))
            write_paths = _texts(unit.get("write_paths"))
            # Raw lists: an empty or non-string entry must fail here, not disappear.
            raw_paths = [*_raw_texts(unit.get("write_paths")), *_raw_texts(unit.get("test_target_paths"))]
            self._unique("G9", f"write_paths of closure unit `{unit_id}`", _raw_texts(unit.get("write_paths")))
            for path in raw_paths:
                editable = bool(path) and (
                    self.policy.normalize_editable_path(path) == path
                    or (path in self.stand_in_modules and self._stand_in_module_path(path, browser_roots))
                )
                if not editable:
                    self.add("G9", f"Path `{path}` of closure unit `{unit_id}` is not editable under the project profile.", unit_id)
                if any(path == item or path.startswith(f"{item}/") for item in protected):
                    self.add("G9", f"Path `{path}` of closure unit `{unit_id}` is protected; only Harness writes it.", unit_id)
                if path == RUNTIME_PROBE_SPEC_PATH:
                    self.add("G9", f"`{path}` is rendered by Harness; closure unit `{unit_id}` may not write it.", unit_id)
            for path in _texts(unit.get("test_target_paths")):
                if path not in write_paths or not self.policy.is_test_path(path):
                    self.add("G9", f"Test target `{path}` of closure unit `{unit_id}` must be a test path in its write_paths.", unit_id)
        if _texts(self.contract.get("new_write_targets")) != expected_new_write_targets(self.contract, context=self.context):
            self.add("G9", "new_write_targets must be exactly the closure write paths outside the frozen Plan.")

    # G10 -- stages a unit may run and services a probe may target.
    def g10_stages_and_services(self) -> None:
        for unit in self.units:
            for stage_id in _texts(unit.get("required_stage_ids")):
                if stage_id not in CLOSURE_DEVELOPER_STAGE_IDS:
                    self.add("G10", f"Closure unit `{unit.get('unit_id')}` cannot require stage `{stage_id}`.", _text(unit.get("unit_id")))
        for probe in self.probes:
            if _text(probe.get("service")) not in CLOSURE_PROBE_SERVICES:
                self.add("G10", f"Probe `{probe.get('probe_id')}` targets unknown service `{probe.get('service')}`.", _text(probe.get("probe_id")))

    # G11 -- every resolved input is proven by a probe or test intent; env inputs need a stand-in.
    def g11_coverage(self) -> None:
        proven = {
            input_id for probe in self.probes for input_id in _texts(probe.get("proves_input_ids"))
        } | {
            input_id
            for unit in self.units
            for intent in _dicts(unit.get("test_intents"))
            for input_id in _texts(intent.get("proves_input_ids"))
        }
        stand_in_targets = {_text(item.get("for_input_id")) for item in self.stand_ins}
        unresolved = {_text(row.get("input_id")) for row in self.unresolved}
        for input_id, _, resolution in self._resolved_inputs():
            if _text(resolution.get("kind")) == "unresolved_external":
                continue
            if input_id not in proven:
                self.add("G11", f"Input `{input_id}` is not proven by any probe or closure test intent.", input_id)
            if (
                _text(_dict(resolution.get("provider")).get("kind")) == "env_var"
                and input_id not in stand_in_targets
                and input_id not in unresolved
            ):
                self.add("G11", f"Env-provided input `{input_id}` needs a verification stand-in or an unresolved row.", input_id)

    # G12 -- closure unit dependencies exist and are acyclic.
    def g12_dependencies(self) -> None:
        graph: dict[str, list[str]] = {}
        for unit in self.units:
            unit_id = _text(unit.get("unit_id"))
            graph[unit_id] = []
            for dependency in _texts(unit.get("depends_on")):
                if dependency == unit_id or dependency not in self.units_by_id:
                    self.add("G12", f"Closure unit `{unit_id}` depends on unknown or self unit `{dependency}`.", unit_id)
                else:
                    graph[unit_id].append(dependency)
        state: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            state[node] = 1
            for dependency in graph.get(node, []):
                if state.get(dependency) == 1:
                    cycle = trail[trail.index(dependency):] + [dependency] if dependency in trail else [node, dependency]
                    self.add("G12", f"Closure unit dependencies form a cycle: {' -> '.join(cycle)}.", *cycle)
                elif not state.get(dependency):
                    visit(dependency, [*trail, dependency])
            state[node] = 2

        for node in sorted(graph):
            if not state.get(node):
                visit(node, [node])

    # G13 -- runtime config change shape; unsupported kinds stop before a checkpoint.
    def g13_runtime_config(self) -> None:
        if not self.changes:
            return
        policy = runtime_config_policy(self.policy.project_profile_id)
        if policy is None:
            self.add("G13", "The project profile has no runtime config policy; closure cannot declare changes.")
            return
        executable = set(policy["executable_kinds"])
        for change in self.changes:
            kind = _text(change.get("kind"))
            if kind in dict(policy["kinds"]) and kind not in executable:
                self.add(
                    "G13",
                    f"Runtime config change kind `{kind}` is not executable yet ({sorted(executable)}).",
                    _text(change.get("unit_id")),
                    code=CONFIG_UNSUPPORTED,
                )
        synthetic_plan = {
            "runtime_config_impact": {"status": "required", "reason": "runtime closure", "changes": self.changes},
            "work_units": [
                {
                    "unit_id": _text(unit.get("unit_id")),
                    "planned_code_targets": [{"path": path} for path in _texts(unit.get("write_paths"))],
                }
                for unit in self.units
            ],
        }
        for message in runtime_config_impact_issues(
            structured_plan=synthetic_plan,
            repo_runtime_config=self.repo_runtime_config,
            planning_policy=self.policy,
        ):
            self.add("G13", message.replace("runtime_config_impact change", "runtime_config_changes item"))

    # G15 -- probes cite real scenarios and every unblocked scenario has a probe.
    def g15_scenarios(self) -> None:
        scenario_ids = set(_texts(self.context.get("scenario_ids")))
        blocked = {scenario for row in self.unresolved for scenario in _texts(row.get("blocks_scenarios"))}
        probed: set[str] = set()
        for probe in self.probes:
            for scenario_id in _texts(probe.get("scenario_ids")):
                if scenario_id not in scenario_ids:
                    self.add("G15", f"Probe `{probe.get('probe_id')}` cites unknown scenario `{scenario_id}`.", _text(probe.get("probe_id")))
                probed.add(scenario_id)
        for scenario_id in sorted(scenario_ids - blocked - probed):
            self.add("G15", f"Scenario `{scenario_id}` is not blocked and has no runtime probe.", scenario_id)

    # helpers
    def _resolved_inputs(self) -> list[tuple[str, dict[str, object], dict[str, object]]]:
        return [
            (_text(item.get("input_id")), item, _dict(item.get("resolution")))
            for item in self.inputs
            if isinstance(item.get("resolution"), dict)
        ]

    def _resolution_shape(self, input_id: str, resolution: dict[str, object]) -> None:
        kind = _text(resolution.get("kind"))
        if kind not in RESOLUTION_KINDS:
            self.add("G1", f"Input `{input_id}` resolution kind `{kind}` is not one of {list(RESOLUTION_KINDS)}.", input_id)
        if not _text(resolution.get("reason")):
            self.add("G1", f"Input `{input_id}` resolution needs a reason.", input_id)
        provider = resolution.get("provider")
        if kind == "unresolved_external":
            if provider is not None:
                self.add("G1", f"unresolved_external input `{input_id}` must have provider=null.", input_id)
            return
        if not isinstance(provider, dict):
            self.add("G1", f"Input `{input_id}` resolution `{kind}` needs a provider.", input_id)
            return
        provider_kind = _text(provider.get("kind"))
        if provider_kind == "symbol":
            if not self._symbol_ref(provider.get("ref")):
                self.add("G1", f"Symbol provider of input `{input_id}` must be `<path>#<symbol>`.", input_id)
        elif provider_kind == "env_var":
            if not _ENV_KEY.fullmatch(_text(provider.get("env_key"))):
                self.add("G1", f"Env provider of input `{input_id}` env_key must match {_ENV_KEY.pattern}.", input_id)
            if not _canonical_repo_path(provider.get("read_by")):
                self.add("G1", f"Env provider of input `{input_id}` read_by must be a repository path.", input_id)
            if _text(provider.get("change_ref")) not in CHANGE_REFS:
                self.add("G1", f"Env provider of input `{input_id}` change_ref must be one of {list(CHANGE_REFS)}.", input_id)
        else:
            self.add("G1", f"Provider of input `{input_id}` kind must be one of {list(PROVIDER_KINDS)}.", input_id)

    def _known_input(self, gate: str, input_id: str, label: str) -> None:
        if input_id not in self.inputs_by_id:
            self.add(gate, f"{label} unknown input `{input_id}`.", input_id)

    def _unique(self, gate: str, label: str, values: list[str]) -> None:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                self.add(gate, f"{label} `{value}` is not unique.", value)
            seen.add(value)

    def _symbol_ref(self, value: object) -> tuple[str, str] | None:
        match = _SYMBOL_REF.fullmatch(_text(value))
        if not match or not _canonical_repo_path(match.group(1)):
            return None
        return match.group(1), match.group(2)

    def _stand_in_module_path(self, path: str, roots: tuple[str, ...]) -> bool:
        return (
            bool(_canonical_repo_path(path))
            and any(path.startswith(f"{root}/") for root in roots)
            and path.endswith(".ts")
            and not self.policy.is_test_path(path)
            and path != RUNTIME_PROBE_SPEC_PATH
        )


# --------------------------------------------------------------------------- helpers


def _input_scenarios(contract: dict[str, object], *, context: dict[str, object]) -> dict[str, list[str]]:
    """Scenarios each input blocks: Harness inputs from their surfaces/units; closure inputs via source_refs."""

    scenario_ids = set(_texts(context.get("scenario_ids")))
    node_scenarios = _dict(context.get("design_node_scenarios"))
    result = {
        _text(item.get("input_id")): sorted(set(_texts(item.get("scenario_ids"))))
        for item in _dicts(context.get("runtime_inputs"))
    }
    for item in _dicts(contract.get("runtime_inputs")):
        input_id = _text(item.get("input_id"))
        if input_id in result:
            continue
        refs = _texts(item.get("source_refs"))
        result[input_id] = sorted(
            {ref for ref in refs if ref in scenario_ids}
            | {scenario for ref in refs for scenario in _texts(node_scenarios.get(ref))}
        )
    return result


def _plan_env_changes(work_packet: dict[str, object]) -> list[dict[str, str]]:
    impact = canonical_runtime_config_impact(work_packet.get("runtime_config_impact"))
    return [
        {
            "name": _text(change.get("name")),
            "read_by": _text(change.get("read_by")),
            "default": _text(change.get("default")),
            "unit_id": _text(change.get("unit_id")),
        }
        for change in impact["changes"]
        if change.get("kind") == "env_var"
    ]


def _public_input(item: dict[str, object]) -> dict[str, object]:
    return {
        "input_id": _text(item.get("input_id")),
        "origin": _text(item.get("origin")),
        "name": _text(item.get("name")),
        "design_source": item.get("design_source") if isinstance(item.get("design_source"), str) else None,
        "source_refs": list(_texts(item.get("source_refs"))),
        "consumer": _text(item.get("consumer")),
        "provided_by": _text(item.get("provided_by")),
    }


def _resolution(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    provider = value.get("provider")
    if isinstance(provider, dict):
        kind = _text(provider.get("kind"))
        if kind == "symbol":
            provider = {"kind": "symbol", "ref": _text(provider.get("ref"))}
        elif kind == "env_var":
            provider = {
                "kind": "env_var",
                "env_key": _text(provider.get("env_key")),
                "read_by": _text(provider.get("read_by")),
                "change_ref": _text(provider.get("change_ref")),
            }
        else:
            provider = {"kind": kind}
    elif provider is not None:
        provider = {"kind": _text(provider)}
    return {
        "kind": _text(value.get("kind")),
        "provider": provider,
        "decision_ref": _text(value.get("decision_ref")) or None,
        "reason": _text(value.get("reason")),
    }


def _target_ref(value: object) -> str:
    target = _dict(value)
    path, symbol = _text(target.get("path")), _text(target.get("symbol"))
    return f"{path}#{symbol}" if path and symbol else ""


def _digest_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def _canonical_repo_path(value: object) -> str:
    path = _text(value)
    if not path or "\\" in path or path.startswith("/") or path.endswith("/") or ":" in path:
        return ""
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return ""
    return path


def _issue(gate: str, message: str, *refs: str, code: str = CONTRACT_INVALID) -> dict[str, object]:
    return {"gate": gate, "error_code": code, "message": message, "refs": sorted({ref for ref in refs if ref})}


def _dedupe_issues(issues: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str]] = set()
    result = []
    for item in issues:
        key = (str(item.get("gate")), str(item.get("message")))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value: object) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _raw_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _raw_texts(value: object) -> list[str]:
    # Keep the Planner's list order and duplicates so G1/G9 can report them exactly.
    return [_text(item) for item in _raw_list(value)]


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
