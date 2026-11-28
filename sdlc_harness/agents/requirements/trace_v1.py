"""Requirement trace V1: the light traceability model of H-OC Phase 1 and its mechanical checks.

Design reads the Markdown requirements; this model keeps only what traceability needs. Use-case clauses (`UC-n`,
verbatim quotes) are the root of the graph, acceptance criteria (`AC-n`) cover clauses, implied obligations (`IMP-n`)
serve clauses and cite repository files. The same checks run on the Markdown frame and on the converted JSON.
"""

from __future__ import annotations

from collections.abc import Callable
import re
import unicodedata

TRACE_SCHEMA_VERSION = "requirement_trace_v1"
COVERAGE_THRESHOLD = 0.9

_ID = re.compile(r"\b((?:UC|AC|IMP|Q)-\d+)\b")
_DEFINITION = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+|\|[ \t]*)?(?:\*\*|__)?`?((?:UC|AC|IMP|Q)-\d+)\b(.*)$",
    re.MULTILINE,
)
_QUOTED = re.compile(r'["“]([^"“”]+)["”]')
_NO_BEHAVIOR = "(no observable behavior"
# A use-case template field label at the start of a line ("Description:", "Entry conditions:", "- Author:") is
# structure, not content, so the sentence after it is what a clause must quote.
_FIELD_LABEL = re.compile(r"^(?:[-*][ \t]+)?(?:\S+[ \t]+){0,2}\S+:[ \t]+(?=\S)")


def defined_ids(markdown: str) -> set[str]:
    """IDs defined at the start of a line (list item, numbered item, heading or first table cell)."""

    return {match.group(1) for match in _DEFINITION.finditer(markdown)}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def use_case_segments(source_text: str) -> list[str]:
    """Sentences of the use case that must be quoted: headings (ending with ':') and fragments under 3 words skipped."""

    segments: list[str] = []
    for line in source_text.splitlines():
        for index, piece in enumerate(re.split(r"(?<=[.!?;])\s+", line.strip())):
            piece = _normalize(piece)
            if index == 0:
                piece = _FIELD_LABEL.sub("", piece)
            if piece.endswith(":") or len(piece.split()) < 3:
                continue
            segments.append(piece)
    return segments


def coverage_issues(source_text: str, quotes: list[str]) -> list[str]:
    """Every use-case sentence must have most of its letters and digits inside some clause quote."""

    normalized = _normalize(source_text)
    covered = [False] * len(normalized)
    for quote in quotes:
        needle = _normalize(quote)
        if not needle:
            continue
        start = normalized.find(needle)
        while start != -1:
            for index in range(start, start + len(needle)):
                covered[index] = True
            start = normalized.find(needle, start + 1)
    issues = []
    cursor = 0
    for segment in use_case_segments(source_text):
        position = normalized.find(segment, cursor)
        if position == -1:
            position = normalized.find(segment)
        if position == -1:
            continue
        cursor = position + len(segment)
        letters = [index for index in range(position, position + len(segment)) if normalized[index].isalnum()]
        if letters and sum(covered[index] for index in letters) / len(letters) < COVERAGE_THRESHOLD:
            issues.append(f'No UC clause quotes this use-case sentence: "{segment}"')
    return issues


def parse_markdown_frame(markdown: str) -> dict[str, dict[str, dict[str, object]]]:
    """Read the few fields the layout fixes: clause quotes, AC Covers, IMP Serves and Repo. Nothing else is parsed."""

    frame: dict[str, dict[str, dict[str, object]]] = {"uc": {}, "ac": {}, "imp": {}, "q": {}}
    for ident, (rest, block) in definition_blocks(markdown).items():
        kind = ident.split("-")[0]
        if kind == "UC":
            text = rest.strip().lstrip("*_` ").strip()
            observable = _NO_BEHAVIOR not in text.lower()
            quote_part = re.split(r"\(no observable behavior", text, flags=re.IGNORECASE)[0]
            frame["uc"][ident] = {
                "quotes": _QUOTED.findall(quote_part), "observable": observable, "reason": None if observable else text,
            }
        elif kind == "AC":
            covers = block_field(block, "Covers")
            frame["ac"][ident] = {
                "covers": sorted(set(_ID.findall(covers or ""))) if covers is not None else None,
                "api": block_field(block, "API"),
                "web": block_field(block, "Web"),
            }
        elif kind == "IMP":
            serves = re.search(r"Serves:\s*([^.\n]*)", block)
            repo = re.search(r"Repo:\s*(.*)", block)
            frame["imp"][ident] = {
                "serves": sorted(set(_ID.findall(serves.group(1)))) if serves else None,
                "repo": re.findall(r"`([^`]+)`", repo.group(1)) if repo else None,
            }
        else:
            frame["q"][ident] = {}
    return frame


def definition_blocks(markdown: str) -> dict[str, tuple[str, str]]:
    """Each defined ID with the rest of its defining line and its block (up to the next definition)."""

    blocks: dict[str, tuple[str, str]] = {}
    matches = list(_DEFINITION.finditer(markdown))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks[match.group(1)] = (match.group(2), markdown[match.start():end])
    return blocks


def block_field(block: str, name: str) -> str | None:
    match = re.search(rf"^[ \t]*[-*+]?[ \t]*\**{name}\**[ \t]*:[ \t]*(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def frame_from_trace(trace: dict[str, object]) -> dict[str, dict[str, dict[str, object]]]:
    return {
        "uc": {
            str(item.get("id")): {
                "quotes": item.get("quotes") if isinstance(item.get("quotes"), list) else [item.get("quote")],
                "observable": item.get("observable") is not False,
                "reason": item.get("reason"),
            }
            for item in _items(trace.get("uc_clauses"))
        },
        "ac": {
            str(item.get("id")): {"covers": item.get("covers"), "api": item.get("api"), "web": item.get("web")}
            for item in _items(trace.get("acceptance_criteria"))
        },
        "imp": {
            str(item.get("id")): {"serves": item.get("serves"), "repo": item.get("repo_evidence")}
            for item in _items(trace.get("implied_obligations"))
        },
        "q": {},
    }


def frame_issues(
    frame: dict[str, dict[str, dict[str, object]]],
    source_text: str,
    repo_file_exists: Callable[[str], bool],
) -> list[str]:
    """Mechanical checks of a frame (from Markdown or JSON): quotes, sentence coverage, covers, serves, repo files."""

    issues: list[str] = []
    clauses, criteria, obligations = frame["uc"], frame["ac"], frame["imp"]
    if not clauses:
        issues.append("No use-case clause UC-n is defined.")
    if not criteria:
        issues.append("No acceptance criterion AC-n is defined.")
    normalized_source = _normalize(source_text)
    quotes = []
    for ident, clause in sorted(clauses.items()):
        clause_quotes = [quote for quote in clause.get("quotes") or [] if isinstance(quote, str) and quote.strip()]
        if not clause_quotes:
            issues.append(f'{ident}: write its verbatim quote in double quotes right after the ID.')
        for quote in clause_quotes:
            if _normalize(quote) not in normalized_source:
                issues.append(f'{ident}: the quote "{quote}" is not verbatim from the use case.')
            else:
                quotes.append(quote)
        if clause.get("observable") is False and not str(clause.get("reason") or "").strip():
            issues.append(f"{ident}: a clause without observable behavior needs a reason.")
    issues += coverage_issues(source_text, quotes)
    covered: set[str] = set()
    for ident, criterion in sorted(criteria.items()):
        covers = criterion.get("covers")
        if not isinstance(covers, list) or not covers:
            issues.append(f"{ident}: add a 'Covers:' line naming the UC clauses it covers.")
            continue
        unknown = [ref for ref in covers if ref not in clauses]
        if unknown:
            issues.append(f"{ident}: covers clauses that do not exist: {unknown}.")
        covered.update(covers)
    for ident, clause in sorted(clauses.items()):
        if clause.get("observable") is not False and ident not in covered:
            issues.append(f"{ident}: no acceptance criterion covers it; cover it or mark it '(no observable behavior: <reason>)'.")
    for ident, obligation in sorted(obligations.items()):
        serves = obligation.get("serves")
        if not isinstance(serves, list) or not serves or any(ref not in clauses for ref in serves):
            issues.append(f"{ident}: 'Serves:' must name existing UC clauses.")
        repo = obligation.get("repo")
        if not isinstance(repo, list) or not repo:
            issues.append(f"{ident}: 'Repo:' must cite the repository files that show it, in backticks.")
        else:
            missing = [path for path in repo if not repo_file_exists(re.split(r"[:#\s]", str(path), maxsplit=1)[0])]
            if missing:
                issues.append(f"{ident}: these repository files do not exist: {missing}.")
    return issues


def _items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
