"""Read-only provider files: the repository files that define what a TypeScript source imports.

A unit's Developer and its Code Validator read these instead of guessing the names, signatures and
return values of the code a unit calls. Resolution is deterministic and never reads `node_modules`:
relative specifiers resolve like the bundler, workspace packages through their `package.json`
entry, and a barrel's re-exports are followed to the file that defines the imported name.
"""

from __future__ import annotations

import json
from pathlib import Path
import posixpath
import re
from typing import Iterable

from .runtime_config import _workspaces

_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_IMPORT = re.compile(r"""\bimport\s+(?!\()([^;"']*?)\s*from\s*["']([^"']+)["']""")
_REEXPORT = re.compile(r"""\bexport\s+(?:type\s+)?(\*(?:\s+as\s+\w+)?|\{[^}]*\})\s*from\s*["']([^"']+)["']""")
_MAX_BARREL_DEPTH = 4


def provider_files(
    *,
    target_path: Path,
    source_paths: Iterable[str],
    exclude: Iterable[str] = (),
    limit: int = 10,
) -> list[dict[str, object]]:
    """Each provider file the sources import, in import order, with the sources and names that import it.

    Rows are ``{"path", "imported_by", "names"}``; external packages, files outside the worktree and the
    sources themselves are skipped.
    """

    root = target_path.resolve()
    sources = [path.replace("\\", "/") for path in source_paths]
    skipped = set(sources) | {path.replace("\\", "/") for path in exclude}
    packages = _workspace_packages(root)
    rows: dict[str, dict[str, object]] = {}
    for source in sources:
        file = root / source
        if not file.is_file() or file.suffix not in _SOURCE_SUFFIXES:
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        for clause, specifier in _IMPORT.findall(text):
            module = _resolve_specifier(root, source, specifier, packages)
            if not module:
                continue
            for name in _imported_names(clause):
                path = _defining_file(root, module, name, depth=0) or module
                if path in skipped or "node_modules" in path.split("/"):
                    continue
                if path not in rows:
                    if len(rows) >= limit:
                        continue
                    rows[path] = {"path": path, "imported_by": [], "names": []}
                row = rows[path]
                if source not in row["imported_by"]:
                    row["imported_by"].append(source)
                if name != "*" and name not in row["names"]:
                    row["names"].append(name)
    return list(rows.values())


def _imported_names(clause: str) -> list[str]:
    """Names an import clause takes from its module: `default`, named originals, or `*` for a namespace."""

    clause = re.sub(r"^type\s+", "", clause.strip())
    names: list[str] = []
    braces = re.search(r"\{([^}]*)\}", clause)
    if braces:
        for item in braces.group(1).split(","):
            original = re.sub(r"^type\s+", "", item.strip()).split(" as ")[0].strip()
            if original:
                names.append(original)
        clause = clause.replace(braces.group(0), "")
    for part in (piece.strip() for piece in clause.split(",")):
        if part.startswith("*"):
            names.append("*")
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", part):
            names.append("default")
    return names


def _workspace_packages(root: Path) -> dict[str, str]:
    """Workspace package name -> manifest directory (repo-relative)."""

    return {
        str(item["name"]): posixpath.dirname(str(item["manifest"]))
        for item in _workspaces(root)
        if item.get("name") and posixpath.dirname(str(item["manifest"]))
    }


def _resolve_specifier(root: Path, importer: str, specifier: str, packages: dict[str, str]) -> str:
    if specifier.startswith(("./", "../")):
        return _resolve_file(root, posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier)))
    name, subpath = _split_package(specifier)
    directory = packages.get(name)
    if directory is None:
        return ""
    return _package_entry(root, directory, subpath)


def _split_package(specifier: str) -> tuple[str, str]:
    parts = specifier.split("/")
    count = 2 if specifier.startswith("@") else 1
    return "/".join(parts[:count]), "/".join(parts[count:])


def _package_entry(root: Path, directory: str, subpath: str) -> str:
    try:
        manifest = json.loads((root / directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    exports = manifest.get("exports")
    key = f"./{subpath}" if subpath else "."
    entry = exports if isinstance(exports, str) and not subpath else None
    if isinstance(exports, dict):
        entry = exports.get(key)
        if isinstance(entry, dict):
            entry = next(
                (entry[condition] for condition in ("types", "import", "default", "require") if isinstance(entry.get(condition), str)),
                None,
            )
    if not isinstance(entry, str) and not subpath:
        entry = next((manifest[field] for field in ("types", "main") if isinstance(manifest.get(field), str)), "src/index")
    if not isinstance(entry, str):
        entry = subpath
    return _resolve_file(root, posixpath.normpath(posixpath.join(directory, entry)))


def _resolve_file(root: Path, joined: str) -> str:
    """The worktree file a module path names (TypeScript may import `./x.js` for `./x.ts`)."""

    if joined.startswith("../") or joined == "..":
        return ""
    stem = next((joined[: -len(suffix)] for suffix in _SOURCE_SUFFIXES if joined.endswith(suffix)), joined)
    for candidate in (
        joined,
        *(f"{stem}{suffix}" for suffix in _SOURCE_SUFFIXES),
        *(f"{joined}/index{suffix}" for suffix in _SOURCE_SUFFIXES),
    ):
        if (root / candidate).is_file():
            return candidate
    return ""


def _defining_file(root: Path, module: str, name: str, *, depth: int) -> str:
    """The file that defines `name`, following the module's re-exports; ``""`` when none is found."""

    if name == "*" or depth > _MAX_BARREL_DEPTH:
        return module if name == "*" else ""
    text = (root / module).read_text(encoding="utf-8", errors="replace")
    if _defines(text, name):
        return module
    for exported, specifier in _REEXPORT.findall(text):
        if not specifier.startswith(("./", "../")):
            continue
        if exported.startswith("*"):
            if " as " in exported:
                continue
            wanted = name
        else:
            wanted = ""
            for item in exported.strip("{}").split(","):
                original, _, alias = re.sub(r"^type\s+", "", item.strip()).partition(" as ")
                if (alias.strip() or original.strip()) == name:
                    wanted = original.strip()
                    break
            if not wanted:
                continue
        target = _resolve_file(root, posixpath.normpath(posixpath.join(posixpath.dirname(module), specifier)))
        if target:
            found = _defining_file(root, target, wanted, depth=depth + 1)
            # A named re-export names its source even when the definition itself is not recognised there.
            if found or not exported.startswith("*"):
                return found or target
    return ""


def _defines(text: str, name: str) -> bool:
    if name == "default":
        return re.search(r"\bexport\s+default\b", text) is not None
    declared = re.search(
        rf"\bexport\s+(?:declare\s+)?(?:default\s+)?(?:async\s+)?"
        rf"(?:function\*?|const|let|var|class|interface|type|enum|abstract\s+class)\s+{re.escape(name)}\b",
        text,
    )
    if declared:
        return True
    # A local `export { name }` list (no `from`) exports a name declared in this file.
    for match in re.finditer(r"\bexport\s+(?:type\s+)?\{([^}]*)\}(?!\s*from)", text):
        for item in match.group(1).split(","):
            original, _, alias = re.sub(r"^type\s+", "", item.strip()).partition(" as ")
            if (alias.strip() or original.strip()) == name:
                return True
    return False
