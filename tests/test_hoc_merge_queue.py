from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from sdlc_harness.agents.developer.hoc_merge_queue import (
    Commit,
    GateResult,
    MergeQueue,
    QueueChanges,
    QueueResult,
    attributes_content,
    check_turn,
    merge_commit,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

GREEN = GateResult(True, "unit", "all tests pass", "")
RED = GateResult(False, "unit", "test failed", "unit.log")
BARREL = "src/index.ts"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def commit(root: Path, files: dict[str, str]) -> str:
    write_files(root, files)
    git(root, "add", "--", *files)
    git(root, "commit", "-m", "test change")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "integration"
    root.mkdir()
    git(root, "init", "--initial-branch=integration")
    git(root, "config", "user.name", "H-OC tests")
    git(root, "config", "user.email", "hoc@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_text("/docs/sdlc-local/\n", encoding="utf-8")
    commit(root, {BARREL: 'export { base } from "./base";\n', "src/shared.ts": "base\n"})
    return root


def agent(root: Path, name: str) -> Path:
    path = root.parent / name
    git(root, "worktree", "add", "-b", name, str(path), "HEAD")
    return path


def attributes(root: Path, *barrels: str) -> Path:
    path = root.parent / "run attributes"
    path.write_text(attributes_content(barrels), encoding="utf-8", newline="\n")
    return path


def green(_root: Path) -> GateResult:
    return GREEN


def test_scope_violation_leaves_head_index_and_worktree_for_the_fix(repo):
    branch = agent(repo, "a")
    head = git(branch, "rev-parse", "HEAD")
    files = {"src/a.ts": "allowed\n", "src/shared.ts": "outside\n", "src/other.ts": "outside\n",
             "docs/sdlc-local/agent.json": "{}"}
    write_files(branch, files)
    git(branch, "add", "--", "src/a.ts")
    index_path = Path(git(branch, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    index = index_path.read_bytes()
    result = check_turn(
        branch, scope={"src/a.ts"}, barrels=(), gate=lambda _: pytest.fail("gate ran"), message="rejected turn",
    )
    assert result.result == "scope_rejected"
    assert result.files == ("src/other.ts", "src/shared.ts")
    assert result.sha is None
    assert git(branch, "rev-parse", "HEAD") == head
    assert index_path.read_bytes() == index
    assert all((branch / path).read_bytes() == content.encode("utf-8") for path, content in files.items())

    (branch / "src" / "other.ts").unlink()
    write_files(branch, {"src/shared.ts": "base\n", "src/a.ts": "repaired\n"})
    fixed = check_turn(branch, scope={"src/a.ts"}, barrels=(), gate=green, message="allowed repair")
    assert fixed.result == "queued"
    assert fixed.sha == git(branch, "rev-parse", "HEAD")
    assert git(branch, "rev-parse", "HEAD^") == head
    assert git(branch, "log", "-1", "--format=%s|%an|%ae") == "allowed repair|H-OC tests|hoc@example.invalid"
    assert git(branch, "diff", "--name-only", "HEAD^", "HEAD") == "src/a.ts"
    assert git(branch, "show", "HEAD:src/a.ts") == "repaired"
    assert (branch / "docs" / "sdlc-local" / "agent.json").is_file()
    assert git(branch, "status", "--porcelain") == ""


def test_scope_rejects_force_added_local_docs(repo):
    path = "docs/sdlc-local/agent.json"
    write_files(repo, {path: "{}"})
    git(repo, "add", "-f", "--", path)
    head = git(repo, "rev-parse", "HEAD")
    result = check_turn(
        repo, scope={path}, barrels=(), gate=lambda _: pytest.fail("gate ran"), message="rejected docs",
    )
    assert result.result == "scope_rejected"
    assert result.files == (path,)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "diff", "--cached", "--name-only") == path


@pytest.mark.parametrize("allowed_path", ["src/shared.ts", "src/moved.ts"])
def test_scope_checks_both_sides_of_a_rename(repo, allowed_path):
    git(repo, "mv", "--", "src/shared.ts", "src/moved.ts")
    result = check_turn(
        repo, scope={allowed_path}, barrels=(), gate=lambda _: pytest.fail("gate ran"), message="rename",
    )
    assert result.result == "scope_rejected"
    assert result.files == tuple({"src/shared.ts", "src/moved.ts"} - {allowed_path})


def test_unchanged_worktree_returns_no_changes(repo):
    head = git(repo, "rev-parse", "HEAD")
    write_files(repo, {"docs/sdlc-local/agent.json": "{}"})
    result = check_turn(repo, scope=(), barrels=(), gate=lambda _: pytest.fail("gate ran"), message="empty")
    assert result == QueueResult("no_changes")
    assert git(repo, "rev-parse", "HEAD") == head


def test_staging_can_cancel_changes_to_an_allowed_file(repo):
    head = git(repo, "rev-parse", "HEAD")
    write_files(repo, {"src/shared.ts": "staged\n"})
    git(repo, "add", "--", "src/shared.ts")
    write_files(repo, {"src/shared.ts": "base\n"})
    result = check_turn(
        repo, scope={"src/shared.ts"}, barrels=(), gate=lambda _: pytest.fail("gate ran"), message="empty",
    )
    assert result == QueueResult("no_changes")
    assert git(repo, "rev-parse", "HEAD") == head


def test_own_gate_failure_reports_agent_worktree_and_log(repo):
    branch = agent(repo, "a")
    head = git(branch, "rev-parse", "HEAD")
    write_files(branch, {"src/a.ts": "new", BARREL: 'export { a } from "./a";\n'})

    def gate(root):
        assert root == branch
        assert git(root, "rev-parse", "HEAD^") == head
        assert git(root, "status", "--porcelain") == ""
        return RED

    result = check_turn(branch, scope={"src/a.ts"}, barrels={BARREL}, gate=gate, message="own gate test")
    assert result.result == "own_gate_failed"
    assert result.gate == RED
    assert result.sha == git(branch, "rev-parse", "HEAD")


def test_merge_conflict_aborts_with_conflicting_files(repo):
    branch = agent(repo, "a")
    sha = commit(branch, {"src/shared.ts": "agent\n"})
    head = commit(repo, {"src/shared.ts": "integration\n"})
    result = merge_commit(
        repo, sha, attributes_file=attributes(repo), barrels=(), gate=lambda _: pytest.fail("gate ran")
    )
    assert result.result == "merge_conflict"
    assert result.files == ("src/shared.ts",)
    assert result.integration_head == git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "src" / "shared.ts").read_text(encoding="utf-8") == "integration\n"


def test_interaction_failure_rolls_back_h_and_lists_suspects(repo):
    first, second = agent(repo, "a"), agent(repo, "b")
    write_files(first, {"src/a.ts": "a"})
    write_files(second, {"src/b.ts": "b"})
    attr = attributes(repo)

    def gate(root):
        return RED if all((root / "src" / name).exists() for name in ("a.ts", "b.ts")) else GREEN

    first_result = check_turn(first, scope={"src/a.ts"}, barrels=(), gate=gate, message="first")
    second_result = check_turn(second, scope={"src/b.ts"}, barrels=(), gate=gate, message="second")
    assert first_result.result == second_result.result == "queued"
    a, b = first_result.sha, second_result.sha
    assert merge_commit(repo, a, attributes_file=attr, barrels=(), gate=gate).result == "merged"
    head = git(repo, "rev-parse", "HEAD")
    assert gate(repo).ok
    failure = merge_commit(repo, b, attributes_file=attr, barrels=(), gate=gate)
    assert failure.result == "integration_gate_failed"
    assert failure.gate == RED
    assert failure.suspects == (a, head)
    assert failure.integration_head == git(repo, "rev-parse", "HEAD") == head
    assert gate(repo).ok
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "src" / "b.ts").exists()


def test_fifo_uses_received_sequence_then_agent_id():
    queue = MergeQueue()
    commits = [Commit(3, "agent-a", "third"), Commit(2, "agent-b", "second"), Commit(2, "agent-a", "first")]
    for item in commits[1:] + commits[:1]:
        queue.submit(item, QueueResult("queued"))
    for item in reversed(commits):
        assert queue.take() == (item, ())
        queue.finish(item, QueueResult("merged"))
    assert queue.take() is None


@pytest.mark.parametrize("no_commit", ["scope_rejected", "no_changes"])
def test_merge_queue_only_accepts_committed_turns(no_commit):
    queue = MergeQueue()
    with pytest.raises(ValueError, match=no_commit):
        queue.submit(Commit(1, "a", "missing"), QueueResult(no_commit))
    repair = Commit(1, "a", "repair")
    assert queue.submit(repair, QueueResult("queued")) == "queued"
    assert queue.take() == (repair, ())


def test_failed_commit_holds_later_commits_until_the_fix_carries_them(repo):
    branch = agent(repo, "a")
    queue = MergeQueue()
    attr = attributes(repo)

    def submit(seq, path, content, *, fixes=None):
        write_files(branch, {path: content})
        checked = check_turn(branch, scope={path}, barrels=(), gate=green, message="checked turn")
        assert checked.sha
        item = Commit(seq, "a", checked.sha, fixes=fixes)
        return item, queue.submit(item, checked)

    first, result = submit(1, "src/a.ts", "broken in H")
    assert result == "queued"
    assert queue.take() == (first, ())
    second, result = submit(2, "src/b.ts", "checked while first merged")
    assert result == "queued"
    failed = merge_commit(repo, first.sha, attributes_file=attr, barrels=(), gate=lambda _: RED)
    assert queue.finish(first, failed) == QueueChanges(held=(second.sha,))

    third, result = submit(3, "src/c.ts", "checked after the failure")
    assert result == "held"
    assert queue.take() is None
    repair, result = submit(4, "src/a.ts", "fixed", fixes=first.sha)
    assert result == "queued"
    ticket = queue.take()
    assert ticket == (repair, (first.sha, second.sha, third.sha))
    next_turn, result = submit(5, "src/d.ts", "checked after the repair")
    assert result == "held"
    merged = merge_commit(repo, repair.sha, attributes_file=attr, barrels=(), gate=green, carried=ticket[1])
    assert merged.result == "merged"
    assert queue.finish(repair, merged) == QueueChanges(
        released=(next_turn.sha,), carried=(first.sha, second.sha, third.sha)
    )
    assert (repo / "src" / "a.ts").read_text(encoding="utf-8") == "fixed"
    assert (repo / "src" / "b.ts").is_file() and (repo / "src" / "c.ts").is_file()
    assert not (repo / "src" / "d.ts").exists()
    assert queue.take() == (next_turn, ())


def test_union_merge_uses_run_attributes_leaving_common_attributes_untouched(repo):
    first, second = agent(repo, "a"), agent(repo, "b")
    info = repo / ".git" / "info" / "attributes"
    info.write_bytes(b"# existing attributes\n")
    base = 'export { base } from "./base";\n'
    a = commit(first, {BARREL: base + 'export { a } from "./a";\n'})
    b = commit(second, {BARREL: base + 'export { b } from "./b";\n'})
    attr = attributes(repo, BARREL)
    assert attr.read_bytes() == b"src/index.ts merge=union\n"
    for sha in (a, b):
        assert merge_commit(repo, sha, attributes_file=attr, barrels={BARREL}, gate=green).result == "merged"
    assert set((repo / BARREL).read_text(encoding="utf-8").splitlines()) == {
        'export { base } from "./base";', 'export { a } from "./a";', 'export { b } from "./b";',
    }
    assert info.read_bytes() == b"# existing attributes\n"
    assert "core.attributesfile" not in git(repo, "config", "--local", "--list")


def test_g11_rejects_duplicate_export_after_union(repo):
    first, second = agent(repo, "a"), agent(repo, "b")
    base = 'export { base } from "./base";\n'
    a = commit(first, {BARREL: base + 'export { a as shared } from "./a";\n'})
    b = commit(second, {BARREL: base + 'export * as shared from "./b";\n'})
    attr = attributes(repo, BARREL)
    assert merge_commit(repo, a, attributes_file=attr, barrels={BARREL}, gate=green).result == "merged"
    head = git(repo, "rev-parse", "HEAD")
    failed = merge_commit(repo, b, attributes_file=attr, barrels={BARREL}, gate=green)
    assert failed.result == "integration_gate_failed"
    assert failed.gate == GateResult(False, "G11", f"{BARREL} has duplicate export shared", "")
    assert failed.integration_head == git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""


def test_g11_rejects_barrel_that_stops_being_reexports(repo):
    branch = agent(repo, "a")
    sha = commit(branch, {BARREL: "export const live = true;\n"})
    head = git(repo, "rev-parse", "HEAD")
    failed = merge_commit(repo, sha, attributes_file=attributes(repo, BARREL), barrels={BARREL}, gate=green)
    assert failed.result == "integration_gate_failed"
    assert failed.gate == GateResult(False, "G11", f"{BARREL} must contain only re-exports", "")
    assert failed.integration_head == git(repo, "rev-parse", "HEAD") == head


def test_git_error_is_explicit_and_h_stays_at_previous_head(repo):
    head = git(repo, "rev-parse", "HEAD")
    missing = "f" * 40
    result = merge_commit(
        repo, missing, attributes_file=attributes(repo), barrels=(), gate=lambda _: pytest.fail("gate ran")
    )
    assert result.result == "git_error" and "rev-parse" in result.error
    assert result.integration_head == git(repo, "rev-parse", "HEAD") == head


def test_check_turn_refuses_worktree_without_head(tmp_path):
    git(tmp_path, "init", "--initial-branch=empty")
    result = check_turn(tmp_path, scope=(), barrels=(), gate=lambda _: pytest.fail("gate ran"), message="empty")
    assert result.result == "git_error"
    assert result.sha is None and "rev-parse" in result.error


def test_check_turn_refuses_merge_in_progress(repo):
    branch = agent(repo, "a")
    sha = commit(branch, {"src/a.ts": "new"})
    git(repo, "merge", "--no-ff", "--no-commit", sha)
    result = check_turn(
        repo, scope={"src/a.ts"}, barrels=(), gate=lambda _: pytest.fail("gate ran"), message="unfinished",
    )
    assert result.result == "git_error"
    assert result.sha is None and git(repo, "rev-parse", "MERGE_HEAD") == sha
