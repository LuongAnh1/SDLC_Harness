"""Thăm dò Developer.plan với DeepSeek thật mà không thay đổi runtime.

Script này đọc state/artifact của một run đã tới `waiting_design_approval`, dựng
đúng prompt Phase 3 Developer.plan, gọi Chat Completions trực tiếp và lưu raw
response ra `.debug/`. Nó không gọi workflow approve, không ghi state, không
sync Jira/Confluence.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sdlc_harness.agents.developer.planner import (
    _design_mappings,
    _developer_plan_prompt,
    _parse_developer_plan_payload,
    _test_intents,
)
from sdlc_harness.core.config import load_settings, repo_root
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.repo_context import CodeGraphContextProvider
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows.phase3 import _issue_key_from_state, _planned_branch


SYSTEM_PROMPT = (
    "You are SDLC Harness Developer.plan. Return JSON only. "
    "Create a concrete implementation plan, but do not write code."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe raw DeepSeek response for Developer.plan.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--llm-mode", default="deepseek", choices=["deepseek", "openai_compatible"])
    parser.add_argument("--output-dir", default=".debug/deepseek_developer_plan")
    args = parser.parse_args()

    root = repo_root()
    settings = load_settings(root=root, overrides={"SDLC_LLM_MODE": args.llm_mode})
    if not settings.llm_api_key:
        raise HarnessError(
            "debug_probe_missing_key",
            "SDLC_LLM_API_KEY is required to probe DeepSeek.",
            run_id=args.run_id,
        )

    store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
    state = store.read_state(args.run_id)
    artifact_paths = _string_dict(state.get("latest_artifacts"))
    requirement_packet = _read_text(root, _required(artifact_paths, "requirement_packet"))
    design_artifact = _read_text(root, _required(artifact_paths, "design_artifact"))
    requirement_design_trace = _read_json(root, _required(artifact_paths, "requirement_design_trace"))
    class_graph = _read_json(root, _required(artifact_paths, "class_diagram_graph"))
    sequence_graph = _read_json(root, _required(artifact_paths, "sequence_diagram_graph"))

    target_path = settings.target_repo_path.resolve()
    target_files = _target_source_files(target_path)
    issue_key = _issue_key_from_state(state)
    planned_branch = _planned_branch(prefix=settings.implementation_branch_prefix, issue_key=issue_key)
    repo_context = CodeGraphContextProvider(fake_mode=False).collect(
        target_path=target_path,
        target_files=target_files,
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        issue_key=issue_key,
    ).to_dict()
    base_mappings = _design_mappings(
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_files=target_files,
    )
    base_test_intents = _test_intents(
        mappings=base_mappings,
        test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
    )
    prompt = _developer_plan_prompt(
        run_id=args.run_id,
        source_ref=str(state.get("source_ref") or ""),
        issue_key=issue_key,
        requirement_packet=requirement_packet,
        design_artifact=design_artifact,
        requirement_design_trace=requirement_design_trace,
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        repo_context=repo_context,
        base_mappings=base_mappings,
        base_test_intents=base_test_intents,
        target_repo_path=str(target_path),
        target_repo_remote=_git_output(target_path, "remote", "get-url", "origin"),
        target_base_branch=settings.target_base_branch,
        planned_branch=planned_branch,
        target_files=target_files,
        test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
        repair_feedback="",
        previous_review={},
    )

    output_dir = (root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefix = output_dir / f"{args.run_id}_{stamp}_developer_plan"

    (prefix.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")
    response = _call_chat_completions(settings=settings, prompt=prompt)
    (prefix.with_suffix(".response.json")).write_text(
        json.dumps(response["raw_response"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    content = response["content"]
    (prefix.with_suffix(".content.txt")).write_text(content, encoding="utf-8")

    parse_report = _parse_report(content)
    (prefix.with_suffix(".parse.json")).write_text(
        json.dumps(parse_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": args.run_id,
                "model": settings.llm_model,
                "output_prefix": str(prefix),
                "content_chars": len(content),
                "parse": parse_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _call_chat_completions(*, settings, prompt: str) -> dict[str, object]:
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    if settings.llm_mode == "deepseek":
        payload["thinking"] = {"type": settings.llm_thinking_mode}
    request = urllib.request.Request(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as http_response:
            raw = http_response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HarnessError("debug_probe_http_failed", f"DeepSeek HTTP error {exc.code}: {body[:500]}") from exc
    data = json.loads(raw)
    content = str(data["choices"][0]["message"]["content"])
    return {"raw_response": data, "content": content}


def _parse_report(content: str) -> dict[str, object]:
    try:
        parsed = _parse_developer_plan_payload(content)
    except HarnessError as exc:
        return {"ok": False, "error_code": exc.error_code, "message": exc.message}
    return {
        "ok": True,
        "keys": sorted(parsed.keys()),
        "summary": parsed.get("summary", ""),
        "implementation_steps": len(parsed.get("implementation_steps", [])),
        "proposed_files": len(parsed.get("proposed_files", [])),
        "design_mappings": len(parsed.get("design_mappings", [])),
        "test_intents": len(parsed.get("test_intents", [])),
    }


def _required(paths: dict[str, str], key: str) -> str:
    value = paths.get(key, "")
    if not value:
        raise HarnessError("debug_probe_missing_artifact", f"Missing artifact path: {key}")
    return value


def _read_text(root: Path, relative_path: str) -> str:
    return (root / relative_path).read_text(encoding="utf-8")


def _read_json(root: Path, relative_path: str) -> dict[str, object]:
    parsed = json.loads((root / relative_path).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise HarnessError("debug_probe_invalid_artifact", f"JSON artifact must be an object: {relative_path}")
    return parsed


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _target_source_files(target_path: Path) -> list[str]:
    files: list[str] = []
    for base in ("src/main/java", "src/test/java"):
        root = target_path / base
        if root.exists():
            for path in sorted(root.rglob("*.java")):
                files.append(str(path.relative_to(target_path)).replace("\\", "/"))
    return files


def _git_output(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
