"""QA/Tester gate cho Phase 4 sau khi Validator code trace đã pass.

QA không chỉ đọc rồi chấm cảm tính. Agent này nhận packet đã được workflow nén
lại, sinh acceptance/system tests trong phạm vi `src/test/java/`, để workflow
apply test vào target repo và chạy Verification Runner. Kết quả pass/fail của
QA được quyết định bởi test thực thi, còn vòng sửa ngược về Developer sẽ là
slice sau.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object

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
        }


class QaTesterAgent:
    """Vai QA/Tester: sinh acceptance tests, chưa tự repair code."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def generate_tests(self, *, context: dict[str, object]) -> QaGeneratedTestSuite:
        """Sinh acceptance/system tests bằng LLM hoặc deterministic fallback."""

        if not self.llm_client:
            generated = _deterministic_generated_tests(context)
            _require_v2_scenario_coverage(generated.scenarios, context=context)
            return generated
        system = (
            "You are QA/Tester in a TraceDev-style SDLC harness. "
            "Return JSON only. Generate executable tests from the approved context."
        )
        prompt = _qa_generation_prompt(context)
        for attempt in range(2):
            raw = self.llm_client.complete(system=system, prompt=prompt)
            try:
                return _generated_suite_from_raw(raw=raw, context=context)
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
                )
        raise AssertionError("QA generation retry loop must return or raise")


def _generated_suite_from_raw(*, raw: str, context: dict[str, object]) -> QaGeneratedTestSuite:
    """Parse and validate one complete QA response before any test file is applied."""

    payload = _parse_generated_tests_payload(raw)
    generated = QaGeneratedTestSuite(
        summary=str(payload.get("summary") or "").strip(),
        test_files=_dict_items(payload.get("test_files")),
        scenarios=_dict_items(payload.get("scenarios")),
        risks=_string_items(payload.get("risks")),
        raw_response=raw,
        generation_context=_dict_value(context.get("context_budget")),
        requirement_contract=_dict_value(context.get("requirement_contract")),
    )
    _require_v2_scenario_coverage(generated.scenarios, context=context)
    return generated


def _qa_generation_prompt(context: dict[str, object]) -> str:
    return f"""QA Tester Generate Acceptance Tests v1

Generate executable acceptance/system tests for the approved use case. The
packet intentionally follows a compact TraceDev-style tester input: accepted
requirement/use case, current code under test, Verification Runner report, and
Validator trace summary. Full design and implementation plan are references only
in this slice.

Return JSON only with this shape:
{{
  "summary": "short test-generation summary",
  "test_files": [
    {{
      "path": "src/test/java/.../SomeAcceptanceTest.java",
      "content": "complete Java test source code",
      "scenarios": ["scenario covered by this file"]
    }}
  ],
  "scenarios": [
    {{
      "name": "scenario name",
      "expected_result": "observable acceptance result",
      "test_file": "src/test/java/.../SomeAcceptanceTest.java",
      "risk": "short risk or empty string",
      "requirement_ids": ["PRE-1", "MF-1"]
    }}
  ],
  "risks": ["short risk item"]
}}

Rules:
- Generate at least one complete Java test file.
- Every test path must be under src/test/java/ and end with .java.
- Prefer the target repo's existing lightweight test style. If no framework is visible, write a plain Java class with a main method and throw IllegalStateException on failure.
- Do not modify production files.
- For Requirement V2, every scenario must list canonical stable requirement_ids and the scenario union must cover every canonical requirement exactly from the packet.
- Do not request requirement/design changes in this slice; report them as risks.
- Keep the JSON compact. Do not include markdown fences.

QA Generation Packet:
{json.dumps(context, ensure_ascii=False, indent=2)}
""".strip()


def _qa_generation_repair_prompt(
    *,
    context: dict[str, object],
    invalid_output: str,
    validation_error: str,
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

Previous response:
{invalid_output}
""".strip()


def _parse_generated_tests_payload(raw: str) -> dict[str, object]:
    try:
        parsed = parse_first_json_object(raw, expected_keys={"summary", "test_files", "scenarios"})
    except json.JSONDecodeError as exc:
        raise QaTesterLlmError(
            "QA/Tester LLM did not return a valid JSON object.",
            raw_response=raw,
        ) from exc
    test_files = _normalized_test_files(parsed.get("test_files"), raw_response=raw)
    scenarios = [
        {**item, "requirement_ids": _string_items(item.get("requirement_ids"))}
        for item in _dict_items(parsed.get("scenarios"))
    ]
    if not scenarios:
        raise QaTesterLlmError(
            "QA/Tester LLM returned no scenarios.",
            raw_response=raw,
        )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "test_files": test_files,
        "scenarios": scenarios,
        "risks": _string_items(parsed.get("risks")),
    }


def _normalized_test_files(value: object, *, raw_response: str) -> list[dict[str, object]]:
    files = _dict_items(value)
    if not files:
        raise QaTesterLlmError(
            "QA/Tester LLM returned no test files.",
            raw_response=raw_response,
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in files:
        path = _normalize_test_path(str(item.get("path") or ""), raw_response=raw_response)
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
        normalized.append(
            {
                "path": path,
                "content": content,
                "scenarios": _string_items(item.get("scenarios")),
            }
        )
    return normalized


def _normalize_test_path(raw_path: str, *, raw_response: str) -> str:
    path = raw_path.strip().replace("\\", "/").lstrip("/")
    if (
        not path
        or path.startswith("../")
        or "/../" in path
        or path == ".."
        or not path.startswith("src/test/java/")
        or not path.endswith(".java")
    ):
        raise QaTesterLlmError(
            f"QA/Tester LLM returned an unsafe or unsupported test path: {raw_path}",
            raw_response=raw_response,
        )
    return path


def _deterministic_generated_tests(context: dict[str, object]) -> QaGeneratedTestSuite:
    run_id = str(context.get("run_id") or "")
    issue_key = str(context.get("issue_key") or "")
    path = "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageQaAcceptanceTest.java"
    content = """package com.sdlc.etour.heritage;

public final class DeleteCulturalHeritageQaAcceptanceTest {
    public static void main(String[] args) {
        DeleteCulturalHeritageService service = new DeleteCulturalHeritageService();
        if (!service.deleteHeritage("qa-heritage-1")) {
            throw new IllegalStateException("Expected delete operation to succeed");
        }
        if (!service.isDeleted("qa-heritage-1")) {
            throw new IllegalStateException("Expected deleted heritage to be observable");
        }
    }
}
"""
    requirement_ids = [
        str(item.get("id") or "").strip()
        for item in _dict_items(context.get("canonical_requirements"))
        if str(item.get("id") or "").strip()
    ]
    return QaGeneratedTestSuite(
        summary=f"Fake QA generated acceptance tests for run {run_id} and {issue_key or 'source'}.",
        test_files=[
            {
                "path": path,
                "content": content.strip(),
                "scenarios": ["Delete cultural heritage success path"],
            }
        ],
        scenarios=[
            {
                "name": "Delete cultural heritage success path",
                "expected_result": "Selected heritage is marked deleted after confirmation.",
                "test_file": path,
                "risk": "",
                "requirement_ids": requirement_ids,
            }
        ],
        risks=[],
        generation_context=_dict_value(context.get("context_budget")),
        requirement_contract=_dict_value(context.get("requirement_contract")),
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
