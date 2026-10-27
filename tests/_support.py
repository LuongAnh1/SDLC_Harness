"""Hạ tầng dùng chung cho test, không chứa hành vi production."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable


PopulateRepository = Callable[[Path], None]


def populate_typescript_target(target: Path) -> None:
    """Dựng target TypeScript tối thiểu nhưng đủ manifest và verification contract."""

    from sdlc_harness.profiles import get_language_profile, get_project_profile

    project = get_project_profile("typescript_fullstack_v1")
    language = get_language_profile(project.language_profile_id)
    for component in project.components:
        for editable_root in component.editable_roots:
            root = target / editable_root
            root.mkdir(parents=True, exist_ok=True)
            (root / ".gitkeep").write_text("", encoding="utf-8")
    for required_path in project.required_paths:
        path = target / required_path
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / ".gitkeep").write_text("", encoding="utf-8")

    package = {
        "name": "sdlc-typescript-test-target",
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "typecheck": "node -e \"process.exit(0)\"",
            "test:unit": "node scripts/fake-vitest.cjs",
        },
    }
    lock = {
        "name": package["name"],
        "version": package["version"],
        "lockfileVersion": 3,
        "requires": True,
        "packages": {"": {"name": package["name"], "version": package["version"]}},
    }
    (target / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    (target / "package-lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    (target / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (target / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (target / "tsconfig.base.json").write_text("{}\n", encoding="utf-8")
    (target / "vitest.config.ts").write_text("export default {};\n", encoding="utf-8")
    (target / "playwright.config.ts").write_text("export default {};\n", encoding="utf-8")
    (target / ".github").mkdir(exist_ok=True)
    (target / ".github/.gitkeep").write_text("", encoding="utf-8")
    for relative in (
        "apps/web/package.json",
        "apps/web/tsconfig.json",
        "apps/web/vite.config.ts",
        "apps/api/package.json",
        "apps/api/tsconfig.json",
        "packages/contracts/package.json",
        "packages/contracts/tsconfig.json",
        "packages/db/package.json",
        "packages/db/prisma7.config.ts",
        "packages/db/tsconfig.json",
    ):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "export default {};\n", encoding="utf-8")

    source = target / "apps/api/src/CulturalHeritage.ts"
    source.write_text("export class CulturalHeritage {}\n", encoding="utf-8")
    service = target / "apps/api/src/DeleteCulturalHeritageService.ts"
    service.write_text(
        "export class DeleteCulturalHeritageService { deleteHeritage(id: string): void { void id; } }\n",
        encoding="utf-8",
    )
    test = target / "apps/api/src/CulturalHeritage.test.ts"
    test.write_text('import { describe, expect, it } from "vitest";\ndescribe("heritage", () => { it("works", () => expect(true).toBe(true)); });\n', encoding="utf-8")

    scripts = target / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "fake-vitest.cjs").write_text(
        """const fs = require("node:fs");
const path = require("node:path");
const output = process.argv.find((value) => value.startsWith("--outputFile="));
const selected = process.argv.filter((value) => /\\.(test|spec)\\.tsx?$/.test(value));
if (output) {
  const file = output.slice("--outputFile=".length);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify({
    success: true,
    testResults: selected.map((name) => ({
      name: path.resolve(name),
      assertionResults: [{ status: "passed" }],
    })),
  }));
}
""",
        encoding="utf-8",
    )

    manifest = {
        "version": project.target_manifest_schema,
        "project_profile_id": project.profile_id,
        "project_profile_schema": project.schema_version,
        "language_profile_id": language.profile_id,
        "language_profile_schema": language.schema_version,
        "components": [
            {"component_id": component.component_id, "editable_roots": list(component.editable_roots)}
            for component in project.components
        ],
        "protected_paths": list(project.protected_paths),
        "required_paths": list(project.required_paths),
        "verification_stage_ids": list(project.verification_stage_ids),
    }
    manifest_path = target / ".sdlc/target-profile.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class MaterializedGitRepository:
    remote: Path
    canonical: Path
    seed: Path | None = None


class GitRepositoryTemplate:
    """Giữ bare remote nền và cấp bản sao mutable riêng cho từng test."""

    def __init__(self, populate: PopulateRepository):
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        seed = root / "seed"
        remote = root / "remote.git"
        seed.mkdir()
        remote.mkdir()
        populate(seed)
        git(remote, "init", "--bare")
        git(seed, "init")
        git(seed, "config", "user.name", "SDLC Test")
        git(seed, "config", "user.email", "sdlc-test@example.com")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "initial")
        git(seed, "branch", "-M", "main")
        git(seed, "remote", "add", "origin", str(remote))
        git(seed, "push", "-u", "origin", "main")
        self._remote = remote

    def close(self) -> None:
        self._temp_dir.cleanup()

    def materialize(
        self,
        root: Path,
        *,
        canonical_name: str = "target_repo",
        include_seed: bool = False,
    ) -> MaterializedGitRepository:
        root.mkdir(parents=True, exist_ok=True)
        remote = root / "target.git"
        canonical = root / canonical_name
        shutil.copytree(self._remote, remote)
        git(root, "clone", "-q", "-b", "main", str(remote), str(canonical))
        git(canonical, "config", "user.name", "SDLC Test")
        git(canonical, "config", "user.email", "sdlc-test@example.com")
        seed: Path | None = None
        if include_seed:
            seed = root / "seed"
            git(root, "clone", "-q", "-b", "main", str(remote), str(seed))
            git(seed, "config", "user.name", "SDLC Test")
            git(seed, "config", "user.email", "sdlc-test@example.com")
        return MaterializedGitRepository(remote=remote, canonical=canonical, seed=seed)


class RuntimeDirectoryTemplate:
    """Sao chép checkpoint runtime bất biến và đổi các path máy cục bộ."""

    def __init__(self, runtime_root: Path):
        self._source_runtime_root = runtime_root.resolve()
        self._temp_dir = tempfile.TemporaryDirectory()
        self._snapshot_root = Path(self._temp_dir.name) / "runtime"
        shutil.copytree(self._source_runtime_root, self._snapshot_root)

    def close(self) -> None:
        self._temp_dir.cleanup()

    def materialize(self, destination: Path, *, replacements: dict[str, str] | None = None) -> None:
        destination = destination.resolve()
        if destination.exists():
            raise AssertionError(f"Runtime destination already exists: {destination}")
        shutil.copytree(self._snapshot_root, destination)
        substitutions = {
            str(self._source_runtime_root): str(destination),
            self._source_runtime_root.as_posix(): destination.as_posix(),
            **(replacements or {}),
        }
        for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            rewritten_name = self._rewrite_text(path.name, substitutions)
            if rewritten_name != path.name:
                path.rename(path.with_name(rewritten_name))
        for path in destination.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                rewritten = self._rewrite_value(payload, substitutions)
                path.write_text(
                    json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                continue
            if path.suffix in {".md", ".txt", ".jsonl"}:
                content = path.read_text(encoding="utf-8")
                path.write_text(self._rewrite_text(content, substitutions), encoding="utf-8")
        self._refresh_implementation_plan_bindings(destination)
        self._refresh_vertical_design_bindings(destination)

    @staticmethod
    def _refresh_implementation_plan_bindings(destination: Path) -> None:
        """Rebind copied Phase 3 fixtures after their machine-local paths change."""

        for review_path in destination.glob(
            "runs/*/artifacts/implementation_plan_review_v*.json"
        ):
            suffix = review_path.stem.removeprefix("implementation_plan_review_")
            plan_path = review_path.with_name(f"implementation_plan_structured_{suffix}.json")
            if not plan_path.is_file():
                continue
            review = json.loads(review_path.read_text(encoding="utf-8"))
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            review["implementation_plan_contract_version"] = str(plan.get("version") or "")
            review["implementation_plan_sha256"] = (
                "sha256:" + hashlib.sha256(plan_path.read_bytes()).hexdigest()
            )
            review_path.write_text(
                json.dumps(review, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _refresh_vertical_design_bindings(destination: Path) -> None:
        """Recompute immutable graph digests after test fixture run/path substitution."""

        from sdlc_harness.agents.designer.graph import graph_digest
        from sdlc_harness.agents.requirements import RequirementModelV2
        from sdlc_harness.agents.validator import build_vertical_trace_graph, validate_vertical_trace_coverage

        for state_path in destination.glob("runs/*/state.json"):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            artifacts = state.get("latest_artifacts")
            if not isinstance(artifacts, dict):
                continue

            def artifact(key: str) -> Path | None:
                value = str(artifacts.get(key) or "")
                if not value:
                    return None
                path = Path(value)
                return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path

            design_path = artifact("vertical_design_graph")
            requirement_path = artifact("requirement_model")
            trace_path = artifact("vertical_trace_graph")
            report_path = artifact("vertical_trace_validation")
            if not all(path and path.is_file() for path in (design_path, requirement_path, trace_path, report_path)):
                continue
            design = json.loads(design_path.read_text(encoding="utf-8"))
            design["artifact_digest"] = graph_digest(design)
            design_path.write_text(json.dumps(design, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            requirement = RequirementModelV2.from_dict(json.loads(requirement_path.read_text(encoding="utf-8")))
            trace = build_vertical_trace_graph(requirement_model=requirement, design_graph=design)
            trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report = validate_vertical_trace_coverage(
                trace,
                mode=str(state.get("vertical_trace_mode") or "shadow"),
            ).to_dict()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _rewrite_value(self, value: object, substitutions: dict[str, str]) -> object:
        if isinstance(value, dict):
            return {key: self._rewrite_value(item, substitutions) for key, item in value.items()}
        if isinstance(value, list):
            return [self._rewrite_value(item, substitutions) for item in value]
        if isinstance(value, str):
            return self._rewrite_text(value, substitutions)
        return value

    @staticmethod
    def _rewrite_text(value: str, substitutions: dict[str, str]) -> str:
        for source, destination in substitutions.items():
            value = value.replace(source, destination)
        return value


class DirectoryOverlayTemplate:
    """Lưu cây file làm overlay, bỏ metadata Git machine-local."""

    def __init__(self, source: Path):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._snapshot_root = Path(self._temp_dir.name) / "overlay"
        shutil.copytree(source, self._snapshot_root, ignore=shutil.ignore_patterns(".git"))

    def apply(self, destination: Path) -> None:
        shutil.copytree(self._snapshot_root, destination, dirs_exist_ok=True)

    def close(self) -> None:
        self._temp_dir.cleanup()


def create_fast_run_workspace(manager: object, *, run_id: str, state: dict[str, object]):
    """Tạo worktree thật, bỏ các guard đã được khóa ở workspace contract suite."""

    from sdlc_harness.storage.runtime import utc_now_iso
    from sdlc_harness.workspaces import RunWorkspaceRecord, branch_name_for_run

    canonical = manager.settings.target_repo_path.resolve()
    worktree = manager.expected_worktree_path(run_id)
    base_ref = f"origin/{manager.settings.target_base_branch}"
    base_sha = git(canonical, "rev-parse", "--verify", base_ref)
    branch = branch_name_for_run(
        prefix=manager.settings.implementation_branch_prefix,
        source_ref=str(state.get("source_ref") or ""),
        issue_key=manager._source_identity(state),
        run_id=run_id,
    )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(canonical, "worktree", "add", "-q", "-b", branch, str(worktree), base_sha)
    now = utc_now_iso()
    record = RunWorkspaceRecord(
        run_id=run_id,
        source_ref=str(state.get("source_ref") or ""),
        canonical_repo=str(canonical),
        worktree_path=str(worktree),
        branch=branch,
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=base_sha,
        remote=manager.settings.target_repo_remote,
        lifecycle="active",
        provisioning_mode="created_for_run",
        created_at=now,
        updated_at=now,
    )
    profile_changed = manager._bind_or_validate_profile(record, state=state, allow_bind=True)
    manager._write_record_locked(record)
    manager._reconcile_state(record, state=state, force_write=profile_changed)
    return record

