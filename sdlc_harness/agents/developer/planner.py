"""Developer.plan cho Phase 3: biến design đã duyệt thành kế hoạch sửa code.

Module này chưa sửa target repo. Nó chỉ đọc requirement/design/trace graph và
tạo implementation plan có cấu trúc để Validator có thể kiểm tra trước khi
human duyệt sang Phase 4. Khi Validator hybrid phát hiện thiếu sót, module này
tạo version plan kế tiếp theo feedback nhưng vẫn không ghi vào target repo.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ..requirements import RequirementModelV2
from ...core.errors import HarnessError
from ...llm.client import strip_code_fence


@dataclass(frozen=True)
class ImplementationPlanDraft:
    """Kết quả Developer.plan gồm bản Markdown và JSON máy đọc được."""

    markdown: str
    structured: dict[str, object]


class DeveloperPlanAgent:
    """Vai Developer ở Phase 3: lập kế hoạch, chưa sinh code."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def _llm_plan(
        self,
        *,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        repo_context: dict[str, object],
        base_mappings: list[dict[str, object]],
        base_test_intents: list[dict[str, object]],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str,
        repair_feedback: str,
        previous_review: dict[str, object],
        canonical_requirement: RequirementModelV2 | None,
    ) -> dict[str, object]:
        """Nhờ Developer LLM đọc context repo và viết plan giàu ngữ cảnh hơn."""

        if not self.llm_client:
            return {}
        prompt = _developer_plan_prompt(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            repo_context=repo_context,
            base_mappings=base_mappings,
            base_test_intents=base_test_intents,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repair_feedback=repair_feedback,
            previous_review=previous_review,
            canonical_requirement=canonical_requirement,
        )
        raw = self.llm_client.complete(
            system=(
                "You are SDLC Harness Developer.plan. Return JSON only. "
                "Create a concrete implementation plan, but do not write code."
            ),
            prompt=prompt,
        )
        try:
            return _parse_developer_plan_payload(raw)
        except HarnessError as exc:
            return {"_developer_llm_parse_error": exc.message}

    def plan(
        self,
        *,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str,
        repo_context: dict[str, object] | None = None,
        repair_feedback: str = "",
        previous_review: dict[str, object] | None = None,
        workspace_mode: str = "",
        workspace_ref: str = "",
        base_sha: str = "",
        canonical_requirement: RequirementModelV2 | None = None,
        requirement_graph: dict[str, object] | None = None,
        requirement_artifact_refs: dict[str, str] | None = None,
    ) -> ImplementationPlanDraft:
        """Tạo plan V1 từ graph đã duyệt để checkpoint không còn prose mơ hồ."""

        repo_context = repo_context or {}
        mappings = _design_mappings(
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_files=target_files,
        )
        test_intents = _test_intents(mappings=mappings, test_command=test_command)
        llm_payload = self._llm_plan(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            repo_context=repo_context,
            base_mappings=mappings,
            base_test_intents=test_intents,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repair_feedback=repair_feedback,
            previous_review=previous_review or {},
            canonical_requirement=canonical_requirement,
        )
        mappings = _merge_mappings(mappings, _dict_items(llm_payload.get("design_mappings")))
        test_intents = _dict_items(llm_payload.get("test_intents")) or test_intents
        requirement_coverage = _requirement_coverage(
            requirement_graph=requirement_graph or {},
            requirement_design_trace=requirement_design_trace,
            mappings=mappings,
        ) if canonical_requirement is not None else []
        llm_parse_error = str(llm_payload.get("_developer_llm_parse_error") or "").strip()
        structured = {
            "version": "implementation_plan_structured_v1",
            "planning_mode": (
                "llm_developer_with_codegraph_context_v1"
                if self.llm_client and not llm_parse_error
                else "deterministic_graph_mapping_after_llm_parse_error_v1"
                if self.llm_client
                else "deterministic_graph_mapping_v1"
            ),
            "run_id": run_id,
            "source_ref": source_ref,
            "issue_key": issue_key,
            "target_repository": {
                "local_path": target_repo_path,
                "remote": target_repo_remote,
                "base_branch": target_base_branch,
                "planned_branch": planned_branch,
                "workspace_mode": workspace_mode,
                "workspace_ref": workspace_ref,
                "base_sha": base_sha,
            },
            "inputs": {
                "requirement_design_trace_version": requirement_design_trace.get("version", ""),
                "class_graph_version": class_graph.get("version", ""),
                "sequence_graph_version": sequence_graph.get("version", ""),
            },
            "requirement_contract": {
                "version": "v2" if canonical_requirement is not None else "v1",
                "revision": canonical_requirement.revision if canonical_requirement is not None else 0,
                "artifact_refs": requirement_artifact_refs or {},
            },
            "repo_context": _repo_context_summary(repo_context),
            "repo_evidence": _string_items(llm_payload.get("repo_evidence"))
            or _repo_context_evidence_lines(repo_context),
            "implementation_steps": _dict_items(llm_payload.get("implementation_steps"))
            or _implementation_steps_from_mappings(mappings),
            "proposed_files": _dict_items(llm_payload.get("proposed_files"))
            or _proposed_files_from_mappings(mappings),
            "design_mappings": mappings,
            "requirement_coverage": requirement_coverage,
            "test_intents": test_intents,
            "assumptions": [
                "Phase 3 tạo branch/worktree riêng cho run để lập kế hoạch; Phase 4 mới sửa code, chạy test và mở Draft PR.",
                "Các target file chưa tồn tại được hiểu là file Phase 4 có thể tạo mới trong run workspace.",
            ],
            "open_questions": [
                "Nếu human muốn đổi kiến trúc hoặc đổi package Java, cần request_change trước khi duyệt plan.",
            ],
            "developer_llm": {
                "used": bool(self.llm_client),
                "summary": str(llm_payload.get("summary") or "").strip(),
                "parse_error": llm_parse_error,
                "repair_feedback_attached": bool(repair_feedback.strip()),
            },
            "out_of_scope": [
                "Không sửa source/test hoặc tạo commit trong Phase 3.",
                "Không ghi file source/test trong target repo ở Phase 3.",
                "Không tạo Pull Request trong Phase 3.",
            ],
        }
        markdown = _markdown_plan(
            structured=structured,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
        )
        return ImplementationPlanDraft(markdown=markdown, structured=structured)

    def repair_plan(
        self,
        *,
        previous_draft: ImplementationPlanDraft,
        review_report: dict[str, object],
        developer_feedback: str,
        iteration: int,
        run_id: str,
        source_ref: str,
        issue_key: str,
        requirement_packet: str,
        design_artifact: str,
        requirement_design_trace: dict[str, object],
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        target_repo_path: str,
        target_repo_remote: str,
        target_base_branch: str,
        planned_branch: str,
        target_files: list[str],
        test_command: str,
        repo_context: dict[str, object] | None = None,
        workspace_mode: str = "",
        workspace_ref: str = "",
        base_sha: str = "",
        canonical_requirement: RequirementModelV2 | None = None,
        requirement_graph: dict[str, object] | None = None,
        requirement_artifact_refs: dict[str, str] | None = None,
    ) -> ImplementationPlanDraft:
        """Tạo lại plan dựa trên feedback của Validator.review_plan.

        V1 vẫn giữ plan deterministic từ graph để không sinh target ngoài phạm vi.
        Feedback của LLM được ghi vào artifact để Phase 4/human thấy lý do vòng
        repair và để sau này thay bằng Developer LLM prompt mà không đổi workflow.
        """

        repaired = self.plan(
            run_id=run_id,
            source_ref=source_ref,
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_repo_path=target_repo_path,
            target_repo_remote=target_repo_remote,
            target_base_branch=target_base_branch,
            planned_branch=planned_branch,
            target_files=target_files,
            test_command=test_command,
            repo_context=repo_context,
            repair_feedback=developer_feedback,
            previous_review=review_report,
            workspace_mode=workspace_mode,
            workspace_ref=workspace_ref,
            base_sha=base_sha,
            canonical_requirement=canonical_requirement,
            requirement_graph=requirement_graph,
            requirement_artifact_refs=requirement_artifact_refs,
        )
        structured = {
            **repaired.structured,
            "repair_iteration": iteration,
            "repair_source": "validator_review_plan",
            "repair_feedback": developer_feedback,
            "previous_plan_version": previous_draft.structured.get("artifact_ref", ""),
            "previous_review": review_report,
        }
        markdown = (
            repaired.markdown.rstrip()
            + "\n\n## Repair Feedback Addressed\n\n"
            + f"- Repair iteration: {iteration}\n"
            + f"- Validator feedback: {developer_feedback or 'No free-form feedback provided.'}\n"
        )
        return ImplementationPlanDraft(markdown=markdown, structured=structured)


def _developer_plan_prompt(
    *,
    run_id: str,
    source_ref: str,
    issue_key: str,
    requirement_packet: str,
    design_artifact: str,
    requirement_design_trace: dict[str, object],
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    repo_context: dict[str, object],
    base_mappings: list[dict[str, object]],
    base_test_intents: list[dict[str, object]],
    target_repo_path: str,
    target_repo_remote: str,
    target_base_branch: str,
    planned_branch: str,
    target_files: list[str],
    test_command: str,
    repair_feedback: str,
    previous_review: dict[str, object],
    canonical_requirement: RequirementModelV2 | None,
) -> str:
    canonical_section = (
        "Canonical Requirement V2 (source of truth; use stable IDs exactly):\n"
        + json.dumps(canonical_requirement.to_dict(), ensure_ascii=False, indent=2)
        if canonical_requirement is not None
        else "Canonical Requirement V2: not enabled for this V1 run."
    )
    return f"""Developer Implementation Plan v1

You are creating the Phase 3 implementation plan checkpoint for a TraceDev-style SDLC workflow.
Do not write source code. Do not create commits or branches. Your job is to explain exactly how
the approved requirement/design should be implemented in the target repository in Phase 4.

Return JSON only with this shape:
{{
  "summary": "short summary for human reviewer",
  "repo_evidence": ["specific repo evidence from CodeGraph/context, max 8 items"],
  "implementation_steps": [
    {{
      "order": 1,
      "title": "short step title",
      "description": "what Phase 4 should change",
      "files": ["src/main/java/..."],
      "design_refs": ["class:...", "method:...", "sequence:..."]
    }}
  ],
  "proposed_files": [
    {{
      "path": "src/main/java/...",
      "action": "create|update|verify",
      "reason": "why this file is needed",
      "related_design_elements": ["class:..."]
    }}
  ],
  "design_mappings": [
    {{
      "design_element_id": "class:...",
      "design_element_type": "class|method|sequence_message",
      "design_element_name": "...",
      "planned_code_target": "src/main/java/...#method",
      "reason": "concrete implementation reason",
      "test_intent": "how to verify this element"
    }}
  ],
  "test_intents": [
    {{"scope": "unit_or_service", "intent": "specific behavior to verify", "command": "{test_command}"}}
  ],
  "assumptions": [],
  "open_questions": []
}}

Rules:
- Keep the whole response compact and below 3000 output tokens.
- Do not repeat all base deterministic mappings. The harness already preserves
  them in Python. In `design_mappings`, return only mappings you improve or
  correct using repo evidence. Return [] when the base mappings are good enough.
- `implementation_steps` must be 3-6 high-level steps, not one step per design element.
- `proposed_files` must be max 12 files and grouped by file, not by method.
- `test_intents` must be max 6 focused tests.
- Do not add behavior outside the approved requirement/design.
- Proposed files must stay under src/main/java or src/test/java.
- Use CodeGraph evidence when naming existing classes/files.
- If this is a repair iteration, address the validator feedback directly.

Run context:
- run_id: {run_id}
- source_ref: {source_ref}
- issue_key: {issue_key}
- target_repo_path: {target_repo_path}
- target_repo_remote: {target_repo_remote}
- target_base_branch: {target_base_branch}
- planned_branch: {planned_branch}
- test_command: {test_command}

Target files:
{json.dumps(target_files, ensure_ascii=False, indent=2)}

{canonical_section}

Approved requirement packet:
{_excerpt(requirement_packet, limit=6000)}

Approved design artifact:
{_excerpt(design_artifact, limit=6000)}

Requirement-design trace:
{json.dumps(requirement_design_trace, ensure_ascii=False, indent=2)}

Class graph:
{json.dumps(class_graph, ensure_ascii=False, indent=2)}

Sequence graph:
{json.dumps(sequence_graph, ensure_ascii=False, indent=2)}

Base deterministic mappings:
{json.dumps(base_mappings, ensure_ascii=False, indent=2)}

Base test intents:
{json.dumps(base_test_intents, ensure_ascii=False, indent=2)}

CodeGraph repo context:
{json.dumps(repo_context, ensure_ascii=False, indent=2)}

Repair feedback:
{repair_feedback or "none"}

Previous review:
{json.dumps(previous_review, ensure_ascii=False, indent=2)}
""".strip()


def _parse_developer_plan_payload(raw: str) -> dict[str, object]:
    try:
        parsed = _parse_first_json_object(
            raw,
            expected_keys={
                "summary",
                "repo_evidence",
                "implementation_steps",
                "proposed_files",
                "design_mappings",
                "test_intents",
            },
        )
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan LLM did not return valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan LLM must return a JSON object.",
        )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "repo_evidence": _string_items(parsed.get("repo_evidence")),
        "implementation_steps": _dict_items(parsed.get("implementation_steps")),
        "proposed_files": _dict_items(parsed.get("proposed_files")),
        "design_mappings": _dict_items(parsed.get("design_mappings")),
        "test_intents": _dict_items(parsed.get("test_intents")),
        "assumptions": _string_items(parsed.get("assumptions")),
        "open_questions": _string_items(parsed.get("open_questions")),
    }


def _parse_first_json_object(raw: str, *, expected_keys: set[str]) -> dict[str, object]:
    """Parse JSON object top-level mong muốn, bỏ qua object con/prompt echo."""

    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _has_expected_keys(parsed, expected_keys):
        return parsed

    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and _has_expected_keys(candidate, expected_keys):
            return candidate
    raise json.JSONDecodeError("No matching top-level JSON object found", text, 0)


def _has_expected_keys(candidate: dict[str, object], expected_keys: set[str]) -> bool:
    return bool(expected_keys.intersection(candidate.keys()))


def _merge_mappings(base: list[dict[str, object]], llm_items: list[dict[str, object]]) -> list[dict[str, object]]:
    by_id = {str(item.get("design_element_id") or ""): dict(item) for item in base}
    for item in llm_items:
        element_id = str(item.get("design_element_id") or "")
        if not element_id:
            continue
        current = by_id.get(element_id, {})
        by_id[element_id] = {
            **current,
            **{key: value for key, value in item.items() if str(value).strip()},
        }
    ordered: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in base:
        element_id = str(item.get("design_element_id") or "")
        if element_id in by_id:
            ordered.append(by_id[element_id])
            seen.add(element_id)
    for element_id, item in by_id.items():
        if element_id not in seen:
            ordered.append(item)
    return ordered


def _repo_context_summary(repo_context: dict[str, object]) -> dict[str, object]:
    evidence = str(repo_context.get("evidence") or "")
    return {
        "mode": repo_context.get("mode", ""),
        "status": repo_context.get("status", ""),
        "query": repo_context.get("query", ""),
        "source_files": repo_context.get("source_files", []),
        "evidence_char_count": len(evidence),
    }


def _repo_context_evidence_lines(repo_context: dict[str, object]) -> list[str]:
    evidence = str(repo_context.get("evidence") or "").strip()
    if not evidence:
        return []
    lines = [line.strip() for line in evidence.splitlines() if line.strip()]
    return lines[:8]


def _implementation_steps_from_mappings(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    files = _proposed_files_from_mappings(mappings)
    return [
        {
            "order": 1,
            "title": "Map approved design to code targets",
            "description": "Implement the approved class and sequence behavior using the mapped source/test files.",
            "files": [str(item.get("path") or "") for item in files],
            "design_refs": [str(item.get("design_element_id") or "") for item in mappings],
        },
        {
            "order": 2,
            "title": "Run verification",
            "description": "Add or update tests for the mapped behavior, then run the configured target repo test command.",
            "files": [str(item.get("path") or "") for item in files if "src/test/" in str(item.get("path") or "")],
            "design_refs": [str(item.get("design_element_id") or "") for item in mappings],
        },
    ]


def _proposed_files_from_mappings(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in mappings:
        target = str(item.get("planned_code_target") or "").split("#", 1)[0]
        if not target:
            continue
        entry = result.setdefault(
            target,
            {
                "path": target,
                "action": "create_or_update",
                "reason": "Needed by approved design mapping.",
                "related_design_elements": [],
            },
        )
        refs = entry.setdefault("related_design_elements", [])
        if isinstance(refs, list):
            refs.append(str(item.get("design_element_id") or ""))
    return list(result.values())


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _requirement_coverage(
    *,
    requirement_graph: dict[str, object],
    requirement_design_trace: dict[str, object],
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Derive canonical requirement -> approved design -> code/test coverage in Python."""

    mapping_by_name: dict[str, list[dict[str, object]]] = {}
    for mapping in mappings:
        name = str(mapping.get("design_element_name") or "").strip()
        if name:
            mapping_by_name.setdefault(name, []).append(mapping)

    trace_targets: dict[str, list[str]] = {}
    for trace_key in ("class_trace", "sequence_trace"):
        for item in _dict_items(requirement_design_trace.get(trace_key)):
            requirement_id = str(item.get("requirement") or "").strip()
            if not requirement_id:
                continue
            targets = trace_targets.setdefault(requirement_id, [])
            for link in _dict_items(item.get("links")):
                target = str(link.get("target") or "").strip()
                if target and target not in targets:
                    targets.append(target)

    coverage: list[dict[str, object]] = []
    for requirement in _dict_items(requirement_graph.get("requirements")):
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        targets = trace_targets.get(requirement_id, [])
        resolved: list[dict[str, object]] = []
        for target in targets:
            candidates = _mappings_for_trace_target(target=target, mapping_by_name=mapping_by_name)
            for mapping in candidates:
                if mapping not in resolved:
                    resolved.append(mapping)
        coverage.append(
            {
                "requirement_id": requirement_id,
                "content": str(requirement.get("content") or "").strip(),
                "design_targets": targets,
                "design_element_ids": _unique_strings(
                    str(item.get("design_element_id") or "") for item in resolved
                ),
                "planned_code_targets": _unique_strings(
                    str(item.get("planned_code_target") or "") for item in resolved
                ),
                "test_intents": _unique_strings(str(item.get("test_intent") or "") for item in resolved),
            }
        )
    return coverage


def _mappings_for_trace_target(
    *,
    target: str,
    mapping_by_name: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    candidates = list(mapping_by_name.get(target, []))
    candidates.extend(
        mapping
        for name, named_mappings in mapping_by_name.items()
        if name.endswith(f": {target}")
        for mapping in named_mappings
    )
    # Class trace may point to an approved attribute. Phase 3 plans at class/file
    # granularity, so resolve `Class.attribute` through its owning class mapping.
    if "." in target:
        owner = target.split(".", 1)[0]
        candidates.extend(mapping_by_name.get(owner, []))
    return candidates


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _design_mappings(
    *,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    target_files: list[str],
) -> list[dict[str, object]]:
    mappings: list[dict[str, object]] = []
    class_targets = _class_target_files(class_graph=class_graph, target_files=target_files)

    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        class_file = class_targets.get(class_name) or _default_java_file(class_name)
        mappings.append(
            {
                "design_element_id": f"class:{class_name}",
                "design_element_type": "class",
                "design_element_name": class_name,
                "planned_code_target": class_file,
                "reason": f"Implement or update class `{class_name}` from the approved class diagram.",
                "test_intent": f"Cover behavior exposed through `{class_name}` in unit or flow tests.",
            }
        )
        for method in _dict_items(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if not method_name:
                continue
            mappings.append(
                {
                    "design_element_id": f"method:{class_name}.{method_name}",
                    "design_element_type": "method",
                    "design_element_name": f"{class_name}.{method_name}",
                    "planned_code_target": f"{class_file}#{method_name}",
                    "reason": f"Implement method `{class_name}.{method_name}` from the approved class diagram.",
                    "test_intent": f"Exercise `{method_name}` through the primary use-case flow or direct unit test.",
                }
            )

    for diagram in _dict_items(sequence_graph.get("sequenceDiagrams")):
        diagram_id = str(diagram.get("id") or "sequence_diagram").strip()
        for index, message in enumerate(_dict_items(diagram.get("messages")), start=1):
            source = str(message.get("from") or "").strip()
            target = str(message.get("to") or "").strip()
            text = str(message.get("message") or "").strip()
            if not text:
                continue
            code_target = _sequence_message_target(
                message_text=text,
                source=source,
                target=target,
                class_targets=class_targets,
            )
            mappings.append(
                {
                    "design_element_id": f"sequence:{diagram_id}:{index}",
                    "design_element_type": "sequence_message",
                    "design_element_name": f"{source}->{target}: {text}",
                    "planned_code_target": code_target,
                    "reason": "Preserve the approved sequence interaction in the implementation flow.",
                    "test_intent": f"Verify sequence step `{text}` through an integration or service-level test.",
                }
            )
    return mappings


def _test_intents(*, mappings: list[dict[str, object]], test_command: str) -> list[dict[str, str]]:
    methods = [
        str(item.get("design_element_name") or "")
        for item in mappings
        if item.get("design_element_type") == "method"
    ]
    primary_methods = ", ".join(methods[:5]) if methods else "primary use-case behavior"
    return [
        {
            "scope": "unit_or_service",
            "intent": f"Validate {primary_methods}.",
            "command": test_command,
        },
        {
            "scope": "regression",
            "intent": "Run the target repo test command after Phase 4 code changes.",
            "command": test_command,
        },
    ]


def _class_target_files(*, class_graph: dict[str, object], target_files: list[str]) -> dict[str, str]:
    existing = {Path(path).stem: path for path in target_files if path.endswith(".java")}
    result: dict[str, str] = {}
    for node in _dict_items(class_graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if class_name:
            result[class_name] = existing.get(class_name, _default_java_file(class_name))
    return result


def _default_java_file(class_name: str) -> str:
    return f"src/main/java/com/sdlc/etour/heritage/{class_name}.java"


def _sequence_message_target(
    *,
    message_text: str,
    source: str,
    target: str,
    class_targets: dict[str, str],
) -> str:
    method_name = message_text.split("(", 1)[0].strip().replace(" ", "")
    participant_class = _participant_to_class(target, class_targets) or _participant_to_class(source, class_targets)
    if participant_class:
        code_file = class_targets.get(participant_class, _default_java_file(participant_class))
        if method_name:
            return f"{code_file}#{method_name}"
        return code_file
    return "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"


def _participant_to_class(participant: str, class_targets: dict[str, str]) -> str:
    normalized = participant.lower()
    for class_name in class_targets:
        lowered = class_name.lower()
        if normalized == lowered or normalized in lowered or lowered.endswith(normalized):
            return class_name
    return ""


def _markdown_plan(
    *,
    structured: dict[str, object],
    requirement_packet: str,
    design_artifact: str,
) -> str:
    repo = _object_dict(structured.get("target_repository"))
    mappings = _dict_items(structured.get("design_mappings"))
    requirement_coverage = _dict_items(structured.get("requirement_coverage"))
    test_intents = _dict_items(structured.get("test_intents"))
    implementation_steps = _dict_items(structured.get("implementation_steps"))
    proposed_files = _dict_items(structured.get("proposed_files"))
    repo_evidence = [str(item) for item in structured.get("repo_evidence", []) if str(item).strip()]
    assumptions = [str(item) for item in structured.get("assumptions", []) if str(item).strip()]
    open_questions = [str(item) for item in structured.get("open_questions", []) if str(item).strip()]
    out_of_scope = [str(item) for item in structured.get("out_of_scope", []) if str(item).strip()]

    lines = [
        "# Implementation Plan V1",
        "",
        "## Run Context",
        "",
        f"- Run id: `{structured.get('run_id', '')}`",
        f"- Source ref: `{structured.get('source_ref', '')}`",
        f"- Issue key: `{structured.get('issue_key', '')}`",
        "",
        "## Target Repository",
        "",
        f"- Local path: `{repo.get('local_path', '')}`",
        f"- Remote: `{repo.get('remote', '')}`",
        f"- Base branch: `{repo.get('base_branch', '')}`",
        f"- Planned branch: `{repo.get('planned_branch', '')}`",
        f"- Workspace mode: `{repo.get('workspace_mode', '')}`",
        f"- Base SHA: `{repo.get('base_sha', '')}`",
        "",
        "## Repo Evidence",
        "",
    ]
    if repo_evidence:
        for item in repo_evidence[:12]:
            lines.append(f"- {item}")
    else:
        lines.append("- No CodeGraph evidence was attached.")

    lines.extend(
        [
            "",
            "## Implementation Steps",
            "",
        ]
    )
    for item in implementation_steps:
        files = ", ".join(f"`{path}`" for path in item.get("files", []) if str(path).strip())
        refs = ", ".join(f"`{ref}`" for ref in item.get("design_refs", []) if str(ref).strip())
        lines.append(
            f"{item.get('order', '-')}. **{item.get('title', 'Implementation step')}**: "
            f"{item.get('description', '')} Files: {files or 'n/a'}. Design refs: {refs or 'n/a'}."
        )

    lines.extend(
        [
            "",
            "## Proposed Files",
            "",
            "| File | Action | Reason | Design refs |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in proposed_files:
        refs = ", ".join(str(ref) for ref in item.get("related_design_elements", []) if str(ref).strip())
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(f"`{item.get('path') or ''}`"),
                    _table_cell(str(item.get("action") or "")),
                    _table_cell(str(item.get("reason") or "")),
                    _table_cell(refs),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
        "## Design To Code Mapping",
        "",
        "| Design element | Type | Planned code target | Reason | Test intent |",
        "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in mappings:
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(str(item.get("design_element_name") or "")),
                    _table_cell(str(item.get("design_element_type") or "")),
                    _table_cell(f"`{item.get('planned_code_target') or ''}`"),
                    _table_cell(str(item.get("reason") or "")),
                    _table_cell(str(item.get("test_intent") or "")),
                ]
            )
            + " |"
        )

    if requirement_coverage:
        lines.extend(
            [
                "",
                "## Requirement Coverage",
                "",
                "| Requirement | Content | Approved design targets | Planned code targets | Test intents |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in requirement_coverage:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _table_cell(str(item.get("requirement_id") or "")),
                        _table_cell(str(item.get("content") or "")),
                        _table_cell(", ".join(_string_items(item.get("design_targets")))),
                        _table_cell(", ".join(_string_items(item.get("planned_code_targets")))),
                        _table_cell("; ".join(_string_items(item.get("test_intents")))),
                    ]
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Test Intent",
            "",
        ]
    )
    for item in test_intents:
        lines.append(f"- {item.get('scope')}: {item.get('intent')} Run `{item.get('command')}`.")

    lines.extend(["", "## Assumptions / Open Questions", ""])
    for item in assumptions:
        lines.append(f"- Assumption: {item}")
    for item in open_questions:
        lines.append(f"- Open question: {item}")

    lines.extend(["", "## Out Of Scope", ""])
    for item in out_of_scope:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Approved Requirement Summary",
            "",
            _excerpt(requirement_packet),
            "",
            "## Approved Design Summary",
            "",
            _excerpt(design_artifact),
            "",
        ]
    )
    return "\n".join(lines)


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _object_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _excerpt(content: str, *, limit: int = 1400) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n... truncated for implementation planning ..."
