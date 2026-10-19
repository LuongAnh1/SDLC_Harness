"""Tài liệu có phiên bản cho bố cục runtime cục bộ."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
import time
from uuid import uuid4

from ..core.errors import HarnessError


RUNTIME_LAYOUT_VERSION = "runtime_layout_v3"
_README_LOCK = RLock()
RUNTIME_README = """# SDLC Harness Runtime

Runtime layout version: `runtime_layout_v3`

This directory contains local operational state for SDLC Harness. It can include
requirements, generated artifacts, LLM metadata, sync reports, and diagnostics.
Do not commit or share the directory as a whole.

## Layout

```text
.runtime/
  README.md
  config.json
  index/active_runs.json
  locks/
  operations/op_<timestamp>_<suffix>.json
  idempotency/<sha256>.json
  runs/<run_id>/
    state.json
    events.jsonl
    artifacts/
    sync/
    developer_execution/
```

Directories are created only when their feature is used.

## Sources of truth

| Location | Purpose |
| --- | --- |
| `config.json` | Dashboard-managed runtime overrides; secrets may be present and the file must stay local. |
| `index/active_runs.json` | Maps each active source to its current run. |
| `runs/<run_id>/state.json` | Current workflow phase, status, checkpoint, actions, and artifact references. |
| `runs/<run_id>/events.jsonl` | Append-only timeline ordered by per-run `sequence`; the sequence is the read cursor. |
| `runs/<run_id>/artifacts/` | Requirement, design, plan, code, test, QA, and traceability evidence. |
| `runs/<run_id>/sync/` | Jira, Confluence, and GitHub synchronization results. |
| `runs/<run_id>/developer_execution/` | Developer execution diagnostics. |
| `operations/*.json` | Durable state for asynchronous API write requests. |
| `idempotency/*.json` | Hashed idempotency keys mapped to operations. |
| `locks/*.lock` | Cross-process mutation locks; file presence does not mean the lock is held. |

`state.json` is the source of current truth. Operation records describe one API
request. Events are an observation timeline and must not be used to reconstruct or
authorize workflow state.

## Runtime config

`config.json` is written atomically by the authenticated Config API. Effective
precedence is defaults, root `.env`, this runtime file, OS environment, then a
single invocation/action override. OS values and bootstrap fields are read-only
in the dashboard. Blank or masked secret inputs keep the existing secret.

Config save/reset is blocked while an operation is queued or running. API host,
port, token, and config-file location remain bootstrap settings. Queue and
shutdown changes require an API restart; other changes apply to the next
operation because application settings are rebuilt for each command.

## Events

- Each complete JSONL line follows `event_v1` and has a strictly increasing sequence.
- `GET /api/v1/runs/{run_id}/events?after=<sequence>&limit=<n>` reads events after a cursor.
- Event payloads contain allowlisted metadata only. Prompts, model output, clarification
  answers, stdout/stderr, shell commands, credentials, and local paths are excluded.
- Events remain with the run until the run directory is deleted.
- A partial final line left by a crash is removed before the next append. Corruption in
  any complete line is reported as `event_log_corrupted`.

## Cleanup

Keep runs that still have a checkpoint or active operation. When deleting a run, also
remove its active index entry and run-specific locks. Incomplete operations found after
restart become `interrupted` and are not replayed automatically.
"""


def ensure_runtime_readme(runtime_root: Path) -> None:
    """Create or update README atomically, without rewriting identical content."""

    path = runtime_root / "README.md"
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with _README_LOCK:
            runtime_root.mkdir(parents=True, exist_ok=True)
            if _readme_is_current(path):
                return
            temp_path.write_text(RUNTIME_README, encoding="utf-8")
            for attempt in range(20):
                try:
                    temp_path.replace(path)
                    break
                except PermissionError:
                    if _readme_is_current(path):
                        return
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
    except OSError as exc:
        raise HarnessError("runtime_write_failed", "Runtime README could not be written.") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _readme_is_current(path: Path) -> bool:
    try:
        return path.is_file() and path.read_text(encoding="utf-8") == RUNTIME_README
    except OSError:
        return False
