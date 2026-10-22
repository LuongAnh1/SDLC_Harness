"""QA/Tester gate cho Phase 4 sau khi Validator code trace đã pass.

Agent nhận packet đã được workflow nén lại, sinh acceptance/system tests theo
QA policy bất biến của run, để workflow apply test vào target repo và giao cho
Verification Runner thực thi. Run mới dùng TypeScript/Vitest và không đưa command
thực thi vào prompt hay output của LLM; run Java lịch sử không đi qua lớp này.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from ...profiles import QaTestPolicy

QA_STATUSES = {"pass", "fail", "skipped"}


class QaTesterLlmError(HarnessError):
    """Lỗi QA LLM có giữ raw response để workflow ghi artifact debug."""

    def __init__(self, message: str, *, raw_response: str = ""):
        super().__init__("qa_tester_llm_failed", message)
        self.raw_response = raw_response


@dataclass(frozen=True)
class QaGeneratedTestSuite:
    """Bộ acceptance/system test do QA sinh ra trước khi workflow thực thi."""

    summary: str
    test_files: list[dict[str, object]]
    scenarios: list[dict[str, object]]
    risks: list[str]
    raw_response: str = ""
    generation_context: dict[str, object] | None = None
    requirement_contract: dict[str, object] | None = None
    generation_profile: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "qa_generated_tests_v1",
            "mode": "qa_generated_acceptance_tests_v1",
            "summary": self.summary,
            "test_files": self.test_files,
            "scenarios": self.scenarios,
            "risks": self.risks,
            "generation_context": self.generation_context or {},
            "requirement_contract": self.requirement_contract or {"version": "v1", "revision": 0},
            "generation_profile": self.generation_profile or {},
        }


@dataclass(frozen=True)
class QaTestResult:
    """Kết quả QA/Tester đã được chuẩn hóa cho workflow và quality report."""

    status: str
    summary: str
    scenarios: list[dict[str, object]]
    evidence: list[str]
    risks: list[str]
    raw_response: str = ""
    review_context: dict[str, object] | None = None
    requirement_contract: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, object]:
        contract = self.requirement_contract or {"version": "v1", "revision": 0}
        mode = "qa_generated_acceptance_tests_v2" if contract.get("version") == "v2" else "qa_generated_acceptance_tests_v1"
        return {
            "version": "qa_test_report_v1",
            "mode": mode,
            "requirement_contract": contract,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "scenarios": self.scenarios,
            "evidence": self.evidence,
            "risks": self.risks,
            "review_context": self.review_context or {},
        }

    def to_quality_report(self) -> dict[str, object]:
        requirement_ids = sorted(
            {
                requirement_id
                for scenario in self.scenarios
                for requirement_id in _string_items(scenario.get("requirement_ids"))
            }
        )
        contract = self.requirement_contract or {"version": "v1", "revision": 0}
        mode = "qa_generated_acceptance_tests_v2" if contract.get("version") == "v2" else "qa_generated_acceptance_tests_v1"
        review_context = self.review_context or {}
        return {
            "ok": self.ok,
            "status": self.status,
            "mode": mode,
            "requirement_contract_version": contract.get("version", "v2"),
            "requirement_revision": contract.get("revision", 0),
            "summary": self.summary,
            "scenario_count": len(self.scenarios),
            "risk_count": len(self.risks),
            "requirement_ids": requirement_ids,
            "requirements_covered": len(requirement_ids),
            "selected_test_files": list(review_context.get("selected_test_files") or []),
            "verified_test_files": list(review_context.get("verified_test_files") or []),
        }


class QaTesterAgent:
    """Vai QA/Tester: sinh acceptance tests, chưa tự repair code."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def generate_tests(
        self,
        *,
        context: dict[str, object],
        qa_policy: QaTestPolicy | None = None,
    ) -> QaGeneratedTestSuite:
        """Sinh acceptance/system tests bằng LLM hoặc deterministic fallback."""

        if qa_policy is None:
            raise HarnessError(
                "qa_test_policy_invalid",
                "QA generation requires the immutable run QA policy.",
            )
        if not self.llm_client:
            generated = _deterministic_generated_tests(context, qa_policy=qa_policy)
            _require_v2_scenario_coverage(generated.scenarios, context=context)
            return generated
        system = (
            "You are QA/Tester in a TraceDev-style SDLC harness. "
            "Return JSON only. Generate executable tests from the approved context."
        )
        prompt = _profiled_qa_generation_prompt(context=context, qa_policy=qa_policy)
        for attempt in range(2):
            raw = self.llm_client.complete(system=system, prompt=prompt)
            try:
                return _generated_suite_from_raw(raw=raw, context=context, qa_policy=qa_policy)
            except HarnessError as exc:
                recoverable = exc.error_code in {
                    "qa_tester_llm_failed",
                    "qa_requirement_coverage_invalid",
                }
                if not recoverable or attempt == 1:
                    if not str(getattr(exc, "raw_response", "")):
                        exc.raw_response = raw
                    raise
                prompt = _qa_generation_repair_prompt(
                    context=context,
                    invalid_output=raw,
                    validation_error=exc.message,
                    qa_policy=qa_policy,
                )
        raise AssertionError("QA generation retry loop must return or raise")


def _generated_suite_from_raw(
    *,
    raw: str,
    context: dict[str, object],
    qa_policy: QaTestPolicy,
) -> QaGeneratedTestSuite:
    """Parse and validate one complete QA response before any test file is applied."""

    payload = _parse_generated_tests_payload(raw, qa_policy=qa_policy)
    generated = QaGeneratedTestSuite(
        summary=str(payload.get("summary") or "").strip(),
        test_files=_dict_items(payload.get("test_files")),
        scenarios=_dict_items(payload.get("scenarios")),
        risks=_string_items(payload.get("risks")),
        raw_response=raw,
        generation_context=_dict_value(context.get("context_budget")),
        requirement_contract=_dict_value(context.get("requirement_contract")),
        generation_profile=qa_policy.metadata(),
    )
    _require_v2_scenario_coverage(generated.scenarios, context=context)
    return generated


def _profiled_qa_generation_prompt(
    *,
    context: dict[str, object],
    qa_policy: QaTestPolicy,
) -> str:
    return f"""QA Tester Generate Profiled Acceptance Tests v1

Generate executable Vitest tests for the approved use case and current code.
Return JSON only with this shape:
{{
  "summary": "short test-generation summary",
  "test_files": [
    {{
      "path": "apps/api/src/example/SomeAcceptance.test.ts",
      "content": "complete TypeScript Vitest source code",
      "scenarios": ["scenario covered by this file"]
    }}
  ],
  "scenarios": [
    {{
      "name": "scenario name",
      "expected_result": "observable acceptance result",
      "test_file": "same exact path as one generated test file",
      "risk": "short risk or empty string",
      "requirement_ids": ["REQ-1"]
    }}
  ],
  "risks": ["short risk item"]
}}

Rules:
- Generate at least one complete TypeScript Vitest test using imports from `vitest`.
- Use only a test root and exact suffix listed in QA Test Policy below.
- Keep tests co-located with the component code under test. Do not create Playwright/E2E specs.
- Do not modify production files, package manifests, configs, lockfiles, or scripts.
- Use repo-relative imports and the current code shown in the packet; do not invent APIs.
- Every scenario.test_file must exactly reference one generated test_files.path.
- For Requirement V2, every scenario must list canonical stable requirement_ids and their union must cover the packet's complete canonical set without invented IDs.
- Do not include markdown fences or executable command fields.

QA Test Policy:
{json.dumps(qa_policy.prompt_context(), ensure_ascii=False, indent=2)}

QA Generation Packet:
{json.dumps(context, ensure_ascii=False, indent=2)}
""".strip()


def _profiled_repair_rule(qa_policy: QaTestPolicy) -> str:
    return (
        "\nKeep every test file inside the supplied Vitest QA Test Policy, use TypeScript "
        "Vitest source, and do not return commands or Playwright specs."
    )


def _qa_generation_repair_prompt(
    *,
    context: dict[str, object],
    invalid_output: str,
    validation_error: str,
    qa_policy: QaTestPolicy,
) -> str:
    canonical_ids = [
        str(item.get("id") or "").strip()
        for item in _dict_items(context.get("canonical_requirements"))
        if str(item.get("id") or "").strip()
    ]
    return f"""QA Tester Repair Invalid Acceptance Test JSON

The previous response failed validation:
{validation_error}

Return one corrected JSON object only, using the same schema as the original
request. Preserve complete executable test file content when it is valid.
Every scenario must contain a non-empty `requirement_ids` array. Across all
scenarios, use every canonical ID exactly from this list and invent no IDs:
{json.dumps(canonical_ids, ensure_ascii=False)}
{_profiled_repair_rule(qa_policy)}

Previous response:
{invalid_output}
""".strip()


def _parse_generated_tests_payload(
    raw: str,
    *,
    qa_policy: QaTestPolicy,
) -> dict[str, object]:
    try:
        parsed = parse_first_json_object(raw, expected_keys={"summary", "test_files", "scenarios"})
    except json.JSONDecodeError as exc:
        raise QaTesterLlmError(
            "QA/Tester LLM did not return a valid JSON object.",
            raw_response=raw,
        ) from exc
    test_files = _normalized_test_files(
        parsed.get("test_files"),
        raw_response=raw,
        qa_policy=qa_policy,
    )
    scenarios = [
        {**item, "requirement_ids": _string_items(item.get("requirement_ids"))}
        for item in _dict_items(parsed.get("scenarios"))
    ]
    if not scenarios:
        raise QaTesterLlmError(
            "QA/Tester LLM returned no scenarios.",
            raw_response=raw,
        )
    generated_paths = {str(item.get("path") or "") for item in test_files}
    for scenario in scenarios:
        test_file = str(scenario.get("test_file") or "").strip()
        if not test_file or test_file not in generated_paths:
            raise QaTesterLlmError(
                "Every profiled QA scenario must reference one exact generated test file.",
                raw_response=raw,
            )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "test_files": test_files,
        "scenarios": scenarios,
        "risks": _string_items(parsed.get("risks")),
    }


def _normalized_test_files(
    value: object,
    *,
    raw_response: str,
    qa_policy: QaTestPolicy,
) -> list[dict[str, object]]:
    files = _dict_items(value)
    if not files:
        raise QaTesterLlmError(
            "QA/Tester LLM returned no test files.",
            raw_response=raw_response,
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in files:
        path = _normalize_test_path(
            str(item.get("path") or ""),
            raw_response=raw_response,
            qa_policy=qa_policy,
        )
        if path in seen:
            raise QaTesterLlmError(
                f"QA/Tester LLM returned duplicate test file path: {path}",
                raw_response=raw_response,
            )
        seen.add(path)
        content = str(item.get("content") or "").strip()
        if not content:
            raise QaTesterLlmError(
                f"QA/Tester LLM returned empty content for test file: {path}",
                raw_response=raw_response,
            )
        if not re.search(
            r"\bfrom\s+['\"]vitest['\"]",
            content,
        ):
            raise QaTesterLlmError(
                f"Profiled QA test must import its API from vitest: {path}",
                raw_response=raw_response,
            )
        normalized.append(
            {
                "path": path,
                "content": content,
                "scenarios": _string_items(item.get("scenarios")),
            }
        )
    return normalized


def _normalize_test_path(
    raw_path: str,
    *,
    raw_response: str,
    qa_policy: QaTestPolicy,
) -> str:
    path = qa_policy.normalize_test_path(raw_path)
    if not path:
        raise QaTesterLlmError(
            f"QA/Tester LLM returned a path outside the profiled Vitest policy: {raw_path}",
            raw_response=raw_response,
        )
    return path


def _deterministic_generated_tests(
    context: dict[str, object],
    *,
    qa_policy: QaTestPolicy,
) -> QaGeneratedTestSuite:
    return _deterministic_profiled_tests(context=context, qa_policy=qa_policy)


def _deterministic_profiled_tests(
    *,
    context: dict[str, object],
    qa_policy: QaTestPolicy,
) -> QaGeneratedTestSuite:
    run_id = str(context.get("run_id") or "")
    issue_key = str(context.get("issue_key") or "")
    source_path = ""
    code_under_test = _dict_value(context.get("code_under_test"))
    for item in _dict_items(code_under_test.get("files")):
        candidate = qa_policy.planning_policy.normalize_mapping_path(item.get("path"))
        if candidate:
            source_path = candidate
            break
    path = qa_policy.planning_policy.test_path_for_source(
        source_path,
        "GeneratedQaAcceptance",
    )
    if not path:
        default_rule = next(
            (rule for rule in qa_policy.component_rules if rule.component_id == "api"),
            qa_policy.component_rules[0],
        )
        path = f"{default_rule.test_root}/GeneratedQaAcceptance{default_rule.test_suffix}"
    requirement_ids = [
        str(item.get("id") or "").strip()
        for item in _dict_items(context.get("canonical_requirements"))
        if str(item.get("id") or "").strip()
    ]
    encoded_ids = json.dumps(requirement_ids, ensure_ascii=False)
    content = f'''import {{ describe, expect, it }} from "vitest";

describe("approved requirement acceptance", () => {{
  it("retains the canonical requirement evidence for this generated test", () => {{
    const requirementIds = {encoded_ids};
    expect(requirementIds).toEqual({encoded_ids});
  }});
}});
'''
    scenario_name = "Execute the generated Vitest acceptance check"
    return QaGeneratedTestSuite(
        summary=f"Fake QA generated a profiled Vitest test for run {run_id} and {issue_key or 'source'}.",
        test_files=[
            {
                "path": path,
                "content": content.strip(),
                "scenarios": [scenario_name],
            }
        ],
        scenarios=[
            {
                "name": scenario_name,
                "expected_result": "Vitest executes the generated file successfully.",
                "test_file": path,
                "risk": "",
                "requirement_ids": requirement_ids,
            }
        ],
        risks=[],
        generation_context=_dict_value(context.get("context_budget")),
        requirement_contract=_dict_value(context.get("requirement_contract")),
        generation_profile=qa_policy.metadata(),
    )


def _require_v2_scenario_coverage(
    scenarios: list[dict[str, object]],
    *,
    context: dict[str, object],
) -> None:
    contract = _dict_value(context.get("requirement_contract"))
    if contract.get("version") != "v2":
        return
    expected = {
        str(item.get("id") or "").strip()
        for item in _dict_items(context.get("canonical_requirements"))
        if str(item.get("id") or "").strip()
    }
    actual: set[str] = set()
    for item in scenarios:
        scenario_ids = _string_items(item.get("requirement_ids"))
        if not scenario_ids:
            raise HarnessError(
                "qa_requirement_coverage_invalid",
                "Every Requirement V2 QA scenario must reference at least one canonical stable ID.",
            )
        actual.update(scenario_ids)
    if not expected or actual != expected:
        missing = sorted(expected - actual)
        invented = sorted(actual - expected)
        raise HarnessError(
            "qa_requirement_coverage_invalid",
            f"QA scenarios must cover the canonical stable IDs exactly; missing={missing}, invented={invented}.",
        )


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}
