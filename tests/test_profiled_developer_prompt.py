from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc_harness.agents.developer.execution import _action_prompt, _build_manifest
from sdlc_harness.agents.developer.fake_generation import fake_developer_files
from sdlc_harness.agents.developer.profile_context import developer_generation_context
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.fake import (
    fake_code_trace_review,
    fake_qa_generated_tests,
)
from sdlc_harness.profiles import (
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    implementation_signature_contract_digest,
)


SOURCE_PATH = "apps/api/src/HealthService.ts"
TEST_PATH = "apps/api/src/HealthService.test.ts"
PROFILE = {
    "project_profile_id": "typescript_fullstack_v1",
    "language_profile_id": "typescript_v1",
    "policy_version": "planning_path_policy_v1",
}


def _packet() -> dict[str, object]:
    signature: dict[str, object] = {
        "version": "implementation_signature_contract_v1",
        "profile": PROFILE,
        "entries": [
            {
                "contract_id": "SIG-TEST",
                "design_element_id": "method:HealthService.check",
                "source_path": SOURCE_PATH,
                "symbol_kind": "method",
                "owner_kind": "class",
                "qualified_name": "HealthService.check",
                "parameters": [
                    {
                        "name": "value",
                        "type": "string",
                        "optional": False,
                        "rest": False,
                    }
                ],
                "return_type": "Promise<boolean>",
                "type_parameters": [],
                "async": True,
            }
        ],
    }
    signature["digest"] = implementation_signature_contract_digest(signature)
    return {
        "run_id": "run-profiled-developer",
        "schema_version": PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
        "approved_plan": {"structured_plan_sha256": "sha256:approved"},
        "effective_design_contract": {},
        "requirement_contract": {"version": "v2"},
        "canonical_requirements": [{"id": "REQ-1", "content": "Read health."}],
        "implementation_steps": [
            {"order": 1, "title": "Implement health", "files": [TEST_PATH, SOURCE_PATH]}
        ],
        "proposed_files": [
            {"path": TEST_PATH, "purpose": "Test health"},
            {"path": SOURCE_PATH, "purpose": "Implement health"},
        ],
        "requirement_coverage_by_file": {
            SOURCE_PATH: [{"requirement_id": "REQ-1"}],
            TEST_PATH: [{"requirement_id": "REQ-1"}],
        },
        "design_mappings_by_file": {
            SOURCE_PATH: [
                {
                    "design_element_id": "method:HealthService.check",
                    "planned_code_target": f"{SOURCE_PATH}#check",
                }
            ]
        },
        "implementation_signature_contract": signature,
        "run_workspace": {"base_sha": "abc123"},
        "output_contract": {
            "allowed_paths": [TEST_PATH, SOURCE_PATH],
            "path_policy": {
                "version": "editable_path_policy_v1",
                "project_profile_id": PROFILE["project_profile_id"],
                "language_profile_id": PROFILE["language_profile_id"],
                "planning_policy_version": PROFILE["policy_version"],
                "component_roots": {"api": ["apps/api/src"]},
                "protected_paths": [],
            },
        },
    }


def test_profiled_generation_context_rejects_signature_profile_drift() -> None:
    packet = _packet()
    signature = packet["implementation_signature_contract"]
    assert isinstance(signature, dict)
    signature["profile"] = {
        **PROFILE,
        "language_profile_id": "other_language_v1",
    }
    signature["digest"] = implementation_signature_contract_digest(signature)

    with pytest.raises(HarnessError) as caught:
        developer_generation_context(packet)

    assert caught.value.error_code == "developer_prompt_profile_invalid"


def test_fake_llm_developer_generates_typescript() -> None:
    packet = _packet()
    files = fake_developer_files(packet)
    assert {item["path"] for item in files} == {SOURCE_PATH, TEST_PATH}
    combined = "\n".join(str(item["content"]) for item in files)
    assert "export class HealthService" in combined
    assert "async check(value: string): Promise<boolean>" in combined
    assert "from \"vitest\"" in combined
    assert "package com." not in combined


def test_tool_loop_manifest_binds_profile_signatures_and_orders_tests_last(tmp_path) -> None:
    packet = _packet()
    manifest = _build_manifest(
        run_id="run-profiled-developer",
        work_packet=packet,
        target_path=tmp_path,
        mode="act",
        repair_feedback=None,
        repair_iteration=0,
    )

    assert manifest["version"] == "developer_execution_manifest_v7"
    assert manifest["items"][0]["paths"] == [SOURCE_PATH, TEST_PATH]
    source = manifest["items"][0]["files"][0]
    assert source["approved_signatures"][0]["qualified_name"] == "HealthService.check"
    prompt = _action_prompt(
        item=manifest["items"][0],
        manifest=manifest,
        history=[],
        target_path=tmp_path,
        max_write_chars=12000,
        force_incremental=False,
        action_index=1,
        max_actions=8,
        mutated=False,
        context_window_tokens=32000,
    )
    assert "typescript_prompt_v1" in prompt
    assert "HealthService.check" in prompt


def test_profiled_fake_qa_and_code_trace_have_no_java_paths() -> None:
    qa_prompt = (
        "QA Test Policy:\n"
        + json.dumps(
            {
                **PROFILE,
                "framework": "vitest",
                "components": [
                    {
                        "component_id": "api",
                        "test_root": "apps/api/src",
                        "test_suffix": ".test.ts",
                    }
                ],
            }
        )
        + "\n\nQA Generation Packet:\n"
        + json.dumps({"canonical_requirements": [{"id": "REQ-1"}]})
    )
    qa = json.loads(fake_qa_generated_tests(qa_prompt))
    assert qa["test_files"][0]["path"].endswith(".test.ts")
    assert "vitest" in qa["test_files"][0]["content"]

    trace_prompt = (
        "Code Trace LLM Review v1\nReturn only valid JSON. Do not wrap the JSON in markdown.\n\n"
        + json.dumps(
            {
                "static_trace_mappings": [
                    {
                        "requirement_ref": "REQ-1",
                        "design_element_ids": ["method:HealthService.check"],
                        "code_ref": f"{SOURCE_PATH}#check",
                        "test_ref": TEST_PATH,
                    }
                ]
            }
        )
    )
    trace = json.loads(fake_code_trace_review(trace_prompt))
    assert trace["graph_edges"][0]["code_ref"] == f"{SOURCE_PATH}#check"
    assert ".java" not in json.dumps(trace).lower()
