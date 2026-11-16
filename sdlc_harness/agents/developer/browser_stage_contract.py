"""Browser Readiness (L5.4.1B): Harness-derived demo isolation scope and its mechanical gates.

Every input is a typed artifact: the owner-approved runtime closure contract, the frozen
planning path policy, the profile's browser-stage facts and TypeScript AST module facts.
Nothing here interprets use-case meaning; ``tests/test_browser_stage_contract.py`` asserts the
module carries no use-case vocabulary.

Gates (plan section 2): R1 scope (files outside the readiness scope keep their baselines),
R3 production build scan (in the verification runner), R4 the flagged page renders a demo
literal, R5 no production file imports a demo module statically. R2 is the verification suite.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import json
from pathlib import Path
import posixpath
import re

from ...profiles.planning import PlanningPathPolicy
from ...profiles.verification import BrowserStageFacts
from .closure_contract import build_closure_work_packet

READINESS_CONTEXT_SCHEMA_VERSION = "browser_readiness_context_v1"
READINESS_WORK_PACKET_MODE = "browser_readiness"
READINESS_UNIT_KEY = "browser-readiness"
# D20 (owner): a demo literal is a value string literal of at least this many characters.
MIN_DEMO_LITERAL_CHARS = 8
READINESS_UNIT_RULES = (
    "Requirement, Design, Plan and the approved runtime closure stay the authority: never change approved "
    "behavior or approved signatures. Preserve exported names; an unapproved helper shape may change only when "
    "every repository caller is inside write_paths and is updated in this unit.",
    "Load every demo module only through a dynamic import() that runs when the build-time flag in `demo_flag` "
    "equals its `enabled_value`; no production file may import a demo module statically. The flag comparison "
    "must directly guard the dynamic import so the disabled build can eliminate the demo module.",
    "A disabled build passes only when no value in `readiness_acceptance.disabled_build.forbidden_literals` "
    "appears anywhere under its output_dir; merely avoiding the import at runtime is insufficient.",
    "A flagged browser capture passes only when it renders at least one value in "
    "`readiness_acceptance.enabled_capture.required_any_literal`.",
    "The direct flag guard plus dynamic import is the approved implementation mechanism. Do not speculate about "
    "whether the bundler will eliminate or emit the module; the disabled-build scan and flagged browser capture are "
    "the authoritative proof.",
    "Every build without the flag keeps the approved production values exactly as before.",
    "Files in frozen_baseline_paths are approved earlier output: read them, never edit them; an edit is drift.",
    "Update only test_target_paths for the new loading API. Other co-located tests stay verification-only and "
    "may keep importing demo modules statically.",
)
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

UI_POLISH_CONTRACT_SCHEMA_VERSION = "ui_polish_contract_v1"
UI_POLISH_WORK_PACKET_MODE = "ui_polish"
_POLISH_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
UI_POLISH_UNIT_RULES = (
    "Requirement, Design, Plan and the approved runtime closure are frozen authority.",
    "Change presentation only. Do not change behavior, data flow, accessible semantics, copy, tests, runtime "
    "configuration, demo fixtures or Harness-rendered files.",
    "The exact Design visual_contract overrides the app visual profile when they differ.",
    "Use the approved profile tokens and keep visual changes inside the paths of this unit.",
)

BROWSER_REPAIR_RUNTIME_RULES = (
    "The approved runtime closure is behavior authority for browser repair, including its runtime inputs, demo "
    "decisions, probes, stand-ins and observed probe outcomes.",
    "Runtime closure authority is read-only context: it never adds write paths, stages, dependencies or permission "
    "to change approved Requirement, Design, Plan or production defaults.",
    "A browser label, ARIA state or screenshot is an observable UI outcome, not proof of an HTTP status. Use typed "
    "probe observations or captured network observations for boundary status claims.",
    "When browser evidence and direct probe evidence differ, preserve both and diagnose the missing composition hop; "
    "never remove an approved runtime branch merely to make the browser symptom disappear.",
)


def build_browser_repair_work_packet(
    *,
    frozen_packet: dict[str, object],
    closure_contract: dict[str, object],
    closure_contract_ref: str,
    closure_context: dict[str, object],
    probe_reports: list[dict[str, object]],
    browser_evidence_reports: list[dict[str, object]],
) -> dict[str, object]:
    """Add approved runtime authority to Plan units without expanding their execution scope."""

    probe_observations = []
    for report in probe_reports:
        source_ref = _text(report.get("source_ref"))
        for result in _dicts(report.get("probe_results")):
            observation = _dict(result.get("observation"))
            probe_observations.append(
                {
                    key: value
                    for key, value in {
                        "source_ref": source_ref,
                        "probe_id": _text(result.get("probe_id") or observation.get("probe_id")),
                        "status": _text(result.get("status")),
                        "actual_status": observation.get("actual_status"),
                        "actual_items": observation.get("actual_items"),
                    }.items()
                    if value is not None and value != ""
                }
            )
    browser_network_observations = []
    for report in browser_evidence_reports:
        source_ref = _text(report.get("source_ref"))
        for record in _dicts(report.get("records")):
            # Same ref the browser Validators cite, so a finding's evidence_refs match these observations.
            evidence_ref = (
                f"{_text(report.get('variant')) or 'browser'}:{_text(record.get('view_id'))}:"
                f"{_text(record.get('viewport_id'))}:s{int(record.get('step') or 0):02d}"
            )
            for event in _dicts(record.get("network")):
                browser_network_observations.append(
                    {
                        key: value
                        for key, value in {
                            "source_ref": source_ref,
                            "evidence_ref": evidence_ref,
                            "kind": _text(event.get("kind")),
                            "method": _text(event.get("method")),
                            "path": _text(event.get("path")),
                            "cross_origin": event.get("cross_origin"),
                            "request_header_names": _texts(event.get("request_header_names")) or None,
                            "status": event.get("status"),
                            "error": _text(event.get("error")),
                            "network_truncated": bool(record.get("network_truncated")),
                        }.items()
                        if value is not None and value != ""
                    }
                )
    authority = {
        "schema_version": "browser_repair_runtime_authority_v1",
        "contract_ref": closure_contract_ref,
        "rules": list(BROWSER_REPAIR_RUNTIME_RULES),
        "execution_facts": _texts(closure_context.get("execution_facts")),
        "runtime_inputs": _dicts(closure_contract.get("runtime_inputs")),
        "demo_decisions": _dicts(closure_contract.get("demo_decisions")),
        "runtime_probes": _dicts(closure_contract.get("runtime_probes")),
        "verification_stand_ins": _dicts(closure_contract.get("verification_stand_ins")),
        "runtime_config_changes": _dicts(closure_contract.get("runtime_config_changes")),
        "probe_observations": probe_observations,
        "browser_network_observations": browser_network_observations,
    }
    packet = json.loads(json.dumps(frozen_packet))
    units = packet.get("execution_units")
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict):
            continue
        unit["runtime_closure_authority"] = authority
        unit["input_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                {key: value for key, value in unit.items() if key != "input_digest"},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    return packet


def demo_fixture_providers(
    contract: dict[str, object],
    *,
    planning_policy: PlanningPathPolicy,
    facts: BrowserStageFacts,
) -> list[dict[str, object]]:
    """Provider files of approved demo fixtures inside the browser bundle (typed contract fields only)."""

    by_path: dict[str, dict[str, list[str]]] = {}
    for item in _dicts(contract.get("runtime_inputs")):
        resolution = _dict(item.get("resolution"))
        provider = _dict(resolution.get("provider"))
        if _text(resolution.get("kind")) != "approved_demo_fixture" or _text(provider.get("kind")) != "symbol":
            continue
        path, _, symbol = _text(provider.get("ref")).partition("#")
        if (
            planning_policy.component_for_path(path) != facts.browser_component_id
            or planning_policy.is_test_path(path)
        ):
            continue
        entry = by_path.setdefault(path, {"symbols": [], "input_ids": [], "decision_refs": []})
        for key, value in (
            ("symbols", symbol),
            ("input_ids", _text(item.get("input_id"))),
            ("decision_refs", _text(resolution.get("decision_ref"))),
        ):
            if value and value not in entry[key]:
                entry[key].append(value)
    return [{"path": path, **by_path[path]} for path in sorted(by_path)]


def browser_component_files(
    paths: Iterable[str],
    *,
    planning_policy: PlanningPathPolicy,
    facts: BrowserStageFacts,
) -> list[str]:
    """Editable source and test files of the browser component (stylesheets carry no imports)."""

    return sorted(
        {
            path
            for path in paths
            if planning_policy.component_for_path(path) == facts.browser_component_id
            and not planning_policy.is_stylesheet_path(path)
        }
    )


def resolve_module(importer: str, specifier: str, known: set[str]) -> str:
    """The known file a relative specifier names, resolved like the bundler; ``""`` otherwise.

    shortcut: relative specifiers only; a tsconfig path alias to a demo module is not followed.
    Add alias resolution if a target profile declares aliases.
    """

    if not specifier.startswith(("./", "../")):
        return ""
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if joined.startswith("../"):
        return ""
    stem = next((joined[: -len(suffix)] for suffix in _SOURCE_SUFFIXES if joined.endswith(suffix)), joined)
    candidates = [
        joined,
        *(f"{stem}{suffix}" for suffix in _SOURCE_SUFFIXES),
        *(f"{joined}/index{suffix}" for suffix in _SOURCE_SUFFIXES),
    ]
    return next((candidate for candidate in candidates if candidate in known), "")


def demo_module_references(
    *,
    demo_paths: set[str],
    files: list[str],
    module_facts: dict[str, dict[str, object]],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    """Every reference from another component file to a demo module, with its AST kind."""

    known = set(files)
    rows: list[dict[str, object]] = []
    for path in files:
        if path in demo_paths:
            continue
        for reference in _dicts(_dict(module_facts.get(path)).get("module_references")):
            target = resolve_module(path, _text(reference.get("specifier")), known)
            row = {
                "path": path,
                "demo_module": target,
                "kind": _text(reference.get("kind")),
                "test": planning_policy.is_test_path(path),
            }
            if target in demo_paths and row not in rows:
                rows.append(row)
    return rows


def derive_readiness_context(
    *,
    run_id: str,
    version: int,
    closure_contract: dict[str, object],
    closure_contract_ref: str,
    closure_context: dict[str, object],
    planning_policy: PlanningPathPolicy,
    facts: BrowserStageFacts,
    files: list[str],
    module_facts: dict[str, dict[str, object]],
    read_text: Callable[[str], str | None],
) -> dict[str, object]:
    """Exact readiness scope: demo modules, their production importers, co-located tests, demo literals."""

    providers = demo_fixture_providers(closure_contract, planning_policy=planning_policy, facts=facts)
    base: dict[str, object] = {
        "schema_version": READINESS_CONTEXT_SCHEMA_VERSION,
        "version": f"browser_readiness_context_v{version}",
        "run_id": run_id,
        "closure_contract": closure_contract_ref,
        "browser_component_id": facts.browser_component_id,
        "demo_flag_env_key": facts.demo_flag_env_key,
        "build_output_dir": facts.build_output_dir,
        "min_demo_literal_chars": MIN_DEMO_LITERAL_CHARS,
        "demo_modules": providers,
    }
    if not providers:
        return {
            **base,
            "status": "not_required",
            "reason": "The approved closure resolves no input with a demo fixture inside the browser component.",
        }
    demo_paths = {str(item["path"]) for item in providers}
    new_targets = set(_texts(closure_contract.get("new_write_targets")))
    plan_paths = set(_texts(closure_context.get("plan_allowed_paths")))
    not_isolable = sorted(path for path in demo_paths if path not in new_targets or path in plan_paths)
    if not_isolable:
        return _blocked(
            base,
            "browser_readiness_demo_module_not_isolable",
            f"Demo fixture provider(s) {not_isolable} were not created by the runtime closure, so loading them "
            "behind the flag would change approved Plan code.",
        )
    missing = sorted(path for path in demo_paths if path not in files)
    if missing:
        return _blocked(base, "browser_readiness_demo_module_missing", f"Demo fixture provider(s) {missing} do not exist.")

    references = demo_module_references(
        demo_paths=demo_paths, files=files, module_facts=module_facts, planning_policy=planning_policy
    )
    production_importers = sorted(
        {str(row["path"]) for row in references if not row["test"] and row["kind"] != "type_only"}
    )
    sources = sorted({*demo_paths, *production_importers})
    tests = sorted(
        path
        for path in files
        if planning_policy.is_test_path(path) and planning_policy.source_path_for_test(path) in sources
    )
    tests_requiring_update = sorted(
        path for path in tests if planning_policy.source_path_for_test(path) in production_importers
    )
    candidates = list(
        dict.fromkeys(
            literal
            for path in sorted(demo_paths)
            for literal in _texts(_dict(module_facts.get(path)).get("string_literals"))
        )
    )
    production_texts = {
        path: read_text(path) or ""
        for path in files
        if path not in demo_paths and not planning_policy.is_test_path(path)
    }
    shared = [
        {"literal": literal, "files": sorted(path for path, text in production_texts.items() if literal in text)}
        for literal in candidates
        if len(literal) >= MIN_DEMO_LITERAL_CHARS and any(literal in text for text in production_texts.values())
    ]
    shared_values = {str(item["literal"]) for item in shared}
    demo_literals = [
        literal for literal in candidates if len(literal) >= MIN_DEMO_LITERAL_CHARS and literal not in shared_values
    ]
    context = {
        **base,
        "status": "required",
        "reason": "",
        "references": references,
        "static_importers": sorted(
            {str(row["path"]) for row in references if not row["test"] and row["kind"] == "static"}
        ),
        "sources": sources,
        "tests": tests,
        "tests_requiring_update": tests_requiring_update,
        "write_scope": sorted({*sources, *tests}),
        "demo_literals": demo_literals,
        # Already in production source, so a build containing them proves nothing about the demo.
        "shared_literals": shared,
        "short_literals": [literal for literal in candidates if len(literal) < MIN_DEMO_LITERAL_CHARS],
    }
    if not demo_literals:
        return _blocked(
            context,
            "browser_readiness_no_demo_literal",
            "The demo fixture providers hold no demo-only string literal, so R3 and R4 cannot be checked.",
        )
    return context


def build_readiness_work_packet(
    *,
    context: dict[str, object],
    context_ref: str,
    closure_contract: dict[str, object],
    closure_contract_ref: str,
    closure_context: dict[str, object],
    frozen_packet: dict[str, object],
    version: int,
) -> dict[str, object]:
    """One Developer unit over the exact readiness scope, projected like a closure unit."""

    write_scope = _texts(context.get("write_scope"))
    tests = _texts(context.get("tests"))
    tests_requiring_update = (
        _texts(context.get("tests_requiring_update")) if "tests_requiring_update" in context else tests
    )
    modules = _dicts(context.get("demo_modules"))
    demo_literals = _texts(context.get("demo_literals"))
    input_ids = sorted({input_id for module in modules for input_id in _texts(module.get("input_ids"))})
    flag = _text(context.get("demo_flag_env_key"))
    digest = hashlib.sha256(
        json.dumps({"write_scope": write_scope, "demo_modules": modules}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    unit = {
        "unit_id": f"BR-{digest[:12].upper()}",
        "key": READINESS_UNIT_KEY,
        "component_id": _text(context.get("browser_component_id")),
        "objective": (
            f"Load {[module['path'] for module in modules]} only through a dynamic import() that runs when the "
            f"build-time flag `{flag}` equals \"1\"; every other build keeps the approved production values. "
            "The disabled production bundle must contain none of the derived demo literals, and the flagged "
            "browser capture must render one. Update only tests of production importers that use the loading path."
        ),
        "depends_on": [],
        "resolves_input_ids": input_ids,
        "write_paths": write_scope,
        "test_target_paths": tests_requiring_update,
        "required_stage_ids": ["typecheck", "unit"] if tests else ["typecheck"],
        "test_intents": (
            [
                {
                    "intent": (
                        "Tests of production importers cover the updated composition/loading API. The Harness "
                        "bundle scan and flagged browser capture, not a boolean test seam, prove the build flag."
                    )
                }
            ]
            if tests_requiring_update
            else []
        ),
        "context_paths": write_scope,
    }
    packet = build_closure_work_packet(
        contract={
            "runtime_inputs": [
                item for item in _dicts(closure_contract.get("runtime_inputs")) if _text(item.get("input_id")) in input_ids
            ],
            "demo_decisions": _dicts(closure_contract.get("demo_decisions")),
            "runtime_probes": _dicts(closure_contract.get("runtime_probes")),
            "verification_stand_ins": [],
            "runtime_config_changes": [],
            "closure_units": [unit],
        },
        context=closure_context,
        frozen_packet=frozen_packet,
        version=version,
        contract_ref=closure_contract_ref,
    )
    execution_unit = {
        key: value for key, value in _dicts(packet.get("execution_units"))[0].items() if key != "input_digest"
    }
    prompt_closure_context = _dict(execution_unit.get("closure_context"))
    for irrelevant_key in ("probes", "stand_ins", "env_providers"):
        prompt_closure_context.pop(irrelevant_key, None)
    protected_signature_ids = sorted(
        {
            _text(entry.get("contract_id"))
            for entry in _dicts(_dict(frozen_packet.get("implementation_signature_contract")).get("entries"))
            if _text(entry.get("source_path")) in write_scope and _text(entry.get("contract_id"))
        }
    )
    execution_unit["title"] = "Browser readiness: demo isolation"
    execution_unit["signature_entry_ids"] = protected_signature_ids
    execution_unit["closure_context"] = {
        **prompt_closure_context,
        "rules": list(READINESS_UNIT_RULES),
        "demo_flag": {"env_key": flag, "enabled_value": "1"},
        "readiness_acceptance": {
            "disabled_build": {
                "output_dir": _text(context.get("build_output_dir")),
                "forbidden_literals": demo_literals,
            },
            "enabled_capture": {"required_any_literal": demo_literals},
        },
        "new_file_roots": [],
    }
    execution_unit["input_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(execution_unit, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        **packet,
        "version": f"browser_readiness_work_packet_v{version}",
        "mode": READINESS_WORK_PACKET_MODE,
        "browser_readiness_context": context_ref,
        "runtime_config_impact": {
            "status": "not_required",
            "reason": "Browser readiness changes no runtime configuration.",
            "changes": [],
        },
        "execution_units": [execution_unit],
    }


def readiness_scope_drift(
    *,
    baselines: dict[str, str],
    current_dirty: Iterable[str],
    current_sha256: Callable[[str], str],
    allowed: Iterable[str],
) -> dict[str, list[str]]:
    """R1: files outside the readiness scope keep the hashes they had when the session started."""

    allowed_set = set(allowed)
    return {
        "drifted": sorted(
            path for path, sha in baselines.items() if path not in allowed_set and current_sha256(path) != sha
        ),
        "outside": sorted(
            path
            for path in current_dirty
            if path not in allowed_set
            and path not in baselines
            and path != ".codegraph"
            and not path.startswith(".codegraph/")
        ),
    }


def static_import_problems(
    *,
    context: dict[str, object],
    files: list[str],
    module_facts: dict[str, dict[str, object]],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    """R5: no production file of the browser component imports a demo module statically."""

    flag = _text(context.get("demo_flag_env_key"))
    demo_paths = {_text(item.get("path")) for item in _dicts(context.get("demo_modules"))}
    return [
        {
            "source": "R5",
            "paths": [str(row["path"])],
            "issue": (
                f"`{row['path']}` imports demo module `{row['demo_module']}` statically, so every production "
                "build bundles it."
            ),
            "expected_change": (
                f"`{row['path']}` loads `{row['demo_module']}` only through a dynamic import() that runs when "
                f"`{flag}` equals \"1\"."
            ),
        }
        for row in demo_module_references(
            demo_paths=demo_paths, files=files, module_facts=module_facts, planning_policy=planning_policy
        )
        if not row["test"] and row["kind"] == "static"
    ]


def demo_render_problems(
    *,
    capture_results: Iterable[dict[str, object]],
    demo_literals: list[str],
    view_id: str,
) -> list[dict[str, object]]:
    """R4: with the flag, the last capture of the baseline view shows a demo literal at every viewport."""

    last: dict[str, dict[str, object]] = {}
    for item in capture_results:
        if _text(item.get("view_id")) != view_id:
            continue
        viewport = _text(item.get("viewport_id"))
        if viewport not in last or int(item.get("step") or 0) >= int(last[viewport].get("step") or 0):
            last[viewport] = dict(item)
    if not last:
        return [
            {
                "source": "R4",
                "paths": [],
                "issue": f"No capture of view {view_id} exists, so the flagged page was never observed.",
                "expected_change": "With the flag set, the served page renders the approved demo fixtures.",
            }
        ]
    problems = []
    for viewport, item in sorted(last.items()):
        text = _text(item.get("aria_snapshot"))
        if _text(item.get("status")) == "passed" and any(literal in text for literal in demo_literals):
            continue
        problems.append(
            {
                "source": "R4",
                "paths": [],
                "issue": (
                    f"View {view_id} at {viewport} (status {item.get('status')}) shows none of the demo literals in "
                    "its accessible text."
                ),
                "expected_change": "With the flag set, the served page renders the approved demo fixtures.",
            }
        )
    return problems


def readiness_findings(problems: list[dict[str, object]], *, unit_id: str) -> list[dict[str, object]]:
    """Every readiness problem belongs to the single readiness unit (exact unit_ids for the executor)."""

    return [
        {
            "finding_id": f"BRF-{index}",
            "source": _text(problem.get("source")),
            "unit_ids": [unit_id],
            "routing_rule": 0,
            "issue": _text(problem.get("issue")),
            "expected_change": _text(problem.get("expected_change")),
            **({"paths": _texts(problem.get("paths"))} if _texts(problem.get("paths")) else {}),
        }
        for index, problem in enumerate(problems, start=1)
    ]


# ---------------------------------------------------------------------- UI Polish (L5.4.4)


def visual_contracts_by_node(frozen_packet: dict[str, object]) -> dict[str, dict[str, object]]:
    """Approved visual contracts keyed by stable Design node ID."""

    result: dict[str, dict[str, object]] = {}
    for unit in _dicts(frozen_packet.get("execution_units")):
        for binding in _dicts(unit.get("surface_contracts")):
            if _text(binding.get("surface")).lower() != "ui":
                continue
            node_id = _text(binding.get("design_node_id"))
            experience = _dict(_dict(binding.get("surface_contract")).get("experience_contract"))
            visual = _dict(experience.get("visual_contract"))
            if node_id and visual:
                result[node_id] = visual
    return result


def assemble_ui_polish_contract(
    *,
    run_id: str,
    version: int,
    planner_output: dict[str, object],
    planning_policy: PlanningPathPolicy,
) -> dict[str, object]:
    """Assign stable unit IDs and component ownership without repairing Planner output."""

    raw_units = _dicts(planner_output.get("polish_units"))
    ids = {
        _text(unit.get("key")): "UP-" + hashlib.sha256(_text(unit.get("key")).encode("utf-8")).hexdigest()[:12].upper()
        for unit in raw_units
        if _text(unit.get("key"))
    }
    units: list[dict[str, object]] = []
    for raw in raw_units:
        key = _text(raw.get("key"))
        write_paths = _texts(raw.get("write_paths"))
        new_paths = _texts(raw.get("new_paths"))
        components = {
            planning_policy.component_for_path(path)
            for path in [*write_paths, *new_paths]
            if planning_policy.component_for_path(path)
        }
        units.append(
            {
                "unit_id": ids.get(key, ""),
                "key": key,
                "component_id": next(iter(components)) if len(components) == 1 else "",
                "objective": _text(raw.get("objective")),
                "write_paths": write_paths,
                "new_paths": new_paths,
                "depends_on": [ids.get(item, item) for item in _texts(raw.get("depends_on"))],
                "authority_refs": _texts(raw.get("authority_refs")),
            }
        )
    return {
        "schema_version": UI_POLISH_CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "version": version,
        "authority": _dict(planner_output.get("authority")),
        "polish_units": units,
        "out_of_scope": _texts(planner_output.get("out_of_scope")),
    }


def polish_contract_rule_issues(
    *,
    contract: dict[str, object],
    expected_profile_ref: dict[str, object],
    visual_profile: dict[str, object],
    visual_contracts: dict[str, dict[str, object]],
    frozen_packet: dict[str, object],
    planning_policy: PlanningPathPolicy,
    target_path: Path,
    forbidden_paths: Iterable[str],
) -> list[dict[str, object]]:
    """U1-U6 mechanical checks; semantic scope and aesthetics belong to the Validator."""

    issues: list[dict[str, object]] = []

    def add(gate: str, message: str, *, unit: str = "", paths: Iterable[str] = ()) -> None:
        path_list = list(paths)
        issues.append(
            {
                "gate": gate,
                "message": message,
                **({"unit_key": unit} if unit else {}),
                **({"paths": path_list} if path_list else {}),
            }
        )

    units = _dicts(contract.get("polish_units"))
    authority = _dict(contract.get("authority"))
    profile_ref = _dict(authority.get("visual_profile_ref"))
    design_ids = _texts(authority.get("design_node_ids"))
    if contract.get("schema_version") != UI_POLISH_CONTRACT_SCHEMA_VERSION:
        add("U1", f"schema_version must be {UI_POLISH_CONTRACT_SCHEMA_VERSION}.")
    if not units:
        add("U1", "polish_units must contain at least one unit.")
    if len(design_ids) != len(set(design_ids)) or not design_ids:
        add("U1", "authority.design_node_ids must be a non-empty unique list.")
    if not isinstance(contract.get("out_of_scope"), list):
        add("U1", "out_of_scope must be a list.")

    keys = [_text(unit.get("key")) for unit in units]
    unit_ids = [_text(unit.get("unit_id")) for unit in units]
    expected_ids = {
        key: "UP-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()
        for key in keys
        if key
    }
    if any(not _POLISH_KEY.fullmatch(key) for key in keys) or len(keys) != len(set(keys)):
        add("U1", "Every polish unit key must be unique lowercase letters/digits/hyphen/underscore.")
    if len(unit_ids) != len(set(unit_ids)) or any(
        _text(unit.get("unit_id")) != expected_ids.get(_text(unit.get("key")), "") for unit in units
    ):
        add("U1", "Every polish unit must have the deterministic ID assigned from its key.")
    known_ids = set(unit_ids)
    graph: dict[str, list[str]] = {}
    for unit in units:
        unit_id = _text(unit.get("unit_id"))
        key = _text(unit.get("key"))
        depends = _texts(unit.get("depends_on"))
        graph[unit_id] = depends
        if not _text(unit.get("objective")):
            add("U1", "objective must be non-empty.", unit=key)
        if not _texts(unit.get("write_paths")) and not _texts(unit.get("new_paths")):
            add("U1", "A polish unit needs write_paths or new_paths.", unit=key)
        unknown = sorted(set(depends) - known_ids)
        if unknown or unit_id in depends:
            add("U1", f"depends_on contains unknown/self IDs: {unknown or [unit_id]}.", unit=key)
    if _has_cycle(graph):
        add("U1", "polish unit dependencies contain a cycle.")

    ui_components: set[str] = set()
    node_test_paths: dict[str, set[str]] = {}
    for plan_unit in _dicts(frozen_packet.get("execution_units")):
        surfaces = {_text(value).lower() for value in plan_unit.get("surfaces", []) if _text(value)}
        ui_bindings = [
            item for item in _dicts(plan_unit.get("surface_contracts")) if _text(item.get("surface")).lower() == "ui"
        ]
        if ui_bindings or "ui" in surfaces:
            component = _text(plan_unit.get("component_id"))
            if component:
                ui_components.add(component)
        tests = set(_texts(plan_unit.get("test_target_paths")))
        for node_id in _texts(plan_unit.get("design_node_ids")):
            node_test_paths.setdefault(node_id, set()).update(tests)

    protected = tuple(planning_policy.protected_paths)
    forbidden = set(forbidden_paths)
    profile_tokens = set(_dict(visual_profile.get("tokens")))
    known_authority = profile_tokens | set(visual_contracts)
    expected_design_ids = set(visual_contracts)
    if set(design_ids) != expected_design_ids:
        add("U4", f"authority.design_node_ids must be exactly {sorted(expected_design_ids)}.")
    for unit in units:
        key = _text(unit.get("key"))
        paths = [*_texts(unit.get("write_paths")), *_texts(unit.get("new_paths"))]
        components = {planning_policy.component_for_path(path) for path in paths}
        if "" in components or len(components) != 1 or not components.issubset(ui_components):
            add("U2", "Every path must belong to one editable component with a UI surface.", unit=key, paths=paths)
        if _text(unit.get("component_id")) not in components:
            add("U2", "component_id does not match the unit paths.", unit=key)
        protected_hits = sorted(
            path for path in paths if any(path == root or path.startswith(f"{root}/") for root in protected)
        )
        if protected_hits:
            add("U2", "Protected paths are not editable by UI Polish.", unit=key, paths=protected_hits)
        unit_design_ids = set(_texts(unit.get("authority_refs"))) & set(visual_contracts)
        allowed_tests = set().union(*(node_test_paths.get(node_id, set()) for node_id in unit_design_ids))
        foreign_tests = sorted(
            path for path in paths if planning_policy.is_test_path(path) and path not in allowed_tests
        )
        if foreign_tests:
            add("U2", "A polish unit may not edit another Plan unit's test.", unit=key, paths=foreign_tests)

        missing = sorted(path for path in _texts(unit.get("write_paths")) if not (target_path / path).is_file())
        if missing:
            add("U3", "write_paths must already exist.", unit=key, paths=missing)
        invalid_new = sorted(
            path
            for path in _texts(unit.get("new_paths"))
            if (target_path / path).exists()
            or not planning_policy.normalize_editable_path(path)
            or planning_policy.is_test_path(path)
        )
        if invalid_new:
            add("U3", "new_paths must not exist and must use an allowed source/stylesheet suffix.", unit=key, paths=invalid_new)

        refs = _texts(unit.get("authority_refs"))
        unknown_refs = sorted(set(refs) - known_authority)
        if not refs or unknown_refs:
            add("U4", f"authority_refs must cite known profile tokens or visual Design IDs: {unknown_refs}.", unit=key)
        forbidden_hits = sorted(set(paths) & forbidden)
        if forbidden_hits:
            add("U6", "Demo modules and Harness-rendered files are outside UI Polish scope.", unit=key, paths=forbidden_hits)

    if profile_ref != expected_profile_ref:
        add("U5", "visual_profile_ref no longer matches the pinned profile file.")
    return issues


def build_ui_polish_work_packet(
    *,
    contract: dict[str, object],
    contract_ref: str,
    frozen_packet: dict[str, object],
    version: int,
) -> dict[str, object]:
    """Project the approved polish units through the existing closure packet machinery."""

    # Read-only context is the whole approved Polish scope: a stylesheet-only unit must see the markup it styles.
    scope_paths = [path for unit in _dicts(contract.get("polish_units")) for path in _texts(unit.get("write_paths"))]
    closure_units = [
        {
            **unit,
            "write_paths": [*_texts(unit.get("write_paths")), *_texts(unit.get("new_paths"))],
            "test_target_paths": [],
            "resolves_input_ids": [],
            "required_stage_ids": ["typecheck"],
            "test_intents": [],
            "context_paths": list(
                dict.fromkeys([*_texts(unit.get("write_paths")), *_texts(unit.get("new_paths")), *scope_paths])
            ),
        }
        for unit in _dicts(contract.get("polish_units"))
    ]
    packet = build_closure_work_packet(
        contract={
            "runtime_inputs": [],
            "runtime_config_changes": [],
            "demo_decisions": [],
            "runtime_probes": [],
            "verification_stand_ins": [],
            "closure_units": closure_units,
        },
        context={"plan_allowed_paths": _texts(_dict(frozen_packet.get("output_contract")).get("allowed_paths"))},
        frozen_packet=frozen_packet,
        version=version,
        contract_ref=contract_ref,
    )
    by_id = {_text(unit.get("unit_id")): unit for unit in _dicts(contract.get("polish_units"))}
    execution_units = []
    for unit in _dicts(packet.get("execution_units")):
        polish = by_id.get(_text(unit.get("unit_id")), {})
        payload = {
            **unit,
            "title": f"UI Polish: {_text(polish.get('key'))}",
            "surfaces": ["ui"],
            "design_node_ids": [
                ref for ref in _texts(polish.get("authority_refs")) if ref.startswith("VDN-")
            ],
            "closure_context": {
                **_dict(unit.get("closure_context")),
                "rules": list(UI_POLISH_UNIT_RULES),
                "visual_authority": contract.get("authority"),
                "authority_refs": _texts(polish.get("authority_refs")),
                "new_paths": _texts(polish.get("new_paths")),
            },
        }
        payload["input_digest"] = "sha256:" + hashlib.sha256(
            json.dumps({key: value for key, value in payload.items() if key != "input_digest"}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        execution_units.append(payload)
    return {
        **packet,
        "version": f"ui_polish_work_packet_v{version}",
        "mode": UI_POLISH_WORK_PACKET_MODE,
        "ui_polish_contract": contract_ref,
        "runtime_config_impact": {"status": "not_required", "reason": "UI Polish changes no runtime config.", "changes": []},
        "execution_units": execution_units,
    }


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dep) for dep in graph.get(node, []) if dep in graph):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _blocked(context: dict[str, object], error_code: str, message: str) -> dict[str, object]:
    return {**context, "status": "blocked", "blocked": {"error_code": error_code, "message": message}}


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: object) -> str:
    return str(value or "").strip()


def _texts(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
