"""Deterministic UI baseline of the frozen web app for the fixed React/TypeScript profile.

The context is evidence for the Designer, not Design authority: it tells which entrypoint,
composition chain, component props, data clients, auth signals, styles and verification
capability already exist so runtime surface bindings can be checked without an LLM.
"""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path
import re

from ...profiles import (
    VERIFICATION_STAGE_POLICY_VERSION,
    build_verification_stage_policy_version,
    get_project_profile,
)
from ...core.coerce import dict_copies as _dicts
from ...core.errors import HarnessError
from ..validator.typescript_ast import TypeScriptAstExtractor

UI_REPO_CONTEXT_VERSION = "ui_repo_experience_context_v2"
WEB_COMPONENT_ID = "web"
MAX_WEB_FILES = 200
MAX_FILE_BYTES = 200_000

_ASYNC_EXPORT = re.compile(r"export\s+async\s+function\s+([A-Za-z_]\w*)")
_AUTH_SIGNAL = re.compile(r"\b(session\w*|auth\w*|login\w*|bearer\w*|accessToken\w*)\b", re.I)
_VITEST_ENV = re.compile(r"environment\s*:\s*[\"']([\w-]+)[\"']")
# Per-file opt-in, valid only in a test file's leading comment block.
_VITEST_ENV_DIRECTIVE = re.compile(r"^\s*(?://|/\*+|\*)\s*@vitest-environment\s+([\w-]+)", re.M)
_DIRECTIVE_SCAN_CHARS = 2000
_DOM_ENVIRONMENTS = frozenset({"jsdom", "happy-dom"})
# HTTP clients: a default/namespace import from a package (or an instance it `.create()`s)
# called with a literal URL, e.g. `axios.get("articles")`, `ky.post(`url`)`.
_PACKAGE_DEFAULT_IMPORT = re.compile(
    r"import\s+(?:([A-Za-z_$][\w$]*)|\*\s+as\s+([A-Za-z_$][\w$]*))\s*(?:,\s*\{[^}]*\})?\s*from\s*[\"']([^\"'.][^\"']*)[\"']"
)
_CLIENT_FACTORY = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\.create\s*\(")
_HTTP_METHOD_CALL = r"(?<![\w$.]){name}\.(?:get|post|put|patch|delete|head|options|request)\s*\(\s*[\"'`]"
_STYLE_SUFFIXES = (".css", ".scss", ".sass", ".less")


def build_ui_repo_context(
    *,
    target_path: Path,
    landscape: dict[str, object],
    project_profile_id: str,
    verification_policy_version: str = "",
    ast_extractor: TypeScriptAstExtractor | None = None,
    typescript_package_root: Path | None = None,
) -> dict[str, object] | None:
    """Return the UI baseline, or None when the frozen landscape has no web component.

    ``verification_policy_version`` is the run's pinned stage policy; empty means the current policy.
    """

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
    test_environments: list[str] = []
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
            elif path.suffix in {".ts", ".tsx"} and (".test." in path.name or ".spec." in path.name):
                with path.open(encoding="utf-8", errors="replace") as handle:
                    found = _VITEST_ENV_DIRECTIVE.search(handle.read(_DIRECTIVE_SCAN_CHARS))
                if found:
                    test_environments.append(found.group(1))
            elif path.suffix in {".ts", ".tsx"}:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    raw = path.read_bytes()
                    # Same hash as DesignerToolRegistry repo refs so the Designer can cite either.
                    file_hashes[relative] = "sha256:" + hashlib.sha256(raw).hexdigest()
                    sources[relative] = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")

    evidence: set[str] = set()
    uncertainty: list[str] = []
    components: dict[str, dict[str, object]] = {}
    try:
        facts = (ast_extractor or TypeScriptAstExtractor()).extract_ui_facts(
            target_path=root_path, source_paths=list(sources), typescript_package_root=typescript_package_root,
        )
        extraction_status = "complete"
    except HarnessError as exc:
        facts = {}
        extraction_status = "unavailable"
        uncertainty.append(f"UI AST extraction unavailable ({exc.error_code}): {exc.message}")
    render_facts: dict[str, dict[str, object]] = {}
    for relative, file in facts.items():
        if file.get("content_sha256") != file_hashes[relative]:
            extraction_status = "partial"
            uncertainty.append(f"Source changed during UI extraction: {relative}.")
            continue
        if file.get("diagnostics"):
            extraction_status = "partial"
            uncertainty.append(f"TypeScript parse diagnostics in {relative}: {file['diagnostics']}")
        for component in _dicts(file.get("components")):
            ref = f"{relative}#{component['symbol']}"
            line = component["start_line"]
            evidence_ref = f"repo:{relative}#L{line}@{file_hashes[relative]}"
            components[ref] = {
                "path": relative, "symbol": component["symbol"], "props": component["props"],
                "props_status": component["props_status"], "evidence_ref": evidence_ref,
            }
            render_facts[ref] = {**component, "renders_status": "unknown" if file.get("diagnostics") else ("partial" if component.get("unresolved_tags") else "known")}
            if component.get("uncertainty") or component.get("unresolved_tags"):
                extraction_status = "partial"
                uncertainty.append(f"{ref}: {component.get('uncertainty') or component.get('unresolved_tags')}")

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
        ref = next((candidate for candidate in _strings(_dict(facts.get(entry_path)).get("entrypoint_candidates"))
                    if candidate in components), "")
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
            component_facts = render_facts[ref]
            renders = [str(edge["ref"]) for edge in _dicts(component_facts.get("renders")) if edge["ref"] in components]
            passes = {str(edge["ref"]): _strings(edge.get("props")) for edge in _dicts(component_facts.get("renders"))}
            queue.extend(renders)
            chain.append(
                {
                    "ref": ref,
                    "path": path,
                    "symbol": components[ref]["symbol"],
                    "renders": renders,
                    "passes_props": passes,
                    "renders_status": component_facts["renders_status"],
                    "evidence_ref": components[ref]["evidence_ref"],
                }
            )
            evidence.add(str(components[ref]["evidence_ref"]))

    data_clients = [
        {"ref": f"{relative}#{symbol}", "path": relative, "symbol": symbol}
        for relative, text in sources.items()
        if _performs_http(text)
        for symbol in _ASYNC_EXPORT.findall(text)
    ]
    auth_paths = sorted(relative for relative, text in sources.items() if _AUTH_SIGNAL.search(text))
    inline_styles = sum(text.count("style={") for text in sources.values())
    class_names = sum(text.count("className=") for text in sources.values())

    stage_ids = list(
        build_verification_stage_policy_version(
            project=get_project_profile(project_profile_id),
            policy_version=verification_policy_version or VERIFICATION_STAGE_POLICY_VERSION,
        ).stage_ids
    )
    unit_environment = ""
    for candidate in ("vitest.config.ts", *(f"{Path(root).parent.as_posix()}/vitest.config.ts" for root in roots)):
        config_path = root_path / candidate
        if config_path.is_file():
            found = _VITEST_ENV.search(config_path.read_text(encoding="utf-8", errors="replace"))
            unit_environment = found.group(1) if found else "node"
            break
    dom_directives = collections.Counter(env for env in test_environments if env in _DOM_ENVIRONMENTS)
    dom_test_directive = (
        f"// @vitest-environment {dom_directives.most_common(1)[0][0]}"
        if dom_directives and unit_environment not in _DOM_ENVIRONMENTS
        else ""
    )

    context: dict[str, object] = {
        "schema_version": UI_REPO_CONTEXT_VERSION,
        "project_profile_id": project_profile_id,
        "web_roots": roots,
        "extraction": {"method": "typescript_compiler_ast_v1", "status": extraction_status},
        "entrypoint": entrypoint,
        "composition_chain": chain,
        "components": dict(sorted(components.items())),
        "data_clients": data_clients,
        "auth": {"present": bool(auth_paths), "evidence_paths": auth_paths,
                 "status": "unverified_hint", "method": "source_identifier_scan"},
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
            "dom_capable": unit_environment in _DOM_ENVIRONMENTS or bool(dom_directives),
            # The config default is not a DOM: new DOM tests opt in with the same first-line directive the repo uses.
            **({"dom_test_directive": dom_test_directive} if dom_test_directive else {}),
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


def _performs_http(text: str) -> bool:
    """True for a module that calls ``fetch(`` or an HTTP client imported from a package with a literal URL."""

    if "fetch(" in text:
        return True
    clients = {default or namespace for default, namespace, _ in _PACKAGE_DEFAULT_IMPORT.findall(text)}
    clients |= {name for name, factory in _CLIENT_FACTORY.findall(text) if factory in clients}
    return any(re.search(_HTTP_METHOD_CALL.format(name=re.escape(name)), text) for name in clients)


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


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
