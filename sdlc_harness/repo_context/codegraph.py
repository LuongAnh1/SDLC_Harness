"""CodeGraph context cho target repo mà Phase 3 sẽ lập kế hoạch sửa.

Phase 3 chưa ghi code, nhưng Developer cần hiểu snapshot worktree thật thay vì
chỉ đoán file từ tên class trong design graph. Module này khởi tạo `.codegraph/`
ngay trong worktree của run, đọc context qua CodeGraph, rồi trả evidence ngắn để
Developer LLM lập implementation plan.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError

MAX_EVIDENCE_CHARS = 12000
DEFAULT_TIMEOUT_SECONDS = 45
CODEGRAPH_SNAPSHOT_VERSION = "repo_codegraph_snapshot_v1"
_STRUCTURED_OPERATIONS = frozenset(
    {"symbols", "callers", "callees", "impact", "affected_tests", "files"}
)
_CAPABILITY_OPERATIONS = _STRUCTURED_OPERATIONS | {"node", "explore"}


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
        vertical_design_graph: dict[str, object] | None = None,
    ) -> RepoContext:
        """Thu thập context liên quan tới design đã duyệt."""

        self.require_index(target_path)
        query = _context_query(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
            issue_key=issue_key,
            vertical_design_graph=vertical_design_graph or {},
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

    def query_readonly(self, target_path: Path, query: str) -> str:
        """Query một index đã tồn tại; không tự tạo generated state cho L2."""

        self.require_index(target_path)
        if self.fake_mode:
            return _fake_repo_evidence(target_path=target_path, target_files=[])
        return _run_codegraph_explore(
            target_path=target_path,
            query=query,
            timeout_seconds=self.timeout_seconds,
        )

    def query_structured(
        self,
        target_path: Path,
        *,
        operation: str,
        query: str = "",
        symbol: str = "",
        kind: str = "",
        limit: int = 10,
        depth: int = 2,
        file_filter: str = "",
        pattern: str = "",
        files: list[str] | None = None,
    ) -> dict[str, object]:
        """Run one allowlisted CodeGraph JSON query against an existing index."""

        self.require_index(target_path)
        normalized = operation.strip().lower()
        if normalized not in _STRUCTURED_OPERATIONS:
            raise HarnessError("target_repo_codegraph_failed", f"Unsupported CodeGraph operation: {operation}")
        if self.fake_mode:
            return {
                "operation": normalized,
                "results": [],
                "fake": True,
            }
        bounded_limit = min(20, max(1, int(limit)))
        bounded_depth = min(5, max(1, int(depth)))
        if normalized == "symbols":
            command = ["query", query, "--limit", str(bounded_limit), "--json"]
            if kind:
                command.extend(["--kind", kind])
        elif normalized in {"callers", "callees"}:
            command = [normalized, symbol, "--limit", str(bounded_limit), "--json"]
        elif normalized == "impact":
            command = ["impact", symbol, "--depth", str(bounded_depth), "--json"]
        elif normalized == "affected_tests":
            bounded_files = [str(item).strip().replace("\\", "/") for item in (files or [])]
            bounded_files = [item for item in bounded_files if item][:20]
            if not bounded_files:
                raise HarnessError("target_repo_codegraph_failed", "Affected-tests query requires files.")
            command = ["affected", *bounded_files, "--depth", str(bounded_depth), "--json"]
        else:
            command = ["files", "--format", "flat", "--json"]
            if file_filter:
                command.extend(["--filter", file_filter])
            if pattern:
                command.extend(["--pattern", pattern])
        return _run_codegraph_json(
            target_path=target_path,
            command=command,
            timeout_seconds=self.timeout_seconds,
        )

    def query_capability(
        self,
        target_path: Path,
        *,
        operation: str,
        query: str = "",
        symbol: str = "",
        file: str = "",
        files: list[str] | None = None,
        kind: str = "",
        limit: int = 10,
        depth: int = 2,
        file_filter: str = "",
        pattern: str = "",
    ) -> dict[str, object]:
        """Run one bounded CodeGraph capability and normalize text/JSON output."""

        normalized = operation.strip().lower()
        requested_operation = normalized
        if normalized not in _CAPABILITY_OPERATIONS:
            raise HarnessError("target_repo_codegraph_failed", f"Unsupported CodeGraph operation: {operation}")
        self.require_index(target_path)
        file_symbol_map = normalized == "symbols" and bool(file) and not query
        if file_symbol_map:
            normalized = "node"
        if normalized in _STRUCTURED_OPERATIONS:
            return self.query_structured(
                target_path,
                operation=normalized,
                query=query,
                symbol=symbol,
                kind=kind,
                limit=limit,
                depth=depth,
                file_filter=file_filter,
                pattern=pattern,
                files=files,
            )
        if self.fake_mode:
            result = {
                "operation": requested_operation,
                "evidence": "fake CodeGraph evidence",
                "fake": True,
            }
            if requested_operation != normalized:
                result["resolved_operation"] = normalized
            return result
        if normalized == "explore":
            evidence = _run_codegraph_explore(
                target_path=target_path,
                query=query,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            bounded_limit = min(400, max(1, int(limit)))
            command = ["node"]
            if file:
                command.extend(["--file", file])
                if file_symbol_map:
                    command.append("--symbols-only")
                command.extend(["--limit", str(bounded_limit)])
            elif symbol:
                command.extend([symbol])
            else:
                raise HarnessError("target_repo_codegraph_failed", "Node query requires symbol or file.")
            evidence = _run_codegraph_command(
                target_path=target_path,
                arguments=command,
                timeout_seconds=self.timeout_seconds,
            )
        result = {
            "operation": requested_operation,
            "evidence": _truncate(evidence, limit=MAX_EVIDENCE_CHARS),
            "truncated": len(evidence) > MAX_EVIDENCE_CHARS,
        }
        if requested_operation != normalized:
            result["resolved_operation"] = normalized
        return result

    def sync_if_stale(self, target_path: Path) -> dict[str, object]:
        """Incrementally sync pending worktree changes and return a fresh snapshot."""

        self.require_index(target_path)
        if self.fake_mode:
            return {"synced": False, "snapshot": self.snapshot_readonly(target_path)}
        status = _run_codegraph_status(target_path=target_path, timeout_seconds=self.timeout_seconds)
        synced = _pending_change_count(status) > 0
        if synced:
            _run_codegraph_sync(target_path=target_path, timeout_seconds=self.timeout_seconds)
            status = _run_codegraph_status(target_path=target_path, timeout_seconds=self.timeout_seconds)
        return {"synced": synced, "snapshot": _validated_snapshot(status)}

    def snapshot_readonly(self, target_path: Path) -> dict[str, object]:
        """Return a stable freshness snapshot without mutating the index."""

        self.require_index(target_path)
        if self.fake_mode:
            return _with_snapshot_digest(
                {
                    "schema_version": CODEGRAPH_SNAPSHOT_VERSION,
                    "status": "fresh",
                    "version": "fake",
                    "file_count": 0,
                    "node_count": 0,
                    "edge_count": 0,
                    "languages": [],
                    "extraction_version": 0,
                }
            )
        return _validated_snapshot(
            _run_codegraph_status(target_path=target_path, timeout_seconds=self.timeout_seconds)
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
            if not initialized:
                _run_codegraph_sync(target_path=target_path, timeout_seconds=self.timeout_seconds)
            status = _run_codegraph_status(target_path=target_path, timeout_seconds=self.timeout_seconds)
            snapshot = _validated_snapshot(status)
            smoke = _run_codegraph_explore(
                target_path=target_path,
                query="project structure entrypoints tests",
                timeout_seconds=self.timeout_seconds,
            )
        elif after:
            snapshot = _with_snapshot_digest({
                "schema_version": CODEGRAPH_SNAPSHOT_VERSION,
                "status": "fresh",
                "version": "fake",
                "file_count": 0,
                "node_count": 0,
                "edge_count": 0,
                "languages": [],
                "extraction_version": 0,
            })
        else:
            snapshot = {}
        return {
            "codegraph_exists_before": before,
            "codegraph_exists_after": after,
            "codegraph_initialized": initialized,
            "smoke_ok": bool(after),
            "smoke_excerpt": _truncate(smoke or "", limit=2000),
            "snapshot": snapshot,
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


def _run_codegraph_sync(*, target_path: Path, timeout_seconds: int) -> None:
    _run_codegraph_command(
        target_path=target_path,
        arguments=["sync", ".", "--quiet"],
        timeout_seconds=timeout_seconds,
    )


def _run_codegraph_status(*, target_path: Path, timeout_seconds: int) -> dict[str, object]:
    return _run_codegraph_json(
        target_path=target_path,
        command=["status", ".", "--json"],
        timeout_seconds=timeout_seconds,
    )


def _run_codegraph_json(
    *,
    target_path: Path,
    command: list[str],
    timeout_seconds: int,
) -> dict[str, object]:
    raw = _run_codegraph_command(
        target_path=target_path,
        arguments=command,
        timeout_seconds=timeout_seconds,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError("target_repo_codegraph_failed", "CodeGraph returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise HarnessError("target_repo_codegraph_failed", "CodeGraph returned an invalid JSON object.")
    return payload


def _run_codegraph_command(
    *,
    target_path: Path,
    arguments: list[str],
    timeout_seconds: int,
) -> str:
    for prefix in (["rtk", "codegraph"], ["codegraph"]):
        try:
            completed = subprocess.run(
                [*prefix, *arguments],
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
                f"CodeGraph command timed out after {timeout_seconds}s.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise HarnessError(
                "target_repo_codegraph_failed",
                f"CodeGraph command failed: {_truncate(detail, limit=500)}",
            ) from exc
    raise HarnessError(
        "target_repo_codegraph_failed",
        "CodeGraph command was not found. Install CodeGraph or run through the expected local toolchain.",
    )


def _validated_snapshot(status: dict[str, object]) -> dict[str, object]:
    index = status.get("index") if isinstance(status.get("index"), dict) else {}
    pending_total = _pending_change_count(status)
    stale_reasons: list[str] = []
    if not bool(status.get("initialized")):
        stale_reasons.append("not_initialized")
    if str(index.get("state") or "") != "complete":
        stale_reasons.append("index_incomplete")
    if bool(index.get("reindexRecommended")):
        stale_reasons.append("reindex_recommended")
    mismatch = status.get("worktreeMismatch")
    if mismatch is not None and mismatch is not False and mismatch != "":
        stale_reasons.append("worktree_mismatch")
    if pending_total:
        stale_reasons.append("pending_changes")
    if stale_reasons:
        raise HarnessError(
            "target_repo_codegraph_stale",
            "CodeGraph index is not fresh after sync: " + ", ".join(stale_reasons),
        )
    return _with_snapshot_digest({
        "schema_version": CODEGRAPH_SNAPSHOT_VERSION,
        "status": "fresh",
        "version": str(status.get("version") or ""),
        "file_count": int(status.get("fileCount") or 0),
        "node_count": int(status.get("nodeCount") or 0),
        "edge_count": int(status.get("edgeCount") or 0),
        "languages": sorted(str(item) for item in status.get("languages", []) if str(item)),
        "extraction_version": int(index.get("currentExtractionVersion") or 0),
    })


def _pending_change_count(status: dict[str, object]) -> int:
    pending = status.get("pendingChanges") if isinstance(status.get("pendingChanges"), dict) else {}
    return sum(int(pending.get(key) or 0) for key in ("added", "modified", "removed"))


def _with_snapshot_digest(value: dict[str, object]) -> dict[str, object]:
    payload = {key: item for key, item in value.items() if key != "artifact_digest"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "artifact_digest": "sha256:" + hashlib.sha256(encoded).hexdigest()}


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
    vertical_design_graph: dict[str, object] | None = None,
) -> str:
    terms: list[str] = [issue_key]
    for node in _dict_items((vertical_design_graph or {}).get("nodes")):
        binding = node.get("runtime_binding")
        if not isinstance(binding, dict):
            continue
        for endpoint in (binding.get("target"), binding.get("attachment")):
            if not isinstance(endpoint, dict):
                continue
            path = str(endpoint.get("path") or endpoint.get("owner_path") or "").strip()
            symbol = str(endpoint.get("symbol") or endpoint.get("owner_symbol") or "").strip()
            if path:
                terms.append(Path(path).stem)
            if symbol:
                terms.append(symbol)
    for edge in _dict_items((vertical_design_graph or {}).get("edges")):
        contract = edge.get("boundary_contract")
        if not isinstance(contract, dict):
            continue
        for key in ("consumer_operation", "provider_operation"):
            operation = str(contract.get(key) or "").strip()
            if operation:
                terms.append(operation)
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
