"""Kiểm contract directive ngôn ngữ và prompt đang hoạt động ở Phase 3."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from sdlc_harness.agents.developer.planner import _developer_plan_prompt
from sdlc_harness.agents.validator.plan_review import (
    ImplementationPlanReview,
    _llm_review_prompt,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import fake_completion
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_profile_snapshot,
    get_language_profile,
    get_project_profile,
    get_prompt_directive,
    prompt_directive_from_snapshot,
    resolve_language_prompt_directive,
)


_JAVA_ASSUMPTIONS = (".java", "src/main/java", "src/test/java", "maven", "gradle", "junit")


def _typescript_prompt_context():
    language = get_language_profile("typescript_v1")
    project = get_project_profile("typescript_fullstack_v1")
    return (
        build_planning_path_policy(project=project, language=language),
        resolve_language_prompt_directive(language),
    )


def _developer_prompt(*, repair_feedback: str = "") -> str:
    policy, directive = _typescript_prompt_context()
    return _developer_plan_prompt(
        run_id="run_prompt_contract",
        source_ref="jira_issue:SDLC-1",
        issue_key="SDLC-1",
        requirement_packet="Delete one approved record.",
        design_artifact="Approved service and repository design.",
        requirement_design_trace={},
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        repo_context={},
        base_mappings=[],
        base_test_intents=[],
        target_repo_path="target",
        target_repo_remote="origin",
        target_base_branch="main",
        planned_branch="feature/run_prompt_contract",
        target_files=[],
        test_command="verification-stage:unit",
        repair_feedback=repair_feedback,
        previous_review={"ok": False} if repair_feedback else {},
        canonical_requirement=None,
        planning_policy=policy,
        prompt_directive=directive,
    )


def test_prompt_directive_registry_resolves_snapshot_and_fails_closed() -> None:
    directive = get_prompt_directive("typescript_prompt_v1")
    snapshot = build_profile_snapshot(
        project_profile_id="typescript_fullstack_v1",
        target_remote="https://example.invalid/target.git",
        target_base_branch="main",
    )

    assert prompt_directive_from_snapshot(snapshot) == directive

    with pytest.raises(HarnessError) as unknown:
        get_prompt_directive("unknown_prompt_v1")
    assert unknown.value.error_code == "prompt_directive_not_supported"

    wrong_language = replace(get_language_profile("typescript_v1"), profile_id="other_language_v1")
    with pytest.raises(HarnessError) as mismatch:
        resolve_language_prompt_directive(wrong_language)
    assert mismatch.value.error_code == "prompt_directive_mismatch"


@pytest.mark.parametrize("repair_feedback", ("", "Keep the repair inside the approved targets."))
def test_active_typescript_developer_prompt_has_no_java_assumptions(repair_feedback: str) -> None:
    prompt = _developer_prompt(repair_feedback=repair_feedback)
    lowered = prompt.lower()

    assert '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    assert "TypeScript" in prompt
    assert "apps/web/src/Example.tsx" in prompt
    assert "apps/web/src/Example.test.tsx" in prompt
    assert all(token not in lowered for token in _JAVA_ASSUMPTIONS)


def test_active_typescript_validator_prompt_uses_same_directive() -> None:
    policy, directive = _typescript_prompt_context()
    prompt = _llm_review_prompt(
        structured_plan={"planning_profile": policy.metadata(), "design_mappings": []},
        requirement_packet="Delete one approved record.",
        design_artifact="Approved service and repository design.",
        requirement_design_trace={},
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        rule_review=ImplementationPlanReview(ok=True, mode="implementation_plan_rule_review_v2"),
        planning_policy=policy,
        prompt_directive=directive,
    )
    lowered = prompt.lower()

    assert '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    assert "TypeScript" in prompt
    assert all(token not in lowered for token in _JAVA_ASSUMPTIONS)


def test_fake_developer_plan_follows_language_directive() -> None:
    payload = json.loads(fake_completion(_developer_prompt()))
    encoded = json.dumps(payload, ensure_ascii=False).lower()

    assert ".ts" in encoded
    assert ".java" not in encoded
    assert "src/main/java" not in encoded
    assert "src/test/java" not in encoded
