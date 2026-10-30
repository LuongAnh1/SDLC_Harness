"""Bounded read-only adapter for the external ui-ux-pro-max search skill."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from ...core.errors import HarnessError


UIUX_CONTRACT_VERSION = "uiux_pro_max_search_v1"
UIUX_EXPERIENCE_CONTRACT_VERSION = "ui_experience_contract_v1"
UIUX_MODE_STATE_KEY = "designer_uiux_mode"
UIUX_CONTRACT_STATE_KEY = "designer_uiux_contract_version"
UIUX_MODES = frozenset({"off", "enforce"})
UIUX_ALLOWED_DOMAINS = frozenset({"ux", "icons", "react"})
UIUX_ALLOWED_STACKS = frozenset({"react"})
UIUX_MAX_RESULTS = 3
UIUX_MAX_OUTPUT_CHARS = 12_000
_SAFE_QUERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{1,118}[A-Za-z0-9]$")


class UiUxGuidanceProvider:
    """Execute only the skill's JSON search command; never persist skill output."""

    def __init__(
        self,
        *,
        skill_root: Path | None,
        timeout_seconds: int = 30,
        fake_mode: bool = False,
    ) -> None:
        self.skill_root = skill_root.resolve() if skill_root is not None else None
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.fake_mode = fake_mode

    def preflight(self) -> dict[str, object]:
        if self.fake_mode:
            return {
                "schema_version": UIUX_CONTRACT_VERSION,
                "status": "available",
                "provider": "fake",
                "fingerprint": "sha256:fake-uiux-pro-max",
            }
        script = self._script_path()
        data_root = self._data_root()
        fingerprint = _tree_fingerprint([script, *sorted(data_root.rglob("*"))])
        return {
            "schema_version": UIUX_CONTRACT_VERSION,
            "status": "available",
            "provider": "external_skill",
            "fingerprint": fingerprint,
        }

    def search(self, arguments: object) -> dict[str, object]:
        args = dict(arguments) if isinstance(arguments, dict) else {}
        query = str(args.get("query") or "").strip()
        words = query.split()
        if not _SAFE_QUERY.fullmatch(query) or not 2 <= len(words) <= 8:
            raise HarnessError(
                "designer_tool_input_invalid",
                "uiux.search_guidance query must contain 2-8 generic ASCII terms without paths or project-private text.",
            )
        mode = str(args.get("mode") or "").strip().lower()
        if mode == "domain":
            selector = str(args.get("domain") or "").strip().lower()
            if selector not in UIUX_ALLOWED_DOMAINS:
                raise HarnessError(
                    "designer_tool_input_invalid",
                    f"Unsupported UI/UX domain: {selector}",
                )
            selector_args = ["--domain", selector]
        elif mode == "stack":
            selector = str(args.get("stack") or "").strip().lower()
            if selector not in UIUX_ALLOWED_STACKS:
                raise HarnessError(
                    "designer_tool_input_invalid",
                    f"Unsupported UI/UX stack: {selector}",
                )
            selector_args = ["--stack", selector]
        else:
            raise HarnessError(
                "designer_tool_input_invalid",
                "uiux.search_guidance mode must be domain or stack.",
            )

        if self.fake_mode:
            payload: dict[str, object] = {
                "domain": selector,
                "query": query,
                "count": 1,
                "results": [
                    {
                        "Category": "Accessibility" if mode == "domain" else "React",
                        "Issue": "Bounded fake guidance",
                        "Description": "Preserve observable states, keyboard focus, and feedback.",
                        "Do": "Implement the approved UI interaction contract.",
                        "Don't": "Invent a new product-wide design system.",
                        "Severity": "High",
                    }
                ],
            }
        else:
            command = [
                sys.executable,
                str(self._script_path()),
                query,
                *selector_args,
                "--max-results",
                str(UIUX_MAX_RESULTS),
                "--json",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.skill_root),
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise HarnessError(
                    "designer_uiux_failed",
                    f"UI/UX guidance search timed out after {self.timeout_seconds}s.",
                ) from exc
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = str(getattr(exc, "stderr", "") or exc).strip()[:500]
                raise HarnessError("designer_uiux_failed", f"UI/UX guidance search failed: {detail}") from exc
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise HarnessError("designer_uiux_failed", "UI/UX guidance search returned invalid JSON.") from exc
            if not isinstance(parsed, dict):
                raise HarnessError("designer_uiux_failed", "UI/UX guidance search returned an invalid object.")
            payload = parsed

        results = _bounded_results(payload.get("results"))
        output = {
            "schema_version": UIUX_CONTRACT_VERSION,
            "status": "available",
            "mode": mode,
            "selector": selector,
            "query": query,
            "count": len(results),
            "no_match": not results,
            "results": results,
        }
        encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > UIUX_MAX_OUTPUT_CHARS:
            raise HarnessError("designer_uiux_failed", "UI/UX guidance output exceeded the bounded result size.")
        return output

    def _script_path(self) -> Path:
        if self.skill_root is None:
            raise HarnessError("designer_uiux_unavailable", "UI/UX skill root is not configured.")
        script = (self.skill_root / "scripts" / "search.py").resolve()
        if not _within(script, self.skill_root) or not script.is_file():
            raise HarnessError("designer_uiux_unavailable", "UI/UX skill scripts/search.py is missing.")
        return script

    def _data_root(self) -> Path:
        if self.skill_root is None:
            raise HarnessError("designer_uiux_unavailable", "UI/UX skill root is not configured.")
        data_root = (self.skill_root / "data").resolve()
        if not _within(data_root, self.skill_root) or not data_root.is_dir():
            raise HarnessError("designer_uiux_unavailable", "UI/UX skill data directory is missing.")
        return data_root


def _bounded_results(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise HarnessError("designer_uiux_failed", "UI/UX guidance result list is missing.")
    results: list[dict[str, str]] = []
    for item in value[:UIUX_MAX_RESULTS]:
        if not isinstance(item, dict):
            raise HarnessError("designer_uiux_failed", "UI/UX guidance result contains an invalid item.")
        results.append(
            {
                str(key)[:80]: str(raw).strip()[:500]
                for key, raw in item.items()
                if str(key).strip() and str(raw).strip()
            }
        )
    return results


def _tree_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    files = [path for path in paths if path.is_file()]
    if not files:
        raise HarnessError("designer_uiux_unavailable", "UI/UX skill has no searchable data files.")
    for path in sorted(files, key=lambda item: item.as_posix().casefold()):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def guidance_evidence_ref(output: dict[str, object]) -> str:
    encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "uiux:sha256:" + hashlib.sha256(encoded).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
