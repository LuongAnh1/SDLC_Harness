"""The integration agent's e2e session with a fake runner and a fake service process (no Docker, no browser)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_verification_stage_policy, get_project_profile
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows import hoc_e2e as e2e

RUN = "run_20261128_120000_00000003"
POLICY = build_verification_stage_policy(project=get_project_profile("typescript_fullstack_v1"))


@pytest.fixture
def session(tmp_path, monkeypatch):
    store = RuntimeStore(repo_root=tmp_path, runtime_root=tmp_path / "runtime")
    root = tmp_path / "h"
    root.mkdir()
    seen = {"stages": [], "processes": [], "prepared": "passed", "boots": True}

    class Runner:
        def run_harness_stages(self, *, target_path, run_id, verification_policy, stage_ids):
            assert target_path == root and run_id == RUN
            seen["stages"].append(stage_ids)
            return SimpleNamespace(status=seen["prepared"], reason="prepare failed", to_dict=lambda: {})

    class Process:
        def __init__(self, argv, *, cwd, stdout, stderr, env):
            self.env = env
            seen["processes"].append((argv, cwd, env))
            if seen["boots"]:
                Path(env["SDLC_E2E_READY"]).write_text("ready", encoding="utf-8")

        def poll(self):
            return None if seen["boots"] else 1

        def wait(self, timeout):
            assert Path(self.env["SDLC_E2E_STOP"]).exists()
            return 0

    monkeypatch.setattr(e2e, "VerificationRunner", Runner)
    monkeypatch.setattr(e2e, "sandbox_browsers", lambda runtime_root: tmp_path / "browsers")
    monkeypatch.setattr(e2e.shutil, "which", lambda name: "npx-fake")
    monkeypatch.setattr(e2e.subprocess, "Popen", Process)
    return store, root, seen, tmp_path


def test_session_prepares_boots_the_repository_servers_and_cleans_up(session):
    store, root, seen, tmp_path = session
    with e2e.integration_e2e_session(root, store=store, run_id=RUN, verification_policy=POLICY) as env:
        config = (root / e2e.AGENT_CONFIG).read_text(encoding="utf-8")
        assert 'import base from "../../playwright.config"' in config and "webServer: undefined" in config
        assert env["SDLC_E2E_CONFIG"] == e2e.AGENT_CONFIG and env["CI"] == "1"
        assert env["DATABASE_URL"].startswith("postgresql://") and env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "browsers")
    assert seen["stages"] == [("database_ready", "migrate_and_seed", "frontend_build")]
    argv, cwd, process_env = seen["processes"][0]
    assert argv == ["npx-fake", "playwright", "test", "--config", "docs/sdlc-local/playwright.hold.config.ts"]
    assert cwd == root and process_env["DATABASE_URL"] == env["DATABASE_URL"]
    assert list((root / "docs/sdlc-local").iterdir()) == []
    assert e2e.e2e_command().endswith("playwright test <spec files> --config " + e2e.AGENT_CONFIG)


@pytest.mark.parametrize(("prepared", "boots", "message"), [
    ("failed", True, "Preparing the database and web build failed"),
    ("passed", False, "The services did not start"),
])
def test_session_fails_closed_and_leaves_nothing_staged(session, prepared, boots, message):
    store, root, seen, _ = session
    seen.update(prepared=prepared, boots=boots)
    with pytest.raises(HarnessError, match=message) as error:
        with e2e.integration_e2e_session(root, store=store, run_id=RUN, verification_policy=POLICY):
            raise AssertionError("the turn must not start")
    assert error.value.error_code == "hoc_e2e_session_failed"
    assert not (root / e2e.AGENT_CONFIG).exists()


@pytest.mark.skipif(os.name != "nt", reason="the browser copy is for the Windows Codex sandbox")
def test_sandbox_browsers_copies_the_needed_browsers_once(tmp_path, monkeypatch):
    source = tmp_path / "ms-playwright"
    for name in ("chromium_headless_shell-1248", "ffmpeg-1013", "chromium-1248"):
        (source / name).mkdir(parents=True)
        (source / name / "marker.txt").write_text(name, encoding="utf-8")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(source))
    target = e2e.sandbox_browsers(tmp_path / "runtime")
    assert sorted(path.name for path in target.iterdir()) == ["chromium_headless_shell-1248", "ffmpeg-1013"]
    (target / "ffmpeg-1013" / "marker.txt").write_text("kept", encoding="utf-8")
    e2e.sandbox_browsers(tmp_path / "runtime")
    assert (target / "ffmpeg-1013" / "marker.txt").read_text(encoding="utf-8") == "kept"
