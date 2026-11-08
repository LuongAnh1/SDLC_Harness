"""Read-only runtime configuration facts of the frozen target worktree (L5.1.8 M2).

The Planner sees these facts instead of raw config files: workspace manifests and their
dependencies, compose services, environment variables the source already reads, and the
names an `.env.example` declares. Nothing here edits the worktree.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

REPO_RUNTIME_CONFIG_VERSION = "repo_runtime_config_v1"

_ENV_READ = re.compile(
    r"process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\])"
    r"|import\.meta\.env\.([A-Z][A-Z0-9_]*)"
)
_ENV_EXAMPLE_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def collect_repo_runtime_config(*, target_path: Path, source_files: list[str]) -> dict[str, object]:
    root = target_path.resolve()
    return {
        "schema_version": REPO_RUNTIME_CONFIG_VERSION,
        "workspaces": _workspaces(root),
        "compose_services": _compose_services(root / "compose.yaml"),
        "env_reads": _env_reads(root, source_files),
        "env_example": _env_example(root / ".env.example"),
    }


def _workspaces(root: Path) -> list[dict[str, object]]:
    manifests = [root / "package.json"]
    for parent in ("apps", "packages"):
        if (root / parent).is_dir():
            manifests.extend(sorted((root / parent).glob("*/package.json")))
    result = []
    for path in manifests:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append(
            {
                "manifest": path.relative_to(root).as_posix(),
                "name": str(payload.get("name") or ""),
                "version": str(payload.get("version") or ""),
                "dependencies": sorted(dict(payload.get("dependencies") or {})),
                "dev_dependencies": sorted(dict(payload.get("devDependencies") or {})),
            }
        )
    return result


def _compose_services(path: Path) -> list[dict[str, str]]:
    # shortcut: line-based read of top-level `services:` (2-space keys, `image:` below); use a
    # YAML parser if a profile ships compose files in another layout.
    if not path.is_file():
        return []
    services: list[dict[str, str]] = []
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_services = line.rstrip() == "services:"
            continue
        if not in_services:
            continue
        key = re.fullmatch(r"  ([A-Za-z0-9_.-]+):\s*", line)
        if key:
            services.append({"service": key.group(1), "image": ""})
            continue
        image = re.fullmatch(r"\s{4,}image:\s*(\S+)\s*", line)
        if image and services:
            services[-1]["image"] = image.group(1)
    return services


def _env_reads(root: Path, source_files: list[str]) -> list[dict[str, object]]:
    # shortcut: only direct `process.env.X` / `import.meta.env.X` reads; reads through an aliased
    # env object are missed, upgrade to CodeGraph references if that becomes common.
    found: dict[str, set[str]] = {}
    for relative in source_files:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _ENV_READ.finditer(text):
            name = next(group for group in match.groups() if group)
            found.setdefault(name, set()).add(Path(relative).as_posix())
    return [{"name": name, "files": sorted(files)} for name, files in sorted(found.items())]


def _env_example(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"present": False, "names": []}
    return {"present": True, "names": sorted(set(_ENV_EXAMPLE_LINE.findall(path.read_text(encoding="utf-8"))))}
