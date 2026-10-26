"""Khảo sát AS-IS có giới hạn cho workspace đã bind immutable profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
            if component_id == "api":
                for method, expression in _FASTIFY_ROUTE.findall(text):
                    route = " ".join(expression.strip().strip("; ").split())[:160]
                    anchors.append(_anchor("api_route", component_id, relative, symbol=f"{method.upper()} {route}"))
            if component_id == "database":
                for model, operation in _PRISMA_OPERATION.findall(text):
                    anchors.append(_anchor("data_operation", component_id, relative, symbol=f"{model}.{operation}"))
        elif relative.endswith(".prisma"):
            for model in sorted(set(_PRISMA_MODEL.findall(text))):
                anchors.append(_anchor("data_model", component_id, relative, symbol=model))

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
