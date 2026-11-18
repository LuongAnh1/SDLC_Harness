"""Documentation Closure (L5.5.2): after `qa_tested`, a Harness-owned README section and an owner checkpoint.

```text
qa_tested (documentation closure pinned enforce)
  -> build the section from proven evidence (no LLM) -> gates DC1-DC5 -> write README.md
  -> waiting_documentation_review (owner) -> documentation_approved
```
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from ..agents.developer.documentation_contract import (
    DOCUMENTATION_PATH,
    documentation_context,
    documentation_rule_issues,
    parse_env_example,
    proven_commands,
    render_documentation_block,
    splice_documentation_block,
)
from ..core.errors import HarnessError
from ..profiles import get_project_profile
from ..profiles.verification import build_browser_stage_policy
from ..storage.runtime import utc_now_iso
from ..verification.registry import (
    BROWSER_CAPTURE_SPEC_PATH,
    RUNTIME_PROBE_SPEC_PATH,
    resolve_verification_stage_specs,
)
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import _dict_items
from .phase4_closure import _next_closure_artifact_version, _write_closure_json, _write_closure_text

DOCUMENTATION_CLOSURE_CONTRACT_VERSION = "documentation_closure_v1"
DOCUMENTATION_CHECKPOINT_TYPE = "documentation_closure_review"
ALLOWED_DOCUMENTATION_REVIEW_ACTIONS = ["approve_checkpoint", "get_status", "cancel_run"]
ALLOWED_DOCUMENTATION_AUTOMATION_ACTIONS = ["continue_run", "get_status", "cancel_run"]
_DOCUMENTATION_PHASE = "documentation_closure"
# Harness-rendered specs: run evidence, never committed to the target branch (owner D4).
HARNESS_EVIDENCE_PATHS = (RUNTIME_PROBE_SPEC_PATH, BROWSER_CAPTURE_SPEC_PATH)


class PhaseFourDocumentationCycle:
    """Own `qa_tested` -> `documentation_approved` for runs pinned to Documentation Closure."""

    def _documentation_pin(self, state: dict[str, object]) -> dict[str, object]:
        return _dict_value(state.get("documentation_closure"))

    def _continue_documentation_closure(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        # A redraft needs a fresh owner approval: an earlier approval never carries over to new bytes.
        pin = {
            key: value
            for key, value in self._documentation_pin(state).items()
            if key not in {"approved_checkpoint_id", "approved_at"}
        } or {
            "mode": "enforce",
            "contract_version": DOCUMENTATION_CLOSURE_CONTRACT_VERSION,
            "pinned_at": utc_now_iso(),
        }
        state["documentation_closure"] = {**pin, "status": "drafting"}
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            target_path: Path = inputs["target_path"]
            browser = _dict_value(state.get("browser_stages"))
            qa = _dict_value(browser.get("qa"))
            refs = {
                "verification": str(qa.get("verification") or ""),
                "reviews": str(qa.get("reviews") or ""),
                "plan": str(_dict_value(browser.get("journey")).get("plan") or ""),
                "readiness": str(_dict_value(browser.get("readiness")).get("context") or ""),
            }
            missing = sorted(key for key, ref in refs.items() if not ref)
            if missing:
                raise HarnessError(
                    "documentation_closure_evidence_missing",
                    f"Documentation Closure needs passed browser-stage evidence; missing: {missing}.",
                    run_id=run_id,
                )
            def read(ref: str) -> dict[str, object]:
                return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)

            stage_results = _dict_items(_dict_value(read(refs["verification"]).get("verification")).get("stage_results"))
            policy = build_browser_stage_policy(project=get_project_profile(inputs["planning_policy"].project_profile_id))
            specs = [spec.evidence() for spec in resolve_verification_stage_specs(policy, run_id=run_id)]
            env_example = self._documentation_env_example(latest_artifacts, target_path=target_path, run_id=run_id)
            demo_flag_env_key = str(read(refs["readiness"]).get("demo_flag_env_key") or "")
            context = documentation_context(
                run_id=run_id,
                issue_key=str(inputs["frozen_packet"].get("issue_key") or ""),
                source_verification=refs["verification"],
                specs=specs,
                stage_results=stage_results,
                env_example=env_example,
                demo_flag_env_key=demo_flag_env_key,
                scenario_reviews=_dict_items(read(refs["reviews"]).get("reviews")),
                not_reachable=_dict_items(read(refs["plan"]).get("not_reachable")),
            )
            block = render_documentation_block(context)
            readme_path = target_path / DOCUMENTATION_PATH
            original = _read_text_exact(readme_path)
            updated = splice_documentation_block(original, block)
            issues = documentation_rule_issues(
                context=context,
                block=block,
                original_readme=original,
                new_readme=updated,
                proven=proven_commands(specs=specs, stage_results=stage_results),
                env_example=env_example,
                demo_flag_env_key=demo_flag_env_key,
            )
            if issues:
                raise HarnessError(
                    "documentation_closure_gate_failed",
                    "; ".join(f"{item['gate']}: {item['issue']}" for item in issues),
                    run_id=run_id,
                )
            with open(readme_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
            version = _next_closure_artifact_version(self.store, run_id, "documentation_closure")
            artifact = {
                "schema_version": DOCUMENTATION_CLOSURE_CONTRACT_VERSION,
                "run_id": run_id,
                "version": version,
                "path": DOCUMENTATION_PATH,
                "context": context,
                "block": block,
                "readme_sha256_before": _sha256(original),
                "readme_sha256_after": _sha256(updated),
                "gates": {"ok": True, "gate_ids": ["DC1", "DC2", "DC3", "DC4", "DC5"], "issues": []},
            }
            artifact_ref = _write_closure_json(self.store, run_id, f"documentation_closure_v{version}.json", artifact)
            checkpoint_ref = _write_closure_text(
                self.store,
                run_id,
                f"documentation_closure_checkpoint_v{version}.md",
                _documentation_checkpoint_markdown(
                    run_id=run_id, version=version, original=original, updated=updated, context=context
                ),
            )
        except HarnessError as exc:
            state["documentation_closure"] = {**self._documentation_pin(state), "status": "blocked"}
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": _DOCUMENTATION_PHASE,
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Documentation Closure bị chặn cho run {run_id}: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts.update(
            {"documentation_closure": artifact_ref, "documentation_closure_checkpoint": checkpoint_ref}
        )
        state["documentation_closure"] = {
            **self._documentation_pin(state),
            "status": "waiting_review",
            "version": version,
            "artifact": artifact_ref,
            "readme_sha256": artifact["readme_sha256_after"],
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _DOCUMENTATION_PHASE,
                "status": "waiting_documentation_review",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": f"checkpoint_documentation_closure_{run_id}_v{version}",
                    "type": DOCUMENTATION_CHECKPOINT_TYPE,
                    "artifact_path": checkpoint_ref,
                    "documentation_path": artifact_ref,
                },
                "allowed_next_actions": ALLOWED_DOCUMENTATION_REVIEW_ACTIONS,
                "last_error": None,
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "documentation_closure": {
                        "ok": True,
                        "version": version,
                        "artifact": artifact_ref,
                        "command_count": sum(len(item["commands"]) for item in context["commands"]),
                        "env_count": len(context["env"]),
                    },
                },
                "summary_for_user": (
                    f"Documentation Closure v{version} cho run {run_id} đã ghi mục README do Harness quản lý; "
                    "đang chờ anh duyệt."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _documentation_publication_paths(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
    ) -> tuple[list[str], list[str]]:
        """(paths to add to the publication manifest, Harness evidence kept out of it).

        Adds the env file the approved runtime configuration owns, the package files the dependency tool owns
        (owner D5) and the approved README section (D2).
        """

        pin = self._documentation_pin(state)
        added: set[str] = set()
        receipt_ref = str(_string_dict(state.get("latest_artifacts")).get("runtime_config_execution") or "")
        if receipt_ref:
            receipt = _read_json_artifact(self._existing_artifact_path(receipt_ref, run_id=run_id), run_id=run_id)
            added.add(str(receipt.get("path") or ""))
        for requirement in _dict_items(work_packet.get("package_dependency_requirements")):
            added.add(str(requirement.get("consumer_manifest_path") or requirement.get("manifest_path") or ""))
            added.add(str(requirement.get("lockfile_path") or "package-lock.json"))
        if pin.get("approved_checkpoint_id"):
            added.add(DOCUMENTATION_PATH)
        return sorted(path.replace("\\", "/") for path in added if path), list(HARNESS_EVIDENCE_PATHS)

    def _documentation_env_example(
        self,
        latest_artifacts: dict[str, str],
        *,
        target_path: Path,
        run_id: str,
    ) -> dict[str, str]:
        """Keys and values of the env file the approved runtime configuration materialized, if any."""

        receipt_ref = str(latest_artifacts.get("runtime_config_execution") or "")
        if not receipt_ref:
            return {}
        receipt = _read_json_artifact(self._existing_artifact_path(receipt_ref, run_id=run_id), run_id=run_id)
        path = str(receipt.get("path") or "")
        env_file = target_path / path
        return parse_env_example(_read_text_exact(env_file)) if path and env_file.is_file() else {}

    def approve_documentation_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != "waiting_documentation_review":
                raise HarnessError(
                    "checkpoint_not_current", f"Run is not waiting for documentation review: {run_id}", run_id=run_id
                )
            checkpoint = _dict_value(state.get("pending_checkpoint"))
            current_id = str(checkpoint.get("checkpoint_id") or "")
            if checkpoint.get("type") != DOCUMENTATION_CHECKPOINT_TYPE or (checkpoint_id and checkpoint_id != current_id):
                raise HarnessError("checkpoint_not_current", "This is not the current documentation checkpoint.", run_id=run_id)
            pin = self._documentation_pin(state)
            target_path = self._browser_inputs(state=state, run_id=run_id)["target_path"]
            # The owner approves the exact bytes the gates checked; an edit after drafting needs a new draft.
            if _sha256(_read_text_exact(target_path / DOCUMENTATION_PATH)) != pin.get("readme_sha256"):
                raise HarnessError(
                    "documentation_changed",
                    f"{DOCUMENTATION_PATH} changed after Documentation Closure drafted it.",
                    run_id=run_id,
                )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": _DOCUMENTATION_PHASE,
                    "status": "documentation_approved",
                    "pending_checkpoint": None,
                    "documentation_closure": {
                        **pin,
                        "status": "approved",
                        "approved_checkpoint_id": current_id,
                        "approved_at": utc_now_iso(),
                    },
                    "allowed_next_actions": ALLOWED_DOCUMENTATION_AUTOMATION_ACTIONS,
                    "last_error": None,
                    "summary_for_user": f"Anh đã duyệt mục README cho run {run_id}.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)


def _read_text_exact(path: Path) -> str:
    """Text with its own newline bytes (no universal-newline translation)."""

    if not path.is_file():
        return ""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _documentation_checkpoint_markdown(
    *,
    run_id: str,
    version: int,
    original: str,
    updated: str,
    context: dict[str, object],
) -> str:
    diff = difflib.unified_diff(
        original.splitlines(),
        updated.splitlines(),
        fromfile=f"a/{DOCUMENTATION_PATH}",
        tofile=f"b/{DOCUMENTATION_PATH}",
        lineterm="",
    )
    return "\n".join(
        [
            f"# Documentation Closure v{version}",
            "",
            f"Run `{run_id}`. The Harness wrote only the marked section of `{DOCUMENTATION_PATH}` from verification "
            "that passed; no LLM wrote it. Approve to make it part of the change; Final Verification then reruns "
            "every listed command.",
            "",
            f"- Commands: {sum(len(item['commands']) for item in _dict_items(context.get('commands')))}",
            f"- Environment variables: {len(_dict_items(context.get('env')))}",
            f"- Source verification: `{context.get('source_verification')}`",
            "",
            "## Diff",
            "",
            "```diff",
            *diff,
            "```",
            "",
        ]
    )
