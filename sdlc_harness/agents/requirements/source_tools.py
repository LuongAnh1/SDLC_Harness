"""Công cụ chỉ đọc nguồn bất biến cho Requirement Refiner tool-loop."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

from ...core.errors import HarnessError


MAX_READ_LINES = 200
MAX_READ_CHARS = 12_000
MAX_SEARCH_RESULTS = 10
MIN_SEARCH_QUERY_CHARS = 2
MAX_SEARCH_QUERY_CHARS = 200


class RequirementSourceCatalog:
    """Giữ allowlist source snapshot đã xác minh và thực thi list/search/read."""

    def __init__(
        self,
        *,
        run_id: str,
        sources: list[dict[str, object]],
        source_text_by_id: dict[str, str],
    ):
        self.run_id = run_id
        self._sources: list[dict[str, object]] = []
        self._texts: dict[str, str] = {}
        seen: set[str] = set()
        for raw in sources:
            source = deepcopy(raw)
            source_id = str(source.get("id") or "")
            text = source_text_by_id.get(source_id)
            required_strings = ("type", "source_ref", "locator", "artifact_ref", "fingerprint")
            invalid_metadata = any(
                not isinstance(source.get(key), str) or not str(source.get(key)).strip()
                for key in required_strings
            ) or not (source.get("revision") is None or isinstance(source.get("revision"), str))
            if (
                not source_id
                or source_id in seen
                or invalid_metadata
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise HarnessError(
                    "requirement_source_catalog_invalid",
                    f"Invalid or duplicate source catalog entry: {source_id!r}.",
                    run_id=run_id,
                )
            if any(len(line) > MAX_READ_CHARS for line in text.splitlines()):
                raise HarnessError(
                    "requirement_source_catalog_invalid",
                    f"Source contains a line larger than the read budget: {source_id}.",
                    run_id=run_id,
                )
            expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if source.get("fingerprint") != expected:
                raise HarnessError(
                    "source_snapshot_corrupted",
                    f"Source fingerprint does not match immutable snapshot: {source_id}.",
                    run_id=run_id,
                )
            seen.add(source_id)
            self._sources.append(source)
            self._texts[source_id] = text
        if not self._sources:
            raise HarnessError(
                "requirement_source_catalog_invalid",
                "Requirement source catalog must contain at least one source.",
                run_id=run_id,
            )

    @property
    def sources(self) -> list[dict[str, object]]:
        return deepcopy(self._sources)

    @property
    def source_text_by_id(self) -> dict[str, str]:
        return dict(self._texts)

    def list_sources(self) -> dict[str, object]:
        return {
            "ok": True,
            "sources": [
                {
                    "source_id": source["id"],
                    "type": source["type"],
                    "source_ref": source["source_ref"],
                    "locator": source["locator"],
                    "revision": source["revision"],
                    "fingerprint": source["fingerprint"],
                    "line_count": len(self._texts[str(source["id"])].splitlines()),
                }
                for source in self._sources
            ],
        }

    def search(
        self,
        *,
        query: object,
        source_ids: object = None,
        max_results: object = MAX_SEARCH_RESULTS,
    ) -> dict[str, object]:
        if not isinstance(query, str):
            return self._error("requirement_source_query_invalid", "query must be a string.")
        needle = query.strip()
        if not MIN_SEARCH_QUERY_CHARS <= len(needle) <= MAX_SEARCH_QUERY_CHARS:
            return self._error(
                "requirement_source_query_invalid",
                f"query must contain {MIN_SEARCH_QUERY_CHARS}-{MAX_SEARCH_QUERY_CHARS} characters.",
            )
        selected, error = self._selected_source_ids(source_ids)
        if error:
            return error
        try:
            requested_limit = int(max_results)
        except (TypeError, ValueError):
            return self._error("requirement_source_limit_invalid", "max_results must be an integer.")
        limit = max(1, min(MAX_SEARCH_RESULTS, requested_limit))
        folded = needle.casefold()
        matches: list[dict[str, object]] = []
        returned_chars = 0
        for source_id in selected:
            lines = self._texts[source_id].splitlines()
            source = self._source(source_id)
            for index, line in enumerate(lines):
                if folded not in line.casefold():
                    continue
                start_index = max(0, index - 1)
                end_index = min(len(lines), index + 2)
                snippet = "\n".join(lines[start_index:end_index])
                if len(snippet) > MAX_READ_CHARS:
                    start_index = index
                    end_index = index + 1
                    snippet = line
                added = len(snippet) + (1 if matches else 0)
                if matches and returned_chars + added > MAX_READ_CHARS:
                    return {"ok": True, "query": needle, "matches": matches, "truncated": True}
                matches.append(
                    {
                        "source_id": source_id,
                        "source_ref": source["source_ref"],
                        "fingerprint": source["fingerprint"],
                        "line_start": start_index + 1,
                        "line_end": end_index,
                        "text": snippet,
                    }
                )
                returned_chars += added
                if len(matches) >= limit:
                    return {"ok": True, "query": needle, "matches": matches, "truncated": True}
        return {"ok": True, "query": needle, "matches": matches, "truncated": False}

    def read(
        self,
        *,
        source_id: object,
        start_line: object = 1,
        line_count: object = MAX_READ_LINES,
    ) -> dict[str, object]:
        normalized_id = str(source_id or "").strip()
        if normalized_id not in self._texts:
            return self._error(
                "requirement_source_not_allowed",
                f"Source is not present in the run catalog: {normalized_id!r}.",
            )
        try:
            start = int(start_line)
            count = int(line_count)
        except (TypeError, ValueError):
            return self._error(
                "requirement_source_range_invalid",
                "start_line and line_count must be integers.",
            )
        if start < 1 or count < 1:
            return self._error(
                "requirement_source_range_invalid",
                "start_line and line_count must be positive.",
            )
        count = min(count, MAX_READ_LINES)
        lines = self._texts[normalized_id].splitlines()
        if start > max(1, len(lines)):
            return self._error(
                "requirement_source_range_invalid",
                f"start_line exceeds source length: {len(lines)}.",
            )
        selected: list[str] = []
        char_count = 0
        for line in lines[start - 1 : start - 1 + count]:
            added = len(line) + (1 if selected else 0)
            if selected and char_count + added > MAX_READ_CHARS:
                break
            if not selected and added > MAX_READ_CHARS:
                selected.append(line[:MAX_READ_CHARS])
                char_count = MAX_READ_CHARS
                break
            selected.append(line)
            char_count += added
        end_line = start + len(selected) - 1
        source = self._source(normalized_id)
        has_more = end_line < len(lines)
        return {
            "ok": True,
            "source_id": normalized_id,
            "source_ref": source["source_ref"],
            "fingerprint": source["fingerprint"],
            "line_start": start,
            "line_end": end_line,
            "text": "\n".join(selected),
            "next_start_line": end_line + 1 if has_more else None,
            "end_of_source": not has_more,
            "truncated_by_limit": has_more,
        }

    def _selected_source_ids(self, value: object) -> tuple[list[str], dict[str, object] | None]:
        if value is None:
            return [str(source["id"]) for source in self._sources], None
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return [], self._error(
                "requirement_source_ids_invalid",
                "source_ids must be a list of source IDs.",
            )
        selected = list(dict.fromkeys(value))
        unknown = [source_id for source_id in selected if source_id not in self._texts]
        if unknown:
            return [], self._error(
                "requirement_source_not_allowed",
                "Sources are not present in the run catalog: " + ", ".join(unknown) + ".",
            )
        return selected, None

    def _source(self, source_id: str) -> dict[str, object]:
        return next(source for source in self._sources if source["id"] == source_id)

    @staticmethod
    def _error(code: str, message: str) -> dict[str, object]:
        return {"ok": False, "error_code": code, "message": message}


def observation_text_contains_quote(observation: dict[str, object], quote: object) -> bool:
    """Kiểm quote trên text tool đã trả, dùng cùng chuẩn whitespace với validator."""

    normalized_quote = " ".join(str(quote or "").split())
    normalized_text = " ".join(str(observation.get("text") or "").split())
    return bool(normalized_quote) and normalized_quote in normalized_text

