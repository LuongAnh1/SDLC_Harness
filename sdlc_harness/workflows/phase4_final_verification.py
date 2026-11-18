"""Final Verification (L5.5.3): prove the exact tree the Draft PR will publish, after the owner approved the docs.

```text
documentation_approved
  -> F1 worktree unchanged since the last browser verification (README: exactly the approved bytes)
  -> F2 CI parity: a temporary tree = run HEAD + publication manifest only; the repository's own suite
       (`verification_stage_policy_v2`) with the reviewed env: no stand-in, no demo flag, no Harness specs
  -> F3 every documented command passed in F2; the documented demo build reruns with only the demo flag
  -> final_verification_passed | blocked
```

Mechanical only; a failure blocks for the owner (no repair in v1, owner D6). Environment failures can retry.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from ..agents.developer.documentation_contract import (
    DEMO_BUILD_STAGE_ID,
    DOCUMENTATION_PATH,
    documented_commands,
    proven_commands,
)
from ..agents.developer.runtime_config import worktree_dirty_paths
from ..core.errors import HarnessError
from ..profiles import VERIFICATION_STAGE_POLICY_VERSION, build_verification_stage_policy_version, get_project_profile
from ..storage.runtime import utc_now_iso
from ..verification.registry import resolve_verification_stage_specs
from ..verification.runner import _windows_io_path
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import _dict_items, _required_phase4_artifact
from .phase4_browser import _verification_digest, _worktree_fingerprint
from .phase4_closure import _next_closure_artifact_version, _write_closure_json
from .phase4_documentation import _read_text_exact, _sha256
from ..core import run_state

FINAL_VERIFICATION_SCHEMA_VERSION = "final_verification_v1"
ALLOWED_FINAL_VERIFICATION_ACTIONS = ["continue_run", "get_status", "cancel_run"]
_FINAL_PHASE = "final_verification"
_ENVIRONMENT_ERROR = "final_verification_environment_failed"


class PhaseFourFinalVerificationCycle:
    """Own `documentation_approved` -> `final_verification_passed`."""

    def _final_verification_retry_allowed(self, state: dict[str, object]) -> bool:
        return (
            str(state.get("status") or "") == "blocked"
            and str(state.get("current_phase") or "") == _FINAL_PHASE
            and str(_dict_value(state.get("last_error")).get("error_code") or "") == _ENVIRONMENT_ERROR
        )

    def _continue_final_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = run_state.final_verification(state)
        state[run_state.FINAL_VERIFICATION] = {**pin, "status": "running", "started_at": utc_now_iso()}
        self.store.write_state(state)
        report: dict[str, object] = {"schema_version": FINAL_VERIFICATION_SCHEMA_VERSION, "run_id": run_id}
        try:
            self._run_final_verification(state=state, run_id=run_id, report=report)
        except HarnessError as exc:
            if "f1" in report:
                artifact_ref = self._write_final_verification_report(run_id, {**report, "status": "failed"})
                latest_artifacts["final_verification"] = artifact_ref
            state[run_state.FINAL_VERIFICATION] = {**run_state.final_verification(state), "status": "failed"}
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": _FINAL_PHASE,
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ["continue_run", "get_status", "cancel_run"]
                        if exc.error_code == _ENVIRONMENT_ERROR
                        else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Final Verification bị chặn cho run {run_id}: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        artifact_ref = self._write_final_verification_report(run_id, {**report, "status": "passed"})
        latest_artifacts["final_verification"] = artifact_ref
        state[run_state.FINAL_VERIFICATION] = {
            **run_state.final_verification(state),
            "status": "passed",
            "artifact": artifact_ref,
            "worktree_sha256": _dict_value(report.get("f1")).get("worktree_sha256"),
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _FINAL_PHASE,
                "status": "final_verification_passed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_FINAL_VERIFICATION_ACTIONS,
                "last_error": None,
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "final_verification": {"ok": True, "artifact": artifact_ref},
                },
                "summary_for_user": (
                    f"Final Verification pass cho run {run_id}: cây sẽ publish chạy xanh bộ kiểm tra của repo "
                    "và mọi lệnh trong README."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _run_final_verification(self, *, state: dict[str, object], run_id: str, report: dict[str, object]) -> None:
        def read(ref: str) -> dict[str, object]:
            return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)

        documentation = run_state.documentation_closure(state)
        if str(documentation.get("status") or "") != "approved" or not documentation.get("artifact"):
            raise HarnessError(
                "final_verification_documentation_not_approved",
                "Final Verification needs the approved Documentation Closure.",
                run_id=run_id,
            )
        inputs = self._browser_inputs(state=state, run_id=run_id)
        target_path: Path = inputs["target_path"]
        doc_artifact = read(str(documentation["artifact"]))

        # F1: nothing but the approved README section changed since the last browser verification.
        verification_ref = str(_dict_value(run_state.browser_stages(state).get("qa")).get("verification") or "")
        captured = _dict_value(read(verification_ref).get("worktree_fingerprint")) if verification_ref else {}
        current = _worktree_fingerprint(target_path)
        changed = _fingerprint_changes(captured, current)
        readme_ok = _sha256(_read_text_exact(target_path / DOCUMENTATION_PATH)) == documentation.get("readme_sha256")
        report["f1"] = {
            "ok": bool(captured.get("files")) and not changed and readme_ok,
            "source_verification": verification_ref,
            "worktree_sha256": current.get("sha256"),
            "changed_since_verification": changed,
            "readme_matches_approved": readme_ok,
        }
        if not report["f1"]["ok"]:
            raise HarnessError(
                "final_verification_failed",
                "F1: the worktree is not the verified tree"
                + (f"; changed since verification: {changed}" if changed else "")
                + ("" if captured.get("files") else "; the last browser verification has no worktree fingerprint")
                + ("" if readme_ok else f"; {DOCUMENTATION_PATH} differs from the approved section"),
                run_id=run_id,
            )

        # F2: the publication manifest must cover every change, then CI's own suite runs on exactly that tree.
        qa_generated = read(_required_phase4_artifact(_string_dict(state.get("latest_artifacts")), "qa_generated_tests", run_id=run_id))
        paths, evidence_only = self._code_review_publication_manifest(
            state=state, run_id=run_id, work_packet=inputs["frozen_packet"], qa_generated=qa_generated
        )
        unpublished = sorted(
            path
            for path in worktree_dirty_paths(target_path)
            if path not in paths and path not in evidence_only and not _is_codegraph(path)
        )
        report["manifest"] = {"paths": paths, "evidence_only": evidence_only, "unpublished_changes": unpublished}
        if unpublished:
            raise HarnessError(
                "final_verification_failed",
                f"F2: changed files outside the publication manifest: {unpublished}",
                run_id=run_id,
            )
        policy = build_verification_stage_policy_version(
            project=get_project_profile(inputs["planning_policy"].project_profile_id),
            policy_version=VERIFICATION_STAGE_POLICY_VERSION,
        )
        # A separate isolation key: F2's database must not share the run's Compose project.
        packet = {**inputs["frozen_packet"], "run_id": f"{run_id}-final", "verification_stage_ids": list(policy.stage_ids)}
        tree = target_path.parent / f"fv_{run_id[-8:]}"
        _create_publication_tree(target_path, tree, paths, run_id=run_id)
        try:
            result = self.verification_runner.run(target_path=tree, work_packet=packet, verification_policy=policy)
            report["f2"] = {
                "ok": result.status == "passed",
                "policy_version": policy.policy_version,
                "tree": "run HEAD + publication manifest; no stand-in, no demo flag, no Harness specs",
                "verification": _verification_digest(result),
            }
            if result.status != "passed":
                raise HarnessError(
                    _ENVIRONMENT_ERROR if _environment_failure(result) else "final_verification_failed",
                    f"F2: {result.reason}",
                    run_id=run_id,
                )

            # F3: every documented command is one F2 ran; the demo build reruns with the documented flag.
            specs = [spec.evidence() for spec in resolve_verification_stage_specs(policy, run_id=run_id)]
            ran = {
                line
                for lines in proven_commands(
                    specs=specs, stage_results=[item.to_dict() for item in result.stage_results]
                ).values()
                for line in lines
            }
            commands = documented_commands(str(doc_artifact.get("block") or ""))
            missing = sorted(line for line in commands if line not in ran)
            demo = next(
                (item for item in _dict_items(_dict_value(doc_artifact.get("context")).get("env")) if item.get("source") == "demo_flag"),
                None,
            )
            report["f3"] = {"ok": not missing, "commands": commands, "not_run_in_f2": missing}
            if missing:
                raise HarnessError("final_verification_failed", f"F3: documented commands F2 did not run: {missing}", run_id=run_id)
            if demo is not None:
                demo_result = self.verification_runner.run_demo_flag_stage(
                    target_path=tree,
                    work_packet=packet,
                    verification_policy=policy,
                    stage_id=DEMO_BUILD_STAGE_ID,
                    demo_flag_env_key=str(demo.get("name") or ""),
                )
                report["f3"] = {
                    **report["f3"],
                    "ok": demo_result.status == "passed",
                    "demo_build": {"env": {str(demo.get("name")): "1"}, "verification": _verification_digest(demo_result)},
                }
                if demo_result.status != "passed":
                    raise HarnessError(
                        _ENVIRONMENT_ERROR if _environment_failure(demo_result) else "final_verification_failed",
                        f"F3: the documented demo build failed: {demo_result.reason}",
                        run_id=run_id,
                    )
        finally:
            _remove_publication_tree(target_path, tree)

    def _write_final_verification_report(self, run_id: str, report: dict[str, object]) -> str:
        version = _next_closure_artifact_version(self.store, run_id, "final_verification")
        return _write_closure_json(
            self.store, run_id, f"final_verification_v{version}.json", {**report, "version": version}
        )


def _fingerprint_changes(captured: dict[str, object], current: dict[str, object]) -> list[str]:
    def rows(value: dict[str, object]) -> dict[str, dict[str, object]]:
        return {
            str(item.get("path") or ""): item
            for item in _dict_items(value.get("files"))
            if str(item.get("path") or "") != DOCUMENTATION_PATH
        }

    before, after = rows(captured), rows(current)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _environment_failure(result) -> bool:
    return result.failure_kind == "environment" or any(
        item.status != "passed" and item.failure_kind == "environment"
        for item in (*result.preparation_results, *result.stage_results)
    )


def _is_codegraph(path: str) -> bool:
    return path == ".codegraph" or path.startswith(".codegraph/")


def _create_publication_tree(target_path: Path, tree: Path, paths: list[str], *, run_id: str) -> None:
    """A detached worktree at the run's HEAD with exactly the manifest's files copied (or deleted)."""

    _remove_publication_tree(target_path, tree)
    completed = subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
        cwd=str(target_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise HarnessError(
            _ENVIRONMENT_ERROR,
            f"F2: could not create the publication tree: {(completed.stderr or completed.stdout).strip()[:500]}",
            run_id=run_id,
        )
    for path in paths:
        source, destination = target_path / path, tree / path
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        elif destination.is_file():
            destination.unlink()


def _remove_publication_tree(target_path: Path, tree: Path) -> None:
    if tree.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)],
            cwd=str(target_path),
            capture_output=True,
            check=False,
        )
    if tree.exists():
        # node_modules paths exceed MAX_PATH on Windows; git may leave them behind.
        shutil.rmtree(_windows_io_path(tree), ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=str(target_path), capture_output=True, check=False)
