from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from sdlc_harness.agents.designer.uiux import (
    UIUX_CONTRACT_VERSION,
    UiUxGuidanceProvider,
    guidance_evidence_ref,
)
from sdlc_harness.agents.designer import (
    GRAPH_VERSION,
    DesignerToolLoopExecutor,
)
from sdlc_harness.agents.designer.projections import render_design_projections
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.repo_context.codegraph import (
    CODEGRAPH_SNAPSHOT_VERSION,
    CodeGraphContextProvider,
    _validated_snapshot,
)
from sdlc_harness.storage.runtime import RuntimeStore


def test_fake_uiux_search_is_bounded_and_has_stable_evidence() -> None:
    provider = UiUxGuidanceProvider(skill_root=None, fake_mode=True)

    result = provider.search(
        {"query": "confirmation focus feedback", "mode": "domain", "domain": "ux"}
    )

    assert result["schema_version"] == UIUX_CONTRACT_VERSION
    assert result["count"] == 1
    assert result["no_match"] is False
    assert guidance_evidence_ref(result) == guidance_evidence_ref(dict(result))
    assert guidance_evidence_ref(result).startswith("uiux:sha256:")


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "apps web private component", "mode": "domain", "domain": "color"},
        {"query": "C:/private/project", "mode": "domain", "domain": "ux"},
        {"query": "focus", "mode": "stack", "stack": "react"},
    ],
)
def test_uiux_search_rejects_unbounded_or_unsupported_input(arguments: dict[str, str]) -> None:
    provider = UiUxGuidanceProvider(skill_root=None, fake_mode=True)

    with pytest.raises(HarnessError) as error:
        provider.search(arguments)

    assert error.value.error_code == "designer_tool_input_invalid"


def test_fake_codegraph_prepare_writes_fresh_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        provider = CodeGraphContextProvider(fake_mode=True)

        prepared = provider.prepare_index(target)

        snapshot = prepared["snapshot"]
        assert prepared["codegraph_initialized"] is True
        assert snapshot["schema_version"] == CODEGRAPH_SNAPSHOT_VERSION
        assert snapshot["status"] == "fresh"
        assert str(snapshot["artifact_digest"]).startswith("sha256:")


def test_codegraph_snapshot_fails_closed_when_pending_changes_remain() -> None:
    status = {
        "initialized": True,
        "version": "1.5.0",
        "fileCount": 2,
        "nodeCount": 3,
        "edgeCount": 4,
        "languages": ["python"],
        "pendingChanges": {"added": 0, "modified": 1, "removed": 0},
        "worktreeMismatch": None,
        "index": {
            "state": "complete",
            "reindexRecommended": False,
            "currentExtractionVersion": 24,
        },
    }

    with pytest.raises(HarnessError) as error:
        _validated_snapshot(status)

    assert error.value.error_code == "target_repo_codegraph_stale"


def test_fake_designer_enforces_ui_experience_and_human_projection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        source = target / "apps" / "web" / "src" / "App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export function App() { return null; }\n", encoding="utf-8")
        settings = load_settings(
            root=root,
            runtime_root=root / ".runtime",
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_UIUX_SKILL_MODE": "enforce",
            },
        )
        requirement = {
            "use_case_id": "UC-UI",
            "scenario_contract": {
                "scenarios": [
                    {
                        "id": "SC-1",
                        "requirement_refs": ["MAIN-1"],
                        "surfaces": {
                            "ui": {"status": "required", "requirement_refs": ["MAIN-1"]}
                        },
                    }
                ]
            },
        }
        landscape = {
            "artifact_digest": "sha256:repo",
            "components": [
                {
                    "component_id": "web",
                    "roots": ["apps/web"],
                    "file_refs": ["apps/web/src/App.tsx"],
                }
            ],
            "anchors": [
                {
                    "anchor_id": "RLA-app",
                    "kind": "ui_component",
                    "component_id": "web",
                    "path": "apps/web/src/App.tsx",
                    "symbol": "App",
                }
            ],
            "scenario_matches": [
                {
                    "scenario_id": "SC-1",
                    "surface": "ui",
                    "candidate_anchor_refs": ["RLA-app"],
                }
            ],
        }
        binding = {
            "requirement_digest": "sha256:req",
            "repo_landscape_digest": "sha256:repo",
            "workspace_ref": "run_workspace_v1",
            "base_sha": "abc123",
            "designer_uiux_mode": "enforce",
            "designer_uiux_contract_version": UIUX_CONTRACT_VERSION,
        }

        result = DesignerToolLoopExecutor(
            settings=settings,
            store=RuntimeStore(repo_root=root, runtime_root=root / ".runtime"),
            llm_client=None,
        ).execute(
            run_id="run-uiux",
            requirement_model=requirement,
            landscape=landscape,
            target_path=target,
            snapshot_binding=binding,
            contract_version=GRAPH_VERSION,
            uiux_mode="enforce",
            uiux_contract_version=UIUX_CONTRACT_VERSION,
        )

        ui_node = next(node for node in result.graph["nodes"] if node["surface"] == "ui")
        experience = ui_node["surface_contract"]["experience_contract"]
        assert experience["schema_version"] == "ui_experience_contract_v1"
        assert len(experience["guidance_refs"]) == 2
        markdown = render_design_projections(result.graph)["design_markdown"]
        assert "## UI Experience" in markdown
        assert "Accessibility:" in markdown
        session = json.loads((root / result.session_path).read_text(encoding="utf-8"))
        guidance = session["guidance_observations"]["SC-1|web"]
        assert all("results" not in item and str(item["results_digest"]).startswith("sha256:") for item in guidance)
