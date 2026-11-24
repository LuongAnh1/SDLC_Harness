"""Khảo sát AS-IS có giới hạn cho workspace đã bind immutable profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import posixpath
import re
from typing import Callable

from ..core.errors import HarnessError
from ..profiles import ProfileSnapshotV1
from ..workspaces import RunWorkspaceRecord


REPO_LANDSCAPE_VERSION = "repo_landscape_v1"
MAX_DISCOVERY_FILES = 4000
MAX_SOURCE_BYTES = 512_000
MAX_INSPECTION_BYTES = 16_000_000
_ANCHOR_KINDS = {
    "api_route",
    "data_model",
    "data_operation",
    "manifest",
    "migration",
    "service_symbol",
    "source_file",
    "test",
    "ui_component",
    "ui_entrypoint",
    "verification_config",
}
_VERIFICATION_CONFIGS = {
    "compose.yaml",
    "package.json",
    "playwright.config.ts",
    "tsconfig.base.json",
    "tsconfig.json",
    "vitest.config.ts",
}
_TS_SYMBOL = re.compile(
    r"(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
_FASTIFY_ROUTE = re.compile(
    r"\b(?:app|server|fastify)\.(get|post|put|patch|delete|options|head)"
    r"\s*\(\s*([^,\n]+)"
)
# Any receiver (app, router, plugin instance) registering a literal "/..." path is an HTTP route.
_LITERAL_ROUTE = re.compile(
    r"\b[A-Za-z_$][A-Za-z0-9_$]*\.(get|post|put|patch|delete|options|head)"
    r"\s*\(\s*([\"'`])(/[^\"'`\n]*)\2"
)
# A router module mounted under a prefix: `x.use("/p", router)`, `x.route("/p", router)`
# or `x.register(plugin, { prefix: "/p" })`.
_MOUNT_PATH_FIRST = re.compile(
    r"\.(?:use|route)\s*\(\s*([\"'`])(/[^\"'`\n]*)\1\s*,\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*[,)]"
)
_MOUNT_PREFIX_OPTION = re.compile(
    r"\.register\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*,\s*\{[^}]*?\bprefix\s*:\s*([\"'`])(/[^\"'`\n]*)\2"
)
# The clause excludes quotes so a side-effect import (`import "./x"`) never swallows the next statement.
_RELATIVE_IMPORT = re.compile(r"import\s+(?:type\s+)?([^;\"'`]*?)\s*from\s*[\"'](\.{1,2}/[^\"']+)[\"']")
_MAX_MOUNT_DEPTH = 8
# Middleware registered without a path (`app.use(handler)`), e.g. Express error handlers.
_PATHLESS_USE = re.compile(r"\.use\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)")
_RELATIVE_REEXPORT = re.compile(r"export\s*\{([^}]*)\}\s*from\s*[\"'](\.{1,2}/[^\"']+)[\"']")
_PRISMA_MODEL = re.compile(r"(?m)^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_PRISMA_OPERATION = re.compile(
    r"\b(?:prisma|client)\.([A-Za-z_$][A-Za-z0-9_$]*)\."
    r"(findUnique|findFirst|findMany|create|createMany|update|updateMany|upsert|delete|deleteMany|count)\b"
)


CodeGraphQuery = Callable[[Path, str], str]


def build_repo_landscape(
    *,
    run_id: str,
    target_path: Path,
    workspace: RunWorkspaceRecord,
    snapshot: ProfileSnapshotV1,
    requirement_model: dict[str, object],
    codegraph_query: CodeGraphQuery | None = None,
) -> dict[str, object]:
    """Tạo artifact deterministic, không ghi target repo và không gọi LLM."""

    if not snapshot.is_workspace_bound or not snapshot.base_sha:
        raise HarnessError(
            "profile_snapshot_mismatch",
            "Repo discovery requires a workspace-bound profile snapshot.",
            run_id=run_id,
        )
    if workspace.run_id != run_id or workspace.base_sha != snapshot.base_sha:
        raise HarnessError(
            "repo_discovery_snapshot_mismatch",
            "Run workspace and immutable profile snapshot do not share the same base SHA.",
            run_id=run_id,
        )
    if workspace.head_sha != workspace.base_sha:
        raise HarnessError(
            "repo_discovery_workspace_drift",
            "Repo discovery requires an unmodified base workspace before Design.",
            run_id=run_id,
        )

    project, language = snapshot.resolve_profiles(run_id=run_id)
    root = target_path.resolve()
    if not root.is_dir():
        raise HarnessError(
            "repo_discovery_workspace_missing",
            "Repo discovery workspace does not exist.",
            run_id=run_id,
        )

    component_roots = dict(snapshot.resolved_component_roots or ())
    files: dict[str, str] = {}
    components: list[dict[str, object]] = []
    for component in project.components:
        component_files: list[str] = []
        for relative_root in component_roots.get(component.component_id, ()):
            approved_root = _safe_path(root, relative_root, run_id=run_id)
            if not approved_root.is_dir():
                raise HarnessError(
                    "repo_discovery_required_path_missing",
                    f"Profile component root is missing: {relative_root}",
                    run_id=run_id,
                )
            for candidate in approved_root.rglob("*"):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                if not _is_within(resolved, approved_root) or not _is_within(resolved, root):
                    raise HarnessError(
                        "repo_discovery_path_escape",
                        f"Repo discovery path escapes its approved root: {candidate}",
                        run_id=run_id,
                    )
                relative = resolved.relative_to(root).as_posix()
                files[relative] = component.component_id
                component_files.append(relative)
                if len(files) > MAX_DISCOVERY_FILES:
                    raise HarnessError(
                        "repo_discovery_budget_exhausted",
                        f"Repo discovery exceeded {MAX_DISCOVERY_FILES} files.",
                        run_id=run_id,
                    )
        components.append(
            {
                "component_id": component.component_id,
                "roots": list(component_roots.get(component.component_id, ())),
                "file_refs": sorted(set(component_files)),
            }
        )

    required_refs: list[str] = []
    for relative in project.required_paths:
        path = _safe_path(root, relative, run_id=run_id)
        if not path.exists():
            raise HarnessError(
                "repo_discovery_required_path_missing",
                f"Required profile path is missing: {relative}",
                run_id=run_id,
            )
        required_refs.append(relative)
        if path.is_file():
            files.setdefault(relative, "profile")

    for relative in project.protected_paths:
        path = _safe_path(root, relative, run_id=run_id)
        if not path.exists():
            continue
        protected_files = [path] if path.is_file() else path.rglob("*")
        for candidate in protected_files:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not _is_within(resolved, path.resolve()) or not _is_within(resolved, root):
                raise HarnessError(
                    "repo_discovery_path_escape",
                    f"Protected repo discovery path escapes its approved root: {candidate}",
                    run_id=run_id,
                )
            files.setdefault(resolved.relative_to(root).as_posix(), "profile")
            if len(files) > MAX_DISCOVERY_FILES:
                raise HarnessError(
                    "repo_discovery_budget_exhausted",
                    f"Repo discovery exceeded {MAX_DISCOVERY_FILES} files.",
                    run_id=run_id,
                )

    anchors: list[dict[str, object]] = []
    api_sources: dict[str, str] = {}
    inspected_file_count = 0
    inspected_bytes = 0
    inspection_truncated = False
    for relative, component_id in sorted(files.items()):
        path = _safe_path(root, relative, run_id=run_id)
        kind = _file_kind(relative, language.test_suffixes)
        anchors.append(_anchor(kind, component_id, relative))
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            inspection_truncated = True
            continue
        if inspected_bytes + size > MAX_INSPECTION_BYTES:
            inspection_truncated = True
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        inspected_file_count += 1
        inspected_bytes += size
        if relative.endswith((".ts", ".tsx")):
            for symbol in sorted(set(_TS_SYMBOL.findall(text))):
                symbol_kind = "ui_component" if component_id == "web" and relative.endswith(".tsx") else "service_symbol"
                anchors.append(_anchor(symbol_kind, component_id, relative, symbol=symbol))
            if component_id == "web" and Path(relative).name in {"main.ts", "main.tsx"}:
                anchors.append(_anchor("ui_entrypoint", component_id, relative, symbol=Path(relative).stem))
            if component_id == "api" and kind != "test":
                api_sources[relative] = text
                for method, expression in _FASTIFY_ROUTE.findall(text):
                    route = " ".join(expression.strip().strip("; ").split())[:160]
                    if route[:1] in {'"', "'", "`"}:
                        continue  # literal paths are read by _literal_route_anchors, which adds mount prefixes
                    anchors.append(_anchor("api_route", component_id, relative, symbol=f"{method.upper()} {route}"))
            if component_id == "database":
                for model, operation in _PRISMA_OPERATION.findall(text):
                    anchors.append(_anchor("data_operation", component_id, relative, symbol=f"{model}.{operation}"))
        elif relative.endswith(".prisma"):
            for model in sorted(set(_PRISMA_MODEL.findall(text))):
                anchors.append(_anchor("data_model", component_id, relative, symbol=model))

    anchors.extend(_literal_route_anchors(api_sources, component_id="api"))
    anchors = _dedupe_anchors(anchors)
    codegraph = _codegraph_enrichment(
        root=root,
        requirement_model=requirement_model,
        anchors=anchors,
        codegraph_query=codegraph_query,
    )
    scenario_matches = _scenario_matches(requirement_model, anchors)
    findings = _conformance_findings(root=root, files=files, required_refs=required_refs)
    uncertainties = []
    if codegraph["status"] != "used":
        uncertainties.append(
            {
                "code": "codegraph_not_used",
                "message": "CodeGraph evidence was unavailable; bounded profile scan was used.",
            }
        )
    if any(item["status"] == "unmatched" for item in scenario_matches):
        uncertainties.append(
            {
                "code": "required_surface_without_candidate",
                "message": "One or more required scenario surfaces have no AS-IS candidate anchor.",
            }
        )
    if inspection_truncated:
        uncertainties.append(
            {
                "code": "inspection_budget_truncated",
                "message": "Symbol inspection reached its deterministic byte budget; inventory remains complete.",
            }
        )

    payload: dict[str, object] = {
        "schema_version": REPO_LANDSCAPE_VERSION,
        "run_id": run_id,
        "snapshot": {
            "workspace_ref": "run_workspace_v1",
            "base_sha": workspace.base_sha,
            "head_sha": workspace.head_sha,
            "project_profile_id": snapshot.project_profile_id,
            "project_profile_schema": snapshot.project_profile_schema,
            "language_profile_id": snapshot.language_profile_id,
            "language_profile_schema": snapshot.language_profile_schema,
            "target_manifest_digest": snapshot.target_manifest_digest,
        },
        "discovery": {
            "mode": "codegraph_enriched" if codegraph["status"] == "used" else "bounded_profile_scan",
            "codegraph": codegraph,
        },
        "components": components,
        "anchors": anchors,
        "scenario_matches": scenario_matches,
        "conformance_findings": findings,
        "uncertainties": uncertainties,
        "summary": {
            "component_count": len(components),
            "file_count": len(files),
            "anchor_count": len(anchors),
            "inspected_file_count": inspected_file_count,
            "inspected_bytes": inspected_bytes,
            "blocking_count": sum(1 for item in findings if item["severity"] == "blocker"),
            "warning_count": sum(1 for item in findings if item["severity"] == "warning"),
            "unmatched_surface_count": sum(1 for item in scenario_matches if item["status"] == "unmatched"),
        },
    }
    payload["artifact_digest"] = _digest(payload)
    validate_repo_landscape(payload, expected_run_id=run_id)
    return payload


def validate_repo_landscape(
    value: object,
    *,
    expected_run_id: str | None = None,
    expected_snapshot: dict[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != REPO_LANDSCAPE_VERSION:
        raise HarnessError("repo_landscape_invalid", "Unsupported repo landscape contract.", run_id=expected_run_id)
    run_id = str(value.get("run_id") or "")
    if not run_id or (expected_run_id and run_id != expected_run_id):
        raise HarnessError("repo_landscape_invalid", "Repo landscape run identity is invalid.", run_id=expected_run_id)
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        raise HarnessError("repo_landscape_invalid", "Repo landscape snapshot is invalid.", run_id=run_id)
    if expected_snapshot is not None and any(snapshot.get(key) != item for key, item in expected_snapshot.items()):
        raise HarnessError(
            "repo_landscape_invalid",
            "Repo landscape snapshot does not match the frozen workspace/profile identity.",
            run_id=run_id,
        )
    anchors = value.get("anchors")
    if not isinstance(anchors, list) or any(not isinstance(item, dict) for item in anchors):
        raise HarnessError("repo_landscape_invalid", "Repo landscape anchors must be an array.", run_id=run_id)
    anchor_ids: set[str] = set()
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        kind = str(anchor.get("kind") or "")
        path = str(anchor.get("path") or "")
        if not anchor_id.startswith("RLA-") or anchor_id in anchor_ids or kind not in _ANCHOR_KINDS:
            raise HarnessError("repo_landscape_invalid", "Repo landscape anchor identity is invalid.", run_id=run_id)
        if not path or "\\" in path or path.startswith("/") or ".." in Path(path).parts:
            raise HarnessError("repo_landscape_invalid", "Repo landscape anchor path is unsafe.", run_id=run_id)
        anchor_ids.add(anchor_id)
    matches = value.get("scenario_matches")
    if not isinstance(matches, list):
        raise HarnessError("repo_landscape_invalid", "Repo landscape scenario matches must be an array.", run_id=run_id)
    for match in matches:
        if not isinstance(match, dict) or any(ref not in anchor_ids for ref in _strings(match.get("candidate_anchor_refs"))):
            raise HarnessError("repo_landscape_invalid", "Repo landscape contains a dangling anchor reference.", run_id=run_id)
    expected_digest = _digest({key: item for key, item in value.items() if key != "artifact_digest"})
    if value.get("artifact_digest") != expected_digest:
        raise HarnessError("repo_landscape_invalid", "Repo landscape digest does not match its content.", run_id=run_id)
    return value


def _file_kind(path: str, test_suffixes: tuple[str, ...]) -> str:
    name = Path(path).name
    if any(name.endswith(suffix) for suffix in test_suffixes):
        return "test"
    if "/migrations/" in f"/{path}" or name == "migration.sql":
        return "migration"
    if name == "package.json":
        return "manifest"
    if path in _VERIFICATION_CONFIGS or name in _VERIFICATION_CONFIGS:
        return "verification_config"
    return "source_file"


def _anchor(kind: str, component_id: str, path: str, *, symbol: str = "") -> dict[str, object]:
    canonical = f"{kind}\0{component_id}\0{path}\0{symbol}".encode("utf-8")
    return {
        "anchor_id": "RLA-" + hashlib.sha256(canonical).hexdigest()[:16],
        "kind": kind,
        "component_id": component_id,
        "path": path,
        "symbol": symbol,
        "evidence_source": "bounded_profile_scan",
    }


def _dedupe_anchors(anchors: list[dict[str, object]]) -> list[dict[str, object]]:
    unique = {str(item["anchor_id"]): item for item in anchors}
    return [unique[key] for key in sorted(unique)]


def _literal_route_anchors(sources: dict[str, str], *, component_id: str) -> list[dict[str, object]]:
    """Route anchors for literal paths, prefixed by every mount of the module that registers them."""

    edges: list[tuple[str, str, str]] = []
    for path, text in sources.items():
        imports = _relative_import_targets(path, text, sources)
        mounts = [(prefix, name) for _, prefix, name in _MOUNT_PATH_FIRST.findall(text)]
        mounts += [(prefix, name) for name, _, prefix in _MOUNT_PREFIX_OPTION.findall(text)]
        for prefix, name in mounts:
            child = imports.get(name)
            if child and child != path:
                edges.append((path, child, prefix))
    prefixes = _mount_prefixes(sources, edges)
    anchors: list[dict[str, object]] = []
    for path, text in sources.items():
        for method, quote, route in _LITERAL_ROUTE.findall(text):
            for prefix in prefixes[path]:
                full = _join_route(prefix, route)[:160]
                anchors.append(_anchor("api_route", component_id, path, symbol=f"{method.upper()} {quote}{full}{quote}"))
    return anchors


def _relative_import_targets(path: str, text: str, sources: dict[str, str]) -> dict[str, str]:
    """Local binding name -> repo path of the relative module it comes from (only modules in ``sources``)."""

    targets: dict[str, str] = {}
    for clause, spec in _RELATIVE_IMPORT.findall(text):
        target = _resolve_relative(path, spec, sources)
        if not target:
            continue
        default, _, named = clause.partition("{")
        names = [default.strip().strip(",").strip()]
        names += [item.strip().partition(" as ")[2].strip() or item.strip() for item in named.rstrip("}").split(",")]
        for name in names:
            name = name.removeprefix("type ").removeprefix("* as ").strip()
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                targets[name] = target
    return targets


def _resolve_relative(path: str, spec: str, sources: dict[str, str]) -> str:
    stem = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    stem = re.sub(r"\.(?:m?js|cjs)$", "", stem)  # ESM TypeScript imports "./x.js" for "./x.ts"
    return next(
        (
            candidate
            for candidate in (stem, f"{stem}.ts", f"{stem}.tsx", f"{stem}.mts", f"{stem}/index.ts")
            if candidate in sources
        ),
        "",
    )


def after_mount_middleware(
    target_path: Path, *, files: list[str], source_files: list[str]
) -> dict[str, list[dict[str, object]]]:
    """Middleware that the modules mounting each file (up the mount chain) register without a path
    after that mount, such as Express error handlers: they answer what the mounted code passes on."""

    root = target_path.resolve()
    sources = {
        path: (root / path).read_text(encoding="utf-8", errors="replace")
        for path in source_files
        if path.endswith((".ts", ".tsx", ".mts")) and (root / path).is_file()
    }
    imports = {path: _relative_import_targets(path, text, sources) for path, text in sources.items()}
    mounted_by: dict[str, list[tuple[str, int]]] = {}
    for path, text in sources.items():
        for match in _MOUNT_PATH_FIRST.finditer(text):
            child = imports[path].get(match.group(3), "")
            if child and child != path:
                mounted_by.setdefault(child, []).append((path, match.end()))
    result: dict[str, list[dict[str, object]]] = {}
    for start in files:
        found: list[dict[str, object]] = []
        frontier, seen = [start], {start}
        for _ in range(_MAX_MOUNT_DEPTH):
            parents = [item for child in frontier for item in mounted_by.get(child, [])]
            for parent, offset in parents:
                text = sources[parent]
                for use in _PATHLESS_USE.finditer(text, offset):
                    module = imports[parent].get(use.group(1), "")
                    if not module:
                        continue
                    entry = {
                        "registered_by": parent,
                        "line": text.count("\n", 0, use.start()) + 1,
                        "name": use.group(1),
                        "path": _declaring_module(module, use.group(1), sources),
                    }
                    if entry not in found:
                        found.append(entry)
            frontier = [parent for parent, _ in parents if parent not in seen]
            seen.update(frontier)
            if not frontier:
                break
        if found:
            result[start] = found
    return result


def _declaring_module(module: str, name: str, sources: dict[str, str]) -> str:
    """The module behind a barrel's `export { x as name } from "./y"` (one level), else `module`."""

    for clause, spec in _RELATIVE_REEXPORT.findall(sources.get(module, "")):
        for item in clause.split(","):
            local, _, alias = item.strip().partition(" as ")
            if (alias.strip() or local.strip()) == name:
                return _resolve_relative(module, spec, sources) or module
    return module


def _mount_prefixes(files: dict[str, str], edges: list[tuple[str, str, str]]) -> dict[str, tuple[str, ...]]:
    incoming: dict[str, list[tuple[str, str]]] = {}
    for parent, child, prefix in edges:
        incoming.setdefault(child, []).append((parent, prefix))
    resolved: dict[str, tuple[str, ...]] = {}

    def resolve(path: str, seen: frozenset[str]) -> tuple[str, ...]:
        if path in resolved:
            return resolved[path]
        if path not in incoming or path in seen or len(seen) >= _MAX_MOUNT_DEPTH:
            return ("",)
        result = tuple(
            sorted(
                {
                    _join_route(parent_prefix, prefix)
                    for parent, prefix in incoming[path]
                    for parent_prefix in resolve(parent, seen | {path})
                }
            )
        )
        resolved[path] = result
        return result

    return {path: resolve(path, frozenset()) for path in files}


def _join_route(prefix: str, route: str) -> str:
    if prefix in {"", "/"}:
        return route or "/"
    if route in {"", "/"}:
        return prefix.rstrip("/") or "/"
    return f"{prefix.rstrip('/')}/{route.lstrip('/')}"


def _scenario_matches(requirement: dict[str, object], anchors: list[dict[str, object]]) -> list[dict[str, object]]:
    by_component: dict[str, list[str]] = {}
    for anchor in anchors:
        by_component.setdefault(str(anchor["component_id"]), []).append(str(anchor["anchor_id"]))
    surface_components = {
        "ui": ("web",),
        "api": ("api", "contracts"),
        "business_rule": ("api", "contracts"),
        "data": ("database",),
    }
    contract = requirement.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    result: list[dict[str, object]] = []
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict):
            continue
        surfaces = scenario.get("surfaces")
        for surface, components in surface_components.items():
            value = surfaces.get(surface) if isinstance(surfaces, dict) else None
            if not isinstance(value, dict) or value.get("status") != "required":
                continue
            refs = sorted({ref for component in components for ref in by_component.get(component, [])})
            result.append(
                {
                    "scenario_id": str(scenario.get("id") or ""),
                    "surface": surface,
                    "status": "candidate" if refs else "unmatched",
                    "candidate_anchor_refs": refs,
                }
            )
    return sorted(result, key=lambda item: (str(item["scenario_id"]), str(item["surface"])))


def _codegraph_enrichment(
    *,
    root: Path,
    requirement_model: dict[str, object],
    anchors: list[dict[str, object]],
    codegraph_query: CodeGraphQuery | None,
) -> dict[str, object]:
    if not (root / ".codegraph").is_dir():
        return {"status": "missing", "query_digest": "", "matched_anchor_refs": []}
    scenarios = requirement_model.get("scenario_contract")
    scenario_items = scenarios.get("scenarios") if isinstance(scenarios, dict) else []
    terms = [str(item.get("name") or "") for item in scenario_items if isinstance(item, dict)]
    query = " ".join(term for term in terms if term)[:1000] or "project structure entrypoints routes schema tests"
    if codegraph_query is None:
        return {"status": "unavailable", "query_digest": _text_digest(query), "matched_anchor_refs": []}
    try:
        output = codegraph_query(root, query)
    except (HarnessError, OSError, RuntimeError):
        return {"status": "failed", "query_digest": _text_digest(query), "matched_anchor_refs": []}
    normalized = output.replace("\\", "/")
    matched = sorted(
        str(anchor["anchor_id"])
        for anchor in anchors
        if str(anchor["path"]) in normalized
    )
    for anchor in anchors:
        if str(anchor["anchor_id"]) in matched:
            anchor["evidence_source"] = "codegraph+bounded_profile_scan"
    return {
        "status": "used",
        "query_digest": _text_digest(query),
        "result_digest": _text_digest(output),
        "matched_anchor_refs": matched,
    }


def _conformance_findings(*, root: Path, files: dict[str, str], required_refs: list[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    package = root / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            findings.append(_finding("package_manifest_invalid", "blocker", "package.json is not valid JSON."))
        else:
            package_manager = str(payload.get("packageManager") or "") if isinstance(payload, dict) else ""
            if package_manager and not package_manager.startswith("npm@"):
                findings.append(_finding("package_manager_mismatch", "blocker", "The frozen profile requires npm."))
            if not (root / "package-lock.json").is_file():
                findings.append(_finding("package_lock_missing", "blocker", "The frozen npm profile requires package-lock.json."))
    if not any(path.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")) for path in files):
        findings.append(_finding("test_inventory_empty", "warning", "No profile-owned TypeScript tests were found."))
    if "package.json" not in required_refs:
        findings.append(_finding("package_manifest_not_required", "warning", "Profile required paths omit package.json."))
    return findings


def _finding(code: str, severity: str, message: str) -> dict[str, object]:
    return {
        "finding_id": "RLF-" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:12],
        "code": code,
        "severity": severity,
        "message": message,
    }


def _safe_path(root: Path, relative: str, *, run_id: str) -> Path:
    if not relative or "\\" in relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise HarnessError("repo_discovery_path_escape", f"Unsafe repository path: {relative}", run_id=run_id)
    resolved = (root / relative).resolve()
    if not _is_within(resolved, root):
        raise HarnessError("repo_discovery_path_escape", f"Repository path escapes workspace: {relative}", run_id=run_id)
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
