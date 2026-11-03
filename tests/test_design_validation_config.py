from __future__ import annotations

from pathlib import Path
import tempfile

from sdlc_harness.core.config import load_settings
from sdlc_harness.interfaces.cli import build_parser


def test_design_semantic_controls_are_available_on_the_current_design_command() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        settings = load_settings(
            root=repo_root,
            runtime_root=Path(directory) / ".runtime",
            overrides={
                "SDLC_DESIGN_VALIDATION_TRIALS": "3",
                "SDLC_DESIGN_SEMANTIC_REVIEW_MODE": "sliced_shadow",
            },
        )
    args = build_parser().parse_args(
        [
            "approve-checkpoint",
            "--run-id",
            "run_source",
            "--design-validation-trials",
            "3",
            "--design-semantic-review-mode",
            "sliced_shadow",
        ]
    )

    assert settings.design_validation_trials == 3
    assert settings.design_semantic_review_mode == "sliced_shadow"
    assert args.design_validation_trials == "3"
    assert args.design_semantic_review_mode == "sliced_shadow"
