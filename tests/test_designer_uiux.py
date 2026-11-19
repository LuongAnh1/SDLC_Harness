from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

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
from sdlc_harness.agents.designer.tools import DesignerToolRegistry
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


def _uiux_registry() -> DesignerToolRegistry:
    anchors = [
        {"anchor_id": "RLA-1", "component_id": "web", "path": "apps/web/src/types/article.ts", "symbol": "Article"},
        {"anchor_id": "RLA-2", "component_id": "web", "path": "apps/web/src/state/store.ts", "symbol": "State"},
        {
            "anchor_id": "RLA-3",
            "component_id": "web",
            "path": "apps/web/src/components/Pages/ArticlePage/ArticlePage.tsx",
            "symbol": "OwnerArticleMeta",
        },
        {"anchor_id": "RLA-4", "component_id": "web", "path": "apps/web/src/article-feed.ts", "symbol": "getFeed"},
    ]
    return DesignerToolRegistry(
        run_id="run-1",
        target_path=Path("."),
        landscape={"components": [], "anchors": anchors},
        graph={"nodes": []},
        fake_mode=True,
        uiux_provider=UiUxGuidanceProvider(skill_root=None, fake_mode=True),
    )


def test_uiux_search_allows_plain_words_that_name_repository_files() -> None:
    output, refs = _uiux_registry().execute(
        "uiux.search_guidance",
        {"query": "article owner edit delete state", "mode": "domain", "domain": "ux"},
    )

    assert output["no_match"] is False
    assert refs and refs[0].startswith("uiux:sha256:")


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("articlepage edit actions", "ArticlePage"),
        ("OwnerArticleMeta getfeed layout", "OwnerArticleMeta, getFeed"),
        ("article-feed loading feedback", "article-feed"),
    ],
)
def test_uiux_search_rejects_compound_repository_names_and_names_them(query: str, names: str) -> None:
    with pytest.raises(HarnessError) as error:
        _uiux_registry().execute("uiux.search_guidance", {"query": query, "mode": "domain", "domain": "ux"})

    assert error.value.error_code == "designer_tool_input_invalid"
    assert f"must not contain repository names: {names}." in error.value.message


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


def test_codegraph_sync_if_stale_refreshes_pending_changes() -> None:
    stale = {
        "initialized": True,
        "version": "1.5.0",
        "fileCount": 2,
        "nodeCount": 3,
        "edgeCount": 4,
        "languages": ["typescript"],
        "pendingChanges": {"added": 0, "modified": 1, "removed": 0},
        "worktreeMismatch": None,
        "index": {
            "state": "complete",
            "reindexRecommended": False,
            "currentExtractionVersion": 24,
        },
    }
    fresh = json.loads(json.dumps(stale))
    fresh["pendingChanges"]["modified"] = 0
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / ".codegraph").mkdir()
        provider = CodeGraphContextProvider()
        with (
            patch(
                "sdlc_harness.repo_context.codegraph._run_codegraph_status",
                side_effect=[stale, fresh],
            ),
            patch("sdlc_harness.repo_context.codegraph._run_codegraph_sync") as sync,
        ):
            result = provider.sync_if_stale(target)

    assert result["synced"] is True
    assert result["snapshot"]["status"] == "fresh"
    sync.assert_called_once()


def test_codegraph_affected_tests_uses_bounded_structured_query() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / ".codegraph").mkdir()
        provider = CodeGraphContextProvider()
        with patch(
            "sdlc_harness.repo_context.codegraph._run_codegraph_json",
            return_value={"tests": ["src/Main.test.ts"]},
        ) as run:
            result = provider.query_capability(
                target,
                operation="affected_tests",
                files=["src/Main.ts"],
                depth=99,
            )

    assert result == {"tests": ["src/Main.test.ts"]}
    assert run.call_args.kwargs["command"] == [
        "affected",
        "src/Main.ts",
        "--depth",
        "5",
        "--json",
    ]


def test_codegraph_symbols_file_uses_bounded_node_capability() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / ".codegraph").mkdir()
        provider = CodeGraphContextProvider()
        with patch(
            "sdlc_harness.repo_context.codegraph._run_codegraph_command",
            return_value="symbol map",
        ) as run:
            result = provider.query_capability(
                target,
                operation="symbols",
                file="apps/web/src/View.tsx",
                limit=80,
            )

    assert result == {
        "operation": "symbols",
        "resolved_operation": "node",
        "evidence": "symbol map",
        "truncated": False,
    }
    assert run.call_args.kwargs["arguments"] == [
        "node",
        "--file",
        "apps/web/src/View.tsx",
        "--symbols-only",
        "--limit",
        "80",
    ]


