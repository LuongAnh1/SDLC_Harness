"""Read-only tool registry used by the L3 Designer loop."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ...core.errors import HarnessError
from ...repo_context import CodeGraphContextProvider
from .graph import apply_designer_delta


MAX_RESULTS = 20
MAX_READ_LINES = 200
MAX_FILE_CHARS = 8_000
MAX_OUTPUT_CHARS = 12_000


class DesignerToolRegistry:
    """Execute a closed set of read-only evidence tools plus validated graph delta."""

    def __init__(
        self,
        *,
        run_id: str,
        target_path: Path,
        landscape: dict[str, object],
        graph: dict[str, object],
        fake_mode: bool,
    ):
        self.run_id = run_id
        self.target_path = target_path.resolve()
        self.landscape = landscape
        self.graph = graph
        self.codegraph = CodeGraphContextProvider(fake_mode=fake_mode)
        roots = {
            str(root)
            for component in _dicts(landscape.get("components"))
            for root in _strings(component.get("roots"))
        }
        self.allowed_roots = tuple(sorted(roots))
        self.anchors = {
            str(item.get("anchor_id") or ""): item
            for item in _dicts(landscape.get("anchors"))
            if str(item.get("anchor_id") or "")
        }
        self.observed_evidence_refs = {
            ref
            for node in _dicts(graph.get("nodes"))
            for ref in _strings(_dict(node.get("runtime_binding")).get("evidence_refs"))
        }

    @property
    def valid_evidence_refs(self) -> set[str]:
        return {*self.anchors, *self.observed_evidence_refs}

    def execute(self, tool_id: str, arguments: object) -> tuple[dict[str, object], list[str]]:
        args = dict(arguments) if isinstance(arguments, dict) else {}
        if tool_id == "landscape.search":
            return self._record_evidence(self._landscape_search(args))
        if tool_id == "landscape.get_anchor":
            return self._record_evidence(self._landscape_get_anchor(args))
        if tool_id == "repo.search":
            return self._record_evidence(self._repo_search(args))
        if tool_id == "repo.read":
            return self._record_evidence(self._repo_read(args))
        if tool_id == "codegraph.query":
            return self._record_evidence(self._codegraph_query(args))
        if tool_id == "design.inspect_partition":
            return self._record_evidence(self._inspect_partition(args))
        if tool_id == "design.propose_delta":
            candidate, temp_refs = apply_designer_delta(
                self.graph,
                args.get("delta"),
                valid_evidence_refs=self.valid_evidence_refs,
            )
            self.graph = candidate
            return {
                "status": "applied",
                "revision": candidate["revision"],
                "artifact_digest": candidate["artifact_digest"],
                "temp_refs": temp_refs,
            }, []
        raise HarnessError("designer_tool_unknown", f"Unknown Designer tool: {tool_id}", run_id=self.run_id)

    def _record_evidence(
        self,
        result: tuple[dict[str, object], list[str]],
    ) -> tuple[dict[str, object], list[str]]:
        output, refs = result
        self.observed_evidence_refs.update(refs)
        return output, refs

    def component_for_path(self, relative: str) -> str:
        normalized = relative.strip().replace("\\", "/")
        matches = [
            str(component.get("component_id") or "")
            for component in _dicts(self.landscape.get("components"))
            if any(
                root == "." or normalized == root or normalized.startswith(f"{root}/")
                for root in _strings(component.get("roots"))
            )
        ]
        return matches[0] if len(set(matches)) == 1 else ""

    def register_evidence_refs(self, refs: list[str]) -> None:
        self.observed_evidence_refs.update(ref for ref in refs if ref)

    def _landscape_search(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        query = str(args.get("query") or "").strip().casefold()
        scenario_id = str(args.get("scenario_id") or "").strip()
        surface = str(args.get("surface") or "").strip()
        filters = {
            key: str(args.get(key) or "").strip()
            for key in ("component_id", "kind")
            if str(args.get(key) or "").strip()
        }
        candidate_refs: set[str] | None = None
        if scenario_id or surface:
            candidate_refs = {
                ref
                for item in _dicts(self.landscape.get("scenario_matches"))
                if (not scenario_id or str(item.get("scenario_id") or "") == scenario_id)
                and (not surface or str(item.get("surface") or "") == surface)
                for ref in _strings(item.get("candidate_anchor_refs"))
            }
        matches: list[dict[str, object]] = []
        for anchor_id, anchor in sorted(self.anchors.items()):
            if candidate_refs is not None and anchor_id not in candidate_refs:
                continue
            if any(str(anchor.get(key) or "") != value for key, value in filters.items()):
                continue
            haystack = " ".join(str(anchor.get(key) or "") for key in ("path", "symbol", "kind", "component_id")).casefold()
            if query and query not in haystack:
                continue
            matches.append(_public_anchor(anchor))
            if len(matches) >= MAX_RESULTS:
                break
        refs = [str(item["anchor_id"]) for item in matches]
        return {"matches": matches, "count": len(matches), "truncated": len(matches) == MAX_RESULTS}, refs

    def _landscape_get_anchor(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        anchor_id = str(args.get("anchor_id") or "").strip()
        anchor = self.anchors.get(anchor_id)
        if anchor is None:
            raise HarnessError("designer_evidence_not_found", f"Unknown landscape anchor: {anchor_id}", run_id=self.run_id)
        return {"anchor": _public_anchor(anchor), "snapshot": _dict(self.landscape.get("snapshot"))}, [anchor_id]

    def _repo_search(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 200:
            raise HarnessError("designer_tool_input_invalid", "repo.search query is empty or too long.", run_id=self.run_id)
        requested_component = str(args.get("component_id") or "").strip()
        files = sorted(
            {
                str(anchor.get("path") or "")
                for anchor in self.anchors.values()
                if str(anchor.get("path") or "")
                and (not requested_component or str(anchor.get("component_id") or "") == requested_component)
            }
        )
        matches: list[dict[str, object]] = []
        evidence: list[str] = []
        for relative in files:
            path = self._safe_file(relative)
            if not path.is_file() or path.stat().st_size > 512_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines, start=1):
                if query.casefold() not in line.casefold():
                    continue
                ref = _repo_ref(relative, path, line=index)
                matches.append({"path": relative, "line": index, "excerpt": line[:500], "evidence_ref": ref})
                evidence.append(ref)
                if len(matches) >= MAX_RESULTS:
                    return {"matches": matches, "count": len(matches), "truncated": True}, evidence
        return {"matches": matches, "count": len(matches), "truncated": False}, evidence

    def _repo_read(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        relative = str(args.get("path") or "").strip().replace("\\", "/")
        start = max(1, int(args.get("start_line") or 1))
        end = max(start, int(args.get("end_line") or start + 79))
        end = min(end, start + MAX_READ_LINES - 1)
        path = self._safe_file(relative)
        if not path.is_file():
            raise HarnessError("designer_evidence_not_found", f"Repository file does not exist: {relative}", run_id=self.run_id)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        excerpt = "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, min(end, len(lines)) + 1))
        excerpt = excerpt[:MAX_FILE_CHARS]
        ref = _repo_ref(relative, path, line=start)
        return {
            "path": relative,
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": excerpt,
            "file_sha256": _file_hash(path),
            "truncated": len(excerpt) >= MAX_FILE_CHARS or end < len(lines),
            "evidence_ref": ref,
        }, [ref]

    def _codegraph_query(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 1000:
            raise HarnessError("designer_tool_input_invalid", "codegraph.query query is empty or too long.", run_id=self.run_id)
        try:
            output = self.codegraph.query_readonly(self.target_path, query)
        except HarnessError as exc:
            if exc.error_code not in {"target_repo_codegraph_missing", "target_repo_codegraph_failed"}:
                raise
            return {
                "status": "unavailable",
                "query_digest": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "uncertainty": exc.error_code,
                "message": "Existing CodeGraph evidence is unavailable; use landscape/repo read tools instead.",
                "truncated": False,
            }, []
        sanitized = output.replace(str(self.target_path), "<workspace>").replace(str(self.target_path).replace("\\", "/"), "<workspace>")
        output_digest = "sha256:" + hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        ref = f"codegraph:{output_digest}"
        return {
            "status": "available",
            "query_digest": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "evidence": sanitized[:MAX_OUTPUT_CHARS],
            "truncated": len(sanitized) > MAX_OUTPUT_CHARS,
            "evidence_ref": ref,
        }, [ref]

    def _inspect_partition(self, args: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        scenario_id = str(args.get("scenario_id") or "").strip()
        component_id = str(args.get("component_id") or "").strip()
        node_ids = {
            str(item.get("id") or "")
            for item in _dicts(self.graph.get("nodes"))
            if str(item.get("scenario_id") or "") == scenario_id
            and (not component_id or str(item.get("component_id") or "") == component_id)
        }
        return {
            "graph_revision": self.graph.get("revision"),
            "graph_digest": self.graph.get("artifact_digest"),
            "partitions": [
                item
                for item in _dicts(self.graph.get("partitions"))
                if str(item.get("scenario_id") or "") == scenario_id
                and (not component_id or str(item.get("component_id") or "") == component_id)
            ],
            "nodes": [item for item in _dicts(self.graph.get("nodes")) if str(item.get("id") or "") in node_ids],
            "edges": [
                item
                for item in _dicts(self.graph.get("edges"))
                if str(item.get("from") or "") in node_ids or str(item.get("to") or "") in node_ids
            ],
        }, []

    def _safe_file(self, relative: str) -> Path:
        path = Path(relative)
        if not relative or "\\" in relative or path.is_absolute() or ".." in path.parts:
            raise HarnessError("designer_repo_path_unsafe", f"Unsafe repository path: {relative}", run_id=self.run_id)
        candidate = self.target_path
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise HarnessError(
                    "designer_repo_path_unsafe",
                    f"Symbolic links are not allowed in repository reads: {relative}",
                    run_id=self.run_id,
                )
        resolved = (self.target_path / path).resolve()
        if not _within(resolved, self.target_path):
            raise HarnessError("designer_repo_path_unsafe", f"Repository path escapes the workspace: {relative}", run_id=self.run_id)
        if self.allowed_roots and not any(_within(resolved, (self.target_path / root).resolve()) for root in self.allowed_roots):
            raise HarnessError("designer_repo_path_unsafe", f"Repository path is outside profile roots: {relative}", run_id=self.run_id)
        return resolved


def _public_anchor(anchor: dict[str, object]) -> dict[str, object]:
    return {
        key: anchor.get(key)
        for key in ("anchor_id", "kind", "component_id", "path", "symbol", "evidence_source")
    }


def _repo_ref(relative: str, path: Path, *, line: int) -> str:
    return f"repo:{relative}#L{line}@{_file_hash(path)}"


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
