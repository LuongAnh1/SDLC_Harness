"""CodeGraph context cho target repo mà Phase 3 sẽ lập kế hoạch sửa.

Phase 3 chưa ghi code, nhưng Developer cần hiểu snapshot worktree thật thay vì
chỉ đoán file từ tên class trong design graph. Module này khởi tạo `.codegraph/`
ngay trong worktree của run, đọc context qua CodeGraph, rồi trả evidence ngắn để
Developer LLM lập implementation plan.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError

MAX_EVIDENCE_CHARS = 12000
DEFAULT_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class RepoContext:
    """Context repo đã thu thập cho Developer.plan."""

    mode: str
    query: str
    evidence: str
    source_files: list[str]
    status: str = "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "status": self.status,
            "query": self.query,
            "source_files": self.source_files,
            "evidence": self.evidence,
        }


class CodeGraphContextProvider:
    """Đọc target repo bằng CodeGraph, có fake mode cho unit test."""

    def __init__(self, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, fake_mode: bool = False):
        self.timeout_seconds = timeout_seconds
        self.fake_mode = fake_mode

    def require_index(self, target_path: Path) -> None:
        """Chặn khi workspace chưa có index sau bước prepare tự động."""

        if not (target_path / ".codegraph").exists():
            raise HarnessError(
                "target_repo_codegraph_missing",
                (
                    "Run workspace has no .codegraph/ index after automatic preparation."
                ),
            )

    def collect(
        self,
        *,
        target_path: Path,
        target_files: list[str],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        issue_key: str,
    ) -> RepoContext:
        """Thu thập context liên quan tới design đã duyệt."""

        self.require_index(target_path)
        query = _context_query(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
            issue_key=issue_key,
        )
        if self.fake_mode:
            return RepoContext(
                mode="fake_codegraph_context_v1",
                query=query,
                source_files=target_files,
                evidence=_fake_repo_evidence(target_path=target_path, target_files=target_files),
            )
        evidence = _run_codegraph_explore(
            target_path=target_path,
            query=query,
            timeout_seconds=self.timeout_seconds,
        )
        return RepoContext(
            mode="codegraph_context_v1",
            query=query,
            source_files=target_files,
            evidence=_truncate(evidence, limit=MAX_EVIDENCE_CHARS),
        )

    def prepare_index(self, target_path: Path) -> dict[str, object]:
        """Khởi tạo index cho đúng snapshot worktree trước khi thu context."""

        before = (target_path / ".codegraph").exists()
        initialized = False
        if not before:
            if self.fake_mode:
                marker = target_path / ".codegraph" / "README.md"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("Fake CodeGraph index for isolated workflow tests.\n", encoding="utf-8")
            else:
                _run_codegraph_init(target_path=target_path, timeout_seconds=self.timeout_seconds)
            initialized = True
        after = (target_path / ".codegraph").exists()
        smoke = None
        if after and not self.fake_mode:
            smoke = _run_codegraph_explore(
                target_path=target_path,
                query="project structure entrypoints tests",
                timeout_seconds=self.timeout_seconds,
            )
        return {
            "codegraph_exists_before": before,
            "codegraph_exists_after": after,
            "codegraph_initialized": initialized,
            "smoke_ok": bool(after),
            "smoke_excerpt": _truncate(smoke or "", limit=2000),
        }


def _run_codegraph_explore(*, target_path: Path, query: str, timeout_seconds: int) -> str:
    for command in (["rtk", "codegraph", "explore", query], ["codegraph", "explore", query]):
        try:
            completed = subprocess.run(
                command,
                cwd=str(target_path),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
            )
            return completed.stdout.strip()
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                "target_repo_codegraph_failed",
                f"CodeGraph explore timed out after {timeout_seconds}s.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            raise HarnessError(
                "target_repo_codegraph_failed",
                f"CodeGraph explore failed: {_truncate(stderr, limit=500)}",
            ) from exc
    raise HarnessError(
        "target_repo_codegraph_failed",
        "CodeGraph command was not found. Install CodeGraph or run through the expected local toolchain.",
    )


def _run_codegraph_init(*, target_path: Path, timeout_seconds: int) -> None:
    for command in (["rtk", "codegraph", "init"], ["codegraph", "init"]):
        try:
            subprocess.run(
                command,
                cwd=str(target_path),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
            )
            return
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                "target_repo_codegraph_failed",
                f"CodeGraph init timed out after {timeout_seconds}s.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            raise HarnessError(
                "target_repo_codegraph_failed",
                f"CodeGraph init failed: {_truncate(stderr, limit=500)}",
            ) from exc
    raise HarnessError(
        "target_repo_codegraph_failed",
        "CodeGraph command was not found. Install CodeGraph or run through the expected local toolchain.",
    )


def _context_query(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    target_files: list[str],
    issue_key: str,
) -> str:
    terms: list[str] = [issue_key]
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if class_name:
            terms.append(class_name)
        for method in _dict_items(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if method_name:
                terms.append(method_name)
    for diagram in _dict_items(sequence_graph.get("sequenceDiagrams")):
        for message in _dict_items(diagram.get("messages")):
            text = str(message.get("message") or "").split("(", 1)[0].strip()
            if text:
                terms.append(text)
    for path in target_files[:8]:
        terms.append(Path(path).stem)
    unique = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.strip()
        if normalized and normalized.lower() not in seen:
            unique.append(normalized)
            seen.add(normalized.lower())
    return " ".join(unique[:24]) or "project structure entrypoints tests"


def _fake_repo_evidence(*, target_path: Path, target_files: list[str]) -> str:
    lines = [
        "**Fake CodeGraph Context**",
        "",
        "This fake context is used only for unit tests with SDLC_LLM_MODE=fake.",
        "",
        "Known source files:",
    ]
    for rel_path in target_files[:8]:
        lines.append(f"- {rel_path}")
        path = target_path / rel_path
        if path.exists() and path.is_file():
            lines.append("```")
            lines.append(_truncate(path.read_text(encoding="utf-8", errors="replace"), limit=1200))
            lines.append("```")
    return "\n".join(lines)


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _truncate(text: str, *, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n\n... truncated ..."
