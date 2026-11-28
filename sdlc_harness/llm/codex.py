"""Codex transport for H-OC Phase 4 turns (execution plan section 3.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from ..core.config import Settings
from ..core.errors import HarnessError
from ..events import emit_current
from ..storage.runtime import RuntimeStore
from ..verification.runner import _kill_process_tree
from ..workspaces.manager import WorkspaceFileLock
from .client import LlmCompletion

_ROLE = re.compile(r"[a-z][a-z0-9-]{0,40}")
_THREAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")

# G15 (owner 2026-11-28): commands an agent may never run. The sandbox blocks writes outside the worktree but not
# loopback connections (probe: both sandboxes reached the run database on 127.0.0.1), and the prompt's allowed
# commands are advice only. A deny list is decidable; an allow list over free-form PowerShell is not (owner chose it).
# shortcut: the list lives in the Codex transport; move it to a shared module when the OpenCode worker (E3) needs it.
_FORBIDDEN = tuple((name, re.compile(pattern, re.IGNORECASE | re.DOTALL)) for name, pattern in (
    ("container", r"\b(docker|docker-compose|podman)(\.exe)?\b"),
    ("database client", r"\b(psql|pg_dump|pg_restore|pg_ctl)(\.exe)?\b"),
    ("database change", r"\bprisma(\.cmd)?\s+(migrate|db)\b|\bnpm\s+run\s+(db:\S*|seed|test:e2e|e2e|verify\S*)"),
    ("browser tests", r"\bplaywright(\.cmd)?\s+test\b"),
    ("server", r"\bnpm\s+(run\s+)?(dev|start|preview|serve)\b"),
    ("network", r"\b(curl|wget|nc|ncat|telnet)(\.exe)?\b|\b(Invoke-WebRequest|Invoke-RestMethod|Test-NetConnection"
                r"|Start-BitsTransfer)\b|\b(iwr|irm)\s|System\.Net\.|\bNet\.(WebClient|Sockets)\b"),
    # inline code only when it reaches the network, a process or a database: agents read files with `python -c`
    ("inline script", r"\b(node|python3?|py)(\.exe)?\s+(-X\s*\S+\s+)?(-e|--eval|-p|--print|-c)\b(?=.*\b(socket|net"
                      r"|https?|urllib|requests|fetch|child_process|subprocess|spawn|execSync|os\.system|os\.popen"
                      r"|psycopg2?|pg|PrismaClient)\b)"),
    ("package install", r"\b(npm|pnpm|yarn)\s+(install|i|ci|add|remove|uninstall|update|up)\b|\bnpx\s+(-y|--yes)\b"
                        r"|\bpip3?\s+install\b|\b(winget|choco|scoop)\b"),
    ("git history", r"\bgit(\.exe)?\s+(-C\s+\S+\s+)?(push|fetch|pull|remote|reset|switch|commit|worktree|rebase|merge"
                    r"|cherry-pick|revert|clone|submodule|update-ref|filter-branch|tag|config|gc|stash|clean"
                    r"|branch\s+-[dmfc])\b|\bgit(\.exe)?\s+(-C\s+\S+\s+)?checkout\b(?!\s+--\s)"),
    ("process control", r"\b(Stop-Process|Start-Process|taskkill|pkill|killall)\b|\bkill\s+-\d"),
    ("system change", r"\b(setx|schtasks|Set-ItemProperty|New-ItemProperty)\b|\breg(\.exe)?\s+(add|delete)\b"),
))


def forbidden_command(command: str, *, e2e_config: str | None = None) -> str | None:
    """The G15 rule a Codex shell command breaks, or None.

    `e2e_config` is the Harness-staged Playwright config of an integration turn whose services the Harness runs: a
    browser-test command naming exactly that config is allowed, every other one still breaks G15.
    """

    for name, pattern in _FORBIDDEN:
        if not pattern.search(command):
            continue
        if name == "browser tests" and e2e_config and _uses_only_config(command, e2e_config):
            continue
        return name
    return None


def _uses_only_config(command: str, config: str) -> bool:
    configs = re.findall(r"--config(?:=|\s+)[\"']?([^\s\"';|&]+)", command.replace("\\", "/"))
    return bool(configs) and all(item == config for item in configs)


@dataclass(frozen=True)
class CodexTurn:
    completion: LlmCompletion
    session_id: str
    touched: tuple[str, ...]
    outside: tuple[str, ...]


def _find_codex() -> str | None:
    executable = shutil.which("codex")
    if executable:
        return executable
    local = os.environ.get("LOCALAPPDATA")
    if local:
        path = Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        if path.is_file():
            return str(path)
    return None


def _run_turn(
    argv: list[str],
    *,
    root: Path,
    task: str,
    timeout: int,
    on_line: Callable[[bytes], None],
    stderr_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    # Separate spool handles let us persist events during the turn without waiting on descendant-held pipes.
    with tempfile.TemporaryDirectory(prefix="sdlc-codex-") as temp:
        events_path = Path(temp) / "events.jsonl"
        with (Path(temp) / "prompt").open("w+b") as prompt, events_path.open("wb") as output, stderr_path.open("xb") as stderr:
            prompt.write(task.encode("utf-8"))
            prompt.seek(0)
            with events_path.open("rb") as events:
                process = subprocess.Popen(
                    argv, cwd=root, stdin=prompt, stdout=output, stderr=stderr,
                    start_new_session=os.name != "nt", env=env,
                )
                deadline = time.monotonic() + timeout
                pending = b""

                def drain(*, final: bool = False) -> None:
                    nonlocal pending
                    while chunk := events.read(65536):
                        lines = (pending + chunk).split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            if line.strip():
                                on_line(line)
                        if not final:
                            return
                    if final and pending.strip():
                        on_line(pending)

                try:
                    while True:
                        code = process.poll()
                        drain(final=code is not None)
                        if code is not None:
                            return code
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        try:
                            process.wait(timeout=min(remaining, 0.1))
                        except subprocess.TimeoutExpired:
                            pass
                except BaseException as exc:
                    if process.poll() is None:
                        _kill_process_tree(process)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                    if isinstance(exc, subprocess.TimeoutExpired):
                        drain()
                    raise


class CodexWorker:
    """Run each role in its own persisted Codex thread and worktree."""

    def __init__(
        self, settings: Settings, *, run_id: str, model: str, effort: str, reviewer_model: str, reviewer_effort: str,
    ):
        self.run_id = run_id
        self.model = model
        self.effort = effort
        self.reviewer_model = reviewer_model
        self.reviewer_effort = reviewer_effort
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def turn(
        self, *, role: str, root: Path, task: str, timeout: int, read_only: bool = False,
        env: dict[str, str] | None = None,
    ) -> CodexTurn:
        """One turn; `env` adds keys to the Codex process environment (its shells inherit them)."""

        if not _ROLE.fullmatch(role):
            raise self._error(f"Invalid Codex role {role!r}.")
        executable = _find_codex()
        if executable is None:
            raise self._error("Codex executable was not found.")
        mode = "read-only" if read_only else "workspace-write"
        model, effort = (self.reviewer_model, self.reviewer_effort) if read_only else (self.model, self.effort)
        session_name = f"codex_{role.replace('-', '_')}_session_v1.json"
        try:
            root = root.resolve(strict=True)
            answer = self.store.artifact_path(self.run_id, f"codex_{role}_{uuid4().hex}_answer.txt")
            if not root.is_dir() or answer.resolve().is_relative_to(root) or answer.exists():
                raise self._error("Codex requires a directory worktree and a fresh answer path outside it.")
            lock = self.store.runtime_root / "locks" / f"codex-{self.run_id}-{role}.lock"
            with WorkspaceFileLock(lock, run_id=self.run_id):
                answer.parent.mkdir(parents=True, exist_ok=True)
                result = self._turn(executable, session_name, root, mode, answer, task, timeout, model=model, effort=effort,
                                    env=env)
        except HarnessError as exc:
            if exc.error_code in {"llm_transport_failed", "hoc_forbidden_command"}:  # G15 stops the run, no retry
                raise
            raise self._error(f"Could not store or lock the Codex {role} turn ({exc.error_code}).") from exc
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise self._error(f"Codex {role} turn could not complete ({type(exc).__name__}).") from exc

        emit_current(
            category="llm", event_type="codex_call_completed", message="Codex call completed.",
            data={"model": model, "effort": effort, "role": role},
        )
        return result

    def _error(self, message: str) -> HarnessError:
        return HarnessError("llm_transport_failed", message, run_id=self.run_id)

    def _saved(
        self, session_name: str, root: Path, mode: str, *, model: str, effort: str,
    ) -> tuple[str | None, dict[str, int]]:
        path = self.store.artifact_path(self.run_id, session_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, dict.fromkeys(_USAGE_FIELDS, 0)
        if not isinstance(data, dict):
            raise self._error("Invalid saved Codex session.")
        thread = data.get("thread_id")
        if not isinstance(thread, str) or not _THREAD.fullmatch(thread):
            raise self._error("Invalid saved Codex thread ID.")
        if not isinstance(data.get("root"), str) or Path(data["root"]) != root or data.get("mode") != mode:
            raise self._error("Saved Codex session is bound to a different root or sandbox mode.")
        if data.get("model") != model or data.get("effort") != effort:
            raise self._error("Saved Codex session is bound to a different model or effort.")
        return thread, self._usage(data.get("cumulative_usage"))

    def _usage(self, usage: object, *, previous: dict[str, int] | None = None) -> dict[str, int]:
        if not isinstance(usage, dict):
            raise self._error("Codex usage is missing or malformed.")
        values = {}
        for key in _USAGE_FIELDS:
            value = usage.get(key)
            if key == "reasoning_output_tokens" and key not in usage and previous is not None:
                value = previous[key]
            if type(value) is not int or value < (previous or {}).get(key, 0):
                raise self._error(f"Invalid cumulative Codex usage field {key}.")
            values[key] = value
        return values

    def _turn(
        self, executable: str, session_name: str, root: Path, mode: str, answer: Path, task: str, timeout: int, *,
        model: str, effort: str, env: dict[str, str] | None = None,
    ) -> CodexTurn:
        expected, previous = self._saved(session_name, root, mode, model=model, effort=effort)
        argv = [executable, "exec"]
        if expected:
            argv += ["resume", expected]
        argv += ["-m", model, "-c", f"model_reasoning_effort={effort}", "-c", 'windows.sandbox="elevated"']
        argv += ["-c", f'sandbox_mode="{mode}"'] if expected else ["-s", mode, "-C", str(root)]
        argv += ["--ignore-user-config", "--json", "-o", str(answer), "-"]

        thread = None
        cumulative = None
        failed = None
        touched: dict[str, None] = {}
        commands_path = answer.with_name(answer.name.replace("_answer.txt", "_commands.jsonl"))

        def audit(kind: str, item: dict) -> None:
            """Log every shell command of the turn; a G15 command kills the turn as soon as it starts."""

            command = item.get("command")
            if not isinstance(command, str) or not command:
                raise self._error("Invalid Codex command item.")
            row = {"event": kind, "command": command, "exit_code": item.get("exit_code"),
                   "output_tail": str(item.get("aggregated_output") or "")[-2000:]}
            with commands_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            rule = forbidden_command(command, e2e_config=(env or {}).get("SDLC_E2E_CONFIG"))
            if rule:
                raise HarnessError(
                    "hoc_forbidden_command", f"G15 forbidden command ({rule}): {command[:300]}", run_id=self.run_id,
                )

        def save(thread_id: str, usage: dict[str, int]) -> None:
            self.store.write_json_artifact(self.run_id, session_name, {
                "thread_id": thread_id, "root": str(root), "mode": mode, "model": model, "effort": effort,
                "cumulative_usage": usage,
            })

        def on_line(line: bytes) -> None:
            nonlocal thread, cumulative, failed
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise self._error("Invalid Codex event.")
            kind = event["type"]
            if kind == "thread.started":
                identity = event.get("thread_id")
                if not isinstance(identity, str) or not _THREAD.fullmatch(identity):
                    raise self._error("Invalid Codex thread ID.")
                if (expected and expected != identity) or (thread and thread != identity):
                    raise self._error("Codex returned a different thread ID.")
                if thread is None and expected is None:
                    save(identity, previous)
                thread = identity
            elif kind in {"turn.failed", "error"}:
                failed = kind
            elif kind == "turn.completed":
                if thread is None or cumulative is not None:
                    raise self._error("Unexpected Codex completion event.")
                cumulative = self._usage(event.get("usage"), previous=previous)
                save(thread, cumulative)
            elif kind == "item.started":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "command_execution":
                    audit(kind, item)
            elif kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise self._error("Invalid Codex completed item.")
                if item.get("type") == "command_execution":
                    audit(kind, item)
                if item.get("type") == "file_change":
                    changes = item.get("changes")
                    if not isinstance(changes, list):
                        raise self._error("Invalid Codex file changes.")
                    for change in changes:
                        path = change.get("path") if isinstance(change, dict) else None
                        if not isinstance(path, str) or not path or "\0" in path:
                            raise self._error("Invalid Codex file change path.")
                        touched[path] = None

        code = _run_turn(argv, root=root, task=task, timeout=timeout, on_line=on_line,
                         stderr_path=answer.with_name(answer.name.replace("_answer.txt", "_stderr.txt")),
                         env={**os.environ, **env} if env else None)
        if failed or code:
            raise self._error(f"Codex turn failed ({failed or f'exit {code}'}).")
        if thread is None or cumulative is None:
            raise self._error("Codex did not report a thread and a completed turn.")
        if answer.is_symlink() or not answer.is_file():
            raise self._error("Codex answer file is missing or invalid.")
        content = answer.read_text(encoding="utf-8")
        if not content.strip():
            raise self._error("Codex answer is empty.")
        usage = {key: cumulative[key] - previous[key] for key in _USAGE_FIELDS}
        usage.update(
            prompt_tokens=usage["input_tokens"], completion_tokens=usage["output_tokens"],
            total_tokens=usage["input_tokens"] + usage["output_tokens"],
        )
        temp = Path(tempfile.gettempdir()).resolve()
        outside = []
        for path in touched:
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root) and not resolved.is_relative_to(temp):
                outside.append(path)
        return CodexTurn(LlmCompletion(content=content, finish_reason="stop", usage=usage), thread, tuple(touched), tuple(outside))
