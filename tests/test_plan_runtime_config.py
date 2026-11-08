"""Runtime configuration planning (L5.1.8 M2): repo facts, Planner decision checks, Phase 4 gate."""

from __future__ import annotations

import json
from pathlib import Path

from sdlc_harness.agents.developer.planner import _component_plan_prompt, _runtime_config_slot
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.profiles.runtime_config import (
    canonical_runtime_config_impact,
    runtime_config_impact_issues,
    unsupported_runtime_config_changes,
)
from sdlc_harness.repo_context.runtime_config import collect_repo_runtime_config


def _policy():
    project = get_project_profile("typescript_fullstack_v1")
    return build_planning_path_policy(project=project, language=get_language_profile(project.language_profile_id))


def _repo(tmp_path: Path) -> dict[str, object]:
    (tmp_path / "apps/api/src").mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "version": "0.1.0", "devDependencies": {"vitest": "1"}})
    )
    (tmp_path / "apps/api/package.json").write_text(
        json.dumps({"name": "@t/api", "version": "0.2.0", "dependencies": {"fastify": "5"}})
    )
    (tmp_path / "compose.yaml").write_text(
        "services:\n  postgres:\n    image: postgres:17\n    ports:\n      - '5432:5432'\nvolumes:\n  data:\n"
    )
    (tmp_path / "apps/api/src/server.ts").write_text(
        "const port = process.env.API_PORT ?? '3001';\nconst a = process.env['WEB_ORIGIN'];\n"
    )
    (tmp_path / ".env.example").write_text("API_PORT=3001\n# comment\n")
    return collect_repo_runtime_config(target_path=tmp_path, source_files=["apps/api/src/server.ts"])


def _plan(impact: dict[str, object]) -> dict[str, object]:
    return {
        "planning_profile": {"project_profile_id": "typescript_fullstack_v1"},
        "work_units": [
            {
                "unit_id": "WU-API",
                "planned_code_targets": [{"path": "apps/api/src/routes.ts", "symbol": "register"}],
            }
        ],
        "runtime_config_impact": impact,
    }


def _env_change(**overrides: object) -> dict[str, object]:
    return {
        "kind": "env_var",
        "unit_id": "WU-API",
        "name": "EXTERNAL_BASE_URL",
        "default": "http://127.0.0.1:4010",
        "read_by": "apps/api/src/routes.ts",
        "purpose": "Endpoint of the external system differs per environment.",
        **overrides,
    }


def test_collects_manifests_services_env_reads_and_env_example(tmp_path: Path) -> None:
    facts = _repo(tmp_path)

    assert [item["manifest"] for item in facts["workspaces"]] == ["package.json", "apps/api/package.json"]
    assert [item["version"] for item in facts["workspaces"]] == ["0.1.0", "0.2.0"]
    assert facts["workspaces"][1]["dependencies"] == ["fastify"]
    assert facts["compose_services"] == [{"service": "postgres", "image": "postgres:17"}]
    assert [item["name"] for item in facts["env_reads"]] == ["API_PORT", "WEB_ORIGIN"]
    assert facts["env_example"] == {"present": True, "names": ["API_PORT"]}


def test_valid_decisions_pass_and_shape_errors_are_reported(tmp_path: Path) -> None:
    facts, policy = _repo(tmp_path), _policy()

    def issues(impact: dict[str, object]) -> list[str]:
        return runtime_config_impact_issues(
            structured_plan=_plan(impact), repo_runtime_config=facts, planning_policy=policy
        )

    assert issues({"status": "required", "reason": "Outbound endpoint.", "changes": [_env_change()]}) == []
    assert issues({"status": "not_required", "reason": "No environment-specific value.", "changes": []}) == []
    assert issues({}) == ["Plan must decide runtime_config_impact: `required` or `not_required` with a reason."]
    assert any(
        "lists changes" in item
        for item in issues({"status": "not_required", "reason": "x", "changes": [_env_change()]})
    )
    assert any("lists no change" in item for item in issues({"status": "required", "reason": "x", "changes": []}))

    bad = issues(
        {
            "status": "required",
            "reason": "x",
            "changes": [
                _env_change(name="API_PORT"),
                _env_change(name="lower", read_by="apps/api/src/other.ts", default=""),
                {
                    "kind": "npm_package",
                    "unit_id": "WU-API",
                    "manifest": "apps/api/package.json",
                    "package": "fastify",
                    "version": "^5.0.0",
                    "used_by": "apps/api/src/routes.ts",
                    "purpose": "x",
                },
                {
                    "kind": "compose_service",
                    "unit_id": "WU-API",
                    "service": "postgres",
                    "image": "mock:latest",
                    "healthcheck": "",
                    "purpose": "x",
                },
                {"kind": "shell_script", "unit_id": "WU-API"},
            ],
        }
    )
    expected = [
        "already read by the repository",
        "must match [A-Z][A-Z0-9_]*",
        "needs a non-empty default",
        "must be a source target of unit",
        "already declared in `apps/api/package.json`",
        "needs an exact version",
        "already exists in compose.yaml",
        "pinned tag other than `latest`",
        "needs a healthcheck command",
        "unsupported kind `shell_script`",
    ]
    for fragment in expected:
        assert any(fragment in item for item in bad), fragment


def test_canonical_decision_keeps_only_typed_fields() -> None:
    decision = canonical_runtime_config_impact(
        {
            "status": "required",
            "reason": "x",
            "changes": [{**_env_change(), "command": "rm -rf /", "dev": True}],
            "extra": 1,
        }
    )
    assert set(decision) == {"status", "reason", "changes"}
    assert set(decision["changes"][0]) == {"kind", "unit_id", "name", "default", "read_by", "purpose"}


def test_phase4_gate_lists_only_kinds_it_cannot_execute() -> None:
    plan = _plan(
        {
            "status": "required",
            "reason": "x",
            "changes": [_env_change(), {"kind": "npm_package", "unit_id": "WU-API", "package": "undici"}],
        }
    )
    assert [item["kind"] for item in unsupported_runtime_config_changes(plan)] == ["npm_package"]
    assert unsupported_runtime_config_changes({"runtime_config_impact": plan["runtime_config_impact"]}) == []


def test_slot_exists_only_with_repo_facts_and_repair_keeps_previous_decision() -> None:
    previous = {"status": "not_required", "reason": "Nothing differs per environment.", "changes": []}
    assert _runtime_config_slot({}, {"status": "required"}) == {}
    assert _runtime_config_slot({"repo_runtime_config": {}}, {}, previous=previous) == {
        "runtime_config_impact": previous
    }
    assert _runtime_config_slot({"repo_runtime_config": {}}, {})["runtime_config_impact"]["status"] == ""


def test_planner_prompt_carries_runtime_config_facts_and_rules() -> None:
    project = get_project_profile("typescript_fullstack_v1")
    prompt = _component_plan_prompt(
        run_id="run_runtime_config",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=[],
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=_policy(),
        prompt_directive=None,
        verification_policy=build_verification_stage_policy(project=project),
        signature_targets=[],
        repair_feedback="",
        previous_review={},
        runtime_config={"repository": {"env_reads": [{"name": "UNIQUE_ENV_READ"}]}, "policy": {}},
    )
    assert "UNIQUE_ENV_READ" in prompt
    assert "Runtime configuration rules:" in prompt
    assert '"runtime_config_impact"' in prompt
    assert "includes sibling workspace packages" in prompt
    assert "not listed in the importing" in prompt
    assert "perform dependency closure" in prompt
    assert "Optional or defaulted injection still needs" in prompt
    assert "name every cross-workspace package check" in prompt
    assert '"kind": "npm_package"' in prompt


def test_repair_prompt_context_carries_the_current_decision() -> None:
    from sdlc_harness.agents.developer.planner import _runtime_config_prompt

    decision = {"status": "required", "reason": "x", "changes": [_env_change()]}
    context = _runtime_config_prompt({"repo_runtime_config": {"env_reads": []}}, _policy(), current_decision=decision)
    assert context["current_decision"]["changes"][0]["name"] == "EXTERNAL_BASE_URL"
    assert "current_decision" not in _runtime_config_prompt({"repo_runtime_config": {}}, _policy())
