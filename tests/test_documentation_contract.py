from __future__ import annotations

import unittest

from sdlc_harness.agents.developer.documentation_contract import (
    DOC_BLOCK_BEGIN,
    DOC_BLOCK_END,
    documentation_context,
    documentation_rule_issues,
    proven_commands,
    render_documentation_block,
    splice_documentation_block,
    strip_documentation_block,
)

SPECS = [
    {"stage_id": "install", "argv": ["npm", "ci"]},
    {"stage_id": "unit", "argv": ["npm", "test"], "additional_argv": [["npm", "run", "test:api"]]},
    {"stage_id": "frontend_build", "argv": ["npm", "run", "build:web"]},
    {"stage_id": "runtime_probe", "argv": ["npx", "playwright", "test", "probe.spec.ts"]},
    {"stage_id": "browser_capture", "argv": ["npx", "playwright", "test", "capture.spec.ts"]},
    {"stage_id": "lint", "argv": ["npm", "run", "lint"]},
]
DEMO_FLAG = "VITE_SDLC_DEMO_RUNTIME"


def _results(*, lint: str = "failed", capture_env: dict[str, object] | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"stage_id": stage_id, "status": "passed"}
        for stage_id in ("install", "unit", "frontend_build", "runtime_probe")
    ]
    capture: dict[str, object] = {"stage_id": "browser_capture", "status": "passed"}
    if capture_env is not None:
        capture["env_keys"] = capture_env
    return [*rows, capture, {"stage_id": "lint", "status": lint}]


def _context(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "run_id": "run_1",
        "issue_key": "UC-1",
        "source_verification": "artifacts/verification_v1.json",
        "specs": SPECS,
        "stage_results": _results(capture_env={"harness_constants": {DEMO_FLAG: "1"}}),
        "env_example": {"ETOUR_BASE_URL": "http://localhost:4010"},
        "demo_flag_env_key": DEMO_FLAG,
        "scenario_reviews": [
            {
                "scenario_id": "SC-1",
                "ok": True,
                "verdicts": [
                    {"obligation_id": "O-1", "verdict": "satisfied"},
                    {"obligation_id": "O-2", "verdict": "not_observable"},
                ],
            }
        ],
        "not_reachable": [{"obligation_id": "O-2", "reason": "settles in a microtask"}],
    }
    arguments.update(overrides)
    return documentation_context(**arguments)  # type: ignore[arg-type]


def _issues(context: dict[str, object], block: str, *, original: str = "# App\n", updated: str | None = None):
    return documentation_rule_issues(
        context=context,
        block=block,
        original_readme=original,
        new_readme=splice_documentation_block(original, block) if updated is None else updated,
        proven=proven_commands(specs=SPECS, stage_results=_results()),
        env_example={"ETOUR_BASE_URL": "http://localhost:4010"},
        demo_flag_env_key=DEMO_FLAG,
    )


class DocumentationContractTests(unittest.TestCase):
    def test_proven_commands_keep_passed_stages_in_order_without_harness_only_stages(self) -> None:
        commands = proven_commands(specs=SPECS, stage_results=_results())

        self.assertEqual(
            commands,
            {
                "install": ["npm ci"],
                "unit": ["npm test", "npm run test:api"],
                "frontend_build": ["npm run build:web"],
            },
        )

    def test_context_lists_env_example_demo_flag_and_unobservable_obligations(self) -> None:
        context = _context()

        self.assertEqual(
            [(item["name"], item["source"], item["value"]) for item in context["env"]],
            [("ETOUR_BASE_URL", "env_example", "http://localhost:4010"), (DEMO_FLAG, "demo_flag", "1")],
        )
        self.assertEqual(context["env"][1]["command"], "npm run build:web")
        self.assertEqual(
            context["scenarios"],
            [
                {
                    "scenario_id": "SC-1",
                    "result": "pass",
                    "satisfied": 1,
                    "not_observable": [{"obligation_id": "O-2", "reason": "settles in a microtask"}],
                }
            ],
        )

    def test_demo_flag_needs_a_passed_capture_that_recorded_it(self) -> None:
        without_flag = _context(stage_results=_results(capture_env={"harness_constants": {}}))
        legacy = _context(stage_results=_results())  # results from before L5.5.1 carry no env_keys

        self.assertNotIn(DEMO_FLAG, [item["name"] for item in without_flag["env"]])
        self.assertIn(DEMO_FLAG, [item["name"] for item in legacy["env"]])

    def test_rendered_block_passes_every_gate_and_is_stable(self) -> None:
        context = _context()
        block = render_documentation_block(context)

        self.assertEqual(_issues(context, block), [])
        self.assertEqual(render_documentation_block(_context()), block)
        self.assertTrue(block.startswith(DOC_BLOCK_BEGIN) and block.endswith(DOC_BLOCK_END))
        self.assertNotIn("playwright", block)

    def test_dc1_rejects_a_command_no_passed_stage_ran(self) -> None:
        context = _context()
        block = render_documentation_block(context).replace("npm ci", "npm ci\nnpm run lint")

        self.assertIn("DC1", [item["gate"] for item in _issues(context, block)])

    def test_dc2_rejects_an_env_name_the_run_did_not_prove(self) -> None:
        context = _context()
        context["env"].append({"name": "API_TOKEN", "source": "env_example", "value": "1"})

        self.assertEqual(
            [item["gate"] for item in _issues(context, render_documentation_block(context))], ["DC2"]
        )

    def test_dc3_rejects_a_readme_change_outside_the_section(self) -> None:
        context = _context()
        block = render_documentation_block(context)
        updated = splice_documentation_block("# App edited\n", block)

        self.assertEqual([item["gate"] for item in _issues(context, block, updated=updated)], ["DC3"])

    def test_dc4_rejects_an_env_value_outside_reviewed_constants(self) -> None:
        context = _context()
        context["env"][0]["value"] = "http://secret.internal"

        self.assertEqual(
            [item["gate"] for item in _issues(context, render_documentation_block(context))], ["DC4"]
        )

    def test_dc5_rejects_a_block_that_does_not_render_from_its_context(self) -> None:
        context = _context()
        block = render_documentation_block(context).replace("### Environment", "### Env")

        self.assertEqual([item["gate"] for item in _issues(context, block)], ["DC5"])

    def test_splice_keeps_crlf_replaces_an_existing_section_and_is_idempotent(self) -> None:
        readme = "# App\r\n\r\nOwner text.\r\n"
        block = render_documentation_block(_context())

        once = splice_documentation_block(readme, block)
        stale = splice_documentation_block(readme, DOC_BLOCK_BEGIN + "\nold section\n" + DOC_BLOCK_END)

        self.assertEqual(splice_documentation_block(once, block), once)
        self.assertEqual(splice_documentation_block(stale, block), once)
        self.assertEqual(once.replace("\r\n", "").count("\n"), 0)
        self.assertTrue(once.startswith(readme.rstrip("\r\n")))
        self.assertEqual(once.count(DOC_BLOCK_BEGIN), 1)
        self.assertEqual(strip_documentation_block(once), strip_documentation_block(readme))


if __name__ == "__main__":
    unittest.main()
