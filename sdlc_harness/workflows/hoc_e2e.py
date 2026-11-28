"""Let the H-OC integration agent run its own browser specs (owner 2026-11-28).

The integration task works alone on H, so nothing races it for the run database or the service ports. The Codex
sandbox still cannot boot the services (its account has no user profile: `tsx` fails in `os.userInfo`) nor read the
Playwright browsers installed under the user's AppData. So for each integration turn the Harness, outside the
sandbox: prepares the run database and the web build with the pinned stages, boots the services through the
repository's own Playwright `webServer` entries (a hold spec keeps them up until the turn ends), and stages a
Playwright config without `webServer` that the agent runs against them. G15 lets the integration role run exactly
`playwright test ... --config <that config>`; the full pipeline after the turn stays the final check.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from ..core.errors import HarnessError
from ..profiles import VerificationStagePolicy
from ..storage.runtime import RuntimeStore
from ..verification.registry import resolve_verification_stage_specs
from ..verification.runner import VerificationRunner, _kill_process_tree, _run_scoped_env

AGENT_CONFIG = "docs/sdlc-local/playwright.harness.config.ts"
_HOLD_CONFIG = "docs/sdlc-local/playwright.hold.config.ts"
_HOLD_SPEC = "docs/sdlc-local/hold.harness.ts"
_PREPARATION_STAGES = ("database_ready", "migrate_and_seed", "frontend_build")
_BROWSER_PREFIXES = ("chromium_headless_shell-", "ffmpeg-", "winldd-")
_READY_TIMEOUT = 240

_AGENT_CONFIG_TEXT = """import base from "../../playwright.config";

// Written by the SDLC Harness for one integration turn: the services already run, started by the Harness.
export default { ...base, testDir: "../../tests/e2e", outputDir: "../../test-results", webServer: undefined };
"""
_HOLD_CONFIG_TEXT = """import base from "../../playwright.config";

// Written by the SDLC Harness: boots the repository's webServer entries and keeps them up during one turn.
export default {
  ...base, testDir: ".", testMatch: "hold.harness.ts", outputDir: "../../test-results/.harness-hold",
  reporter: "line", retries: 0, workers: 1, timeout: 0,
};
"""
_HOLD_SPEC_TEXT = """import { test } from "@playwright/test";
import { existsSync, writeFileSync } from "node:fs";

test("SDLC Harness keeps the services up for one integration turn", async () => {
  test.setTimeout(0);
  writeFileSync(process.env.SDLC_E2E_READY ?? "", "ready");
  while (!existsSync(process.env.SDLC_E2E_STOP ?? "")) {
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
});
"""


def e2e_command(config: str = AGENT_CONFIG) -> str:
    """The one browser-test command the integration agent may run (G15 allows only this config)."""

    npx = "npx.cmd" if os.name == "nt" else "npx"  # the sandbox's PowerShell refuses the npx.ps1 shim
    return f"{npx} playwright test <spec files> --config {config}"


def sandbox_browsers(runtime_root: Path) -> Path | None:
    """A copy of the installed Playwright browsers the Codex sandbox account can read (Windows only).

    shortcut: copies the browsers once from the user's install; upgrade to installing them into a shared path at
    bootstrap when a second machine or a Playwright upgrade needs it.
    """

    if os.name != "nt":
        return None
    source = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    target = runtime_root / "ms-playwright"
    if not source.is_dir():
        raise HarnessError("hoc_e2e_session_failed", "No installed Playwright browsers to share with the agent.")
    for entry in sorted(source.iterdir()):
        if entry.is_dir() and entry.name.startswith(_BROWSER_PREFIXES) and not (target / entry.name).is_dir():
            temporary = target / f".{entry.name}.{uuid4().hex}"
            shutil.copytree(entry, temporary)
            temporary.rename(target / entry.name)
    return target


@contextmanager
def integration_e2e_session(
    root: Path, *, store: RuntimeStore, run_id: str, verification_policy: VerificationStagePolicy,
) -> Iterator[dict[str, str]]:
    """Yield the extra environment of one integration turn while the run's services are up."""

    def failure(message: str) -> HarnessError:
        return HarnessError("hoc_e2e_session_failed", message, run_id=run_id)

    stages = tuple(stage for stage in verification_policy.stage_ids if stage in _PREPARATION_STAGES)
    if "browser_e2e" not in verification_policy.stage_ids or set(stages) != set(_PREPARATION_STAGES):
        raise failure("The pinned verification policy cannot prepare browser specs for the agent.")
    prepared = VerificationRunner().run_harness_stages(
        target_path=root, run_id=run_id, verification_policy=verification_policy, stage_ids=stages,
    )
    prepared_log = store.write_json_artifact(run_id, f"hoc_e2e_prepare_{uuid4().hex}.json", prepared.to_dict())
    if prepared.status != "passed":
        raise failure(f"Preparing the database and web build failed: {prepared.reason} ({store.relative(prepared_log)})")

    spec = next(item for item in resolve_verification_stage_specs(verification_policy, run_id=run_id)
                if item.stage_id == "browser_e2e")
    browsers = sandbox_browsers(store.runtime_root)
    staged = [root / AGENT_CONFIG, root / _HOLD_CONFIG, root / _HOLD_SPEC]
    for path, text in zip(staged, (_AGENT_CONFIG_TEXT, _HOLD_CONFIG_TEXT, _HOLD_SPEC_TEXT)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    signals = Path(tempfile.mkdtemp(prefix="sdlc-e2e-"))
    ready, stop = signals / "ready", signals / "stop"
    scoped = {**dict(spec.fixed_env), **_run_scoped_env(run_id)}
    npx = shutil.which("npx")
    if npx is None:
        raise failure("npx was not found.")
    log_path = store.artifact_path(run_id, f"hoc_e2e_services_{uuid4().hex}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [npx, "playwright", "test", "--config", _HOLD_CONFIG], cwd=root, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, **scoped, "SDLC_E2E_READY": str(ready), "SDLC_E2E_STOP": str(stop)},
        )
        try:
            deadline = time.monotonic() + _READY_TIMEOUT
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise failure(f"The services did not start for the integration turn ({store.relative(log_path)}).")
                time.sleep(0.5)
            env = {**scoped, "SDLC_E2E_CONFIG": AGENT_CONFIG}
            if browsers is not None:
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
            yield env
        finally:
            stop.touch()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
            for path in staged:
                path.unlink(missing_ok=True)
            shutil.rmtree(signals, ignore_errors=True)
