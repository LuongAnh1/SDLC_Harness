"""Regression cho Phase 3 signature contract và Phase 4 TypeScript AST comparator."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sdlc_harness.agents.developer.work_packet import build_developer_work_packet
from sdlc_harness.agents.validator import CodeTraceValidator
from sdlc_harness.agents.validator.code_contract import (
    deterministic_typescript_contract_findings,
)
from sdlc_harness.agents.validator.typescript_ast import TypeScriptAstEvidence
from sdlc_harness.profiles import (
    LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    build_implementation_signature_contract,
    build_code_trace_policy,
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
    implementation_signature_contract_issues,
)


SOURCE_PATH = "apps/api/src/health.ts"
DESIGN_ID = "method:HealthService.check"


@pytest.fixture
def policies():
    project = get_project_profile("typescript_fullstack_v1")
    planning = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    return planning, build_verification_stage_policy(project=project)


def _mapping() -> dict[str, object]:
    return {
        "design_element_id": DESIGN_ID,
        "design_element_type": "method",
        "design_element_name": "HealthService.check",
        "planned_code_target": f"{SOURCE_PATH}#check",
        "component_id": "api",
        "test_intent": "Verify the health contract.",
    }


def _class_graph() -> dict[str, object]:
    return {
        "nodes": [
            {
                "type": "class",
                "className": "HealthService",
                "methods": [
                    {
                        "name": "check",
                        "parameters": [
                            {"name": "prefix", "type": "string | undefined"},
                            {"name": "tags", "type": "T[]"},
                        ],
                        "returnType": "Promise<HealthSnapshot>",
                    }
                ],
            }
        ]
    }


def _candidate() -> dict[str, object]:
    return {
        "version": "implementation_signature_contract_v1",
        "entries": [
            {
                "design_element_id": DESIGN_ID,
                "symbol_kind": "method",
                "owner_kind": "class",
                "qualified_name": "HealthService.check",
                "parameters": [
                    {
                        "name": "prefix",
                        "type": "string | undefined",
                        "optional": False,
                        "rest": False,
                    },
                    {
                        "name": "tags",
                        "type": "T[]",
                        "optional": False,
                        "rest": True,
                    },
                ],
                "return_type": "Promise<HealthSnapshot>",
                "type_parameters": ["T extends string"],
                "async": True,
            }
        ],
    }


def _contract(planning) -> dict[str, object]:
    return build_implementation_signature_contract(
        planning_policy=planning,
        mappings=[_mapping()],
        class_graph=_class_graph(),
        candidate=_candidate(),
    )


def _actual_symbol() -> dict[str, object]:
    return {
        "kind": "method",
        "name": "check",
        "qualified_name": "HealthService.check",
        "parameters": [
            {
                "name": "prefix",
                "type": "string | undefined",
                "optional": False,
                "rest": False,
            },
            {
                "name": "tags",
                "type": "T[]",
                "optional": False,
                "rest": True,
            },
        ],
        "return_type": "Promise<HealthSnapshot>",
        "type_parameters": ["T extends string"],
        "async": True,
        "exported": False,
    }


def _evidence(planning, symbols: list[dict[str, object]]) -> TypeScriptAstEvidence:
    return TypeScriptAstEvidence(
        extractor_id="typescript_compiler_ast_v1",
        profile={
            "version": "code_trace_policy_v1",
            "project_profile_id": planning.project_profile_id,
            "language_profile_id": planning.language_profile_id,
            "symbol_extractor_id": "typescript_compiler_ast_v1",
            "source_suffixes": [".ts", ".tsx"],
            "test_suffixes": [".test.ts", ".test.tsx"],
        },
        files=({"path": SOURCE_PATH, "diagnostics": [], "symbols": symbols},),
    )


class _AcceptingLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return json.dumps(
            {
                "ok": True,
                "confidence": 0.99,
                "summary": "No semantic blockers.",
                "graph_edges": [],
                "missing_links": [],
                "risks": [],
            }
        )


class _StaticExtractor:
    def __init__(self, symbol: dict[str, object]) -> None:
        self.symbol = symbol

    def extract(self, *, target_path: Path, source_paths, policy, run_id=None):
        del target_path, run_id
        return TypeScriptAstEvidence(
            extractor_id=policy.symbol_extractor_id,
            profile=policy.metadata(),
            files=(
                {
                    "path": source_paths[0],
                    "diagnostics": [],
                    "symbols": [self.symbol],
                },
            ),
        )


def test_signature_contract_is_canonical_and_detects_tampering(policies) -> None:
    planning, _ = policies
    contract = _contract(planning)

    assert not implementation_signature_contract_issues(
        contract,
        planning_policy=planning,
        mappings=[_mapping()],
        class_graph=_class_graph(),
    )
    assert contract["entries"][0]["contract_id"].startswith("SIG-")
    assert str(contract["digest"]).startswith("sha256:")

    tampered = copy.deepcopy(contract)
    tampered["entries"][0]["async"] = False
    issues = implementation_signature_contract_issues(
        tampered,
        planning_policy=planning,
        mappings=[_mapping()],
        class_graph=_class_graph(),
    )
    assert any("digest is invalid" in issue for issue in issues)
    assert any("not canonical" in issue for issue in issues)


def test_signature_contract_does_not_infer_types_when_phase3_omits_entry(policies) -> None:
    planning, _ = policies
    contract = build_implementation_signature_contract(
        planning_policy=planning,
        mappings=[_mapping()],
        class_graph=_class_graph(),
        candidate={},
    )

    assert contract["entries"] == []
    issues = implementation_signature_contract_issues(
        contract,
        planning_policy=planning,
        mappings=[_mapping()],
        class_graph=_class_graph(),
    )
    assert issues == [f"Missing implementation signature contract for approved method: {DESIGN_ID}"]


def test_work_packet_v2_carries_contract_and_legacy_v1_remains_readable(
    tmp_path,
    policies,
) -> None:
    planning, verification = policies
    source = tmp_path / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_text("export class HealthService {}\n", encoding="utf-8")
    current_plan = {
        "version": PROFILED_IMPLEMENTATION_PLAN_VERSION,
        "planning_profile": planning.metadata(),
        "target_repository": {},
        "implementation_steps": [{"order": 1, "files": [SOURCE_PATH]}],
        "proposed_files": [{"path": SOURCE_PATH}],
        "design_mappings": [_mapping()],
        "implementation_signature_contract": _contract(planning),
        "verification_stage_ids": list(verification.stage_ids),
        "test_intents": [{"stage_id": "unit"}],
    }
    packet = build_developer_work_packet(
        run_id="run-signature",
        source_ref="fixture:signature",
        issue_key="SDLC-1",
        structured_plan=current_plan,
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={"structured_plan_sha256": "sha256:approved"},
        planning_policy=planning,
        verification_policy=verification,
    )
    assert packet["schema_version"] == PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION
    assert packet["implementation_signature_contract"] == current_plan[
        "implementation_signature_contract"
    ]

    legacy_plan = {key: value for key, value in current_plan.items() if key != "implementation_signature_contract"}
    legacy_plan["version"] = LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION
    legacy_packet = build_developer_work_packet(
        run_id="run-signature-legacy",
        source_ref="fixture:signature",
        issue_key="SDLC-1",
        structured_plan=legacy_plan,
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
        planning_policy=planning,
        verification_policy=verification,
    )
    assert legacy_packet["schema_version"] == LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION
    assert "implementation_signature_contract" not in legacy_packet


def test_typescript_signature_comparator_accepts_exact_and_matching_overload(policies) -> None:
    planning, _ = policies
    contract = _contract(planning)
    wrong_overload = _actual_symbol()
    wrong_overload["return_type"] = "Promise<boolean>"
    packet = {
        "implementation_signature_contract": contract,
        "requirement_coverage": [
            {"requirement_id": "REQ-1", "design_element_ids": [DESIGN_ID]}
        ],
    }

    findings = deterministic_typescript_contract_findings(
        work_packet=packet,
        ast_evidence=_evidence(planning, [wrong_overload, _actual_symbol()]),
    )

    assert findings == []


@pytest.mark.parametrize(
    "mutation",
    (
        "parameter_type",
        "parameter_order",
        "optional",
        "rest",
        "return_type",
        "generic",
        "async",
    ),
)
def test_typescript_signature_comparator_blocks_each_signature_drift(
    policies,
    mutation: str,
) -> None:
    planning, _ = policies
    actual = _actual_symbol()
    if mutation == "parameter_type":
        actual["parameters"][0]["type"] = "string"
    elif mutation == "parameter_order":
        actual["parameters"] = list(reversed(actual["parameters"]))
    elif mutation == "optional":
        actual["parameters"][0]["optional"] = True
    elif mutation == "rest":
        actual["parameters"][1]["rest"] = False
    elif mutation == "return_type":
        actual["return_type"] = "Promise<boolean>"
    elif mutation == "generic":
        actual["type_parameters"] = ["T extends number"]
    else:
        actual["async"] = False

    findings = deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": _contract(planning)},
        ast_evidence=_evidence(planning, [actual]),
    )

    assert len(findings) == 1
    assert findings[0]["type"] == "contract_signature_mismatch"
    assert findings[0]["blocking"] is True
    assert findings[0]["owner"] == "developer"


def test_typescript_signature_comparator_only_normalizes_whitespace(policies) -> None:
    planning, _ = policies
    whitespace_only = _actual_symbol()
    whitespace_only["parameters"][0]["name"] = "renamedPrefix"
    whitespace_only["parameters"][0]["type"] = " string   |   undefined "
    whitespace_only["return_type"] = " Promise < HealthSnapshot > "
    whitespace_only["type_parameters"] = [" T   extends   string "]

    assert deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": _contract(planning)},
        ast_evidence=_evidence(planning, [whitespace_only]),
    ) == []

    different_syntax = _actual_symbol()
    different_syntax["parameters"][1]["type"] = "Array<T>"
    findings = deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": _contract(planning)},
        ast_evidence=_evidence(planning, [different_syntax]),
    )

    assert [item["type"] for item in findings] == ["contract_signature_mismatch"]


def test_typescript_signature_comparator_requires_exact_qualified_owner(policies) -> None:
    planning, _ = policies
    actual = _actual_symbol()
    actual["qualified_name"] = "OtherService.check"

    findings = deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": _contract(planning)},
        ast_evidence=_evidence(planning, [actual]),
    )

    assert len(findings) == 1
    assert findings[0]["type"] == "missing_code_symbol"

    wrong_kind = _actual_symbol()
    wrong_kind["kind"] = "function"
    findings = deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": _contract(planning)},
        ast_evidence=_evidence(planning, [wrong_kind]),
    )

    assert [item["type"] for item in findings] == ["missing_code_symbol"]


def test_function_signature_can_implement_an_approved_interface_operation(policies) -> None:
    planning, _ = policies
    mapping = {
        **_mapping(),
        "design_element_id": "method:HealthPort.readHealth",
        "design_element_name": "HealthPort.readHealth",
        "planned_code_target": f"{SOURCE_PATH}#readHealth",
    }
    graph = {
        "nodes": [
            {
                "type": "interface",
                "className": "HealthPort",
                "methods": [
                    {
                        "name": "readHealth",
                        "parameters": [{"name": "prefix", "type": "String"}],
                        "returnType": "HealthSnapshot",
                    }
                ],
            }
        ]
    }
    contract = build_implementation_signature_contract(
        planning_policy=planning,
        mappings=[mapping],
        class_graph=graph,
        candidate={
            "entries": [
                {
                    "design_element_id": "method:HealthPort.readHealth",
                    "symbol_kind": "function",
                    "owner_kind": "",
                    "qualified_name": "readHealth",
                    "parameters": [
                        {
                            "name": "prefix",
                            "type": "string",
                            "optional": True,
                            "rest": False,
                        }
                    ],
                    "return_type": "Promise<HealthSnapshot>",
                    "type_parameters": [],
                    "async": True,
                }
            ]
        },
    )
    actual = {
        "kind": "function",
        "name": "readHealth",
        "qualified_name": "readHealth",
        "parameters": [
            {"name": "prefix", "type": "string", "optional": True, "rest": False}
        ],
        "return_type": "Promise<HealthSnapshot>",
        "type_parameters": [],
        "async": True,
        "exported": True,
    }

    assert not implementation_signature_contract_issues(
        contract,
        planning_policy=planning,
        mappings=[mapping],
        class_graph=graph,
    )
    assert deterministic_typescript_contract_findings(
        work_packet={"implementation_signature_contract": contract},
        ast_evidence=_evidence(planning, [actual]),
    ) == []


def test_code_trace_keeps_deterministic_signature_blocker_when_llm_accepts(
    tmp_path,
    policies,
) -> None:
    planning, _ = policies
    test_path = "apps/api/src/health.test.ts"
    for path in (SOURCE_PATH, test_path):
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("export {};\n", encoding="utf-8")
    actual = _actual_symbol()
    actual["return_type"] = "Promise<boolean>"
    packet = {
        "run_id": "run-signature-trace",
        "schema_version": PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
        "implementation_signature_contract": _contract(planning),
        "requirement_contract": {"version": "v2", "revision": 1},
        "canonical_requirements": [{"id": "REQ-1", "content": "Read health."}],
        "requirement_coverage": [
            {
                "requirement_id": "REQ-1",
                "content": "Read health.",
                "design_element_ids": [DESIGN_ID],
                "planned_code_targets": [f"{SOURCE_PATH}#check"],
                "test_intents": ["Verify health."],
            }
        ],
        "design_mappings_by_file": {SOURCE_PATH: [_mapping()]},
        "proposed_files": [{"path": SOURCE_PATH}, {"path": test_path}],
        "test_intents": [{"stage_id": "unit"}],
    }
    policy = build_code_trace_policy(
        planning_policy=planning,
        symbol_extractor_id="typescript_compiler_ast_v1",
    )

    result = CodeTraceValidator(
        _AcceptingLlm(),
        symbol_extractor=_StaticExtractor(actual),
    ).validate(
        target_path=tmp_path,
        work_packet=packet,
        code_trace_policy=policy,
    )

    assert result.ok is False
    assert any(
        item.get("type") == "contract_signature_mismatch"
        for item in result.missing_links
    )
