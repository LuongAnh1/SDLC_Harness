"""Deterministic UI baseline of the frozen web app for the fixed React/TypeScript profile.

The context is evidence for the Designer, not Design authority: it tells which entrypoint,
composition chain, component props, data clients, auth signals, styles and verification
capability already exist so runtime surface bindings can be checked without an LLM.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ...profiles import build_verification_stage_policy, get_project_profile

UI_REPO_CONTEXT_VERSION = "ui_repo_experience_context_v1"
WEB_COMPONENT_ID = "web"
MAX_WEB_FILES = 200
MAX_FILE_BYTES = 200_000

# shortcut: regex parse cho profile React/TS cố định, thay bằng AST/CodeGraph khi hỗ trợ profile khác.
_COMPONENT_DECL = re.compile(r"^(?:export\s+)?(?:default\s+)?function\s+([A-Z][A-Za-z0-9_]*)\s*\(", re.M)
_IMPORT = re.compile(
    r"import\s+(?:type\s+)?(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*[\"'](\.{1,2}/[^\"']+)[\"']"
)
_JSX_TAG = re.compile(r"<([A-Z][A-Za-z0-9_]*)((?:[^<>]|=>)*?)/?>", re.S)
_JSX_ATTR = re.compile(r"(?:^|\s)([A-Za-z_][\w]*)\s*=")
_DESTRUCTURED_SIGNATURE = re.compile(r"\(\s*\{([^)]*?)\}\s*:\s*([A-Z][\w]*)")
_NAMED_SIGNATURE = re.compile(r"\(\s*[A-Za-z_]\w*\s*:\s*([A-Z][\w]*)")
_PROP_FIELD = re.compile(r"(?:^|[;,{\n])\s*(?:readonly\s+)?([A-Za-z_]\w*)(\?)?\s*:", re.M)
_INLINE_SIGNATURE = re.compile(r"\(\s*\{([^)]*?)\}\s*:\s*\{([^)]*)\}\s*\)")
_DEFAULTED = re.compile(r"([A-Za-z_]\w*)\s*=")
_ASYNC_EXPORT = re.compile(r"export\s+async\s+function\s+([A-Za-z_]\w*)")
_AUTH_SIGNAL = re.compile(r"\b(session\w*|auth\w*|login\w*|bearer\w*|accessToken\w*)\b", re.I)
_VITEST_ENV = re.compile(r"environment\s*:\s*[\"']([\w-]+)[\"']")
_STYLE_SUFFIXES = (".css", ".scss", ".sass", ".less")
_SKIP_TAGS = frozenset({"StrictMode", "Fragment", "Suspense"})


def build_ui_repo_context(
    *,
    target_path: Path,
    landscape: dict[str, object],
    project_profile_id: str,
) -> dict[str, object] | None:
    """Return the UI baseline, or None when the frozen landscape has no web component."""

    web = next(
        (item for item in _dicts(landscape.get("components")) if item.get("component_id") == WEB_COMPONENT_ID),
        None,
    )
    if web is None:
        return None
    roots = sorted(_strings(web.get("roots")))
    root_path = target_path.resolve()
    sources: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    style_hashes: dict[str, str] = {}
    styles: list[str] = []
    for root in roots:
        base = (root_path / root).resolve()
        if not base.is_dir() or not _within(base, root_path):
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or len(sources) >= MAX_WEB_FILES:
                continue
            relative = path.relative_to(root_path).as_posix()
            if path.suffix in _STYLE_SUFFIXES:
                styles.append(relative)
                style_hashes[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.suffix in {".ts", ".tsx"} and ".test." not in path.name and ".spec." not in path.name:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    raw = path.read_bytes()
                    # Same hash as DesignerToolRegistry repo refs so the Designer can cite either.
                    file_hashes[relative] = "sha256:" + hashlib.sha256(raw).hexdigest()
                    sources[relative] = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")

    evidence: set[str] = set()
    uncertainty: list[str] = []
    components: dict[str, dict[str, object]] = {}
    bodies: dict[str, str] = {}
    imports: dict[str, dict[str, str]] = {}
    for relative, text in sources.items():
        imports[relative] = _resolve_imports(relative, text, sources)
        declarations = list(_COMPONENT_DECL.finditer(text))
        for index, match in enumerate(declarations):
            symbol = match.group(1)
            end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
            body = text[match.start():end]
            ref = f"{relative}#{symbol}"
            line = text.count("\n", 0, match.start()) + 1
            evidence_ref = f"repo:{relative}#L{line}@{file_hashes[relative]}"
            components[ref] = {
                "path": relative,
                "symbol": symbol,
                "props": _props(body, text),
                "evidence_ref": evidence_ref,
            }
            bodies[ref] = body

    entry_anchor = next(
        (
            item
            for item in _dicts(landscape.get("anchors"))
            if item.get("kind") == "ui_entrypoint" and item.get("component_id") == WEB_COMPONENT_ID
        ),
        None,
    )
    entrypoint: dict[str, object] | None = None
    if entry_anchor is not None and str(entry_anchor.get("path") or "") in sources:
        entry_path = str(entry_anchor.get("path"))
        entry_text = sources[entry_path]
        render_at = entry_text.find(".render(")
        ref = next(
            (
                candidate
                for tag, _ in _JSX_TAG.findall(entry_text[render_at:] if render_at >= 0 else "")
                for candidate in (imports[entry_path].get(tag) or f"{entry_path}#{tag}",)
                if tag not in _SKIP_TAGS and candidate in components
            ),
            "",
        )
        root_symbol = ref.rpartition("#")[2]
        if ref:
            entrypoint = {"ref": ref, "path": ref.rpartition("#")[0], "symbol": root_symbol}
        else:
            uncertainty.append(f"Cannot resolve the rendered root component in {entry_path}.")
    else:
        uncertainty.append("Frozen landscape has no readable web ui_entrypoint.")

    chain: list[dict[str, object]] = []
    if entrypoint is not None:
        queue = [str(entrypoint["ref"])]
        seen: set[str] = set()
        while queue:
            ref = queue.pop(0)
            if ref in seen or ref not in components:
                continue
            seen.add(ref)
            path = str(components[ref]["path"])
            renders: list[str] = []
            passes: dict[str, list[str]] = {}
            for tag, attrs in _JSX_TAG.findall(bodies[ref]):
                if tag in _SKIP_TAGS or tag == components[ref]["symbol"]:
                    continue
                child = imports[path].get(tag) or f"{path}#{tag}"
                if child not in components:
                    continue
                if child not in renders:
                    renders.append(child)
                passes[child] = sorted({*passes.get(child, []), *_JSX_ATTR.findall(attrs)})
                queue.append(child)
            chain.append(
                {
                    "ref": ref,
                    "path": path,
                    "symbol": components[ref]["symbol"],
                    "renders": renders,
                    "passes_props": passes,
                    "evidence_ref": components[ref]["evidence_ref"],
                }
            )
            evidence.add(str(components[ref]["evidence_ref"]))

    data_clients = [
        {"ref": f"{relative}#{symbol}", "path": relative, "symbol": symbol}
        for relative, text in sources.items()
        if "fetch(" in text
        for symbol in _ASYNC_EXPORT.findall(text)
    ]
    auth_paths = sorted(relative for relative, text in sources.items() if _AUTH_SIGNAL.search(text))
    inline_styles = sum(text.count("style={") for text in sources.values())
    class_names = sum(text.count("className=") for text in sources.values())

    stage_ids = list(build_verification_stage_policy(project=get_project_profile(project_profile_id)).stage_ids)
    unit_environment = ""
    for candidate in ("vitest.config.ts", *(f"{Path(root).parent.as_posix()}/vitest.config.ts" for root in roots)):
        config_path = root_path / candidate
        if config_path.is_file():
            found = _VITEST_ENV.search(config_path.read_text(encoding="utf-8", errors="replace"))
            unit_environment = found.group(1) if found else "node"
            break

    context: dict[str, object] = {
        "schema_version": UI_REPO_CONTEXT_VERSION,
        "project_profile_id": project_profile_id,
        "web_roots": roots,
        "entrypoint": entrypoint,
        "composition_chain": chain,
        "components": dict(sorted(components.items())),
        "data_clients": data_clients,
        "auth": {"present": bool(auth_paths), "evidence_paths": auth_paths},
        "styles": {
            "stylesheet_files": sorted(styles),
            "stylesheet_digests": dict(sorted(style_hashes.items())),
            "inline_style_attributes": inline_styles,
            "class_name_attributes": class_names,
            "convention": "stylesheet" if styles else ("inline" if inline_styles else "none"),
        },
        "verification": {
            "active_stage_ids": stage_ids,
            "unit_environment": unit_environment,
            "dom_capable": unit_environment in {"jsdom", "happy-dom"},
            "browser_stage_active": "browser_e2e" in stage_ids,
        },
        "evidence_refs": sorted(evidence),
        "uncertainty": uncertainty,
    }
    context["artifact_digest"] = _digest(context)
    return context


def required_props(context: dict[str, object], ref: str) -> list[str]:
    """Props that a caller must supply: not optional and without a destructuring default."""

    component = _dict(_dict(context.get("components")).get(ref))
    return [
        str(item.get("name"))
        for item in _dicts(component.get("props"))
        if not item.get("optional") and not item.get("has_default")
    ]


def chain_path_to(context: dict[str, object], ref: str) -> list[str]:
    """Shortest AS-IS mounting path from the entrypoint to ``ref``; empty when not mounted."""

    entry = str(_dict(context.get("entrypoint")).get("ref") or "")
    hops = {str(item.get("ref") or ""): _strings(item.get("renders")) for item in _dicts(context.get("composition_chain"))}
    paths = {entry: [entry]} if entry in hops else {}
    queue = list(paths)
    while queue:
        current = queue.pop(0)
        if current == ref:
            return paths[current]
        for child in hops.get(current, []):
            if child not in paths:
                paths[child] = [*paths[current], child]
                queue.append(child)
    return []


def _props(body: str, text: str) -> list[dict[str, object]]:
    signature = body[: body.find("{", body.find(")")) + 1] if ")" in body else body[:400]
    inline = _INLINE_SIGNATURE.search(signature)
    if inline is not None:
        defaults = set(_DEFAULTED.findall(inline.group(1)))
        return [
            {"name": name, "optional": bool(optional), "has_default": name in defaults}
            for name, optional in _PROP_FIELD.findall(inline.group(2))
        ]
    destructured = _DESTRUCTURED_SIGNATURE.search(signature)
    type_name = destructured.group(2) if destructured else ""
    defaults = set(_DEFAULTED.findall(destructured.group(1))) if destructured else set()
    if not type_name:
        named = _NAMED_SIGNATURE.search(signature)
        type_name = named.group(1) if named else ""
    if not type_name:
        return []
    block = re.search(
        rf"(?:interface\s+{re.escape(type_name)}\s*\{{|type\s+{re.escape(type_name)}\s*=\s*\{{)(.*?)\n\}}",
        text,
        re.S,
    )
    if block is None:
        return []
    return [
        {"name": name, "optional": bool(optional), "has_default": name in defaults}
        for name, optional in _PROP_FIELD.findall(block.group(1))
    ]


def _resolve_imports(relative: str, text: str, sources: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    base = Path(relative).parent
    for default_name, named, spec in _IMPORT.findall(text):
        target = ""
        stem = (base / spec).as_posix()
        for candidate in (f"{stem}.tsx", f"{stem}.ts", f"{stem}/index.tsx", f"{stem}/index.ts"):
            normalized = Path(candidate).as_posix()
            parts: list[str] = []
            for part in normalized.split("/"):
                if part == "..":
                    if parts:
                        parts.pop()
                elif part not in {"", "."}:
                    parts.append(part)
            normalized = "/".join(parts)
            if normalized in sources:
                target = normalized
                break
        if not target:
            continue
        pairs = [(default_name, default_name)] if default_name else []
        for item in named.split(","):
            item = item.strip().removeprefix("type ").strip()
            if item:
                original, _, local = item.partition(" as ")
                pairs.append((local.strip() or original.strip(), original.strip()))
        for local, original in pairs:
            if local[:1].isupper():
                resolved[local] = f"{target}#{original}"
    return resolved


def _digest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
