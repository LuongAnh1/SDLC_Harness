"""Test the Codex transport without launching a Codex process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.events import event_scope
from sdlc_harness.llm import codex

RUN = "run_20261128_090000_00000001"
THREAD = "019abc00-1234-7654-9876-0123456789ab"


@pytest.fixture
def worker(tmp_path, monkeypatch):
    settings = SimpleNamespace(repo_root=tmp_path, runtime_root=tmp_path / "runtime")
    root = tmp_path / "work tree"
    root.mkdir()
    monkeypatch.setattr(codex, "_find_codex", lambda: "fake-codex")
    return codex.CodexWorker(
        settings, run_id=RUN, model="work-model", effort="max", reviewer_model="review-model", reviewer_effort="high",
    ), root


def session(worker, role="agent-1"):
    path = worker.store.artifact_path(RUN, f"codex_{role.replace('-', '_')}_session_v1.json")
    return json.loads(path.read_text(encoding="utf-8"))


def started(thread=THREAD):
    return {"type": "thread.started", "thread_id": thread}


def completed(input_tokens=100, cached=60, output=12, reasoning=4):
    return {"type": "turn.completed", "usage": {
        "input_tokens": input_tokens, "cached_input_tokens": cached,
        "output_tokens": output, "reasoning_output_tokens": reasoning,
    }}


envs = []


def fake_turn(monkeypatch, events, *, answer="From the answer file\nDONE\n", code=0):
    calls = []

    def run(argv, *, root, task, timeout, on_line, stderr_path, env=None):
        path = Path(argv[argv.index("-o") + 1])
        assert not path.exists() and not path.is_relative_to(root)
        assert stderr_path == path.with_name(path.name.replace("_answer.txt", "_stderr.txt"))
        calls.append((argv, root, task, timeout, path))
        envs.append(env)
        for event in events:
            on_line(json.dumps(event).encode("utf-8"))
        if answer is not None:
            path.write_text(answer, encoding="utf-8")
        return code

    monkeypatch.setattr(codex, "_run_turn", run)
    return calls


@pytest.mark.parametrize("read_only", [False, True])
def test_first_and_resumed_turn_use_explicit_root_mode_and_delta_usage(worker, monkeypatch, read_only):
    transport, root = worker
    role = "reviewer-scope" if read_only else "agent-1"
    mode = "read-only" if read_only else "workspace-write"
    model, effort = ("review-model", "high") if read_only else ("work-model", "max")
    calls = fake_turn(monkeypatch, [
        started(), {"type": "item.completed", "item": {"type": "agent_message", "text": "Not the answer"}},
        completed(),
    ])
    with event_scope(runtime_root=transport.store.runtime_root, command="continue-run", run_id=RUN) as events:
        first = transport.turn(role=role, root=root, task="First prompt", timeout=10, read_only=read_only)
    argv, cwd, prompt, timeout, first_path = calls[0]
    common = ["-m", model, "-c", f"model_reasoning_effort={effort}", "-c", 'windows.sandbox="elevated"']
    assert argv == [
        "fake-codex", "exec", *common, "-s", mode, "-C", str(root.resolve()),
        "--ignore-user-config", "--json", "-o", str(first_path), "-",
    ]
    assert (cwd, prompt, timeout) == (root.resolve(), "First prompt", 10)
    assert first.completion.content == "From the answer file\nDONE\n"
    assert first.completion.finish_reason == "stop"
    assert first.session_id == THREAD and first.touched == first.outside == ()
    assert first.completion.usage == {
        "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 12, "reasoning_output_tokens": 4,
        "prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112,
    }
    assert first_path.parent == transport.store.artifact_path(RUN, "example").parent
    stored = session(transport, role)
    assert stored == {"thread_id": THREAD, "root": str(root.resolve()), "mode": mode,
                      "model": model, "effort": effort, "cumulative_usage": completed()["usage"]}

    second_event = completed(210, 140, 23, 6)
    calls = fake_turn(monkeypatch, [started(), second_event], answer="Second answer")
    with event_scope(runtime_root=transport.store.runtime_root, command="continue-run", run_id=RUN):
        second = transport.turn(role=role, root=root, task="Second prompt", timeout=11, read_only=read_only)
    argv, cwd, prompt, timeout, second_path = calls[0]
    assert argv == [
        "fake-codex", "exec", "resume", THREAD, *common, "-c", f'sandbox_mode="{mode}"',
        "--ignore-user-config", "--json", "-o", str(second_path), "-",
    ]
    assert (cwd, prompt, timeout) == (root.resolve(), "Second prompt", 11)
    assert first_path != second_path and first_path.is_file()
    assert second.completion.usage == {
        "input_tokens": 110, "cached_input_tokens": 80, "output_tokens": 11, "reasoning_output_tokens": 2,
        "prompt_tokens": 110, "completion_tokens": 11, "total_tokens": 121,
    }
    assert session(transport, role)["cumulative_usage"] == second_event["usage"]
    records = [json.loads(line) for line in events.store.event_path(RUN).read_text(encoding="utf-8").splitlines()]
    assert [(record["type"], record["data"]) for record in records] == [
        ("codex_call_completed", {"model": model, "effort": effort, "role": role}),
    ] * 2


def test_sessions_are_bound_to_root_and_mode_and_roles_are_independent(worker, monkeypatch):
    transport, root = worker
    other = root.parent / "other"
    other.mkdir()
    fake_turn(monkeypatch, [started(), completed()])
    transport.turn(role="agent-1", root=root, task="", timeout=10)
    calls = fake_turn(monkeypatch, [started("other-thread"), completed()])
    for next_root, read_only in [(other, False), (root, True)]:
        with pytest.raises(HarnessError, match="different root or sandbox mode") as error:
            transport.turn(role="agent-1", root=next_root, task="", timeout=10, read_only=read_only)
        assert error.value.error_code == "llm_transport_failed"
    assert calls == []
    turn = transport.turn(role="reviewer-scope", root=other, task="", timeout=10, read_only=True)
    assert turn.session_id == "other-thread"
    assert session(transport)["thread_id"] == THREAD


def test_resume_requires_the_saved_thread_id(worker, monkeypatch):
    transport, root = worker
    fake_turn(monkeypatch, [started(), completed()])
    transport.turn(role="agent-1", root=root, task="", timeout=10)
    saved = session(transport)
    fake_turn(monkeypatch, [started("different-thread"), completed()])
    with pytest.raises(HarnessError, match="different thread ID"):
        transport.turn(role="agent-1", root=root, task="", timeout=10)
    assert session(transport) == saved


@pytest.mark.parametrize("field", ["model", "effort"])
@pytest.mark.parametrize("read_only", [False, True])
def test_resume_rejects_mismatched_or_missing_model_binding(worker, monkeypatch, field, read_only):
    transport, root = worker
    role = "reviewer-base" if read_only else "base"
    fake_turn(monkeypatch, [started(), completed()])
    transport.turn(role=role, root=root, task="", timeout=10, read_only=read_only)
    saved = session(transport, role)
    calls = fake_turn(monkeypatch, [])
    for invalid in ({**saved, field: "different"}, {key: value for key, value in saved.items() if key != field}):
        transport.store.write_json_artifact(RUN, f"codex_{role.replace('-', '_')}_session_v1.json", invalid)
        with pytest.raises(HarnessError, match="different model or effort") as error:
            transport.turn(role=role, root=root, task="", timeout=10, read_only=read_only)
        assert error.value.error_code == "llm_transport_failed"
        assert session(transport, role) == invalid
    assert calls == []


def test_thread_saved_before_timeout_kills_only_that_process(worker, monkeypatch):
    transport, root = worker
    output = (json.dumps(started()) + "\n").encode("utf-8")
    killed = []

    class Process:
        def __init__(self, argv, **options):
            assert argv[:2] == ["fake-codex", "exec"]
            assert options["cwd"] == root.resolve()
            assert options["stdin"].read() == "Prompt via stdin".encode("utf-8")
            assert options["start_new_session"] is (os.name != "nt")
            options["stdout"].write(output)
            options["stdout"].flush()
            options["stderr"].write(b"Codex diagnostic\n")
            options["stderr"].flush()

        def poll(self):
            return -1 if killed else None

        def wait(self, *, timeout):
            assert killed == [self]
            return -1

    def kill(process):
        saved = session(transport)
        assert (saved["thread_id"], saved["model"], saved["effort"]) == (THREAD, "work-model", "max")
        killed.append(process)

    monkeypatch.setattr(codex.subprocess, "Popen", Process)
    monkeypatch.setattr(codex, "_kill_process_tree", kill)
    with pytest.raises(HarnessError, match="TimeoutExpired") as error:
        transport.turn(role="agent-1", root=root, task="Prompt via stdin", timeout=0)
    assert error.value.error_code == "llm_transport_failed"
    assert len(killed) == 1
    assert all(value == 0 for value in session(transport)["cumulative_usage"].values())
    artifacts = transport.store.artifact_path(RUN, "example").parent
    (diagnostic,) = artifacts.glob("*_stderr.txt")
    assert diagnostic.read_bytes() == b"Codex diagnostic\n"


@pytest.mark.parametrize(("events", "answer", "code", "message"), [
    ([started(), completed()], None, 0, "answer file is missing"),
    ([started(), completed()], "  \n", 0, "answer is empty"),
    ([started(), completed()], "DONE", 17, "exit 17"),
    ([started(), {"type": "turn.failed"}], "DONE", 0, "turn.failed"),
    ([started(), {"type": "error"}, completed()], "DONE", 0, "error"),
    ([started()], "DONE", 0, "completed turn"),
])
def test_transport_failures_preserve_the_reported_thread(worker, monkeypatch, events, answer, code, message):
    transport, root = worker
    fake_turn(monkeypatch, events, answer=answer, code=code)
    with pytest.raises(HarnessError, match=message) as error:
        transport.turn(role="agent-1", root=root, task="", timeout=10)
    assert error.value.error_code == "llm_transport_failed"
    assert session(transport)["thread_id"] == THREAD
    if events[-1]["type"] == "turn.completed":
        assert session(transport)["cumulative_usage"] == events[-1]["usage"]


def test_touched_paths_are_audited_but_system_temp_is_exempt(worker, monkeypatch, tmp_path):
    transport, root = worker
    temp = tmp_path / "system-temp"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    outside = str(root.parent / "other" / "absolute.ts")
    paths = ["src/a.ts", "../other/relative.ts", str(temp / "scratch"), outside]
    complete = completed()
    del complete["usage"]["reasoning_output_tokens"]
    fake_turn(monkeypatch, [
        started(), {"type": "item.completed", "item": {
            "type": "file_change", "changes": [{"path": path} for path in [*paths, "src/a.ts"]],
        }},
        {"type": "item.updated", "item": {"type": "file_change", "changes": [{"path": "../incomplete"}]}},
        complete,
    ])
    result = transport.turn(role="agent-1", root=root, task="", timeout=10)
    assert result.touched == tuple(paths)
    assert result.outside == ("../other/relative.ts", outside)
    assert result.completion.usage["reasoning_output_tokens"] == 0


SHELL = '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '


@pytest.mark.parametrize("command", [  # commands the r4 agents really ran (2026-11-28), plus a path restore
    SHELL + '"rtk git status --short; rtk git diff --stat; rtk proxy git diff --check"',
    "Get-Content 'docs/sdlc-local/design.md' | Select-Object -First 40",
    "rtk rg --files docs/sdlc-local",
    "rtk proxy npm run typecheck",
    "npm run test:unit -- apps/web/src/components/Pages/Home/Home.test.tsx",
    "npx vitest run apps/api/src/view/articleViewer.test.ts",
    'rtk codegraph explore "prismaErrorHandler P2002 field mapping"',
    "for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Contains('U-24')) { $lines[$i] } }",
    "Select-String -Path 'packages/db/generated/client/models/Article.ts' -Pattern 'published'",
    "git -C . log --oneline -3; git show HEAD --stat",
    "git checkout -- apps/web/src/components/ArticleEditor/ArticleEditor.slice.tsx",
    "git restore apps/web/src/types/article.ts",
    "rtk proxy python -c \"from pathlib import Path; lines=Path('docs/sdlc-local/requirement.md').read_text("
    "encoding='utf-8').splitlines(); print(lines[:5])\"",
])
def test_ordinary_agent_commands_are_not_forbidden(command):
    assert codex.forbidden_command(command) is None


@pytest.mark.parametrize(("command", "rule"), [
    (SHELL + '"docker ps"', "container"),
    ('& "C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe" ps', "container"),
    ("psql -h 127.0.0.1 -p 50233 -U sdlc", "database client"),
    ("npx prisma migrate reset --force", "database change"),
    ("rtk npm run db:reset", "database change"),
    ("npm run test:e2e", "database change"),
    ("npx playwright test tests/e2e/draft-race-api.spec.ts", "browser tests"),
    ("npm run dev --workspace @sdlc-target/api", "server"),
    ("Invoke-WebRequest https://example.invalid", "network"),
    ("curl.exe https://example.invalid", "network"),
    ("node -e \"require('net').connect(50233, '127.0.0.1')\"", "inline script"),
    ("python -X utf8 -c \"import socket; socket.create_connection(('127.0.0.1', 50233))\"", "inline script"),
    ("npm install left-pad", "package install"),
    ("git push origin HEAD", "git history"),
    ("git checkout main", "git history"),
    ("rtk git reset --hard HEAD~1", "git history"),
    ("Stop-Process -Id 42", "process control"),
])
def test_forbidden_commands_name_their_rule(command, rule):
    assert codex.forbidden_command(command) == rule


CONFIG = "docs/sdlc-local/playwright.harness.config.ts"


@pytest.mark.parametrize(("command", "allowed"), [
    (f"npx.cmd playwright test tests/e2e/a.spec.ts --config {CONFIG}", True),
    (f'npx.cmd playwright test tests\\e2e\\a.spec.ts -g "first" --config={CONFIG.replace("/", chr(92))}', True),
    ("npx.cmd playwright test tests/e2e/a.spec.ts", False),  # the repository config would boot the services
    ("npx.cmd playwright test --config playwright.config.ts", False),
    (f"npx.cmd playwright test --config {CONFIG} --config playwright.config.ts", False),
    (f"npx.cmd playwright test --config {CONFIG}; docker ps", False),
    ("npm run test:e2e", False),
])
def test_only_the_harness_config_lets_a_turn_run_browser_specs(command, allowed):
    assert (codex.forbidden_command(command, e2e_config=CONFIG) is None) is allowed
    assert codex.forbidden_command(command) is not None


def test_turn_env_reaches_the_codex_process_and_allows_its_config(worker, monkeypatch):
    transport, root = worker
    run = {"type": "command_execution", "command": SHELL + f'"npx.cmd playwright test --config {CONFIG}"',
           "exit_code": 0, "aggregated_output": "1 passed"}
    envs.clear()
    fake_turn(monkeypatch, [started(), {"type": "item.started", "item": run}, {"type": "item.completed", "item": run},
                            completed()])
    transport.turn(role="integration", root=root, task="Work", timeout=10, env={"SDLC_E2E_CONFIG": CONFIG, "X": "1"})
    assert envs[-1]["X"] == "1" and envs[-1]["PATH"] == os.environ["PATH"]
    fake_turn(monkeypatch, [started(), {"type": "item.started", "item": run}, completed()])
    with pytest.raises(HarnessError, match="browser tests"):
        transport.turn(role="integration", root=root, task="Again", timeout=10)
    assert envs[-1] is None


def test_commands_are_logged_and_a_forbidden_start_stops_the_turn(worker, monkeypatch):
    transport, root = worker
    ok = {"type": "command_execution", "command": SHELL + '"rtk git status --short"', "exit_code": 0,
          "aggregated_output": "M a.ts\n"}
    fake_turn(monkeypatch, [started(), {"type": "item.started", "item": {**ok, "exit_code": None, "aggregated_output": ""}},
                            {"type": "item.completed", "item": ok}, completed()])
    transport.turn(role="agent-1", root=root, task="Work", timeout=10)
    logs = sorted(transport.store.run_dir(RUN).glob("artifacts/codex_agent-1_*_commands.jsonl"))
    rows = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    assert [(row["event"], row["exit_code"], row["output_tail"]) for row in rows] == [
        ("item.started", None, ""), ("item.completed", 0, "M a.ts\n"),
    ]

    bad = {"type": "command_execution", "command": SHELL + '"docker ps"', "status": "in_progress"}
    later = {"type": "item.completed", "item": {**bad, "exit_code": 0}}
    fake_turn(monkeypatch, [started(), {"type": "item.started", "item": bad}, later, completed()])
    with pytest.raises(HarnessError) as error:
        transport.turn(role="agent-1", root=root, task="Work again", timeout=10)
    assert error.value.error_code == "hoc_forbidden_command"
    assert "G15 forbidden command (container)" in str(error.value)
    (new,) = set(transport.store.run_dir(RUN).glob("artifacts/codex_agent-1_*_commands.jsonl")) - set(logs)
    assert [json.loads(line)["event"] for line in new.read_text(encoding="utf-8").splitlines()] == ["item.started"]


def test_find_codex_uses_path_then_the_windows_install_location(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    fallback = tmp_path / "Programs/OpenAI/Codex/bin/codex.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    monkeypatch.setattr(codex.shutil, "which", lambda name: "on-path" if name == "codex" else None)
    assert codex._find_codex() == "on-path"
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    assert codex._find_codex() == str(fallback)
    fallback.unlink()
    assert codex._find_codex() is None
