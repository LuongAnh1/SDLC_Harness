"""Generic rewind (G1.4): send a Phase 4 run back to an earlier node, as if the stages from it on had not run yet.

One owner action replaces the per-stage recoveries the r9 driver wrote by hand. `rewind_run`:

1. accepts only a node of `graph.REWIND_TARGETS` that the artifact index shows on the run's current branch
   (a step an earlier rewind went back before is not an ancestor any more);
2. drops every `latest_artifacts` pointer written since that visit began, unless the key already existed then
   (a key rewritten later, such as `code_change_report`, keeps its newest ref: the code it describes stays);
3. resets the pin of the target stage and of every later stage (`graph.REWIND_STAGES`): attempt counters only
   grow, so no approved step-review exchange of the earlier attempt is ever replayed;
4. puts back what the Harness itself wrote into the worktree for those stages: the Documentation README section
   and the UI Polish edits; each restore is checked by sha256, and a file someone changed afterwards stops the
   rewind instead of being overwritten. Every other code change stays in the worktree;
5. backs up `state.json` into `audit/` and records the rewind in `state.recoveries`.

Artifact files are never deleted. The index records the dropped pointers (`ref: null`, handler `rewind`).
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

from ..agents.developer.documentation_contract import strip_documentation_block
from ..agents.developer.runtime_config import worktree_dirty_paths
from ..core.errors import HarnessError
from ..llm.step_review import _filesystem_path
from ..storage.artifact_index import artifact_scope, artifacts_at, last_visit, read_artifact_index
from ..storage.runtime import utc_now_iso
from .graph import REWIND_STAGES, REWIND_TARGETS, node_for
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4_closure import _file_sha256
from .phase4_documentation import DOCUMENTATION_PATH, _read_text_exact, _sha256
from ..core import run_state

_POLISH_SESSION = re.compile(r"^ui_polish(?:_repair)?_v\d+$")
_STAGE_QUALITY_REPORTS = {
    "journey": ("browser_baseline",),
    "polish": ("ui_polish", "ui_polish_contract", "ui_polish_verification"),
    "qa": ("qa_acceptance",),
    "documentation": ("documentation_closure",),
    "final_verification": ("final_verification",),
    "publication": (),
}


class PhaseFourRewind:
    """Owner action `rewind_run`; the stage hooks below are the only per-stage knowledge it needs."""

    def rewind_run(self, *, run_id: str, node: str, reason: str) -> dict[str, object]:
        if not str(reason or "").strip():
            raise HarnessError("rewind_reason_required", "A rewind needs the owner's reason.", run_id=run_id)
        target = REWIND_TARGETS.get(node)
        if target is None:
            raise HarnessError(
                "rewind_target_not_supported",
                f"Cannot rewind to {node!r}; supported nodes: {sorted(REWIND_TARGETS)}.",
                run_id=run_id,
            )
        with self.workspace_manager.locked_workspace(run_id) as workspace:
            state = self.store.read_state(run_id)
            self._require_frozen_llm_mode(state, run_id=run_id)
            status = str(state.get("status") or "")
            current = node_for(status)
            if current is None or current.kind == "terminal" or status.startswith("code_review"):
                raise HarnessError(
                    "rewind_not_applicable",
                    f"A run at {status!r} cannot be rewound: it is finished or already published.",
                    run_id=run_id,
                )
            run_dir = self.store.run_dir(run_id)
            entries = read_artifact_index(run_dir)
            abandoned = [
                (int(item["index_cutoff"]), int(item["index_position"]))
                for item in state.get("recoveries") or []
                if isinstance(item, dict) and item.get("code") == "rewind" and "index_cutoff" in item
            ]
            cutoff = last_visit(entries, node, excluded=abandoned)
            if cutoff is None:
                raise HarnessError(
                    "rewind_target_not_reached",
                    f"The artifact index has no step of {node!r} on the run's current branch: the run has not "
                    "reached it since provenance is recorded or since an earlier rewind went back before it.",
                    run_id=run_id,
                )
            kept_at_cutoff = artifacts_at(entries, cutoff)
            written_since = {str(entry.get("key") or "") for entry in entries[cutoff:]}
            latest = dict(_string_dict(state.get("latest_artifacts")))
            dropped = {
                key: ref for key, ref in latest.items() if key in written_since and key not in kept_at_cutoff
            }

            rewound = copy.deepcopy(state)
            target_path = Path(workspace.worktree_path)
            plan: dict[str, object] = {"restores": {}, "sessions": []}
            first = REWIND_STAGES.index(target.stage)
            for position, stage in enumerate(REWIND_STAGES[first:]):
                reset = target.reset if position == 0 else "restart"
                getattr(self, f"_rewind_{stage}")(rewound, reset=reset, run_id=run_id, target_path=target_path, plan=plan)
                quality = dict(_dict_value(rewound.get("quality_reports")))
                for key in _STAGE_QUALITY_REPORTS[stage]:
                    quality.pop(key, None)
                rewound["quality_reports"] = quality

            backup = self._rewind_backup(run_dir, node=node)
            restored = _apply_restores(target_path, plan["restores"])
            sessions = _supersede_sessions(run_dir, plan["sessions"])
            if target.browser_status:
                browser = dict(run_state.browser_stages(rewound))
                browser.pop("blocked_from", None)
                rewound[run_state.BROWSER_STAGES] = {**browser, "status": target.browser_status}
            recovered_at = utc_now_iso()
            rewound.setdefault("recoveries", []).append(
                {
                    "code": "rewind",
                    "node": node,
                    "from_status": status,
                    "reason": reason,
                    "dropped_latest_artifacts": dropped,
                    "restored_paths": restored,
                    "superseded_session_dirs": sessions,
                    "state_backup": self.store.relative(backup),
                    # Index lines in [index_cutoff, index_position) are the branch this rewind abandoned.
                    "index_cutoff": cutoff,
                    "index_position": len(entries),
                    "recovered_at": recovered_at,
                }
            )
            rewound.update(
                {
                    "updated_at": recovered_at,
                    "status": node,
                    "current_phase": node_for(node).phase,
                    "latest_artifacts": {key: ref for key, ref in latest.items() if key not in dropped},
                    "pending_step_review": None,
                    "pending_checkpoint": None,
                    "browser_stage_resume": None,
                    "runtime_closure_resume": None,
                    "last_error": None,
                    "allowed_next_actions": ["continue_run", "get_status", "cancel_run"],
                    "summary_for_user": f"Run {run_id} quay lại {node}: {reason}",
                }
            )
            # Not a graph node: the dropped pointers must never look like a visit of `node` to a later rewind.
            with artifact_scope(run_id, node="rewind", handler="rewind_run", repo_root=self.store.repo_root):
                self.store.write_state(rewound)
            return self._status_response(rewound, created_new_run=False)

    def _rewind_backup(self, run_dir: Path, *, node: str) -> Path:
        audit = run_dir / "audit"
        audit.mkdir(exist_ok=True)
        stamp = utc_now_iso().replace(":", "").replace("-", "")
        backup = audit / f"state.before-rewind-{node}-{stamp}.json"
        ordinal = 2
        while backup.exists():
            backup = audit / f"state.before-rewind-{node}-{stamp}-{ordinal}.json"
            ordinal += 1
        shutil.copyfile(run_dir / "state.json", backup)
        return backup

    # ------------------------------------------------------------------ stage hooks (graph.REWIND_STAGES)
    # Each hook resets the pin its stage owns in `state` and adds the worktree restores it needs to `plan`; it
    # never touches the disk, so a refusal leaves the run exactly as it was.

    def _rewind_journey(self, state, *, reset, run_id, target_path, plan) -> None:
        browser = dict(run_state.browser_stages(state))
        journey = _dict_value(browser.get("journey"))
        if not journey:
            return
        # Code repairs that landed stay in the worktree, so their paths stay allowed (r9 journey_planning_restart).
        kept = {key: journey[key] for key in ("review_attempt", "repair_unit_ids", "repair_paths") if key in journey}
        browser["journey"] = {**kept, "status": "ready", "attempt": int(journey.get("attempt") or 1) + 1}
        browser["journey_patch_iterations"] = 0
        state[run_state.BROWSER_STAGES] = browser

    def _rewind_polish(self, state, *, reset, run_id, target_path, plan) -> None:
        browser = dict(run_state.browser_stages(state))
        polish = _dict_value(browser.get("polish"))
        if not polish:
            return
        if polish.get("source_baseline"):
            plan["restores"].update(self._polish_restores(polish, run_id=run_id, target_path=target_path))
            plan["sessions"].append(_POLISH_SESSION)
        browser["polish"] = {
            "status": "restarted",
            "attempt": int(polish.get("attempt") or 1) + 1,
            **({"result_review_attempt": polish["result_review_attempt"]} if "result_review_attempt" in polish else {}),
        }
        browser["repair_iterations"] = {**_dict_value(browser.get("repair_iterations")), "polish": 0}
        state[run_state.BROWSER_STAGES] = browser

    def _polish_restores(self, polish, *, run_id, target_path) -> dict[str, bytes | None]:
        """Polish's source baseline, once it is proven that no one changed a Polish file after Polish verified it."""

        def read(ref: str) -> dict[str, object]:
            return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)

        baseline = [item for item in read(str(polish["source_baseline"])).get("files", []) if isinstance(item, dict)]
        verification_ref = str(polish.get("verification") or "")
        fingerprint = _dict_value(read(verification_ref).get("worktree_fingerprint")) if verification_ref else {}
        if not fingerprint:
            raise HarnessError(
                "rewind_worktree_unverified",
                "UI Polish has no verified worktree fingerprint, so the Harness cannot prove its files hold only "
                "Polish's own edits; finish or block Polish verification first.",
                run_id=run_id,
            )
        verified = {str(item.get("path") or ""): item for item in fingerprint.get("files", []) if isinstance(item, dict)}
        dirty = set(worktree_dirty_paths(target_path))
        changed = []
        restores: dict[str, bytes | None] = {}
        for item in baseline:
            path = str(item.get("path") or "")
            current = _file_sha256(target_path / path) or None
            expected = verified.get(path)
            if expected is None:
                ok = path not in dirty  # Polish verified this file unchanged from the base commit
            else:
                ok = current == ((expected.get("sha256") or None) if expected.get("exists") else None)
            if not ok:
                changed.append(path)
                continue
            restores[path] = _baseline_bytes(item, run_id=run_id)
        if changed:
            raise HarnessError(
                "rewind_worktree_changed",
                f"Polish files changed after UI Polish verified them: {sorted(changed)}; restoring the Polish "
                "baseline would overwrite those edits.",
                run_id=run_id,
            )
        return restores

    def _rewind_qa(self, state, *, reset, run_id, target_path, plan) -> None:
        browser = dict(run_state.browser_stages(state))
        qa = _dict_value(browser.get("qa"))
        if not qa:
            return
        if reset == "rerun":
            browser["qa"] = {**qa, "status": "written"}
        else:
            # The review cache is keyed by prompt content; the evidence is captured again.
            browser["qa"] = {
                "status": "ready",
                "attempt": int(qa.get("attempt") or 1) + 1,
                **({"reviews": qa["reviews"]} if qa.get("reviews") else {}),
            }
        state[run_state.BROWSER_STAGES] = browser

    def _rewind_documentation(self, state, *, reset, run_id, target_path, plan) -> None:
        pin = run_state.documentation_closure(state)
        if not pin:
            return
        if pin.get("artifact"):
            plan["restores"][DOCUMENTATION_PATH] = self._readme_before_documentation(
                pin, run_id=run_id, target_path=target_path
            )
        # The lane stays pinned to the run; only the drafted section goes.
        state[run_state.DOCUMENTATION_CLOSURE] = {
            key: pin[key] for key in ("mode", "contract_version", "pinned_at") if key in pin
        }

    def _readme_before_documentation(self, pin, *, run_id, target_path) -> bytes | None:
        readme = target_path / DOCUMENTATION_PATH
        current = _read_text_exact(readme)
        if _sha256(current) != pin.get("readme_sha256"):
            raise HarnessError(
                "rewind_worktree_changed",
                f"{DOCUMENTATION_PATH} changed after Documentation Closure wrote it; removing the section would "
                "overwrite those edits.",
                run_id=run_id,
            )
        artifact = _read_json_artifact(self._existing_artifact_path(str(pin["artifact"]), run_id=run_id), run_id=run_id)
        stripped = strip_documentation_block(current)
        for ending in ("", "\n", "\r\n", "\n\n", "\r\n\r\n"):
            original = stripped + ending if stripped else ending
            if _sha256(original) == artifact.get("readme_sha256_before"):
                if original or _tracked_at_head(target_path, DOCUMENTATION_PATH):
                    return original.encode("utf-8")
                return None  # there was no README before Documentation Closure
        raise HarnessError(
            "rewind_worktree_unrestorable",
            f"Cannot rebuild the {DOCUMENTATION_PATH} Documentation Closure started from (sha256 mismatch).",
            run_id=run_id,
        )

    def _rewind_final_verification(self, state, *, reset, run_id, target_path, plan) -> None:
        state.pop("final_verification", None)

    def _rewind_publication(self, state, *, reset, run_id, target_path, plan) -> None:
        state.pop("publication", None)


def _baseline_bytes(item: dict[str, object], *, run_id: str) -> bytes | None:
    """The exact bytes a Polish baseline entry names (its text was read with universal newlines)."""

    if not item.get("exists"):
        return None
    content = str(item.get("content") or "")
    for data in (content.encode("utf-8"), content.replace("\n", "\r\n").encode("utf-8")):
        if _sha256_bytes(data) == item.get("sha256"):
            return data
    raise HarnessError(
        "rewind_worktree_unrestorable",
        f"Cannot reproduce the recorded bytes of {item.get('path')} from the Polish baseline.",
        run_id=run_id,
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tracked_at_head(target_path: Path, path: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{path}"], cwd=str(target_path), capture_output=True, check=False
    )
    return completed.returncode == 0


def _apply_restores(target_path: Path, restores: dict[str, bytes | None]) -> list[str]:
    """Write every restore or none: on a failure the files already written get their previous bytes back."""

    previous = {
        path: (target_path / path).read_bytes() if (target_path / path).is_file() else None for path in restores
    }
    written: list[str] = []
    try:
        for path, data in restores.items():
            file = target_path / path
            if data is None:
                file.unlink(missing_ok=True)
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
            written.append(path)
    except OSError as exc:
        for path in written:
            file = target_path / path
            if previous[path] is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(previous[path])
        raise HarnessError("rewind_restore_failed", f"Cannot restore {path}: {exc}") from exc
    return sorted(restores)


def _supersede_sessions(run_dir: Path, patterns: list[re.Pattern[str]]) -> dict[str, str]:
    """Move the Developer sessions of the reset stages aside so the next attempt starts a fresh session."""

    sessions_dir = run_dir / "developer_execution"
    moved: dict[str, str] = {}
    if not patterns or not sessions_dir.is_dir():
        return moved
    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir() or not any(pattern.fullmatch(entry.name) for pattern in patterns):
            continue
        name, ordinal = f"{entry.name}.superseded-rewind", 2
        while _filesystem_path(sessions_dir / name).exists():
            name, ordinal = f"{entry.name}.superseded-rewind-{ordinal}", ordinal + 1
        _filesystem_path(entry).rename(_filesystem_path(sessions_dir / name))
        moved[f"developer_execution/{entry.name}"] = f"developer_execution/{name}"
    return moved
