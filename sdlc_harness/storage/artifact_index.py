"""Artifact provenance (G1.3): which node wrote each artifact, and from which inputs.

`RuntimeStore.write_state` appends one line to `runs/<run_id>/artifact_index.jsonl` for every
`latest_artifacts` key whose ref changed, so no artifact writer has to know about provenance.

A Phase 4 dispatch opens an `artifact_scope`: the node and handler that run, the artifacts read so far (ref and
sha256 at read time, recorded by the shared read helpers) and a worktree fingerprint provider. A line written
inside a scope carries `inputs` and `worktree_fingerprint`; a line written outside one (Phase 1-3, approvals,
driver recoveries) carries `inputs: null`, meaning "not tracked", never "no inputs".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re

from ..core.errors import HarnessError

INDEX_FILENAME = "artifact_index.jsonl"
SCHEMA_VERSION = "artifact_index_v1"
_VERSION = re.compile(r"_v(\d+)(?:[._]|$)")


@dataclass
class _Scope:
    run_id: str
    node: str
    handler: str
    repo_root: Path
    fingerprint: Callable[[], dict[str, object]] | None
    reads: dict[str, str] = field(default_factory=dict)


_SCOPE: ContextVar[_Scope | None] = ContextVar("artifact_scope", default=None)


@contextmanager
def artifact_scope(
    run_id: str,
    *,
    node: str,
    handler: str,
    repo_root: Path,
    fingerprint: Callable[[], dict[str, object]] | None = None,
) -> Iterator[None]:
    """Attribute the artifacts indexed inside this block to `node`/`handler`."""

    token = _SCOPE.set(
        _Scope(run_id=run_id, node=node, handler=handler, repo_root=repo_root.resolve(), fingerprint=fingerprint)
    )
    try:
        yield
    finally:
        _SCOPE.reset(token)


def record_read(path: Path) -> None:
    """Remember an artifact the running node read; a no-op outside a scope."""

    scope = _SCOPE.get()
    if scope is None or not path.is_file():
        return
    scope.reads[_relative(path, scope.repo_root)] = sha256_file(path)


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def index_path(run_dir: Path) -> Path:
    return run_dir / INDEX_FILENAME


def index_artifact_changes(
    *,
    run_dir: Path,
    repo_root: Path,
    state: dict[str, object],
    previous: dict[str, object] | None,
    written_at: str,
) -> None:
    """Append one index line per `latest_artifacts` key whose ref changed in this state write.

    A key that disappears gets a line with `ref: null`, so replaying the index rebuilds `latest_artifacts` at any
    earlier point (`artifacts_at`).
    """

    current = state.get("latest_artifacts")
    current = current if isinstance(current, dict) else {}
    before = (previous or {}).get("latest_artifacts")
    before = before if isinstance(before, dict) else {}
    # shortcut: a rewrite under the same ref is not indexed; every Phase 4 writer versions its filename
    # (r9: 113 of 114 keys) except raw LLM responses, which nothing consumes.
    changed: list[tuple[str, str | None]] = [
        (key, ref) for key, ref in current.items() if isinstance(ref, str) and ref and before.get(key) != ref
    ]
    changed += [
        (key, None)
        for key, ref in before.items()
        if isinstance(ref, str) and ref and not (isinstance(current.get(key), str) and current.get(key))
    ]
    if not changed:
        return
    scope = _SCOPE.get()
    if scope is not None and scope.run_id != str(state.get("run_id") or ""):
        scope = None
    keys_by_ref = {ref: key for key, ref in {**before, **current}.items() if isinstance(ref, str)}
    fingerprint = scope.fingerprint() if scope is not None and scope.fingerprint is not None else None
    transition = {
        "from": str((previous or {}).get("status") or "") or None,
        "to": str(state.get("status") or "") or None,
    }
    lines = []
    for key, ref in changed:
        path = repo_root / ref if ref else None
        line: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "ref": ref,
            "sha256": sha256_file(path) if path is not None and path.is_file() else None,
            "version": _version(ref) if ref else None,
            "node": scope.node if scope is not None else None,
            "handler": scope.handler if scope is not None else None,
            "transition": transition,
            "inputs": None
            if scope is None
            else [
                {"ref": read_ref, "sha256": digest, **({"key": keys_by_ref[read_ref]} if read_ref in keys_by_ref else {})}
                for read_ref, digest in sorted(scope.reads.items())
                if read_ref != ref
            ],
            "written_at": written_at,
        }
        if ref is None:
            line["previous_ref"] = before[key]
        if fingerprint is not None:
            line["worktree_fingerprint"] = fingerprint
        lines.append(json.dumps(line, ensure_ascii=False, sort_keys=True))
    path = index_path(run_dir)
    try:
        _drop_torn_tail(path)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise HarnessError("runtime_write_failed", f"Cannot append the artifact index: {exc}") from exc


def _drop_torn_tail(path: Path) -> None:
    """Cut a last line left half-written by a crash, so the next append starts on a clean line."""

    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("r+b") as handle:
        handle.seek(-1, 2)
        if handle.read(1) == b"\n":
            return
        handle.seek(0)
        handle.truncate(handle.read().rfind(b"\n") + 1)


def read_artifact_index(run_dir: Path) -> list[dict[str, object]]:
    """Index lines in write order; a torn last line (crash mid-append) is skipped, as the next append drops it."""

    path = index_path(run_dir)
    if not path.is_file():
        return []
    rows = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, object]] = []
    for number, row in enumerate(rows, start=1):
        if not row.strip():
            continue
        try:
            entries.append(json.loads(row))
        except json.JSONDecodeError as exc:
            if number == len(rows):
                break
            raise HarnessError("state_corrupted", f"{INDEX_FILENAME} line {number} is not valid JSON") from exc
    return entries


def artifacts_at(entries: list[dict[str, object]], position: int) -> dict[str, str]:
    """The indexed part of `latest_artifacts` just before `entries[position]` was written."""

    artifacts: dict[str, str] = {}
    for entry in entries[:position]:
        key, ref = str(entry.get("key") or ""), entry.get("ref")
        if isinstance(ref, str) and ref:
            artifacts[key] = ref
        else:
            artifacts.pop(key, None)
    return artifacts


def last_visit(
    entries: list[dict[str, object]],
    node: str,
    *,
    excluded: Sequence[tuple[int, int]] = (),
) -> int | None:
    """Position of the first line of the last contiguous run of lines that `node` wrote, if any.

    `excluded` holds `[start, end)` ranges a rewind cut off: lines there belong to an abandoned branch of the run,
    so a node visited only there is not an ancestor of the current status.
    """

    line = [index for index in range(len(entries)) if not any(start <= index < end for start, end in excluded)]
    last = next((at for at in range(len(line) - 1, -1, -1) if entries[line[at]].get("node") == node), None)
    if last is None:
        return None
    while last > 0 and entries[line[last - 1]].get("node") == node:
        last -= 1
    return line[last]


def _version(ref: str) -> int | None:
    match = _VERSION.search(ref.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None


def _relative(path: Path, repo_root: Path) -> str:
    # Same form as `RuntimeStore.relative`, so a read matches the `latest_artifacts` ref it came from.
    try:
        return str(path.resolve().relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
