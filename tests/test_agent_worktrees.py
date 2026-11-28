"""Agent worktree and merge gate tests using local Git and patched verification commands."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
from types import SimpleNamespace

import pytest

from sdlc_harness.agents.developer.hoc_merge_queue import attributes_content
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_verification_stage_policy, get_project_profile
from sdlc_harness.profiles.verification import BROWSER_STAGE_POLICY_VERSION, build_verification_stage_policy_version
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.verification import runner as verification
from sdlc_harness.workspaces.agent_worktrees import AgentWorktrees, make_merge_gate
from sdlc_harness.workspaces.manager import WORKSPACE_ARTIFACT
from sdlc_harness.workspaces.model import RunWorkspaceRecord

RUN = "run_20261128_100000_00000002"
BRANCH = "implementation/agent-worktrees-00000002"
BARREL = "src/index.ts"
GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
PROJECT = get_project_profile("typescript_fullstack_v1")


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write(root, file, content):
    path = root / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def commit(root, file, content):
    write(root, file, content)
    git(root, "add", "--", file)
    git(root, "commit", "-m", f"Update {file}")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path):
    owner = tmp_path / "owner checkout"
    owner.mkdir()
    git(owner, "init", "-b", "main")
    for key, value in [
        ("user.name", "Agent Tests"), ("user.email", "agent@example.invalid"), ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"), ("core.hooksPath", str(tmp_path / "no-hooks")),
    ]:
        git(owner, "config", key, value)
    commit(owner, BARREL, 'export { base } from "./base";\n')
    base = commit(owner, "src/shared.ts", "base\n")
    settings = SimpleNamespace(
        repo_root=tmp_path, runtime_root=tmp_path / "runtime", target_worktree_root=tmp_path / "worktrees",
    )
    h = settings.target_worktree_root / RUN
    h.parent.mkdir()
    git(owner, "worktree", "add", "-b", BRANCH, str(h), base)
    worktrees = AgentWorktrees(settings, run_id=RUN)
    record = RunWorkspaceRecord(
        run_id=RUN, source_ref="fixture:test", canonical_repo=str(owner), worktree_path=str(h),
        branch=BRANCH, base_ref="origin/main", base_sha=base, head_sha=base,
        remote="https://example.invalid/test", lifecycle="active", provisioning_mode="created_for_run",
        created_at="2026-11-28T00:00:00Z", updated_at="2026-11-28T00:00:00Z",
    )
    worktrees.manager.store.write_json_artifact(RUN, WORKSPACE_ARTIFACT, record.to_dict())
    return worktrees, owner, h, base


def run_attributes(worktrees, barrels=()):
    path = worktrees.manager.store.artifact_path(RUN, "attributes")
    path.write_text(attributes_content(barrels), encoding="utf-8")
    return path


@GIT
def test_create_uses_run_location_and_preserves_owner_checkout(workspace):
    worktrees, owner, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    assert agent.root == h.parent / f"{RUN}-agent-1"
    assert agent.branch == f"{BRANCH}-agent-1"
    assert git(agent.root, "branch", "--show-current") == agent.branch
    assert git(agent.root, "rev-parse", "HEAD") == base
    next_tip = commit(agent.root, "src/a.ts", "agent work\n")
    assert worktrees.create(1, base_sha=base) == agent
    assert git(agent.root, "rev-parse", "HEAD") == next_tip
    assert git(owner, "branch", "--show-current") == "main"
    assert git(owner, "rev-parse", "HEAD") == git(h, "rev-parse", "HEAD") == base
    with pytest.raises(HarnessError, match="positive integer"):
        worktrees.remove(None)


@GIT
def test_merge_uses_run_union_attributes_without_touching_shared_attributes(workspace):
    worktrees, owner, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    attrs = run_attributes(worktrees, [BARREL])
    shared_attrs = owner / ".git/info/attributes"
    shared_attrs.write_bytes(b"# owner attributes\n")
    original = 'export { base } from "./base";\n'
    own_tip = commit(agent.root, BARREL, original + 'export { agent } from "./agent";\n')
    h_tip = commit(h, BARREL, original + 'export { harness } from "./harness";\n')
    assert worktrees.merge(1, sha=h_tip, attributes_file=attrs) == ()
    assert set((agent.root / BARREL).read_text(encoding="utf-8").splitlines()) == {
        'export { base } from "./base";', 'export { agent } from "./agent";', 'export { harness } from "./harness";',
    }
    assert git(agent.root, "rev-parse", "HEAD^1", "HEAD^2").splitlines() == [own_tip, h_tip]
    assert git(h, "rev-parse", "HEAD") == h_tip
    assert shared_attrs.read_bytes() == b"# owner attributes\n"


@GIT
def test_merge_refuses_dirty_tree_and_aborts_conflicts(workspace):
    worktrees, _, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    attrs = run_attributes(worktrees)
    own_tip = commit(agent.root, "src/shared.ts", "agent\n")
    h_tip = commit(h, "src/shared.ts", "harness\n")
    write(agent.root, "src/untracked.ts", "dirty\n")
    with pytest.raises(HarnessError, match="clean agent worktree"):
        worktrees.merge(1, sha=h_tip, attributes_file=attrs)
    (agent.root / "src/untracked.ts").unlink()
    assert worktrees.merge(1, sha=h_tip, attributes_file=attrs) == ("src/shared.ts",)
    assert git(agent.root, "rev-parse", "HEAD") == own_tip
    assert git(agent.root, "status", "--porcelain") == ""
    assert (agent.root / "src/shared.ts").read_text(encoding="utf-8") == "agent\n"


@GIT
def test_stage_local_file_is_ignored_read_only_and_replaceable(workspace):
    worktrees, _, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    for number, root in [(1, agent.root), (None, h)]:
        ref = worktrees.stage_local_file(number, name="design.md", text="Design\n")
        assert ref == "docs/sdlc-local/design.md"
        assert git(root, "check-ignore", "--", ref) == ref
        assert not (root / ref).stat().st_mode & stat.S_IWRITE
        worktrees.stage_local_file(number, name="design.md", text="Updated design\n")
        assert (root / ref).read_text(encoding="utf-8") == "Updated design\n"
        assert not (root / ref).stat().st_mode & stat.S_IWRITE
        assert git(root, "status", "--porcelain") == ""
    with pytest.raises(HarnessError, match="Invalid local snapshot"):
        worktrees.stage_local_file(1, name="../escape.md", text="Outside")
    assert worktrees.remove(1) is None


@GIT
@pytest.mark.parametrize("merged", [True, False])
def test_remove_requires_merge_or_bundle_and_preserves_the_exact_tip(workspace, merged):
    worktrees, owner, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    tip = commit(agent.root, "src/a.ts", "agent work\n")
    with pytest.raises(HarnessError, match="audit bundle"):
        worktrees.remove(1)
    assert git(agent.root, "rev-parse", "HEAD") == tip
    if merged:
        git(h, "merge", "--no-ff", "--no-edit", tip)
        assert worktrees.remove(1) is None
    else:
        bundle = worktrees.remove(1, bundle_unmerged=True)
        assert bundle.parent == worktrees.manager.store.run_dir(RUN) / "audit"
        assert git(h, "bundle", "list-heads", str(bundle)) == f"{tip} {agent.ref}"
        assert git(h, "rev-parse", "HEAD") == base
    assert not agent.root.exists()
    assert agent.branch not in git(owner, "branch", "--format=%(refname:short)").splitlines()
    assert git(owner, "rev-parse", "HEAD") == base
    assert worktrees.remove(1) is None


def link_directory(link, target):
    """A directory junction on Windows (what npm creates for workspace packages), a symlink elsewhere."""

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


@GIT
@pytest.mark.parametrize("restart", [False, True])
def test_remove_unlinks_dependency_links_without_following_them(workspace, tmp_path, restart):
    worktrees, owner, h, base = workspace
    agent = worktrees.create(1, base_sha=base)
    commit(agent.root, ".gitignore", "node_modules/\n")
    commit(agent.root, "apps/api/package.json", "{}\n")
    git(h, "merge", "--no-ff", "--no-edit", git(agent.root, "rev-parse", "HEAD"))
    outside = tmp_path / "outside package"
    write(outside, "keep.txt", "keep\n")
    link_directory(agent.root / "node_modules/@t/api", agent.root / "apps/api")
    link_directory(agent.root / "node_modules/@t/outside", outside)
    write(agent.root, "node_modules/plain/index.js", "module\n")
    if restart:
        worktrees.remove_all_locked(restart_audit=worktrees.manager.store.run_dir(RUN) / "audit" / "restart")
    else:
        assert worktrees.remove(1) is None
    assert not agent.root.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert git(h, "worktree", "list", "--porcelain").count("worktree ") == 2


@GIT
def test_prepare_reuses_runner_fingerprints_for_dependencies_and_generated_sources(workspace, monkeypatch):
    worktrees, _, _, base = workspace
    root = worktrees.create(1, base_sha=base).root
    for file in (
        "package-lock.json", "packages/db/package.json", "packages/db/prisma7.config.ts", "packages/db/prisma/schema.prisma",
    ):
        write(root, file, "original input\n")
    calls = []

    def run(argv, *, cwd, env, timeout):
        calls.append((tuple(argv[1:]), Path(cwd)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(verification.shutil, "which", lambda tool, **options: f"fake-{tool}")
    monkeypatch.setattr(verification, "_run_bounded", run)
    policy = build_verification_stage_policy(project=PROJECT)
    assert [item.stage_id for item in worktrees.prepare(
        1, verification_policy=policy, preparation_ids=("dependency_setup",),
    )] == ["dependency_setup"]
    assert calls == [(("ci",), root)]
    assert all(item.status == "passed" for item in worktrees.prepare(1, verification_policy=policy))
    worktrees.prepare(1, verification_policy=policy)
    assert calls == [(("ci",), root), (("run", "db:generate"), root)]
    write(root, "packages/db/prisma/schema.prisma", "updated schema\n")
    worktrees.prepare(1, verification_policy=policy)
    assert calls[-1] == (("run", "db:generate"), root) and len(calls) == 3


@pytest.mark.parametrize("extras", [(), ("migrate_and_seed", "database_ready")])
def test_gate_uses_only_pinned_stages_and_stores_the_log(tmp_path, monkeypatch, extras):
    # This pinned policy contains probe/capture stages with extra prerequisites the merge gate never selects.
    policy = build_verification_stage_policy_version(project=PROJECT, policy_version=BROWSER_STAGE_POLICY_VERSION)
    store = RuntimeStore(repo_root=tmp_path, runtime_root=tmp_path / "runtime")
    root = tmp_path / "gate root"
    scripts = {script: "from profile" for script in ("typecheck", "test:unit", "db:up", "db:migrate", "db:seed")}
    write(root, "package.json", json.dumps({"scripts": scripts}))
    calls = []

    def run(argv, *, cwd, env, timeout):
        assert Path(cwd) == root
        calls.append(argv[2])
        if argv[2].startswith("db:"):
            assert env["COMPOSE_PROJECT_NAME"].startswith("sdlc_")
        return subprocess.CompletedProcess(argv, 0, f"ran {argv[2]}", "")

    monkeypatch.setattr(verification.shutil, "which", lambda tool, **options: f"fake-{tool}")
    monkeypatch.setattr(verification, "_run_bounded", run)
    gate = make_merge_gate(store=store, run_id=RUN, verification_policy=policy, extra_stage_ids=extras)
    result = gate(root)
    assert result.ok and result.stage == "unit"
    assert calls == (["db:up", "db:migrate", "db:seed", "db:seed"] if extras else []) + ["typecheck", "test:unit"]
    log = json.loads((tmp_path / result.log_ref).read_text(encoding="utf-8"))
    assert log["executed_stage_ids"] == (["database_ready", "migrate_and_seed"] if extras else []) + ["typecheck", "unit"]
    assert log["stage_results"][-1]["stdout"] == "ran test:unit"


def test_gate_reports_failed_stage_and_rejects_unapproved_extras(tmp_path, monkeypatch):
    store = RuntimeStore(repo_root=tmp_path, runtime_root=tmp_path / "runtime")
    policy = build_verification_stage_policy(project=PROJECT)
    write(tmp_path, "package.json", json.dumps({"scripts": {"typecheck": "fake", "test:unit": "fake"}}))

    def run(argv, **options):
        assert argv[2] == "typecheck"
        return subprocess.CompletedProcess(argv, 1, "", "compiler error")

    monkeypatch.setattr(verification.shutil, "which", lambda tool, **options: f"fake-{tool}")
    monkeypatch.setattr(verification, "_run_bounded", run)
    result = make_merge_gate(store=store, run_id=RUN, verification_policy=policy)(tmp_path)
    assert not result.ok and result.stage == "typecheck"
    log = json.loads((tmp_path / result.log_ref).read_text(encoding="utf-8"))
    assert log["stage_results"][0]["stderr"] == "compiler error"
    with pytest.raises(HarnessError, match="Invalid H-OC merge gate stages"):
        make_merge_gate(store=store, run_id=RUN, verification_policy=policy, extra_stage_ids=("browser_e2e",))(tmp_path)


def test_gate_lock_is_shared_by_different_gate_instances(tmp_path, monkeypatch):
    policy = build_verification_stage_policy(project=PROJECT)
    store = RuntimeStore(repo_root=tmp_path, runtime_root=tmp_path / "runtime")
    first_entered, second_started, second_entered, release = (threading.Event() for _ in range(4))
    first_root, second_root = tmp_path / "a", tmp_path / "b"

    def run(self, *, target_path, **options):
        if target_path == first_root:
            first_entered.set()
            assert release.wait(10)
        else:
            second_entered.set()
        return verification.VerificationResult("failed", "", str(target_path), 1, 0.0, "", "", "test failure")

    monkeypatch.setattr(verification.VerificationRunner, "run_harness_stages", run)
    first = make_merge_gate(store=store, run_id=RUN, verification_policy=policy)
    second = make_merge_gate(store=store, run_id=RUN, verification_policy=policy)

    def call_second():
        second_started.set()
        return second(second_root)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first, first_root)
        assert first_entered.wait(5)
        b = pool.submit(call_second)
        try:
            assert second_started.wait(5)
            assert not second_entered.wait(0.1)
        finally:
            release.set()
        one, two = a.result(timeout=5), b.result(timeout=5)
        assert second_entered.is_set()
        assert one.log_ref != two.log_ref
