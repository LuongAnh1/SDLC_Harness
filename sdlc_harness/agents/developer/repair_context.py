"""Build bounded repair targets and read-only context for Developer execution."""

from __future__ import annotations

from pathlib import Path
import re


_SYMBOL = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DIAGNOSTIC_SYMBOL = re.compile(
    r"(?im)\bsymbol\s*:\s*(?:method|class|interface|variable)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
_EXCERPT_PER_PATH = 3200
_EXCERPT_RADIUS = 8


def build_repair_closure(
    *,
    work_packet: dict[str, object],
    feedback: dict[str, object] | None,
    target_path: Path | None,
) -> dict[str, object]:
    """Separate files that need repair from related files supplied as context."""

    allowed = _allowed_paths(work_packet)
    if not feedback:
        return {
            "direct_paths": allowed,
            "direct_repair_paths": allowed,
            "context_paths": allowed,
            "contract_anchor_paths": [],
            "path_reasons": {path: ["approved_implementation"] for path in allowed},
            "symbols": [],
            "excerpts": [],
        }

    current_evidence = _current_repair_evidence(feedback)
    contract_evidence = _contract_evidence(feedback)
    combined_evidence = {
        "current": current_evidence,
        "contract": contract_evidence,
    }
    current_text = "\n".join(_walk_strings(current_evidence)).replace("\\", "/").replace("./", "")
    combined_text = "\n".join(_walk_strings(combined_evidence)).replace("\\", "/").replace("./", "")
    current_folded = current_text.casefold()
    combined_folded = combined_text.casefold()

    direct = {path for path in allowed if path.casefold() in current_folded}
    anchors = _code_ref_paths(contract_evidence, allowed=allowed)
    reasons: dict[str, set[str]] = {path: {"direct_finding"} for path in direct}
    for path in anchors:
        reasons.setdefault(path, set()).add("contract_anchor")

    symbols = _feedback_symbols(combined_evidence, allowed=allowed, folded_text=combined_folded)
    for path in allowed:
        if Path(path).stem in symbols:
            reasons.setdefault(path, set()).add("compiler_symbol")

    if target_path is not None:
        for path in allowed:
            source = target_path / path
            if not source.is_file() or source.suffix.casefold() != ".java":
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            matched = [symbol for symbol in symbols if _contains_symbol(text, symbol)]
            if matched:
                reasons.setdefault(path, set()).add("symbol_reference")

    if not direct:
        direct.update(_requirement_fallback_paths(work_packet, current_evidence, allowed))
        for path in direct:
            reasons.setdefault(path, set()).add("implementation_mapping")

    selected = set(reasons) | direct | anchors
    if not selected:
        selected.update(allowed)
        for path in selected:
            reasons.setdefault(path, set()).add("allowed_scope_fallback")

    direct_ordered = [path for path in allowed if path in direct]
    context_ordered = [path for path in allowed if path in selected]
    anchor_ordered = [path for path in allowed if path in anchors]
    excerpts = _repair_excerpts(target_path=target_path, paths=context_ordered, symbols=symbols)
    return {
        "direct_paths": direct_ordered,
        "direct_repair_paths": direct_ordered,
        "context_paths": context_ordered,
        "contract_anchor_paths": anchor_ordered,
        "path_reasons": {path: sorted(reasons.get(path, {"dependency_context"})) for path in context_ordered},
        "symbols": symbols,
        "excerpts": excerpts,
    }


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    output = _dict_value(work_packet.get("output_contract"))
    return list(
        dict.fromkeys(
            str(value).strip().replace("\\", "/")
            for value in _string_items(output.get("allowed_paths"))
            if str(value).strip()
        )
    )


def _current_repair_evidence(feedback: dict[str, object]) -> dict[str, object]:
    """Evidence for the failure being repaired now; excludes persistent objective text."""

    return {
        key: feedback.get(key)
        for key in (
            "feedback_source",
            "path",
            "paths",
            "message",
            "summary",
            "missing_links",
            "verification",
            "failing_scenarios",
            "evidence",
            "risks",
            "applied_tests",
        )
        if feedback.get(key) not in (None, "", [], {})
    }


def _contract_evidence(feedback: dict[str, object]) -> dict[str, object]:
    objective = _dict_value(feedback.get("repair_objective"))
    return {
        key: objective.get(key)
        for key in ("objective_id", "source", "canonical_keys", "contract_findings", "must_preserve")
        if objective.get(key) not in (None, "", [], {})
    }


def _code_ref_paths(value: object, *, allowed: list[str]) -> set[str]:
    allowed_folded = {path.casefold(): path for path in allowed}
    result: set[str] = set()
    for item in _walk_dicts(value):
        code_ref = str(item.get("code_ref") or "").replace("\\", "/")
        code_path = code_ref.split("#", 1)[0].strip().lstrip("./")
        canonical = allowed_folded.get(code_path.casefold())
        if canonical:
            result.add(canonical)
    return result


def _feedback_symbols(
    feedback: dict[str, object],
    *,
    allowed: list[str],
    folded_text: str,
) -> list[str]:
    symbols: set[str] = set()
    for item in _walk_dicts(feedback):
        code_ref = str(item.get("code_ref") or "").replace("\\", "/")
        code_path = code_ref.split("#", 1)[0]
        if code_path:
            stem = Path(code_path).stem
            if _valid_symbol(stem):
                symbols.add(stem)
        if "#" in code_ref:
            candidate = code_ref.split("#", 1)[1].split("(", 1)[0].strip()
            if _valid_symbol(candidate):
                symbols.add(candidate)
        expected = _dict_value(item.get("expected_signature"))
        candidate = str(expected.get("name") or "").strip()
        if _valid_symbol(candidate):
            symbols.add(candidate)
        design_id = str(item.get("design_element_id") or "")
        if design_id.startswith("method:") and "." in design_id:
            candidate = design_id.rsplit(".", 1)[-1].split("(", 1)[0].strip()
            if _valid_symbol(candidate):
                symbols.add(candidate)

    symbols.update(match.group(1) for match in _DIAGNOSTIC_SYMBOL.finditer("\n".join(_walk_strings(feedback))))
    for path in allowed:
        stem = Path(path).stem
        if stem and _compiler_symbol_referenced(folded_text, stem.casefold()):
            symbols.add(stem)
    return sorted(symbols, key=lambda value: (value.casefold(), value))


def _repair_excerpts(*, target_path: Path | None, paths: list[str], symbols: list[str]) -> list[dict[str, object]]:
    if target_path is None:
        return []
    excerpts: list[dict[str, object]] = []
    for path in paths:
        source = target_path / path
        if not source.is_file():
            continue
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [index for index, line in enumerate(lines) if any(_contains_symbol(line, symbol) for symbol in symbols)]
        if not hits:
            continue
        indexes: set[int] = set()
        for hit in hits[:8]:
            indexes.update(range(max(0, hit - _EXCERPT_RADIUS), min(len(lines), hit + _EXCERPT_RADIUS + 1)))
        content = "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(indexes))
        excerpts.append({"path": path, "content": content[:_EXCERPT_PER_PATH]})
    return excerpts


def _requirement_fallback_paths(
    work_packet: dict[str, object],
    feedback: dict[str, object],
    allowed: list[str],
) -> set[str]:
    requirement_ids = {
        str(item.get("requirement_id") or item.get("requirement") or item.get("requirement_ref") or "")
        for item in _walk_dicts(feedback)
    } - {""}
    coverage = _dict_value(work_packet.get("requirement_coverage_by_file"))
    return {
        str(path).replace("\\", "/")
        for path, rows in coverage.items()
        if str(path).replace("\\", "/") in allowed
        and any(str(row.get("requirement_id") or "") in requirement_ids for row in _dict_items(rows))
    }


def _contains_symbol(text: str, symbol: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])", text))


def _valid_symbol(value: str) -> bool:
    return len(value) >= 3 and bool(_SYMBOL.fullmatch(value))


def _compiler_symbol_referenced(text_folded: str, symbol_folded: str) -> bool:
    symbol = re.escape(symbol_folded)
    patterns = (
        rf"\b(?:class|interface|enum|record|extends|implements|final)\s+{symbol}\b",
        rf"\bsymbol\s*:\s*(?:(?:class|interface|enum|record)\s+)?{symbol}\b",
        rf"\bin\s+{symbol}\b",
    )
    return any(re.search(pattern, text_folded) for pattern in patterns)


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
