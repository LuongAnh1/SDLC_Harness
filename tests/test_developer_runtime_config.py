from pathlib import Path

import pytest

from sdlc_harness.agents.developer.execution import (
    _action_prompt,
    _unit_mandatory_obligations,
)
from sdlc_harness.agents.developer.runtime_config import (
    materialize_env_example,
    runtime_config_execution_issues,
    runtime_config_source_issues,
)
from sdlc_harness.core.errors import HarnessError


CHANGE = {
    "kind": "env_var",
    "unit_id": "WU-API",
    "name": "ETOUR_BASE_URL",
    "default": "http://127.0.0.1:8090",
    "read_by": "apps/api/src/cultural-heritage-routes.ts",
    "purpose": "Configure the external ETOUR endpoint.",
}


def _write_source(root: Path, content: str) -> None:
    target = root / str(CHANGE["read_by"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_runtime_config_source_contract_and_prompt_projection(tmp_path: Path) -> None:
    _write_source(
        tmp_path,
        'const baseUrl = process.env.ETOUR_BASE_URL ?? "http://127.0.0.1:8090";\n',
    )

    assert runtime_config_source_issues(repo_root=tmp_path, changes=[CHANGE]) == []
    obligations = _unit_mandatory_obligations(
        {"runtime_config_changes": [CHANGE]}
    )
    assert obligations["runtime_configuration"] == [CHANGE]


def test_recovery_prompt_reuses_tests_without_forcing_artificial_mutation(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "apps/api/src/cultural-heritage.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test('existing coverage', () => {});\n", encoding="utf-8")

    prompt = _action_prompt(
        item={
            "id": "WU-API",
            "paths": ["apps/api/src/cultural-heritage.test.ts"],
            "test_intents": [{"stage_id": "unit", "intent": "Existing coverage."}],
            "test_target_paths": ["apps/api/src/cultural-heritage.test.ts"],
            "required_stage_ids": ["unit"],
            "test_intent_mutation_required": False,
        },
        manifest={
            "version": "developer_execution_manifest_v8",
            "mode": "act",
            "base_sha": "base",
        },
        history=[],
        target_path=tmp_path,
        max_write_chars=18000,
        force_incremental=False,
        action_index=1,
        max_actions=24,
        mutated=False,
        context_window_tokens=128000,
    )

    assert "Do not mutate tests merely to recreate coverage evidence" in prompt
    assert (
        "every listed test_target_path must be mutated in this unit" not in prompt
    )


def test_runtime_config_source_contract_reports_missing_read_and_default(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "export const baseUrl = 'http://127.0.0.1:4100';\n")

    codes = {
        item["error_code"]
        for item in runtime_config_source_issues(repo_root=tmp_path, changes=[CHANGE])
    }

    assert codes == {
        "runtime_config_env_read_missing",
        "runtime_config_default_missing",
    }


def test_materializer_creates_and_validates_env_example(tmp_path: Path) -> None:
    _write_source(
        tmp_path,
        'const baseUrl = process.env.ETOUR_BASE_URL ?? "http://127.0.0.1:8090";\n',
    )

    receipt = materialize_env_example(
        repo_root=tmp_path,
        changes=[CHANGE],
        run_id="run_runtime_config",
    )

    assert receipt["status"] == "materialized"
    assert receipt["changed_paths"] == [".env.example"]
    assert (tmp_path / ".env.example").read_text(encoding="utf-8") == (
        "ETOUR_BASE_URL=http://127.0.0.1:8090\n"
    )
    assert runtime_config_execution_issues(repo_root=tmp_path, changes=[CHANGE]) == []


def test_materializer_updates_only_owned_declaration_then_becomes_noop(
    tmp_path: Path,
) -> None:
    env_example = tmp_path / ".env.example"
    env_example.write_text(
        "# local database\nDATABASE_URL=postgres://localhost/demo\n"
        "ETOUR_BASE_URL=http://127.0.0.1:4100\n",
        encoding="utf-8",
    )

    updated = materialize_env_example(
        repo_root=tmp_path,
        changes=[CHANGE],
        run_id="run_runtime_config",
    )
    unchanged = materialize_env_example(
        repo_root=tmp_path,
        changes=[CHANGE],
        run_id="run_runtime_config",
    )

    assert updated["operations"][0]["operation"] == "updated"
    assert unchanged["status"] == "unchanged"
    assert unchanged["operations"][0]["operation"] == "noop"
    assert env_example.read_text(encoding="utf-8") == (
        "# local database\nDATABASE_URL=postgres://localhost/demo\n"
        "ETOUR_BASE_URL=http://127.0.0.1:8090\n"
    )


def test_materializer_fails_closed_on_duplicate_declaration(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "ETOUR_BASE_URL=http://one\nETOUR_BASE_URL=http://two\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError) as context:
        materialize_env_example(
            repo_root=tmp_path,
            changes=[CHANGE],
            run_id="run_runtime_config",
        )

    assert context.value.error_code == "runtime_config_env_example_duplicate"
