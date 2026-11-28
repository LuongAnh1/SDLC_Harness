"""H-OC Phase 4 merge queue (plan sections 2.1, 2.4, 3.2 and rules G9, G11, G12)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from ...repo_context.plan_facts import _COMMENTS, is_barrel
from ...repo_context.providers import _REEXPORT

_FAILURES = {"own_gate_failed", "merge_conflict", "integration_gate_failed"}
_CHECK_RESULTS = {"queued", "own_gate_failed"}
_MERGE_RESULTS = {"merged", "merge_conflict", "integration_gate_failed"}
_LOCAL_DOCS = "docs/sdlc-local"


@dataclass(frozen=True)
class GateResult:
    ok: bool
    stage: str
    summary: str
    log_ref: str


@dataclass(frozen=True)
class QueueResult:
    result: str
    files: tuple[str, ...] = ()
    gate: GateResult | None = None
    integration_head: str | None = None
    suspects: tuple[str, ...] = ()
    error: str = ""
    sha: str | None = None
    conflict_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Commit:
    seq: int
    agent_id: str
    sha: str
    fixes: str | None = None  # Oldest unresolved failure this repair addresses, including retries.


@dataclass(frozen=True)
class QueueChanges:
    held: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    carried: tuple[str, ...] = ()


class MergeQueue:
    """Bookkeeping for one consumer on H. The caller serializes submit, take and finish."""

    def __init__(self) -> None:
        self._commits: dict[str, Commit] = {}
        self._results: dict[str, str] = {}
        self._active: Commit | None = None

    def _earlier(self, commit: Commit) -> list[Commit]:
        return sorted(
            (item for item in self._commits.values() if item.agent_id == commit.agent_id and item.seq < commit.seq),
            key=lambda item: item.seq,
        )

    def _blocked(self, commit: Commit) -> bool:
        failures = [item.sha for item in self._earlier(commit) if self._results[item.sha] in _FAILURES]
        return bool(failures and commit.fixes != failures[0])

    def submit(self, commit: Commit, result: QueueResult) -> str:
        """Record a checked commit and return its effective turn result."""

        if result.result not in _CHECK_RESULTS:
            raise ValueError(f"Cannot submit a commit with result {result.result!r}")
        if commit.sha in self._commits or any(
            item.agent_id == commit.agent_id and item.seq >= commit.seq for item in self._commits.values()
        ):
            raise ValueError("Expected a new commit in agent branch order")
        if commit.fixes is not None and not any(
            item.sha == commit.fixes and self._results[item.sha] in _FAILURES for item in self._earlier(commit)
        ):
            raise ValueError("A fix must identify a failed commit of the same agent")
        effective = "held" if result.result == "queued" and self._blocked(commit) else result.result
        self._commits[commit.sha] = commit
        self._results[commit.sha] = effective
        return effective

    def take(self) -> tuple[Commit, tuple[str, ...]] | None:
        """Take the next FIFO commit and the earlier SHAs its fix must carry."""

        if self._active is not None:
            raise ValueError("The previous merge is still active")
        self._active = min(
            (item for item in self._commits.values() if self._results[item.sha] == "queued"),
            key=lambda item: (item.seq, item.agent_id),
            default=None,
        )
        if self._active is None:
            return None
        return self._active, self._carried(self._active)

    def _carried(self, commit: Commit) -> tuple[str, ...]:
        if commit.fixes is None:
            return ()
        return tuple(item.sha for item in self._earlier(commit) if self._results[item.sha] != "merged")

    def finish(self, commit: Commit, result: QueueResult) -> QueueChanges:
        """Record a merge and return the changes needed for the MergeFinished event."""

        if commit != self._active or result.result not in _MERGE_RESULTS:
            raise ValueError("Expected a merge outcome for the active commit")
        self._active = None
        self._results[commit.sha] = result.result
        carried = self._carried(commit) if result.result == "merged" else ()
        for sha in carried:
            self._results[sha] = "merged"

        held, released = [], []
        for item in self._commits.values():
            if item.agent_id != commit.agent_id or item.seq <= commit.seq:
                continue
            status = self._results[item.sha]
            if status == "queued" and self._blocked(item):
                self._results[item.sha] = "held"
                held.append(item.sha)
            elif status == "held" and not self._blocked(item):
                self._results[item.sha] = "queued"
                released.append(item.sha)
        return QueueChanges(tuple(held), tuple(released), carried)


def attributes_content(barrels: Iterable[str]) -> str:
    """Git attributes patterns for the exact barrel paths, including paths with spaces."""

    lines = []
    for path in sorted(set(barrels)):
        pattern = re.sub(r"([\\*?\[\]])", r"\\\1", path)
        if "/" not in path or path.startswith(("#", "!")):
            pattern = "/" + pattern
        if re.search(r'\s|["\\]', pattern):
            pattern = json.dumps(pattern, ensure_ascii=False)
        lines.append(f"{pattern} merge=union\n")
    return "".join(lines)


class _GitError(RuntimeError):
    pass


def _git(
    root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, env=env)
    except OSError as exc:
        raise _GitError(str(exc)) from exc
    if check and process.returncode:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise _GitError(f"git {args[0]} exited {process.returncode}: {message}")
    return process


def _paths(output: bytes) -> set[str]:
    return {path.decode("utf-8", errors="surrogateescape") for path in output.split(b"\0") if path}


def _changed(root: Path, before: str, after: str) -> set[str]:
    return _paths(_git(
        root, "diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=none", before, after, "--",
    ).stdout)


def _commit_sha(root: Path, sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise _GitError(f"Expected a commit SHA, got {sha!r}")
    return _git(root, "rev-parse", "--verify", f"{sha}^{{commit}}").stdout.decode("ascii").strip()


def _head_without_merge(root: Path) -> str:
    head = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    if _git(root, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode != 1:
        raise _GitError("Could not confirm that no merge is in progress")
    return head


def _status(root: Path) -> bytes:
    # Optional index refreshes would modify the index even for a rejected turn.
    return _git(
        root, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
        "--no-renames", "--ignore-submodules=none",
    ).stdout


def _clean_head(root: Path) -> str:
    head = _head_without_merge(root)
    if _status(root):
        raise _GitError("Expected a clean worktree")
    return head


def check_turn(
    root: Path, *, scope: Iterable[str], barrels: Iterable[str], gate: Callable[[Path], GateResult], message: str
) -> QueueResult:
    """Check scope on the worktree, then commit allowed changes and run its gate."""

    try:
        head = _head_without_merge(root)
        changed = {record[3:] for record in _paths(_status(root))}
        allowed = set(scope) | set(barrels)
        outside = sorted(
            path for path in changed if path not in allowed
            or path.casefold() == _LOCAL_DOCS or path.casefold().startswith(_LOCAL_DOCS + "/")
        )
        if outside:
            return QueueResult("scope_rejected", files=tuple(outside))
        if not changed:
            return QueueResult("no_changes")
        _git(root, "add", "--all")
        staged = _git(root, "diff", "--cached", "--name-only", "-z", "--ignore-submodules=none", head, "--")
        if not staged.stdout:
            return QueueResult("no_changes")
        _git(root, "commit", "-m", message)
        sha = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    except _GitError as exc:
        return QueueResult("git_error", error=str(exc))
    checked = gate(root)
    return QueueResult("queued" if checked.ok else "own_gate_failed", gate=checked, sha=sha)


@contextmanager
def _preview_index(root: Path):
    # Let Git compare the complete working snapshot, including untracked files, without touching the real index.
    source = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout
    with TemporaryDirectory(prefix="hoc-index-") as temp:
        index = Path(temp) / "index"
        index.write_bytes(Path(os.fsdecode(source.rstrip(b"\r\n"))).read_bytes())
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        _git(root, "add", "--all", env=env)
        yield env


def worktree_diff(root: Path, files: Iterable[str], *, against: str = "HEAD") -> str:
    """Show the current working snapshot, including new files, for the scope reviewer."""

    with _preview_index(root) as env:
        diff = _git(root, "diff", "--cached", "--no-ext-diff", "--no-renames", against, "--",
                    *(f":(literal){path}" for path in files), env=env).stdout
    return diff.decode("utf-8", errors="replace")


def check_merge_resolution(
    root: Path, *, scope: Iterable[str], barrels: Iterable[str], gate: Callable[[Path], GateResult], message: str,
) -> QueueResult:
    """Check the working snapshot against MERGE_HEAD before committing the agent's resolution."""

    try:
        parent = _git(root, "rev-parse", "--verify", "MERGE_HEAD").stdout.decode("ascii").strip()
        unmerged = _paths(_git(root, "diff", "--name-only", "--diff-filter=U", "-z", "--").stdout)
        allowed = set(scope) | set(barrels)
        with _preview_index(root) as env:
            changed = _paths(_git(root, "diff", "--cached", "--name-only", "-z", "--no-renames",
                                  "--ignore-submodules=none", parent, "--", env=env).stdout)
            outside = {path for path in changed if path not in allowed
                       or path.casefold() == _LOCAL_DOCS or path.casefold().startswith(_LOCAL_DOCS + "/")}
            markers = []
            for path in sorted(changed | unmerged):
                file = root / path
                if file.is_file() and not file.is_symlink() and re.search(
                    rb"^(?:<{7}(?:[ \t].*)?|={7}|>{7}(?:[ \t].*)?|\|{7}(?:[ \t].*)?)\r?$",
                    file.read_bytes(), re.MULTILINE,
                ):
                    markers.append(path)
        if outside or markers:
            return QueueResult("scope_rejected", files=tuple(sorted(outside | set(markers))),
                               conflict_markers=tuple(markers))
        _git(root, "add", "--all")
        _git(root, "commit", "-m", message)
        sha = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    except (_GitError, OSError) as exc:
        return QueueResult("git_error", error=str(exc))
    checked = gate(root)
    return QueueResult("queued" if checked.ok else "own_gate_failed", gate=checked, sha=sha)


def _barrel_gate(root: Path, paths: Iterable[str]) -> GateResult | None:
    for path in sorted(paths):
        entry = _git(root, "ls-tree", "-z", "HEAD", "--", f":(literal){path}").stdout
        source = _git(root, "show", f"HEAD:{path}").stdout if entry.startswith(b"100") else b""
        text = source.decode("utf-8", errors="replace")
        if not is_barrel(text):
            return GateResult(False, "G11", f"{path} must contain only re-exports", "")
        names: set[str] = set()
        for match in _REEXPORT.finditer(_COMMENTS.sub("", text)):
            clause = match[1]
            if clause == "*":
                continue  # The typecheck gate resolves names behind wildcard re-exports.
            specifiers = clause[1:-1].split(",") if clause.startswith("{") else [clause]
            for specifier in filter(None, (part.strip() for part in specifiers)):
                name = re.split(r"\s+as\s+", re.sub(r"^type\s+", "", specifier))[-1].strip("\"'")
                if name in names:
                    return GateResult(False, "G11", f"{path} has duplicate export {name}", "")
                names.add(name)
    return None


def merge_commit(
    root: Path,
    sha: str,
    *,
    attributes_file: Path,
    barrels: Iterable[str],
    gate: Callable[[Path], GateResult],
    carried: Iterable[str] = (),
) -> QueueResult:
    """Merge one checked commit on H. Roll back any merge that does not pass both gates."""

    head = None
    attempted = False
    keep = False
    try:
        try:
            head = _clean_head(root)
            sha = _commit_sha(root, sha)
            if not attributes_file.is_file():
                raise _GitError(f"Missing run attributes file: {attributes_file}")
            for ancestor in carried:
                _git(root, "merge-base", "--is-ancestor", _commit_sha(root, ancestor), sha)
            fork = _git(root, "merge-base", head, sha).stdout.decode("ascii").strip()
            attempted = True
            merge = _git(
                root, "-c", f"core.attributesFile={attributes_file.resolve()}", "-c", "rerere.enabled=false",
                "merge", "--no-ff", "--no-edit", "--no-autostash", sha, check=False,
            )
            if merge.returncode:
                files = _paths(_git(root, "diff", "--name-only", "-z", "--diff-filter=U", "--").stdout)
                if not files:
                    message = merge.stderr.decode("utf-8", errors="replace").strip()
                    raise _GitError(f"git merge exited {merge.returncode}: {message}")
                _git(root, "merge", "--abort")
                attempted = False
                return QueueResult("merge_conflict", files=tuple(sorted(files)), integration_head=head)

            merged_head = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
            barrel_paths = set(barrels)
            touched = (_changed(root, head, merged_head) | _changed(root, fork, sha)) & barrel_paths if barrel_paths else ()
            checked = gate(root)
            barrel_check = _barrel_gate(root, touched)
            if not checked.ok or barrel_check is not None:
                suspects = _git(root, "rev-list", "--reverse", "--topo-order", f"{fork}..{head}")
                return QueueResult(
                    "integration_gate_failed", gate=barrel_check if checked.ok else checked, integration_head=head,
                    suspects=tuple(suspects.stdout.decode("ascii").splitlines()),
                )
            keep = True
            return QueueResult("merged", gate=checked, integration_head=merged_head)
        finally:
            if attempted and not keep:
                try:
                    _git(root, "reset", "--hard", head)
                except _GitError as exc:
                    head = None
                    raise _GitError(f"Could not restore the integration worktree: {exc}") from exc
    except _GitError as exc:
        return QueueResult("git_error", integration_head=head, error=str(exc))
