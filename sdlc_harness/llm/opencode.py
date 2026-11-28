"""OpenCode CLI transport for Phase 1."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from ..core.config import Settings
from ..core.errors import HarnessError
from ..events import emit_current
from ..workspaces.manager import RunWorkspaceManager
from .client import LlmCompletion, strip_code_fence


_AGENT = "sdlc-requirement"
_LOCAL_DOCS = Path("docs/sdlc-local")
_SESSION_ARTIFACTS = {
    "refiner": "requirement_opencode_refiner_session_v1.json",
    "reviewer": "requirement_opencode_reviewer_session_v1.json",
}
# Other roles (Design: designer, reviewer-g<n>, reviewer-global, reviewer-final) keep one session each under this name.
_ROLE = re.compile(r"[a-z][a-z0-9-]{0,40}")
_MODEL_PATTERN = r"[\w.-]+/[\w./:-]+(?:#[\w.-]+)?"
# The workspace lock does not wait (a second holder fails at once), so threads of one process take it one at a time.
_ROOT_GUARD = threading.Lock()


def session_artifact(role: str) -> str:
    if role in _SESSION_ARTIFACTS:
        return _SESSION_ARTIFACTS[role]
    if not _ROLE.fullmatch(role):
        raise ValueError(f"invalid OpenCode role {role!r}")
    return f"opencode_{role.replace('-', '_')}_session_v1.json"


def _worker_config(editable_refs: tuple[str, ...]) -> str:
    permissions = [
        {"action": "*", "resource": "*", "effect": "deny"},
        *(
            {"action": action, "resource": "*", "effect": "allow"}
            # OpenCode names an MCP tool "<server>_<tool>", so "codegraph*" covers the CodeGraph server's tools.
            for action in ("read", "glob", "grep", "codegraph*")
        ),
    ]
    permissions.extend({"action": "edit", "resource": ref, "effect": "allow"} for ref in editable_refs)
    # Nothing outside the run worktree: OpenCode checks `external_directory` before `read`/`edit` and the last matching
    # rule wins, so this explicit deny holds whatever base policy OpenCode loads first. Glob/grep paths are not covered
    # by this action; the Design flow audits them after each turn (`paths_outside_worktree`).
    permissions.append({"action": "external_directory", "resource": "*", "effect": "deny"})
    return json.dumps({
        "snapshots": False,
        "formatter": False,
        # The Harness declares CodeGraph itself (it overrides a user entry of the same name). Code Mode stays off so
        # its tools are direct tools: under Code Mode they run only inside `execute`, a script runtime with `fetch`.
        "mcp": {
            "servers": {
                "codegraph": {"type": "local", "command": ["codegraph", "serve", "--mcp"], "codemode": False},
            },
        },
        "agents": {
            _AGENT: {
                "description": "SDLC Requirement worker",
                "mode": "primary",
                "permissions": permissions,
            },
        },
    })


class OpenCodeWorker:
    """Run OpenCode turns in the run worktree; named roles keep one session for the whole run."""

    def __init__(self, settings: Settings, *, run_id: str, model: str):
        if not re.fullmatch(_MODEL_PATTERN, model, flags=re.ASCII):
            raise HarnessError(
                "requirement_opencode_model_invalid",
                "Phase 1 OpenCode model must use provider/model or provider/model#variant.",
                run_id=run_id,
            )
        self.settings = settings
        self.run_id = run_id
        self.model = model
        self.workspaces = RunWorkspaceManager(settings)
        self.store = self.workspaces.store

    def turn(
        self,
        *,
        task: str,
        timeout: int,
        role: str | None = None,
        output_ref: str | None = None,
        editable_refs: tuple[str, ...] = (),
        allow_ok: bool = False,
        exclusive: bool = True,
    ) -> tuple[LlmCompletion, str]:
        """Run one turn. With `output_ref` the answer must be that path (or OK when allowed) and the file must exist.

        An exclusive turn holds the workspace lock while OpenCode runs (it may edit shared files). A turn that edits only
        its own output file (a reviewer) passes `exclusive=False`: it takes the lock only to resolve the worktree, so
        several such turns run in parallel threads."""

        executable = _find_opencode()
        if executable is None:
            raise HarnessError("llm_transport_failed", "OpenCode v2 executable was not found on PATH.", run_id=self.run_id)
        expected_session = self._read_session(role) if role else None
        command = [
            executable, "--standalone", "run", "--format", "json", "--archived",
            "--agent", _AGENT, "--model", self.model,
        ]
        if expected_session:
            command.extend(["--session", expected_session])
        if exclusive:
            with self._root() as root:
                completion, session_id = self._run(
                    root, command, task, timeout, role, expected_session, output_ref, editable_refs, allow_ok,
                )
        else:
            with self._root() as root:
                pass
            completion, session_id = self._run(
                root, command, task, timeout, role, expected_session, output_ref, editable_refs, allow_ok,
            )
        emit_current(
            category="llm",
            event_type="opencode_call_completed",  # shared by every phase; the event's `phase` says which
            message="OpenCode call completed.",
            data={"model": self.model, "role": role, "session_id": session_id, "output_file": output_ref, **(completion.usage or {})},
        )
        return completion, session_id

    def _run(
        self,
        root: Path,
        command: list[str],
        task: str,
        timeout: int,
        role: str | None,
        expected_session: str | None,
        output_ref: str | None,
        editable_refs: tuple[str, ...],
        allow_ok: bool,
    ) -> tuple[LlmCompletion, str]:
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                input=task,
                capture_output=True,
                encoding="utf-8",
                env={
                    **os.environ,
                    "PWD": str(root),
                    "OPENCODE_CONFIG_CONTENT": _worker_config(editable_refs),
                },
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            if role and isinstance(exc, subprocess.TimeoutExpired):
                self._remember_session(role, exc.stdout, expected_session)
            raise HarnessError(
                "llm_transport_failed",
                f"Phase 1 OpenCode process could not complete ({type(exc).__name__}).",
                run_id=self.run_id,
            ) from exc
        if role:
            expected_session = self._remember_session(role, result.stdout, expected_session)
        if result.returncode and not result.stdout.strip():
            raise HarnessError(
                "llm_transport_failed", f"Phase 1 OpenCode exited with status {result.returncode}.", run_id=self.run_id,
            )
        completion, session_id = _read_completion(result.stdout, run_id=self.run_id)
        if role and session_id != expected_session:
            raise HarnessError(
                "llm_transport_failed", f"OpenCode did not confirm the saved {role} session.", run_id=self.run_id,
            )
        if result.returncode:
            raise HarnessError(
                "llm_transport_failed", f"Phase 1 OpenCode exited with status {result.returncode}.", run_id=self.run_id,
            )
        if output_ref is not None and completion.finish_reason == "stop":
            answer = final_answer(completion.content)
            if allow_ok and answer == "OK":
                if (root / output_ref).exists():
                    raise HarnessError(
                        "llm_transport_failed", "OpenCode returned OK alongside an output file.", run_id=self.run_id,
                    )
                completion = replace(completion, content="OK")
            else:
                if answer != output_ref:
                    raise HarnessError(
                        "llm_transport_failed", f"Expected OpenCode to return {output_ref}.", run_id=self.run_id,
                    )
                if not self._read(root, output_ref).strip():
                    raise HarnessError(
                        "llm_transport_failed", f"OpenCode output {output_ref} is empty.", run_id=self.run_id,
                    )
                completion = replace(completion, content=output_ref)
        return completion, session_id

    def paths_outside_worktree(self, session_id: str) -> list[str] | None:
        """Paths the last turn of `session_id` passed to its file tools (read, glob, grep, list, edit, write) that lie
        outside the run worktree, read from `opencode session export` (no LLM call). None when the export is not
        available; the caller records that the audit could not run."""

        executable = _find_opencode()
        if executable is None or not _valid_session_id(session_id):
            return None
        with self._root() as root:
            pass
        try:
            result = subprocess.run(
                [executable, "session", "export", session_id], cwd=str(root), capture_output=True, encoding="utf-8",
                timeout=120, check=False,
            )
            data = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        return outside_paths(data["messages"], root)

    def reserve(self, prefix: str, suffix: str) -> str:
        """A fresh, Git-ignored output path that does not exist yet."""

        with self._root() as root:
            path = self._local_docs(root) / f"{prefix}-{uuid4().hex}{suffix}"
            if path.exists() or path.is_symlink():
                raise HarnessError("llm_transport_failed", "The assigned OpenCode output path already exists.", run_id=self.run_id)
            return self._ignored_ref(root, path)

    def local_ref(self, name: str) -> str:
        """A fixed, Git-ignored path under the local docs directory."""

        with self._root() as root:
            return self._ignored_ref(root, self._local_docs(root) / name)

    def stage(self, label: str, suffix: str, text: str) -> str:
        """Write an immutable, content-addressed input file and return its path."""

        data = text.encode("utf-8")
        with self._root() as root:
            path = self._local_docs(root) / f"{label}-{sha256(data).hexdigest()[:16]}{suffix}"
            ref = self._ignored_ref(root, path)
            try:
                with path.open("xb") as handle:
                    handle.write(data)
                return ref
            except FileExistsError:
                if path.read_bytes() == data:
                    return ref
            except OSError as exc:
                raise HarnessError("llm_transport_failed", "Could not stage an OpenCode input.", run_id=self.run_id) from exc
        raise HarnessError("llm_transport_failed", "An OpenCode input conflicts with its saved snapshot.", run_id=self.run_id)

    def read(self, ref: str) -> str:
        with self._root() as root:
            return self._read(root, ref)

    def write(self, ref: str, text: str) -> None:
        with self._root() as root:
            path = self._checked_path(root, ref)
            path.write_bytes(text.encode("utf-8"))

    def remove(self, ref: str) -> None:
        with self._root() as root:
            self._checked_path(root, ref).unlink(missing_ok=True)

    def worktree(self) -> Path:
        """The run worktree (resolved under the workspace lock, which is released again)."""

        with self._root() as root:
            return root

    def repo_file_checker(self) -> Callable[[str], bool]:
        """A read-only test for repository files or directories cited as evidence (a directory such as a migrations
        folder is valid evidence); the worktree root itself and a path that leaves the worktree never count."""

        with self._root() as root:
            pass

        def exists(ref: str) -> bool:
            path = (root / ref.strip().strip("/")).resolve()
            return path != root and path.is_relative_to(root) and path.exists()

        return exists

    @contextmanager
    def _root(self) -> Iterator[Path]:
        with _ROOT_GUARD, self.workspaces.locked_workspace(self.run_id) as workspace:
            yield Path(workspace.worktree_path).resolve()

    def _checked_path(self, root: Path, ref: str) -> Path:
        path = root / ref
        if path.resolve() != path or not ref.startswith(f"{_LOCAL_DOCS.as_posix()}/"):
            raise HarnessError("llm_transport_failed", f"OpenCode file {ref} is outside its directory.", run_id=self.run_id)
        return path

    def _read(self, root: Path, ref: str) -> str:
        path = self._checked_path(root, ref)
        try:
            if path.is_file():
                return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise HarnessError("llm_transport_failed", f"Could not read OpenCode file {ref}.", run_id=self.run_id) from exc
        raise HarnessError("llm_transport_failed", f"Expected an OpenCode file at {ref}.", run_id=self.run_id)

    def _local_docs(self, root: Path) -> Path:
        path = root / _LOCAL_DOCS
        ignore = path / ".gitignore"
        try:
            if path.resolve() == path and ignore.resolve() == ignore:
                path.mkdir(parents=True, exist_ok=True)
                try:
                    with ignore.open("x", encoding="utf-8") as handle:
                        handle.write("*\n")
                except FileExistsError:
                    if not ignore.is_file():
                        raise
                return path
        except OSError as exc:
            raise HarnessError("llm_transport_failed", "Could not prepare the OpenCode artifact directory.", run_id=self.run_id) from exc
        raise HarnessError(
            "llm_transport_failed", "The OpenCode artifact directory must be inside the run worktree.", run_id=self.run_id,
        )

    def _ignored_ref(self, root: Path, path: Path) -> str:
        ref = path.relative_to(root).as_posix()
        self.workspaces._git(root, "check-ignore", "-q", "--", ref, error_code="llm_transport_failed", run_id=self.run_id)
        return ref

    def _read_session(self, role: str) -> str | None:
        path = self.store.artifact_path(self.run_id, session_artifact(role))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise HarnessError("llm_transport_failed", f"Could not read the saved OpenCode {role} session.", run_id=self.run_id) from exc
        session_id = data.get("session_id") if isinstance(data, dict) else None
        if not _valid_session_id(session_id):
            raise HarnessError("llm_transport_failed", f"The saved OpenCode {role} session ID is invalid.", run_id=self.run_id)
        return session_id

    def _remember_session(self, role: str, output: str | bytes | None, expected: str | None) -> str | None:
        # Capture the identity even if a timeout or invalid answer prevents completion.
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        for line in (output or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") not in {"step_start", "error"}:
                continue
            session_id = event.get("sessionID")
            if not _valid_session_id(session_id):
                continue
            if expected and session_id != expected:
                raise HarnessError("llm_transport_failed", f"OpenCode returned a different {role} session.", run_id=self.run_id)
            if expected is None:
                self.store.write_json_artifact(self.run_id, session_artifact(role), {"session_id": session_id})
                expected = session_id
        return expected


_PATH_KEYS = ("path", "filePath", "file_path", "directory", "cwd")


def outside_paths(messages: list[object], root: Path) -> list[str]:
    """File-tool paths of the last turn (after the last user message) that resolve outside `root`. A glob pattern
    that is itself absolute counts as a path."""

    turn: list[object] = []
    for message in messages:
        if isinstance(message, dict) and message.get("type") == "user":
            turn = []
        elif isinstance(message, dict) and message.get("type") == "assistant":
            turn.append(message)
    root = root.resolve()
    found: list[str] = []
    for message in turn:
        for part in message.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            given = part.get("input") or (part.get("state") or {}).get("input") or {}
            if not isinstance(given, dict):
                continue
            candidates = [str(given[key]) for key in _PATH_KEYS if isinstance(given.get(key), str) and given[key].strip()]
            pattern = given.get("pattern")
            if isinstance(pattern, str) and (Path(pattern).is_absolute() or pattern.startswith(("~", ".."))):
                candidates.append(pattern)
            for raw in candidates:
                path = Path(raw).expanduser()
                resolved = (path if path.is_absolute() else root / path).resolve()
                if resolved != root and not resolved.is_relative_to(root) and raw not in found:
                    found.append(raw)
    return found


def final_answer(text: str) -> str:
    """The verdict line of a file-delivery turn: a worker may explain itself before its final line (OK or a path)."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1].strip("\x60*").strip().replace("\\", "/") if lines else ""


def _valid_session_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"ses_[A-Za-z0-9]+", value) is not None


def _find_opencode() -> str | None:
    executable = shutil.which("opencode.exe" if os.name == "nt" else "opencode")
    if executable is None and os.name == "nt":
        # Launch the npm package binary directly so a timeout kills OpenCode rather than its .cmd shim.
        shim = shutil.which("opencode.cmd")
        if shim:
            native = Path(shim).parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
            if native.is_file():
                executable = str(native)
    return executable


def _read_completion(stdout: str, *, run_id: str) -> tuple[LlmCompletion, str]:
    text_parts: list[str] = []
    finish_reason = ""
    session_id = ""
    prompt_tokens = completion_tokens = 0
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event["type"]
            if kind == "error":
                error = event.get("error")
                name = error.get("name") if isinstance(error, dict) else None
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
                    name = "worker_error"
                raise HarnessError("llm_transport_failed", f"Phase 1 OpenCode error: {name}.", run_id=run_id)
            if kind == "step_start":
                session_id = event["sessionID"]
                text_parts.clear()
                finish_reason = ""
            elif kind == "text":
                text_parts.append(event["part"]["text"])
            elif kind == "step_finish":
                finish_reason = event["part"]["reason"]
                tokens = event["part"].get("tokens") or {}
                cache = tokens.get("cache") or {}
                prompt_tokens += sum(int(value or 0) for value in (
                    tokens.get("input"), cache.get("read"), cache.get("write"),
                ))
                completion_tokens += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        content = strip_code_fence("".join(text_parts)).strip()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise HarnessError("llm_transport_failed", "OpenCode returned invalid JSON events.", run_id=run_id) from exc
    if not session_id or finish_reason not in {"stop", "length"} or (finish_reason == "stop" and not content):
        raise HarnessError("llm_transport_failed", "OpenCode did not return a complete answer.", run_id=run_id)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    return LlmCompletion(content=content, finish_reason=finish_reason, usage=usage), session_id
