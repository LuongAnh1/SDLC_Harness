from __future__ import annotations

import json
from pathlib import Path
import tempfile

from sdlc_harness.core.config import load_settings
from sdlc_harness.interfaces.cli import build_parser
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow


def test_settings_and_cli_expose_consensus_trials() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        settings = load_settings(
            root=repo_root,
            runtime_root=Path(directory) / ".runtime",
            overrides={"SDLC_DESIGN_VALIDATION_TRIALS": "3"},
        )
    args = build_parser().parse_args(
        [
            "experiment-design-consensus",
            "--source-run-id",
            "run_source",
        ]
    )

    assert settings.design_validation_trials == 3
    assert args.design_validation_trials == "3"
    assert args.design_repair_limit == "3"
    assert args.llm_call_budget == "16"
    assert args.experiment_root == ".experiments/design-validator"
    assert args.sync_mode == "none"


def test_experiment_uses_new_run_and_never_syncs() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        runtime_root = Path(directory) / ".runtime"
        experiment_root = Path(directory) / ".experiments" / "design-validator"
        source_settings = load_settings(
            root=repo_root,
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "1",
            },
        )
        phase_one_result = PhaseOneWorkflow(source_settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        source_run_id = str(phase_one_result["run_id"])
        checkpoint = phase_one_result["pending_checkpoint"]
        assert isinstance(checkpoint, dict)
        PhaseTwoWorkflow(source_settings).approve_checkpoint(
            run_id=source_run_id,
            checkpoint_id=str(checkpoint["checkpoint_id"]),
        )
        source_state_path = runtime_root / "runs" / source_run_id / "state.json"
        source_state_before = source_state_path.read_bytes()

        experiment_settings = load_settings(
            root=repo_root,
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "3",
                "SDLC_DESIGN_VALIDATION_TRIALS": "3",
            },
        )
        result = PhaseTwoWorkflow(experiment_settings).experiment_design_consensus(
            source_run_id=source_run_id,
            experiment_root=experiment_root,
            llm_call_budget=16,
        )

        experiment_run_id = str(result["run_id"])
        experiment_dir = experiment_root / "runs" / experiment_run_id
        state = json.loads((experiment_dir / "state.json").read_text(encoding="utf-8"))
        assert experiment_run_id != source_run_id
        assert source_state_path.read_bytes() == source_state_before
        assert not (runtime_root / "runs" / experiment_run_id).exists()
        assert state["experiment"]["source_run_id"] == source_run_id
        assert state["experiment"]["validation_trials"] == 3
        assert state["status"].startswith("design_experiment_")
        assert state["last_sync"] == {"status": "disabled", "mode": "none", "attempted": False}
        assert state["experiment"]["source_integrity"]["unchanged"] is True
        assert state["experiment"]["llm_calls"]["used"] <= 16
        assert not (experiment_dir / "sync").exists()
        assert not (experiment_root / "index" / "active_runs.json").exists()
        assert (experiment_dir / "artifacts" / "experiment_report.json").is_file()
        assert (experiment_dir / "artifacts" / "experiment_report.md").is_file()
        snapshot = experiment_dir / "artifacts" / "requirement_snapshot"
        assert len(list(snapshot.iterdir())) == 6
