"""Mechanical output checks for the semantic UI Polish Validator."""

from __future__ import annotations

import copy
import json

import pytest

from sdlc_harness.agents.developer.closure_planner import PROMPT_CHARS_PER_TOKEN, call_structured_json
from sdlc_harness.agents.validator.browser_review import (
    BrowserQaValidator,
    browser_qa_review_prompt,
    journey_execution_review_prompt,
    parse_browser_qa_review,
    parse_journey_execution_review,
    parse_polish_review,
    polish_result_review_prompt,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.workflows.phase4_browser import (
    _baseline_capture_regressions,
    _findings_with_evidence,
    _journey_patch_context,
    _manifest_text_evidence,
    _selected_evidence,
    _share_identical_values,
)


CONTRACT = {
    "polish_units": [
        {"unit_id": "UP-ONE", "key": "one"},
        {"unit_id": "UP-TWO", "key": "two"},
    ]
}
EVIDENCE = {"baseline:V-0:desktop:s00", "baseline:V-0:mobile:s00"}
AUTHORITY = {"primary", "VDN-UI"}


def test_browser_review_evidence_keeps_network_outcomes() -> None:
    record = {
        "view_id": "BV-one",
        "viewport_id": "desktop",
        "step": 3,
        "status": "passed",
        "network": [
            {
                "kind": "request_failed",
                "method": "DELETE",
                "path": "/records/demo",
                "cross_origin": True,
                "error": "net::ERR_FAILED",
            }
        ],
        "network_truncated": False,
        "screenshot": {"file": "step-03.png"},
    }
    text_evidence = _manifest_text_evidence({"records": [record]}, variant="baseline")
    selected = _selected_evidence(
        manifest={"records": [record]},
        images=[{"label": "baseline:BV-one:desktop:s03", "path": "captures/step-03.png"}],
    )

    assert text_evidence[0]["network"] == record["network"]
    assert text_evidence[0]["network_truncated"] is False
    assert selected[0]["network"] == record["network"]
    assert selected[0]["network_truncated"] is False


MISSED = {"status": "missed", "error": "TimeoutError: locator.waitFor", "network_held": True}
OBSERVE_VIEW = {
    "view_id": "BV-one",
    "key": "pending",
    "steps": [
        {"action": "goto", "path": "/"},
        {"action": "click", "role": "button", "name": "Go", "observe": {"role": "status", "text": "Working"}},
        {"action": "wait_for", "role": "status", "text": "Done"},
    ],
}
OBSERVE_MANIFEST = {
    "records": [
        {"view_id": "BV-one", "viewport_id": "desktop", "step": 0, "status": "passed", "aria_snapshot": "- main"},
        {
            "view_id": "BV-one",
            "viewport_id": "desktop",
            "step": 1,
            "status": "passed",
            "observe": MISSED,
            "aria_snapshot": "- status: Done",
            "focus": {"role": "button", "name": "Go"},
        },
        {"view_id": "BV-one", "viewport_id": "desktop", "step": 2, "status": "passed", "aria_snapshot": "- status: Done"},
    ]
}


def test_missed_observe_reaches_the_planner_as_its_own_kind_of_step_failure() -> None:
    refs = ["baseline:BV-one:desktop:s00", "baseline:BV-one:desktop:s01"]
    failures, gaps = _journey_patch_context(
        findings=[{"view_ids": ["BV-one"], "obligation_ids": ["O-1"], "evidence_refs": refs, "issue": "unseen"}],
        evidence_records=_manifest_text_evidence(OBSERVE_MANIFEST, variant="baseline"),
        views=[OBSERVE_VIEW],
    )

    assert [(item["step_index"], item["kind"], item["error"]) for item in failures] == [
        (1, "observe_missed", MISSED["error"])
    ]
    assert failures[0]["step"] == OBSERVE_VIEW["steps"][1]
    assert gaps[0]["evidence_refs"] == refs


def test_repair_finding_carries_the_captures_it_cites() -> None:
    refs = ["baseline:BV-one:desktop:s01", "baseline:BV-one:desktop:s02", "baseline:BV-gone:desktop:s00"]
    finding = {"unit_ids": ["WU-WEB"], "evidence_refs": refs, "issue": "Pending state is never rendered."}

    enriched = _findings_with_evidence(
        [finding, {"issue": "suite failure without browser evidence"}],
        manifest=OBSERVE_MANIFEST,
        views=[OBSERVE_VIEW],
        variant="baseline",
    )

    first, same = enriched[0]["evidence"]
    assert first["steps"] == OBSERVE_VIEW["steps"][:2]
    assert (first["observe"], first["focus"]) == (MISSED, {"role": "button", "name": "Go"})
    assert (first["aria_snapshot"], first["aria_truncated"]) == ("- status: Done", False)
    # An identical snapshot is sent once; an unknown ref is skipped; a finding without evidence is left alone.
    assert same == {
        "evidence_ref": refs[1],
        "steps": OBSERVE_VIEW["steps"],
        "status": "passed",
        "aria_snapshot_same_as": refs[0],
    }
    assert enriched[1] == {"issue": "suite failure without browser evidence"}
    assert _findings_with_evidence([finding], manifest=None, views=[], variant="baseline") == [finding]


def test_a_transient_state_missed_after_polish_is_a_capture_regression() -> None:
    capture = {"view_id": "BV-one", "viewport_id": "desktop", "step": 1}
    before = {"summary": {"step_failed": [], "observe_missed": []}}

    assert _baseline_capture_regressions(before, {"summary": {"step_failed": [], "observe_missed": [capture]}}) == [
        {"view_id": "BV-one", "viewport_id": "desktop"}
    ]
    assert _baseline_capture_regressions({"summary": {"observe_missed": [capture]}}, {"summary": {"observe_missed": [capture]}}) == []


def test_polish_result_evidence_sends_each_heavy_value_once() -> None:
    styles = [{"target": "body", "props": {"color": "rgb(0, 0, 0)"}}]
    manifest = copy.deepcopy(OBSERVE_MANIFEST)
    for record in manifest["records"]:
        record["styles"] = styles
    before = _manifest_text_evidence(manifest, variant="before")
    after = _manifest_text_evidence(manifest, variant="after")
    after[0]["aria_snapshot"] = "- main:\n  - heading \"Polished\""
    after[1]["styles"] = [{"target": "body", "props": {"color": "rgb(248, 250, 252)"}}]
    after[1]["aria_snapshot"] = None

    shared_before, shared_after = _share_identical_values(before, after)

    assert [item.get("aria_snapshot") for item in shared_before] == ["- main", "- status: Done", None]
    assert shared_before[0]["styles"] == styles and "same_as" not in shared_before[0]
    assert shared_before[1]["same_as"] == {"styles": "before:BV-one:desktop:s00"}
    assert shared_before[2]["same_as"] == {
        "aria_snapshot": "before:BV-one:desktop:s01",
        "styles": "before:BV-one:desktop:s00",
    }
    # Changed values are shown in full, null or empty values stay as they are, unchanged ones point to their first record.
    assert shared_after[0]["aria_snapshot"] == "- main:\n  - heading \"Polished\""
    assert shared_after[0]["same_as"] == {"styles": "before:BV-one:desktop:s00"}
    assert shared_after[1]["aria_snapshot"] is None and shared_after[1]["styles"] == after[1]["styles"]
    assert shared_after[1]["same_as"] == {"focus": "before:BV-one:desktop:s01"}
    assert shared_after[2]["same_as"] == {
        "aria_snapshot": "before:BV-one:desktop:s01",
        "styles": "before:BV-one:desktop:s00",
    }
    assert "aria_snapshot" not in shared_after[2] and "styles" not in shared_after[2]
    assert {item["evidence_ref"] for item in shared_after} == {item["evidence_ref"] for item in after}
    assert before[2]["aria_snapshot"] == "- status: Done"  # the manifest projection itself is not mutated


def test_polish_result_prompt_is_compact_and_explains_shared_values() -> None:
    prompt = polish_result_review_prompt(
        contract={"polish_units": []},
        result_context={"after_evidence": [{"evidence_ref": "after:V-0:desktop:s00", "same_as": {"styles": "x"}}]},
    )

    assert prompt.startswith('{"prompt_kind": "UI Polish Result Review v1"')
    assert "\n" not in prompt
    assert any("`same_as`" in rule for rule in json.loads(prompt)["rules"])


class _SettingsClient:
    def __init__(self, *, window: object, responses: list[str]):
        self.settings = type("Settings", (), {"llm_context_window_tokens": window, "llm_structured_max_tokens": 1000})()
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        del system
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _call(client: _SettingsClient, prompt: str, images: list[dict[str, object]] | None = None) -> dict[str, object]:
    return call_structured_json(
        client,
        system="system",
        prompt=prompt,
        profile_name="polish_validator",
        parse=json.loads,
        contract_error_code="contract_invalid",
        label="Test Validator",
        inconclusive_code="test_inconclusive",
        images=images or [],
    )


# 1000 output tokens leave 1000 prompt tokens in a 2000-token window; "system" is part of the prompt.
FITTING_PROMPT_CHARS = int(1000 * PROMPT_CHARS_PER_TOKEN) - len("system") - 10


def test_oversized_structured_prompt_fails_closed_before_the_provider_call() -> None:
    client = _SettingsClient(window=2000, responses=['{"ok": true}'])

    with pytest.raises(HarnessError) as error:
        _call(client, "x" * (FITTING_PROMPT_CHARS + 50))

    assert error.value.error_code == "test_inconclusive"
    assert "context window" in error.value.message and "no provider call was made" in error.value.message
    assert client.prompts == []
    # Images count against the same window.
    with pytest.raises(HarnessError):
        _call(client, "x" * 100, images=[{"label": "a"}])
    assert client.prompts == []


def test_structured_prompt_that_fits_or_has_no_window_is_sent() -> None:
    for window in (2000, None, 0, True):
        client = _SettingsClient(window=window, responses=['{"ok": true}'])
        assert _call(client, "x" * FITTING_PROMPT_CHARS)["output"] == {"ok": True}
        assert len(client.prompts) == 1


def review(**overrides: object) -> str:
    payload = {
        "summary": "Narrow visual polish.",
        "reviewed_unit_ids": ["UP-ONE", "UP-TWO"],
        "findings": [],
        "observations": [],
        **overrides,
    }
    return json.dumps(payload)


def parse(raw: str) -> dict[str, object]:
    return parse_polish_review(raw, contract=CONTRACT, evidence_refs=EVIDENCE, authority_refs=AUTHORITY)


def test_visual_authority_finding_cites_known_image_and_authority() -> None:
    result = parse(
        review(
            findings=[
                {
                    "severity": "warning",
                    "category": "visual_authority",
                    "unit_ids": ["UP-ONE"],
                    "evidence_refs": ["baseline:V-0:desktop:s00"],
                    "authority_refs": ["primary"],
                    "issue": "Spacing is conservative.",
                    "expected_change": "Use the approved spacing scale.",
                }
            ]
        )
    )
    assert result["ok"] is True
    assert len(result["warnings"]) == 1


@pytest.mark.parametrize(
    "finding, fragment",
    [
        (
            {
                "severity": "warning",
                "category": "visual_authority",
                "unit_ids": ["UP-ONE"],
                "evidence_refs": ["unknown"],
                "authority_refs": ["primary"],
                "issue": "i",
                "expected_change": "e",
            },
            "unknown evidence",
        ),
        (
            {
                "severity": "warning",
                "category": "visual_authority",
                "unit_ids": ["UP-ONE"],
                "evidence_refs": [],
                "authority_refs": [],
                "issue": "i",
                "expected_change": "e",
            },
            "need evidence_refs",
        ),
        (
            {
                "severity": "blocker",
                "category": "design_conflict",
                "unit_ids": ["UP-TWO"],
                "evidence_refs": [],
                "authority_refs": ["VDN-UI"],
                "issue": "i",
                "expected_change": "e",
            },
            "cannot be a blocker",
        ),
        (
            {
                "severity": "blocker",
                "category": "behavior",
                "unit_ids": [],
                "evidence_refs": ["baseline:V-0:desktop:s00"],
                "authority_refs": [],
                "issue": "i",
                "expected_change": "e",
            },
            "must cite known polish units",
        ),
    ],
)
def test_invalid_finding_refs_and_design_conflict_blockers_fail_closed(
    finding: dict[str, object],
    fragment: str,
) -> None:
    with pytest.raises(HarnessError, match=fragment) as error:
        parse(review(findings=[finding]))
    assert error.value.error_code == "ui_polish_review_contract_invalid"


def test_reviewed_units_must_match_contract_exactly() -> None:
    with pytest.raises(HarnessError) as error:
        parse(review(reviewed_unit_ids=["UP-ONE"]))
    assert error.value.error_code == "ui_polish_review_contract_invalid"


def test_result_visual_finding_must_cite_an_image_evidence_ref() -> None:
    finding = {
        "severity": "blocker",
        "category": "visual_authority",
        "unit_ids": ["UP-ONE"],
        "evidence_refs": ["after:V-0:desktop:s00"],
        "authority_refs": ["primary"],
        "issue": "Hierarchy is still flat.",
        "expected_change": "Apply the approved hierarchy token.",
    }
    with pytest.raises(HarnessError, match="image evidence_refs"):
        parse_polish_review(
            review(findings=[finding]),
            contract=CONTRACT,
            evidence_refs={"after:V-0:desktop:s00"},
            visual_evidence_refs={"after:V-0:mobile:s00"},
            authority_refs=AUTHORITY,
        )


BROWSER_SCENARIO = {
    "scenario_id": "SC-1",
    "obligations": [
        {"obligation_id": "VDN-list:state.ready", "summary": "rows are ready"},
        {"obligation_id": "VDN-list:accessibility.1", "summary": "grid is named"},
    ],
}
BROWSER_EVIDENCE = {"qa:BV-one:desktop:s00", "qa:BV-one:mobile:s00"}
BROWSER_OWNERS = {
    "VDN-list:state.ready": {"WU-UI"},
    "VDN-list:accessibility.1": {"WU-UI"},
}


def browser_review(**overrides: object) -> str:
    payload = {
        "summary": "Scenario reviewed.",
        "reviewed_obligation_ids": [
            "VDN-list:state.ready",
            "VDN-list:accessibility.1",
        ],
        "verdicts": [
            {
                "obligation_id": "VDN-list:state.ready",
                "verdict": "satisfied",
                "evidence_refs": ["qa:BV-one:desktop:s00"],
            },
            {
                "obligation_id": "VDN-list:accessibility.1",
                "verdict": "satisfied",
                "evidence_refs": ["qa:BV-one:mobile:s00"],
            },
        ],
        "findings": [],
        "observations": [],
        **overrides,
    }
    return json.dumps(payload)


def parse_browser(raw: str) -> dict[str, object]:
    return parse_browser_qa_review(
        raw,
        scenario=BROWSER_SCENARIO,
        evidence_refs=BROWSER_EVIDENCE,
        ownership=BROWSER_OWNERS,
    )


def test_browser_qa_accepts_exact_satisfied_scenario_and_hashes_images_in_prompt() -> None:
    assert parse_browser(browser_review())["ok"] is True
    prompt = json.loads(
        browser_qa_review_prompt(
            scenario=BROWSER_SCENARIO,
            review_context={"evidence": []},
            images=[{"label": "qa:BV-one:desktop", "sha256": "sha256:abc", "path": "x.png"}],
        )
    )
    assert prompt["image_inputs"] == [{"label": "qa:BV-one:desktop", "sha256": "sha256:abc"}]
    assert "text wins" in " ".join(prompt["rules"])


def test_browser_qa_design_conflict_is_recorded_as_a_non_blocking_warning() -> None:
    result = parse_browser(
        browser_review(
            findings=[
                {
                    "severity": "warning",
                    "category": "design_conflict",
                    "obligation_ids": ["VDN-list:state.ready"],
                    "unit_ids": ["WU-UI"],
                    "evidence_refs": ["qa:BV-one:desktop:s00"],
                    "issue": "Two approved Design IDs disagree.",
                    "expected_change": "Follow the reading that satisfies Requirement and record both IDs.",
                }
            ],
            observations=["VDN-list and VDN-shell conflict; Requirement-consistent reading selected."],
        )
    )

    assert result["ok"] is True
    assert result["blocking_findings"] == []
    assert result["warnings"][0]["category"] == "design_conflict"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"reviewed_obligation_ids": ["VDN-list:state.ready"]}, "reviewed_obligation_ids"),
        (
            {
                "verdicts": [
                    {
                        "obligation_id": "VDN-list:state.ready",
                        "verdict": "violated",
                        "evidence_refs": ["qa:BV-one:desktop:s00"],
                    },
                    {
                        "obligation_id": "VDN-list:accessibility.1",
                        "verdict": "satisfied",
                        "evidence_refs": ["qa:BV-one:mobile:s00"],
                    },
                ]
            },
            "violated verdicts need matching findings",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "implementation",
                        "obligation_ids": ["VDN-list:state.ready"],
                        "unit_ids": ["WU-OTHER"],
                        "evidence_refs": ["qa:BV-one:desktop:s00"],
                        "issue": "broken",
                        "expected_change": "repair",
                    }
                ]
            },
            "candidate Plan units",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "design_conflict",
                        "obligation_ids": ["VDN-list:state.ready"],
                        "unit_ids": ["WU-UI"],
                        "evidence_refs": [],
                        "issue": "conflict",
                        "expected_change": "record it",
                    }
                ]
            },
            "cannot be a blocker",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "implementation",
                        "obligation_ids": ["VDN-list:state.ready"],
                        "unit_ids": ["WU-UI"],
                        "evidence_refs": [],
                        "issue": "broken",
                        "expected_change": "repair",
                    }
                ]
            },
            "must cite supplied scenario evidence",
        ),
    ],
)
def test_browser_qa_contract_failures_are_closed(overrides: dict[str, object], fragment: str) -> None:
    with pytest.raises(HarnessError, match=fragment) as error:
        parse_browser(browser_review(**overrides))
    assert error.value.error_code == "browser_qa_review_contract_invalid"


JOURNEY_SCENARIO = {
    "scenario_id": "SC-1",
    "views": [
        {
            "view_id": "BV-one",
            "key": "success",
            "obligation_refs": ["VDN-list:state.success"],
            "steps": [{"action": "goto", "path": "/"}],
        }
    ],
    "obligations": [
        {"obligation_id": "VDN-list:state.success", "summary": "success is observable"}
    ],
    "not_reachable": [],
    "candidate_units": [
        {"unit_id": "WU-WEB", "component_id": "web", "write_paths": ["apps/web/src/View.tsx"]},
        {"unit_id": "WU-API", "component_id": "api", "write_paths": ["apps/api/src/app.ts"]},
    ],
}
JOURNEY_EVIDENCE = {"baseline:BV-one:desktop:s00"}
JOURNEY_UNITS = {"WU-WEB", "WU-API"}
SATISFIED = {
    "obligation_id": "VDN-list:state.success",
    "verdict": "satisfied",
    "evidence_refs": ["baseline:BV-one:desktop:s00"],
}


def journey_review(**overrides: object) -> str:
    return json.dumps(
        {
            "summary": "Journey state reached.",
            "reviewed_view_ids": ["BV-one"],
            "reviewed_not_reachable_obligation_ids": [],
            "view_evidence": [
                {
                    "view_id": "BV-one",
                    "evidence_refs": ["baseline:BV-one:desktop:s00"],
                }
            ],
            "obligation_verdicts": [SATISFIED],
            "not_reachable_verdicts": [],
            "findings": [],
            "observations": [],
            **overrides,
        }
    )


def parse_journey(raw: str) -> dict[str, object]:
    return parse_journey_execution_review(
        raw,
        scenario=JOURNEY_SCENARIO,
        evidence_refs=JOURNEY_EVIDENCE,
        candidate_unit_ids=JOURNEY_UNITS,
    )


def test_journey_execution_accepts_reached_view_and_candidate_api_unit_is_in_prompt() -> None:
    assert parse_journey(journey_review())["ok"] is True
    prompt = json.loads(
        journey_execution_review_prompt(
            scenario=JOURNEY_SCENARIO,
            review_context={"evidence": []},
            images=[{"label": "baseline:BV-one:desktop", "sha256": "sha256:abc"}],
        )
    )
    assert prompt["prompt_kind"] == "Browser Journey Execution Review v1"
    assert {item["unit_id"] for item in prompt["scenario"]["candidate_units"]} == JOURNEY_UNITS
    assert "view_evidence" in prompt["output_schema"]
    assert "verdicts" not in prompt["output_schema"]
    assert "obligation_verdicts" in prompt["output_schema"]
    assert "reason" in prompt["output_schema"]["not_reachable_verdicts"][0]
    rules = " ".join(prompt["rules"])
    assert "Explicit state, behavior, focus, accessibility, responsive and layout obligations are in scope" in rules
    assert "Cite view_evidence for every supplied view" in rules
    assert "every implementation or journey_defect finding is a blocker" in rules
    assert "do not duplicate a needs_journey verdict in findings" in rules
    assert "Planner owns exact journey steps/locators" in rules


def test_journey_execution_routes_blocked_view_to_api_implementation_unit() -> None:
    result = parse_journey(
        journey_review(
            findings=[
                {
                    "severity": "blocker",
                    "category": "implementation",
                    "view_ids": ["BV-one"],
                    "obligation_ids": ["VDN-list:state.success"],
                    "unit_ids": ["WU-API"],
                    "evidence_refs": ["baseline:BV-one:desktop:s00"],
                    "issue": "The browser request never reaches the approved success state.",
                    "expected_change": "Restore the API browser boundary.",
                }
            ],
        )
    )
    assert result["ok"] is False
    assert result["blocking_findings"][0]["unit_ids"] == ["WU-API"]
    assert result["verdicts"][0]["verdict"] == "blocked"


def test_journey_execution_routes_journey_defect_without_code_unit() -> None:
    result = parse_journey(
        journey_review(
            findings=[
                {
                    "severity": "blocker",
                    "category": "journey_defect",
                    "view_ids": ["BV-one"],
                    "obligation_ids": ["VDN-list:state.success"],
                    "unit_ids": [],
                    "evidence_refs": ["baseline:BV-one:desktop:s00"],
                    "issue": "The typed locator cannot observe the rendered state.",
                    "expected_change": "Patch the typed journey locator.",
                }
            ],
        )
    )

    assert result["ok"] is False
    assert result["blocking_findings"][0]["unit_ids"] == []


def test_journey_finding_obligation_must_belong_to_every_cited_view() -> None:
    second_ref = "baseline:BV-two:desktop:s00"
    scenario = {
        **JOURNEY_SCENARIO,
        "views": [
            *JOURNEY_SCENARIO["views"],
            {
                "view_id": "BV-two",
                "key": "error",
                "obligation_refs": [],
                "steps": [{"action": "goto", "path": "/"}],
            },
        ],
    }
    raw = journey_review(
        reviewed_view_ids=["BV-one", "BV-two"],
        view_evidence=[
            {"view_id": "BV-one", "evidence_refs": ["baseline:BV-one:desktop:s00"]},
            {"view_id": "BV-two", "evidence_refs": [second_ref]},
        ],
        findings=[
            {
                "severity": "blocker",
                "category": "implementation",
                "view_ids": ["BV-one", "BV-two"],
                "obligation_ids": ["VDN-list:state.success"],
                "unit_ids": ["WU-WEB"],
                "evidence_refs": ["baseline:BV-one:desktop:s00", second_ref],
                "issue": "The cited obligation is absent.",
                "expected_change": "Restore the required outcome.",
            }
        ],
    )

    with pytest.raises(HarnessError, match="must belong to every cited view"):
        parse_journey_execution_review(
            raw,
            scenario=scenario,
            evidence_refs={*JOURNEY_EVIDENCE, second_ref},
            candidate_unit_ids=JOURNEY_UNITS,
        )


def test_journey_execution_rejects_unjustified_not_reachable_as_a_planning_gap() -> None:
    obligation_id = "VDN-list:focus.ready"
    scenario = {
        **JOURNEY_SCENARIO,
        "obligations": [
            *JOURNEY_SCENARIO["obligations"],
            {"obligation_id": obligation_id, "summary": "focus enters the ready list heading"},
        ],
        "not_reachable": [
            {"obligation_id": obligation_id, "reason": "The current source does not focus the heading."}
        ],
    }
    verdict = {
        "obligation_id": obligation_id,
        "verdict": "needs_journey",
        "reason": "A wait_for on the ready grid exposes the focus target.",
    }

    def parse(unreachable_verdict: dict[str, str]) -> dict[str, object]:
        return parse_journey_execution_review(
            journey_review(
                reviewed_not_reachable_obligation_ids=[obligation_id],
                not_reachable_verdicts=[unreachable_verdict],
                findings=[],
            ),
            scenario=scenario,
            evidence_refs=JOURNEY_EVIDENCE,
            candidate_unit_ids=JOURNEY_UNITS,
        )

    result = parse(verdict)

    assert result["ok"] is False
    assert result["not_reachable_verdicts"] == [verdict]
    assert result["blocking_findings"][0]["view_ids"] == []
    # The Planner gets its own claim back together with what the Validator says exposes the obligation.
    issue = result["blocking_findings"][0]["issue"]
    assert "The current source does not focus the heading." in issue
    assert "A wait_for on the ready grid exposes the focus target." in issue

    with pytest.raises(HarnessError, match="reason must say what exposes the obligation"):
        parse({"obligation_id": obligation_id, "verdict": "needs_journey"})


def test_journey_execution_design_conflict_is_observation_only() -> None:
    result = parse_journey(
        journey_review(
            findings=[
                {
                    "severity": "warning",
                    "category": "design_conflict",
                    "view_ids": ["BV-one"],
                    "obligation_ids": ["VDN-list:state.success"],
                    "unit_ids": [],
                    "evidence_refs": ["baseline:BV-one:desktop:s00"],
                    "issue": "Two approved Design nodes disagree.",
                    "expected_change": "Follow Requirement and record both Design IDs.",
                }
            ],
            observations=["Requirement-consistent reading selected."],
        )
    )

    assert result["ok"] is True
    assert result["blocking_findings"] == []
    assert result["warnings"][0]["category"] == "design_conflict"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"reviewed_view_ids": []}, "reviewed_view_ids"),
        (
            {"view_evidence": []},
            "view_evidence view IDs",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "implementation",
                        "view_ids": ["BV-one"],
                        "obligation_ids": ["VDN-list:state.success"],
                        "unit_ids": ["WU-OTHER"],
                        "evidence_refs": ["baseline:BV-one:desktop:s00"],
                        "issue": "broken",
                        "expected_change": "repair",
                    }
                ]
            },
            "candidate scenario units",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "blocker",
                        "category": "design_conflict",
                        "view_ids": ["BV-one"],
                        "obligation_ids": ["VDN-list:state.success"],
                        "unit_ids": ["WU-WEB"],
                        "evidence_refs": ["baseline:BV-one:desktop:s00"],
                        "issue": "conflict",
                        "expected_change": "record",
                    }
                ],
            },
            "must be warnings",
        ),
        (
            {
                "findings": [
                    {
                        "severity": "warning",
                        "category": "implementation",
                        "view_ids": ["BV-one"],
                        "obligation_ids": ["VDN-list:state.success"],
                        "unit_ids": ["WU-WEB"],
                        "evidence_refs": ["baseline:BV-one:desktop:s00"],
                        "issue": "broken",
                        "expected_change": "repair",
                    }
                ]
            },
            "implementation and journey_defect findings must be blockers",
        ),
        ({"obligation_verdicts": []}, "obligation_verdicts obligation IDs must be exactly"),
        (
            {"obligation_verdicts": [{**SATISFIED, "evidence_refs": []}]},
            r"obligation_verdicts\[1\].evidence_refs must cite supplied scenario evidence",
        ),
        (
            {"obligation_verdicts": [{**SATISFIED, "verdict": "violated"}]},
            "violated or not_observable obligation verdicts need matching findings",
        ),
        (
            {"obligation_verdicts": [{**SATISFIED, "verdict": "not_observable", "evidence_refs": []}]},
            "violated or not_observable obligation verdicts need matching findings",
        ),
    ],
)
def test_journey_execution_contract_failures_are_closed(
    overrides: dict[str, object], fragment: str
) -> None:
    with pytest.raises(HarnessError, match=fragment) as error:
        parse_journey(journey_review(**overrides))
    assert error.value.error_code == "browser_journey_review_contract_invalid"


class _ScriptedClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        del system
        self.prompts.append(prompt)
        return self.responses.pop(0)


def test_journey_contract_retry_cannot_change_semantic_finding() -> None:
    first = journey_review(
        findings=[
            {
                "severity": "blocker",
                "category": "implementation",
                "view_ids": ["BV-one"],
                "obligation_ids": ["VDN-list:state.success"],
                "unit_ids": [],
                "evidence_refs": ["baseline:BV-one:desktop:s00"],
                "issue": "The implemented state is absent.",
                "expected_change": "Restore the implemented state.",
            }
        ],
    )
    changed = journey_review(
        summary="The view is now treated as reached.",
        findings=[],
    )
    client = _ScriptedClient([first, changed])

    with pytest.raises(HarnessError) as error:
        BrowserQaValidator(client).review_journey_scenario(
            scenario=JOURNEY_SCENARIO,
            review_context={"evidence": []},
            evidence_refs=JOURNEY_EVIDENCE,
            candidate_unit_ids=JOURNEY_UNITS,
            images=[],
        )

    assert error.value.error_code == "browser_journey_review_inconclusive"
    assert "outside the reported mechanical correction" in error.value.message
    assert "`findings[1]` means the first finding" in client.prompts[1]
    assert "exactly the same object keys, list lengths, list item order and values" in client.prompts[1]
    assert "do not add wrapper or metadata keys such as `type`" in client.prompts[1]
    assert "Preserve every semantic decision" in client.prompts[1]
    assert "Previous JSON:" in client.prompts[1]


def test_journey_contract_retry_allows_dependent_view_cleanup() -> None:
    second_ref = "baseline:BV-two:desktop:s00"
    scenario = {
        **JOURNEY_SCENARIO,
        "views": [
            *JOURNEY_SCENARIO["views"],
            {
                "view_id": "BV-two",
                "key": "error",
                "obligation_refs": [],
                "steps": [{"action": "goto", "path": "/"}],
            },
        ],
    }
    common = {
        "reviewed_view_ids": ["BV-one", "BV-two"],
        "view_evidence": [
            {"view_id": "BV-one", "evidence_refs": ["baseline:BV-one:desktop:s00"]},
            {"view_id": "BV-two", "evidence_refs": [second_ref]},
        ],
    }
    first = journey_review(
        **common,
        findings=[
            {
                "severity": "blocker",
                "category": "implementation",
                "view_ids": ["BV-one", "BV-two"],
                "obligation_ids": ["VDN-list:state.success"],
                "unit_ids": ["WU-WEB"],
                "evidence_refs": ["baseline:BV-one:desktop:s00", second_ref],
                "issue": "The obligation is absent in every cited view.",
                "expected_change": "Restore the approved outcome.",
            }
        ],
    )
    corrected = journey_review(
        **common,
        findings=[
            {
                "severity": "blocker",
                "category": "implementation",
                "view_ids": ["BV-one"],
                "obligation_ids": ["VDN-list:state.success"],
                "unit_ids": ["WU-WEB"],
                "evidence_refs": ["baseline:BV-one:desktop:s00"],
                "issue": "The obligation is absent in the cited view.",
                "expected_change": "Restore the approved outcome.",
            }
        ],
    )
    client = _ScriptedClient([first, corrected])

    result = BrowserQaValidator(client).review_journey_scenario(
        scenario=scenario,
        review_context={"evidence": []},
        evidence_refs={*JOURNEY_EVIDENCE, second_ref},
        candidate_unit_ids=JOURNEY_UNITS,
        images=[],
    )

    assert result["ok"] is False
    assert result["blocking_findings"][0]["view_ids"] == ["BV-one"]
    assert result["blocking_findings"][0]["evidence_refs"] == ["baseline:BV-one:desktop:s00"]
    assert result["blocking_findings"][0]["issue"] == "The obligation is absent in the cited view."
    retry_prompt = client.prompts[1]
    assert "keep `findings[1].obligation_ids` unchanged" in retry_prompt
    assert (
        "correct only `findings[1].view_ids`, `findings[1].evidence_refs`, and `findings[1].issue`"
        in retry_prompt
    )
    assert "Correct only the fields named by that contract error" not in retry_prompt


def test_journey_contract_retry_is_unguarded_when_another_error_needs_other_fields() -> None:
    finding = {
        "severity": "blocker",
        "category": "implementation",
        "view_ids": ["BV-one"],
        "obligation_ids": ["VDN-list:state.success"],
        "unit_ids": [],
        "evidence_refs": ["baseline:BV-one:desktop:s00"],
        "issue": "The implemented state is absent.",
        "expected_change": "Restore the implemented state.",
    }
    # Two contract errors: the guarded unit_ids one, and a missing verdict the guard would freeze.
    first = journey_review(findings=[finding], obligation_verdicts=[])
    corrected = journey_review(
        findings=[{**finding, "unit_ids": ["WU-WEB"]}],
        obligation_verdicts=[{**SATISFIED, "verdict": "violated"}],
    )
    client = _ScriptedClient([first, corrected])

    result = BrowserQaValidator(client).review_journey_scenario(
        scenario=JOURNEY_SCENARIO,
        review_context={"evidence": []},
        evidence_refs=JOURNEY_EVIDENCE,
        candidate_unit_ids=JOURNEY_UNITS,
        images=[],
    )

    assert result["blocking_findings"][0]["unit_ids"] == ["WU-WEB"]
    assert result["obligation_verdicts"][0]["verdict"] == "violated"
    assert "Correct only the fields named by that contract error" in client.prompts[1]


def test_needs_journey_contract_rejects_a_duplicated_viewless_finding() -> None:
    obligation_id = "VDN-list:focus.ready"
    scenario = {
        **JOURNEY_SCENARIO,
        "obligations": [
            *JOURNEY_SCENARIO["obligations"],
            {"obligation_id": obligation_id, "summary": "focus enters the ready list heading"},
        ],
        "not_reachable": [
            {"obligation_id": obligation_id, "reason": "The current capture does not expose it."}
        ],
    }
    raw = journey_review(
        reviewed_not_reachable_obligation_ids=[obligation_id],
        not_reachable_verdicts=[
            {"obligation_id": obligation_id, "verdict": "needs_journey", "reason": "A typed capture exposes it."}
        ],
        findings=[
            {
                "severity": "blocker",
                "category": "journey_defect",
                "view_ids": [],
                "obligation_ids": [obligation_id],
                "unit_ids": [],
                "evidence_refs": [],
                "issue": "The current capture cannot judge the approved state.",
                "expected_change": "Add the intended typed capture.",
            }
        ],
    )

    with pytest.raises(HarnessError) as error:
        parse_journey_execution_review(
            raw,
            scenario=scenario,
            evidence_refs=JOURNEY_EVIDENCE,
            candidate_unit_ids=JOURNEY_UNITS,
        )

    assert "view_ids must cite supplied views" in error.value.message
    assert "do not duplicate needs_journey verdicts in findings" in error.value.message
