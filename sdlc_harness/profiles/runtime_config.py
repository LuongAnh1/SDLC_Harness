"""Runtime configuration planning policy for Phase 3 (L5.1.8 M2).

Design states behavior only. The Planner decides whether that behavior needs runtime
configuration (an environment value, an external package, a local service) and declares it
as typed changes. This module owns the allowed change kinds, their shape checks and which
kinds Phase 4 can already execute. It never reads LLM prose to decide anything.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from .planning import PlanningPathPolicy

RUNTIME_CONFIG_POLICY_VERSION = "runtime_config_policy_v1"
RUNTIME_CONFIG_STATUSES = ("required", "not_required")

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NPM_NAME = re.compile(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

# Fields each change kind carries; anything else from the Planner is dropped.
_CHANGE_FIELDS = {
    "env_var": ("name", "default", "read_by", "purpose"),
    "npm_package": ("manifest", "package", "version", "dev", "used_by", "purpose"),
    "compose_service": ("service", "image", "healthcheck", "purpose"),
}

_POLICY_BY_PROJECT = {
    "typescript_fullstack_v1": {
        "kinds": {
            "env_var": (
                "A new environment variable read by one planned source target. The code keeps a "
                "non-secret default so the product and its tests run without it set."
            ),
            "npm_package": (
                "One external dependency for one workspace manifest, with an exact version. Harness "
                "updates the manifest and lockfile; the Planner never edits them."
            ),
            "compose_service": (
                "One new service in compose.yaml with a pinned image tag and a healthcheck command. "
                "Harness adds it; the Planner never edits compose.yaml."
            ),
        },
        # Phase 4 can execute env_var now: it is a code change inside an approved target.
        "executable_kinds": ("env_var",),
        "locked": (
            "package.json scripts and engines",
            "package-lock.json (Harness-generated only)",
            "tsconfig.json, tsconfig.base.json and workspace tsconfig files",
            "vitest.config.ts and playwright.config.ts",
            "apps/web/vite.config.ts and packages/db/prisma7.config.ts",
            ".sdlc and .github",
        ),
    },
}


def runtime_config_policy(project_profile_id: str) -> dict[str, object] | None:
    """Return the runtime config policy of a project profile, or None when it has none."""

    return _POLICY_BY_PROJECT.get(project_profile_id)


def runtime_config_prompt_context(policy: dict[str, object]) -> dict[str, object]:
    return {
        "policy_version": RUNTIME_CONFIG_POLICY_VERSION,
        "change_kinds": {
            kind: {"meaning": meaning, "fields": list(_CHANGE_FIELDS[kind])}
            for kind, meaning in dict(policy["kinds"]).items()
        },
        "locked_configuration": list(policy["locked"]),
    }


def canonical_runtime_config_impact(value: object) -> dict[str, object]:
    """Keep only the typed fields of a Planner decision."""

    impact = value if isinstance(value, dict) else {}
    changes = []
    for item in impact.get("changes") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        change: dict[str, object] = {"kind": kind, "unit_id": str(item.get("unit_id") or "").strip()}
        for field in _CHANGE_FIELDS.get(kind, ()):
            change[field] = bool(item.get(field)) if field == "dev" else str(item.get(field) or "").strip()
        changes.append(change)
    return {
        "status": str(impact.get("status") or "").strip(),
        "reason": str(impact.get("reason") or "").strip(),
        "changes": changes,
    }


def runtime_config_impact_issues(
    *,
    structured_plan: dict[str, object],
    repo_runtime_config: dict[str, object],
    planning_policy: PlanningPathPolicy,
) -> list[str]:
    """Deterministic shape checks; whether a change is needed is the Validator's call."""

    policy = runtime_config_policy(planning_policy.project_profile_id)
    if policy is None:
        return []
    impact = canonical_runtime_config_impact(structured_plan.get("runtime_config_impact"))
    issues: list[str] = []
    status, changes = impact["status"], impact["changes"]
    if status not in RUNTIME_CONFIG_STATUSES:
        return ["Plan must decide runtime_config_impact: `required` or `not_required` with a reason."]
    if not impact["reason"]:
        issues.append("runtime_config_impact needs a reason.")
    if status == "not_required" and changes:
        issues.append("runtime_config_impact is `not_required` but lists changes.")
    if status == "required" and not changes:
        issues.append("runtime_config_impact is `required` but lists no change.")

    targets_by_unit = {
        str(unit.get("unit_id") or ""): {
            str(target.get("path") or "") for target in unit.get("planned_code_targets") or [] if isinstance(target, dict)
        }
        for unit in structured_plan.get("work_units") or []
        if isinstance(unit, dict)
    }
    env_reads = {str(item.get("name") or "") for item in repo_runtime_config.get("env_reads") or []}
    manifests = {
        str(item.get("manifest") or ""): {*item.get("dependencies", []), *item.get("dev_dependencies", [])}
        for item in repo_runtime_config.get("workspaces") or []
    }
    services = {str(item.get("service") or "") for item in repo_runtime_config.get("compose_services") or []}
    kinds = dict(policy["kinds"])
    for index, change in enumerate(changes, start=1):
        label = f"runtime_config_impact change {index}"
        kind, unit_id = str(change["kind"]), str(change["unit_id"])
        if kind not in kinds:
            issues.append(f"{label} has unsupported kind `{kind}`.")
            continue
        if unit_id not in targets_by_unit:
            issues.append(f"{label} names unknown work unit `{unit_id}`.")
        unit_targets = targets_by_unit.get(unit_id, set())
        if not change.get("purpose"):
            issues.append(f"{label} needs a purpose.")
        if kind == "env_var":
            name, read_by = str(change["name"]), str(change["read_by"])
            if not _ENV_NAME.fullmatch(name):
                issues.append(f"{label} env var name `{name}` must match [A-Z][A-Z0-9_]*.")
            elif name in env_reads:
                issues.append(f"{label} env var `{name}` is already read by the repository; it is not new.")
            if not change.get("default"):
                issues.append(f"{label} env var `{name}` needs a non-empty default.")
            if read_by not in unit_targets or planning_policy.normalize_mapping_path(read_by) != read_by:
                issues.append(f"{label} read_by `{read_by}` must be a source target of unit `{unit_id}`.")
        elif kind == "npm_package":
            manifest, package, used_by = str(change["manifest"]), str(change["package"]), str(change["used_by"])
            if manifest not in manifests:
                issues.append(f"{label} manifest `{manifest}` is not a workspace manifest of the repository.")
            elif package in manifests[manifest]:
                issues.append(f"{label} package `{package}` is already declared in `{manifest}`.")
            if not _NPM_NAME.fullmatch(package):
                issues.append(f"{label} package name `{package}` is not a valid npm name.")
            if not _EXACT_VERSION.fullmatch(str(change["version"])):
                issues.append(f"{label} package `{package}` needs an exact version, not a range.")
            workspace = str(PurePosixPath(manifest).parent)
            if used_by not in unit_targets or not used_by.startswith(f"{workspace}/"):
                issues.append(f"{label} used_by `{used_by}` must be a target of unit `{unit_id}` inside `{workspace}`.")
        elif kind == "compose_service":
            service, image = str(change["service"]), str(change["image"])
            if not _SERVICE_NAME.fullmatch(service):
                issues.append(f"{label} service name `{service}` must match [a-z][a-z0-9-]*.")
            elif service in services:
                issues.append(f"{label} service `{service}` already exists in compose.yaml.")
            tag = image.rpartition(":")[2] if ":" in image.rpartition("/")[2] else ""
            if not tag or tag == "latest":
                issues.append(f"{label} image `{image}` needs a pinned tag other than `latest`.")
            if not change.get("healthcheck"):
                issues.append(f"{label} service `{service}` needs a healthcheck command.")
    return issues


def unsupported_runtime_config_changes(structured_plan: dict[str, object]) -> list[dict[str, object]]:
    """Changes of an approved plan that Phase 4 cannot materialize yet."""

    profile = structured_plan.get("planning_profile")
    policy = runtime_config_policy(str(profile.get("project_profile_id") or "") if isinstance(profile, dict) else "")
    if policy is None:
        return []
    impact = canonical_runtime_config_impact(structured_plan.get("runtime_config_impact"))
    executable = set(policy["executable_kinds"])
    return [change for change in impact["changes"] if change["kind"] not in executable]
