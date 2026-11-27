"""H-OC Phase 3: the repository facts the Plan graph needs, read mechanically from the worktree and the profile.

The Plan graph (`agents/developer/plan_graph_v1.py`) types every Design `Uses` edge from these facts, never from unit
names: which package a file belongs to and which packages its code may import (workspace `package.json`), which files a
verification stage runs as a whole-system check (stage commands followed through npm scripts, plus the e2e component),
which files feed a code generator and who compiles against the output (the profile's generated-source preparation),
which files are pure re-export barrels, and which middleware a unit's code is mounted in front of (candidates for a
`reliance` edge the Planner keeps or drops).

Plan `docs/maintenance/2026-11-27-h-oc-phase3-plan-graph.md` sections 2.2, 2.3 and 10.1-10.2.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
import posixpath
import re
import shlex

from .landscape import after_mount_middleware
from .providers import _IMPORT, _REEXPORT, _defining_file, _imported_names, _resolve_specifier, _workspace_packages
from .runtime_config import _workspaces

PLAN_FACTS_SCHEMA_VERSION = "plan_repo_facts_v1"
CHECK_COMPONENT_IDS = ("e2e",)
GENERATED_SOURCE_PREPARATION = "generated_source_setup"
GATE_STAGE_IDS = ("typecheck", "unit")
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts")
_SKIPPED_PARTS = frozenset({"node_modules", "generated", "dist", "build", "coverage", ".codegraph"})
_MAX_SCRIPT_DEPTH = 5
_MAX_IMPORTER_DEPTH = 4
_RUNNERS = frozenset({"tsx", "node", "ts-node"})
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


@dataclass(frozen=True)
class RepoFacts:
    """What the Plan graph may know about the repository; every field is derived, none is written by an LLM."""

    package_dirs: tuple[str, ...]
    imports: Mapping[str, frozenset[str]]
    check_files: frozenset[str]
    generate_inputs: frozenset[str]
    generated_consumers: frozenset[str]
    barrels: frozenset[str]
    editable_roots: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    gate_stage_ids: tuple[str, ...] = GATE_STAGE_IDS
    reliance_candidates: tuple[dict[str, object], ...] = field(default_factory=tuple)
    tests: tuple[dict[str, object], ...] = field(default_factory=tuple)
    test_suffixes: tuple[str, ...] = ()
    check_roots: tuple[str, ...] = ()

    def package_of(self, path: str) -> str:
        return package_of(path, self.package_dirs)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RepoFacts:
        """Read back `to_dict` output (a stored plan input)."""

        imports = data.get("imports") if isinstance(data.get("imports"), dict) else {}
        return cls(
            package_dirs=tuple(str(item) for item in data.get("package_dirs") or ()),
            imports={str(key): frozenset(str(item) for item in value) for key, value in imports.items()},
            check_files=frozenset(str(item) for item in data.get("check_files") or ()),
            generate_inputs=frozenset(str(item) for item in data.get("generate_inputs") or ()),
            generated_consumers=frozenset(str(item) for item in data.get("generated_consumers") or ()),
            barrels=frozenset(str(item) for item in data.get("barrels") or ()),
            editable_roots=tuple(str(item) for item in data.get("editable_roots") or ()),
            protected_paths=tuple(str(item) for item in data.get("protected_paths") or ()),
            gate_stage_ids=(
                tuple(str(item) for item in data.get("gate_stage_ids") or ())
                if "gate_stage_ids" in data else GATE_STAGE_IDS
            ),
            reliance_candidates=tuple(dict(item) for item in data.get("reliance_candidates") or ()),
            tests=tuple(dict(item) for item in data.get("tests") or ()),
            test_suffixes=tuple(str(item) for item in data.get("test_suffixes") or ()),
            check_roots=tuple(str(item) for item in data.get("check_roots") or ()),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_FACTS_SCHEMA_VERSION,
            "package_dirs": list(self.package_dirs),
            "imports": {key: sorted(value) for key, value in sorted(self.imports.items())},
            "check_files": sorted(self.check_files),
            "generate_inputs": sorted(self.generate_inputs),
            "generated_consumers": sorted(self.generated_consumers),
            "barrels": sorted(self.barrels),
            "editable_roots": list(self.editable_roots),
            "protected_paths": list(self.protected_paths),
            "gate_stage_ids": list(self.gate_stage_ids),
            "reliance_candidates": list(self.reliance_candidates),
            "tests": list(self.tests),
            "test_suffixes": list(self.test_suffixes),
            "check_roots": list(self.check_roots),
        }


def clean_path(path: str) -> str:
    """A Design file reference without its ``(new)`` marker or backticks."""

    return path.replace("(new)", "").strip().strip("`").strip().replace("\\", "/")


def package_of(path: str, package_dirs: Iterable[str]) -> str:
    """The workspace package directory owning `path` (longest prefix); ``""`` is the root package."""

    best = ""
    for directory in package_dirs:
        if directory and (path == directory or path.startswith(directory + "/")) and len(directory) > len(best):
            best = directory
    return best


def build_repo_facts(
    target_path: Path,
    unit_files: Mapping[str, Sequence[str]],
    *,
    component_roots: Mapping[str, Sequence[str]],
    protected_paths: Sequence[str] = (),
    stage_argvs: Iterable[tuple[Sequence[str], str]] = (),
    generate_triggers: Iterable[str] = (),
    gate_stage_ids: Sequence[str] = GATE_STAGE_IDS,
    check_component_ids: Sequence[str] = CHECK_COMPONENT_IDS,
    test_suffixes: Sequence[str] = (),
) -> RepoFacts:
    """Facts for the units' files. `stage_argvs` are ``(argv, cwd)`` pairs of the pinned verification stages."""

    root = target_path.resolve()
    files_by_unit = {unit: [clean_path(path) for path in paths] for unit, paths in unit_files.items()}
    all_files = sorted({path for paths in files_by_unit.values() for path in paths})
    workspaces = _workspaces(root)
    dirs = sorted({posixpath.dirname(str(item["manifest"])) for item in workspaces})
    names = {str(item["name"]): posixpath.dirname(str(item["manifest"])) for item in workspaces if item.get("name")}
    imports = {
        posixpath.dirname(str(item["manifest"])): frozenset(
            {posixpath.dirname(str(item["manifest"]))}
            | {names[dep] for dep in [*item["dependencies"], *item["dev_dependencies"]] if dep in names}
        )
        for item in workspaces
    }
    triggers = frozenset(clean_path(path) for path in generate_triggers)
    consumers: set[str] = set()
    for trigger in triggers:
        owner = package_of(trigger, dirs)
        consumers |= {owner} | _dependents(owner, imports)
    check_roots = [
        root_path.rstrip("/")
        for component in check_component_ids
        for root_path in component_roots.get(component, ())
    ]
    entries = _stage_entry_files(root, stage_argvs, names)
    check_files = frozenset(
        path
        for path in all_files
        if path in entries or any(path == item or path.startswith(item + "/") for item in check_roots)
    )
    barrels = frozenset(path for path in all_files if (root / path).is_file() and is_barrel((root / path).read_text(
        encoding="utf-8", errors="replace")))
    editable = tuple(sorted({item.rstrip("/") for roots in component_roots.values() for item in roots}))
    return RepoFacts(
        package_dirs=tuple(dirs),
        imports=imports,
        check_files=check_files,
        generate_inputs=triggers,
        generated_consumers=frozenset(consumers),
        barrels=barrels,
        editable_roots=editable,
        protected_paths=tuple(protected_paths),
        gate_stage_ids=tuple(gate_stage_ids),
        reliance_candidates=tuple(reliance_candidates(root, files_by_unit, dirs)),
        tests=tuple(existing_tests(root, files_by_unit, dirs, check_roots, entries, tuple(test_suffixes))),
        test_suffixes=tuple(test_suffixes),
        check_roots=tuple(check_roots),
    )


def repo_facts_for_profile(
    target_path: Path, unit_files: Mapping[str, Sequence[str]], *, project_profile_id: str
) -> RepoFacts:
    """`build_repo_facts` fed from the pinned project profile and its verification registry."""

    from ..profiles import build_verification_stage_policy
    from ..profiles.registry import get_language_profile, get_project_profile
    from ..verification.registry import resolve_verification_preparation_specs, resolve_verification_stage_specs

    project = get_project_profile(project_profile_id)
    policy = build_verification_stage_policy(project=project)
    stages = resolve_verification_stage_specs(policy)
    preparations = resolve_verification_preparation_specs(policy)
    argvs: list[tuple[Sequence[str], str]] = []
    for stage in stages:
        argvs.append((stage.argv, stage.cwd))
        argvs.extend((extra, stage.cwd) for extra in stage.additional_argv)
    return build_repo_facts(
        target_path,
        unit_files,
        component_roots={component.component_id: component.editable_roots for component in project.components},
        protected_paths=project.protected_paths,
        stage_argvs=argvs,
        generate_triggers=[item.trigger_path for item in preparations if item.preparation_id == GENERATED_SOURCE_PREPARATION],
        gate_stage_ids=tuple(stage_id for stage_id in GATE_STAGE_IDS if stage_id in {stage.stage_id for stage in stages}),
        test_suffixes=get_language_profile(project.language_profile_id).test_suffixes,
    )


def is_barrel(text: str) -> bool:
    """True when every statement of a module is an `export ... from` re-export (order carries no meaning)."""

    statements = [item.strip() for item in _COMMENTS.sub("", text).split(";") if item.strip()]
    return bool(statements) and all(_REEXPORT.fullmatch(item) for item in statements)


def _dependents(package: str, imports: Mapping[str, frozenset[str]]) -> set[str]:
    """Packages whose code may import `package`, directly or through other packages."""

    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for directory, allowed in imports.items():
            if directory not in found and directory != package and (package in allowed or allowed & found):
                found.add(directory)
                changed = True
    return found


def _stage_entry_files(
    root: Path, stage_argvs: Iterable[tuple[Sequence[str], str]], names: Mapping[str, str]
) -> set[str]:
    """Files a stage runs as a program (`tsx file`, `node file`), following `npm run` scripts across workspaces."""

    scripts: dict[str, dict[str, str]] = {}

    def package_scripts(directory: str) -> dict[str, str]:
        if directory not in scripts:
            try:
                manifest = json.loads((root / directory / "package.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            raw = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
            scripts[directory] = {str(key): str(value) for key, value in raw.items()}
        return scripts[directory]

    found: set[str] = set()

    def visit(tokens: list[str], directory: str, depth: int) -> None:
        if not tokens or depth > _MAX_SCRIPT_DEPTH:
            return
        if tokens[0] in _RUNNERS:
            target = next((token for token in tokens[1:] if not token.startswith("-")), "")
            if target:
                found.add(posixpath.normpath(posixpath.join(directory, target)) if directory else posixpath.normpath(target))
            return
        if tokens[0] == "npm" and len(tokens) > 2 and tokens[1] in {"run", "run-script"}:
            script, workspace = tokens[2], directory
            for index, token in enumerate(tokens):
                if token in {"--workspace", "-w"} and index + 1 < len(tokens):
                    workspace = names.get(tokens[index + 1], tokens[index + 1])
                elif token.startswith("--workspace="):
                    value = token.split("=", 1)[1]
                    workspace = names.get(value, value)
            command = package_scripts(workspace).get(script, "")
            for part in re.split(r"&&|\|\||;", command):
                try:
                    visit(shlex.split(part), workspace, depth + 1)
                except ValueError:
                    continue

    for argv, cwd in stage_argvs:
        directory = "" if cwd in {"", "."} else cwd.strip("/")
        visit(list(argv), directory, 0)
    return found


def _source_files(root: Path, dirs: Sequence[str]) -> list[str]:
    files: set[str] = set()
    for directory in dirs:
        if not directory:
            continue
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root).as_posix()
            parts = set(relative.split("/"))
            if path.suffix in _SOURCE_SUFFIXES and path.is_file() and not parts & _SKIPPED_PARTS and ".test." not in path.name:
                files.add(relative)
    return sorted(files)


def reliance_candidates(
    root: Path, files_by_unit: Mapping[str, Sequence[str]], dirs: Sequence[str]
) -> list[dict[str, object]]:
    """`u -> v` when u's code is mounted in front of middleware that lives in a file of unit v.

    From each existing file of u the walk goes up the importers (barrels included) to the code that registers it, then
    `after_mount_middleware` names the pathless middleware registered after that mount (Express error handlers).
    Evidence only: the Planner keeps or drops each candidate.
    """

    sources = _source_files(root, dirs)
    packages = _workspace_packages(root)
    importers: dict[str, set[str]] = {}
    for source in sources:
        text = (root / source).read_text(encoding="utf-8", errors="replace")
        # Type-only imports carry no running code, so nothing in them can pass an error to middleware.
        specifiers = [
            match.group(2)
            for pattern, type_only in ((_IMPORT, "import type "), (_REEXPORT, "export type "))
            for match in pattern.finditer(text)
            if not match.group(0).startswith(type_only)
        ]
        for specifier in specifiers:
            module = _resolve_specifier(root, source, specifier, packages)
            if module and module != source:
                importers.setdefault(module, set()).add(source)
    owners: dict[str, list[str]] = {}
    for unit, paths in files_by_unit.items():
        for path in paths:
            owners.setdefault(path, []).append(unit)
    walks: dict[str, dict[str, str]] = {}
    for unit, paths in files_by_unit.items():
        existing = [path for path in paths if (root / path).is_file()]
        # A barrel is shared by many units: walk from it only when the unit has no file of its own yet.
        own = [path for path in existing if not is_barrel((root / path).read_text(encoding="utf-8", errors="replace"))]
        reached = {path: path for path in (own or existing)}  # reached file -> the unit file it came from
        frontier = list(reached)
        for _ in range(_MAX_IMPORTER_DEPTH):
            following: list[str] = []
            for child in frontier:
                for parent in sorted(importers.get(child, ())):
                    if parent not in reached:
                        reached[parent] = reached[child]
                        following.append(parent)
            frontier = following
            if not frontier:
                break
        walks[unit] = reached
    every = sorted({path for reached in walks.values() for path in reached})
    middleware = after_mount_middleware(root, files=every, source_files=sources) if every else {}
    result: dict[tuple[str, str], dict[str, object]] = {}
    for unit in sorted(walks, key=_unit_number):
        for start, origin in sorted(walks[unit].items()):
            for entry in middleware.get(start, []):
                for owner in owners.get(str(entry.get("path") or ""), []):
                    if owner == unit:
                        continue
                    row = result.setdefault((unit, owner), {"from": unit, "to": owner, "evidence": []})
                    evidence = {
                        "unit_file": origin,
                        "mounted_file": start,
                        "registered_by": entry.get("registered_by"),
                        "line": entry.get("line"),
                        "middleware": entry.get("name"),
                        "middleware_file": entry.get("path"),
                    }
                    if evidence not in row["evidence"]:
                        row["evidence"].append(evidence)
    return [result[key] for key in sorted(result, key=lambda item: (_unit_number(item[0]), _unit_number(item[1])))]


_MOCK = re.compile(r"""\b(?:vi|jest)\.mock\(\s*["']([^"']+)["']""")


def existing_tests(
    root: Path,
    files_by_unit: Mapping[str, Sequence[str]],
    dirs: Sequence[str],
    check_roots: Sequence[str],
    stage_entries: Iterable[str],
    test_suffixes: Sequence[str],
) -> list[dict[str, object]]:
    """Every existing test file that touches a Design unit, with the units it touches.

    A test touches unit U when it imports a name whose defining file belongs to U (barrels are followed to the
    defining file, because a barrel is written by many units), or when it mocks a module that belongs to U. Test files
    that are themselves Design unit files are left out: their unit already owns them. `check` marks whole-system
    checks (inside an e2e component or run directly by a stage).
    """

    if not test_suffixes:
        return []
    owners: dict[str, list[str]] = {}
    for unit, paths in files_by_unit.items():
        for path in paths:
            file = root / path
            if file.is_file() and is_barrel(file.read_text(encoding="utf-8", errors="replace")):
                continue
            owners.setdefault(path, []).append(unit)
    unit_files = {path for paths in files_by_unit.values() for path in paths}
    entries = set(stage_entries)
    packages = _workspace_packages(root)
    candidates: set[str] = set()
    for directory in [*[d for d in dirs if d], *check_roots]:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and path.name.endswith(tuple(test_suffixes)) and not set(relative.split("/")) & _SKIPPED_PARTS:
                candidates.add(relative)
    result: list[dict[str, object]] = []
    for test in sorted(candidates - unit_files):
        text = (root / test).read_text(encoding="utf-8", errors="replace")
        touched: set[str] = set()
        for clause, specifier in _IMPORT.findall(text):
            module = _resolve_specifier(root, test, specifier, packages)
            if not module:
                continue
            for name in _imported_names(clause):
                touched.add(_defining_file(root, module, name, depth=0) or module)
        for specifier in _MOCK.findall(text):
            module = _resolve_specifier(root, test, specifier, packages)
            if module:
                touched.add(module)
        units = sorted({unit for path in touched for unit in owners.get(path, [])}, key=_unit_number)
        if units:
            check = test in entries or any(test.startswith(item.rstrip("/") + "/") for item in check_roots)
            result.append({"path": test, "units": units, "check": check})
    return result


def _unit_number(unit: str) -> int:
    digits = re.sub(r"\D", "", unit)
    return int(digits) if digits else 0
