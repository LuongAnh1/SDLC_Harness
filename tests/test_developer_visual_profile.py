from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from sdlc_harness.agents.designer.uiux import UiUxGuidanceProvider
from sdlc_harness.agents.developer.execution import (
    _action_policy_error,
    _action_prompt,
    _execute_action,
    _pinned_visual_profile_action_reserve,
    _remember_developer_visual_candidate,
    developer_execution_retry_allowed,
)
from sdlc_harness.agents.developer.visual_profile import (
    app_visual_scope_id,
    normalize_visual_candidate,
    read_visual_profiles,
    visual_profile_required,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.repo_context import CodeGraphContextProvider


def _ui_item(*, policy: str = "new_stylesheet", screen_id: str = "records") -> dict[str, object]:
    return {
        "id": f"WU-{screen_id}",
        "paths": ["apps/web/src/Records.tsx", "apps/web/src/records.css"],
        "required_stage_ids": [],
        "surfaces": ["ui"],
        "design_node_ids": ["D-UI-1"],
        "surface_contracts": [
            {
                "design_node_id": "D-UI-1",
                "surface": "ui",
                "surface_contract": {
                    "runtime_surface": {
                        "screen_id": screen_id,
                        "entrypoint": "apps/web/src/main.tsx#Root",
                    },
                    "experience_contract": {
                        "schema_version": "ui_experience_contract_v2",
                        "visual_contract": {"style_target_policy": policy},
                    },
                },
            }
        ],
    }


def _execute_profile_action(
    action: dict[str, object],
    *,
    item: dict[str, object],
    journal: dict[str, object],
    profile_file: Path,
) -> dict[str, object]:
    return _execute_action(
        action=action,
        run_id="run-visual",
        work_packet={},
        repo_root=profile_file.parent,
        item_paths=[str(path) for path in item["paths"]],
        item=item,
        session_dir=profile_file.parent,
        max_write_chars=18_000,
        repo_context_provider=CodeGraphContextProvider(fake_mode=True),
        verification_runner=SimpleNamespace(),
        verification_policy=None,
        uiux_provider=UiUxGuidanceProvider(skill_root=None, fake_mode=True),
        journal=journal,
        visual_profile_file=profile_file,
        visual_app_scope_id=app_visual_scope_id(item),
    )


def test_visual_profile_action_reserve_stays_pinned_after_resume() -> None:
    committed_profile = {"candidate_id": "VP-1"}
    history = [{"action": "uiux.commit_visual_profile"}]

    assert _pinned_visual_profile_action_reserve(
        working_state={},
        history=history,
        visual_required=True,
        app_visual_profile=committed_profile,
    ) == 3
    assert _pinned_visual_profile_action_reserve(
        working_state={"visual_profile_action_reserve": 3},
        history=[],
        visual_required=True,
        app_visual_profile=committed_profile,
    ) == 3
    assert _pinned_visual_profile_action_reserve(
        working_state={},
        history=[],
        visual_required=True,
        app_visual_profile=committed_profile,
    ) == 0


def test_fake_design_system_is_bounded_and_normalizes_without_remote_assets() -> None:
    provider = UiUxGuidanceProvider(skill_root=None, fake_mode=True)
    source = provider.design_system({"query": "professional records admin"})

    candidate = normalize_visual_candidate(
        source,
        app_scope_id="web:apps/web",
        skill_fingerprint="sha256:test",
        design_refs=["D-UI-1"],
    )

    assert candidate["schema_version"] == "developer_visual_profile_v1"
    assert candidate["app_scope_id"] == "web:apps/web"
    assert candidate["typography"]["remote_font_import_allowed"] is False
    assert "google_fonts_url" not in json.dumps(candidate)
    assert "GSAP" not in json.dumps(candidate)
    assert all(row["ratio"] >= row["minimum"] for row in candidate["contrast_checks"])


def test_visual_candidate_rejects_mechanically_bad_contrast() -> None:
    source = UiUxGuidanceProvider(skill_root=None, fake_mode=True).design_system(
        {"query": "professional records admin"}
    )
    source["design_system"]["colors"]["foreground"] = "#F8FAFC"

    with pytest.raises(HarnessError) as error:
        normalize_visual_candidate(
            source,
            app_scope_id="web:apps/web",
            skill_fingerprint="sha256:test",
            design_refs=[],
        )

    assert error.value.error_code == "developer_visual_profile_invalid"


def test_new_stylesheet_blocks_repo_work_until_app_profile_is_committed_and_reused() -> None:
    item = _ui_item()
    capability = {
        "status": "available",
        "optional": True,
        "visual_profile_mode": "enforce",
    }
    assert visual_profile_required(item, capability)
    blocked = _action_policy_error(
        action={"action": "repo.read", "arguments": {"path": "apps/web/src/Records.tsx"}},
        history=[],
        seen_observations=set(),
        mutated=False,
        action_index=1,
        max_actions=27,
        uiux_available=True,
        visual_profile_required=True,
        visual_profile_committed=False,
    )
    assert blocked["error"] == "developer_visual_profile_required"

    with tempfile.TemporaryDirectory() as directory:
        profile_file = Path(directory) / "ui_visual_profiles_v1.json"
        journal: dict[str, object] = {}
        search = _execute_profile_action(
            {
                "action": "uiux.search_visual_profile",
                "arguments": {"query": "professional records admin"},
            },
            item=item,
            journal=journal,
            profile_file=profile_file,
        )
        assert search["ok"] is True
        candidate = search["candidate"]
        _remember_developer_visual_candidate(
            journal=journal,
            app_scope_id="web:apps/web",
            candidate=candidate,
        )
        commit = _execute_profile_action(
            {
                "action": "uiux.commit_visual_profile",
                "arguments": {
                    "candidate_id": candidate["candidate_id"],
                    "rationale": "Fits the approved professional application shell.",
                },
            },
            item=item,
            journal=journal,
            profile_file=profile_file,
        )
        assert commit["ok"] is True
        profile = read_visual_profiles(profile_file)["profiles"]["web:apps/web"]

        second_item = _ui_item(screen_id="record-detail")
        assert app_visual_scope_id(second_item) == "web:apps/web"
        prompt = _action_prompt(
            item=second_item,
            manifest={"version": "developer_execution_manifest_v8", "mode": "act", "base_sha": "base"},
            history=[],
            target_path=Path(directory),
            max_write_chars=18_000,
            force_incremental=False,
            action_index=1,
            max_actions=24,
            mutated=False,
            context_window_tokens=128_000,
            app_visual_profile=profile,
            visual_app_scope_id="web:apps/web",
        )
        context = json.loads(prompt.split("Execution context:\n", 1)[1])
        assert context["app_visual_profile"]["status"] == "committed"
        assert context["app_visual_profile"]["profile"]["candidate_id"] == candidate["candidate_id"]
        assert "uiux.search_visual_profile" not in prompt.split("Available actions:\n", 1)[1]


def test_candidate_budget_and_existing_style_or_off_mode_compatibility() -> None:
    item = _ui_item(policy="reuse_existing")
    assert not visual_profile_required(
        item,
        {"status": "available", "visual_profile_mode": "enforce"},
    )
    assert not visual_profile_required(
        _ui_item(),
        {"status": "available", "visual_profile_mode": "off"},
    )
    rejected = _action_policy_error(
        action={"action": "uiux.search_visual_profile", "arguments": {"query": "minimal admin records"}},
        history=[],
        seen_observations=set(),
        mutated=False,
        action_index=3,
        max_actions=27,
        uiux_available=True,
        visual_profile_required=True,
        visual_profile_committed=False,
        visual_profile_candidate_count=2,
    )
    assert rejected["error"] == "developer_visual_profile_invalid"


def test_visual_profile_failure_is_same_session_retriable() -> None:
    state = {
        "status": "blocked",
        "current_phase": "implementation_writing",
        "last_error": {"error_code": "developer_visual_profile_unavailable"},
        "developer_execution": {"mode": "developer_act"},
        "latest_artifacts": {
            "developer_work_packet": "packet.json",
            "developer_execution_manifest": "manifest.json",
            "developer_execution_journal": "journal.json",
        },
    }
    assert developer_execution_retry_allowed(state)
