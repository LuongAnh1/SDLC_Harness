"""Chạy các stage kiểm chứng của target repo cho Phase 4.

Verification Runner là lớp công cụ, không phải AI Tester. Nó nhận
DeveloperWorkPacket đã được Phase 4 chuẩn bị hoặc refresh. Runner chỉ dùng stage
policy từ snapshot và executable registry đã review. Kết quả có cấu trúc được ghi
vào `test_report_v{n}.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from ..core.errors import HarnessError
from ..events import emit_current
from ..profiles import (
    QaTestPolicy,
    VerificationStagePolicy,
    is_profiled_work_packet_schema,
    profiled_execution_field_paths,
)
from .registry import (
    BROWSER_CAPTURE_SPEC_PATH,
    REPOSITORY_E2E_ROOT,
    PROFILED_ENV_ALLOWLIST,
    PROFILED_FIXED_ENV,
    RUNTIME_PROBE_SPEC_PATH,
    VerificationPreparationSpec,
    VerificationStageSpec,
    resolve_verification_preparation_specs,
    resolve_verification_stage_specs,
)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20000
RUNTIME_PROBE_ANNOTATION_TYPE = "runtime_probe"
BROWSER_CAPTURE_ANNOTATION_TYPE = "browser_capture"
# Playwright failure text kept per failed result and in total, so a browser failure stays diagnosable.
MAX_FAILURE_MESSAGE_CHARS = 600
MAX_FAILURE_DETAIL_CHARS = 4000
_PROBE_ID = re.compile(r"^RP-[1-9][0-9]*$")
_VIEW_ID = re.compile(r"^(V-0|BV-[0-9a-f]{12})$")
_VIEWPORT_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAND_IN_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Keys the runner owns; a contract-declared stand-in may never override them.
_RESERVED_ENV_KEYS = {
    *(key.casefold() for key in PROFILED_ENV_ALLOWLIST),
    *(key.casefold() for key, _ in PROFILED_FIXED_ENV),
    "compose_project_name",
    "sdlc_postgres_port",
    "database_url",
}
_RESERVED_ENV_PREFIXES = ("node_", "npm_", "playwright_", "postgres_")
# Run-scoped database identity; Compose creates it and DATABASE_URL points at it.
_RUN_DATABASE_USER = "postgres"
_RUN_DATABASE_PASSWORD = "postgres"
_RUN_DATABASE_NAME = "sdlc_app"
_RUN_SCOPED_STAGE_IDS = {
    "database_ready",
    "migrate_and_seed",
    "api_database_integration",
    "service_health",
    "browser_e2e",
    "repository_e2e",
    "runtime_probe",
    "browser_capture",
    "cleanup",
}


@dataclass(frozen=True)
class RuntimeProbeRun:
    """What the runtime_probe stage needs from an approved closure contract (IDs only, no argv/env values)."""

    probe_ids: tuple[str, ...]
    stand_in_env_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserCaptureRun:
    """What the browser_capture stage needs: view/viewport IDs and Harness-owned facts, no argv."""

    view_ids: tuple[str, ...]
    viewport_ids: tuple[str, ...]
    stand_in_env_keys: tuple[str, ...] = ()
    # Build-time flag set to "1" for this stage only; empty when the run has no demo module.
    demo_flag_env_key: str = ""
    # R3: none of these may appear in the production build that frontend_build produced.
    forbidden_build_literals: tuple[str, ...] = ()
    build_output_dir: str = ""
    # Screenshots are copied here right after the stage; Playwright wipes test-results/ every run.
    evidence_dir: Path | None = None


@dataclass(frozen=True)
class VerificationStageResult:
    """Bằng chứng thực thi của một stage profile-owned."""

    stage_id: str
    status: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    reason: str
    failure_kind: str = ""
    timed_out: bool = False
    selected_test_files: tuple[str, ...] = ()
    verified_test_files: tuple[str, ...] = ()
    probe_results: tuple[dict[str, object], ...] = ()
    capture_results: tuple[dict[str, object], ...] = ()
    build_scan: dict[str, object] | None = None
    # Env the stage ran with, by source; names only except reviewed constants (L5.5.1, Documentation Closure).
    env_keys: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "stage_id": self.stage_id,
            "status": self.status,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "reason": self.reason,
            "failure_kind": self.failure_kind,
            "timed_out": self.timed_out,
            "selected_test_files": list(self.selected_test_files),
            "verified_test_files": list(self.verified_test_files),
        }
        if self.probe_results:
            payload["probe_results"] = [dict(item) for item in self.probe_results]
        if self.capture_results:
            payload["capture_results"] = [dict(item) for item in self.capture_results]
        if self.build_scan is not None:
            payload["build_scan"] = dict(self.build_scan)
        if self.env_keys is not None:
            payload["env_keys"] = dict(self.env_keys)
        return payload


@dataclass(frozen=True)
class VerificationResult:
    """Kết quả chạy compile/unit test command của target repo."""

    status: str
    command: str
    cwd: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    reason: str
    failure_kind: str = ""
    timed_out: bool = False
    discovered_test_entrypoints: tuple[str, ...] = ()
    executed_test_entrypoints: tuple[str, ...] = ()
    planned_stage_ids: tuple[str, ...] = ()
    executed_stage_ids: tuple[str, ...] = ()
    stage_results: tuple[VerificationStageResult, ...] = ()
    preparation_results: tuple[VerificationStageResult, ...] = ()
    selected_test_files: tuple[str, ...] = ()
    verified_test_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "reason": self.reason,
            "failure_kind": self.failure_kind,
            "timed_out": self.timed_out,
            "discovered_test_entrypoints": list(self.discovered_test_entrypoints),
            "executed_test_entrypoints": list(self.executed_test_entrypoints),
            "planned_stage_ids": list(self.planned_stage_ids),
            "executed_stage_ids": list(self.executed_stage_ids),
            "stage_results": [item.to_dict() for item in self.stage_results],
            "preparation_results": [item.to_dict() for item in self.preparation_results],
            "selected_test_files": list(self.selected_test_files),
            "verified_test_files": list(self.verified_test_files),
        }


class VerificationRunner:
    """Chạy ordered profile stages và phân loại kết quả."""

    def __init__(self, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = max(1, timeout_seconds)

    def run(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        qa_policy: QaTestPolicy | None = None,
        runtime_probe: RuntimeProbeRun | None = None,
        browser_capture: BrowserCaptureRun | None = None,
    ) -> VerificationResult:
        """Run verification and publish bounded result metadata."""

        result = self._run_internal(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=required_test_files,
            verification_policy=verification_policy,
            qa_policy=qa_policy,
            runtime_probe=runtime_probe,
            browser_capture=browser_capture,
        )
        emit_current(
            category="verification",
            event_type="verification_completed",
            message="Verification completed.",
            data={
                "status": result.status,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration_seconds, 3),
                "discovered_tests": len(result.discovered_test_entrypoints),
                "executed_tests": len(result.executed_test_entrypoints),
                "planned_stages": list(result.planned_stage_ids),
                "executed_stages": list(result.executed_stage_ids),
                "selected_test_files": len(result.selected_test_files),
                "verified_test_files": len(result.verified_test_files),
            },
        )
        return result

    def run_demo_flag_stage(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        verification_policy: VerificationStagePolicy,
        stage_id: str,
        demo_flag_env_key: str,
    ) -> VerificationResult:
        """Rerun one reviewed stage with the Harness demo flag set to "1" (L5.5.3 F3: the documented demo build).

        Only the flag is added; argv, cwd and every other env still come from the registry.
        """

        if stage_id not in verification_policy.stage_ids:
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Demo-flag stage is not in the verification policy: {stage_id}",
                run_id=str(work_packet.get("run_id") or "") or None,
            )
        return self._run_profiled_stages(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=None,
            verification_policy=verification_policy,
            qa_policy=None,
            selected_stage_ids=(stage_id,),
            demo_flag_stage=(stage_id, demo_flag_env_key),
        )

    def run_stage(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        stage_id: str,
        required_test_files: list[str] | None = None,
        verification_policy: VerificationStagePolicy | None = None,
    ) -> VerificationResult:
        """Run one approved stage for the current Developer execution unit."""

        run_id = str(work_packet.get("run_id") or "") or None
        if verification_policy is None or stage_id not in verification_policy.stage_ids:
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Developer unit requested an unapproved verification stage: {stage_id}",
                run_id=run_id,
            )
        if verification_policy.owner_for_stage(stage_id) != "developer_intent":
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Developer unit may not run stage owned by {verification_policy.owner_for_stage(stage_id)}: {stage_id}",
                run_id=run_id,
            )
        selected_spec = next(
            (
                spec
                for spec in resolve_verification_stage_specs(verification_policy, run_id=run_id)
                if spec.stage_id == stage_id
            ),
            None,
        )
        if selected_spec is None or not selected_spec.developer_selectable:
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Developer unit may not run outer verification stage: {stage_id}",
                run_id=run_id,
            )
        result = self._run_profiled_stages(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=required_test_files,
            verification_policy=verification_policy,
            qa_policy=None,
            selected_stage_ids=(stage_id,),
            developer_unit_scope=True,
        )
        emit_current(
            category="verification",
            event_type="developer_unit_verification_completed",
            message="Developer unit verification stage completed.",
            data={
                "stage_id": stage_id,
                "status": result.status,
                "failure_kind": result.failure_kind,
                "selected_test_files": list(result.selected_test_files),
                "verified_test_files": list(result.verified_test_files),
            },
        )
        return result

    def _run_internal(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        qa_policy: QaTestPolicy | None = None,
        runtime_probe: RuntimeProbeRun | None = None,
        browser_capture: BrowserCaptureRun | None = None,
    ) -> VerificationResult:
        """Chạy profile-owned stages và fail closed nếu thiếu snapshot policy."""

        if verification_policy is None:
            raise HarnessError(
                "verification_stage_policy_invalid",
                "Verification requires the immutable run stage policy.",
                run_id=str(work_packet.get("run_id") or "") or None,
            )
        return self._run_profiled_stages(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=required_test_files,
            verification_policy=verification_policy,
            qa_policy=qa_policy,
            runtime_probe=runtime_probe,
            browser_capture=browser_capture,
        )

    def _run_profiled_stages(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None,
        verification_policy: VerificationStagePolicy,
        qa_policy: QaTestPolicy | None,
        selected_stage_ids: tuple[str, ...] | None = None,
        developer_unit_scope: bool = False,
        runtime_probe: RuntimeProbeRun | None = None,
        browser_capture: BrowserCaptureRun | None = None,
        demo_flag_stage: tuple[str, str] | None = None,
    ) -> VerificationResult:
        """Chạy ordered smoke suite bằng registry, không đọc executable data từ packet."""

        run_id = str(work_packet.get("run_id") or "") or None
        specs = resolve_verification_stage_specs(verification_policy, run_id=run_id)
        planned = {spec.stage_id for spec in specs}
        # Harness-owned env reaches exactly one stage each; no other stage sees these keys.
        stage_extra_env = {
            "runtime_probe": (
                _runtime_probe_stand_in_env(runtime_probe, run_id=run_id) if "runtime_probe" in planned else {}
            ),
            "browser_capture": (
                _browser_capture_env(browser_capture, run_id=run_id) if "browser_capture" in planned else {}
            ),
        }
        constant_keys = {browser_capture.demo_flag_env_key} if browser_capture is not None else set()
        if demo_flag_stage is not None:
            demo_stage_id, demo_key = demo_flag_stage
            _require_harness_env_key(demo_key, taken=stage_extra_env.get(demo_stage_id) or {}, run_id=run_id)
            stage_extra_env[demo_stage_id] = {**(stage_extra_env.get(demo_stage_id) or {}), demo_key: "1"}
            constant_keys.add(demo_key)
        if selected_stage_ids is not None:
            selected = set(selected_stage_ids)
            specs = tuple(spec for spec in specs if spec.stage_id in selected)
            if tuple(spec.stage_id for spec in specs) != selected_stage_ids:
                raise HarnessError(
                    "verification_stage_policy_invalid",
                    "Developer unit stage selection does not match the immutable verification policy.",
                    run_id=run_id,
                )
        preparation_specs = resolve_verification_preparation_specs(verification_policy, run_id=run_id)
        _validate_profiled_work_packet(
            work_packet,
            verification_policy=verification_policy,
            run_id=run_id,
        )
        planned_stage_ids = tuple(spec.stage_id for spec in specs)
        target_root = target_path.resolve()
        if not target_root.is_dir():
            return _profiled_preflight_failure(
                target_path=target_root,
                planned_stage_ids=planned_stage_ids,
                reason="Verification target repository does not exist or is not a directory.",
            )
        selected_test_files = _profiled_required_test_files(
            target_root=target_root,
            required_test_files=required_test_files or [],
            work_packet=work_packet,
            verification_policy=verification_policy,
            qa_policy=qa_policy,
            run_id=run_id,
            developer_unit_scope=developer_unit_scope,
        )
        scripts, manifest_error = _package_scripts(target_root)
        if manifest_error:
            return _profiled_preflight_failure(
                target_path=target_root,
                planned_stage_ids=planned_stage_ids,
                reason=manifest_error,
                selected_test_files=selected_test_files,
                failure_kind="policy",
            )

        preparation_results: list[VerificationStageResult] = []
        for preparation_spec in preparation_specs:
            preparation_result = _run_profiled_preparation(
                target_root=target_root,
                spec=preparation_spec,
            )
            if preparation_result is None:
                continue
            preparation_results.append(preparation_result)
            if preparation_result.status != "passed":
                return _profiled_result(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    stage_results=[],
                    preparation_results=preparation_results,
                    terminal=preparation_result,
                    selected_test_files=selected_test_files,
                )

        prepared: list[tuple[VerificationStageSpec, Path, str, dict[str, str]]] = []
        for spec in specs:
            required_scripts = {
                value
                for value in (spec.required_package_script, *spec.required_package_scripts)
                if value
            }
            missing_scripts = sorted(required_scripts - scripts)
            if missing_scripts:
                return _profiled_preflight_failure(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    reason=f"package.json is missing required scripts: {missing_scripts}",
                    selected_test_files=selected_test_files,
                    failure_kind="policy",
                )
            stage_cwd = (target_root / spec.cwd).resolve()
            try:
                stage_cwd.relative_to(target_root)
            except ValueError as exc:
                raise HarnessError(
                    "verification_stage_policy_invalid",
                    f"Verification stage cwd escapes target repository: {spec.cwd}",
                    run_id=run_id,
                ) from exc
            if not stage_cwd.is_dir():
                return _profiled_preflight_failure(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    reason=f"Verification stage cwd does not exist: {spec.cwd}",
                    selected_test_files=selected_test_files,
                    failure_kind="policy",
                )
            env = _profiled_verification_env(
                spec,
                run_id=run_id,
                extra_env=stage_extra_env.get(spec.stage_id) or None,
            )
            harness_spec = {
                "runtime_probe": RUNTIME_PROBE_SPEC_PATH if runtime_probe is not None and runtime_probe.probe_ids else "",
                "browser_capture": BROWSER_CAPTURE_SPEC_PATH,
            }.get(spec.stage_id, "")
            if harness_spec and not (target_root / harness_spec).is_file():
                return _profiled_preflight_failure(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    reason=f"Harness-rendered spec is missing for {spec.stage_id}: {harness_spec}",
                    selected_test_files=selected_test_files,
                    failure_kind="policy",
                )
            executable = shutil.which(spec.argv[0], path=_environment_value(env, "PATH") or "")
            if not executable:
                return _profiled_preflight_failure(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    reason=f"Verification executable was not found: {spec.argv[0]}",
                    selected_test_files=selected_test_files,
                    failure_kind="environment",
                )
            prepared.append((spec, stage_cwd, executable, env))
        env_keys_by_stage = {
            spec.stage_id: _stage_env_keys(
                spec,
                run_id=run_id,
                extra_env=stage_extra_env.get(spec.stage_id) or {},
                constant_keys=constant_keys,
            )
            for spec, _, _, _ in prepared
        }

        stage_results: list[VerificationStageResult] = []
        first_failure: VerificationStageResult | None = None
        for spec, stage_cwd, executable, env in prepared:
            if first_failure is not None and not spec.always_run:
                continue
            if spec.stage_id == "runtime_probe" and runtime_probe is not None and not runtime_probe.probe_ids:
                # Every scenario is blocked by an approved unresolved input: nothing to request.
                stage_results.append(
                    VerificationStageResult(
                        stage_id=spec.stage_id,
                        status="passed",
                        argv=(),
                        cwd=spec.cwd,
                        timeout_seconds=spec.timeout_seconds,
                        exit_code=None,
                        duration_seconds=0.0,
                        stdout="",
                        stderr="",
                        reason="No approved runtime probe: every scenario is blocked by an unresolved input.",
                    )
                )
                continue
            if spec.stage_id == "repository_e2e" and not _repository_e2e_specs(target_root):
                stage_results.append(
                    VerificationStageResult(
                        stage_id=spec.stage_id,
                        status="passed",
                        argv=(),
                        cwd=spec.cwd,
                        timeout_seconds=spec.timeout_seconds,
                        exit_code=None,
                        duration_seconds=0.0,
                        stdout="",
                        stderr="",
                        reason="The repository has no Playwright spec of its own.",
                        env_keys=env_keys_by_stage[spec.stage_id],
                    )
                )
                continue
            if spec.stage_id == "runtime_probe":
                stage_selected_files: tuple[str, ...] = (RUNTIME_PROBE_SPEC_PATH,)
            elif spec.stage_id == "repository_e2e":
                stage_selected_files = _repository_e2e_specs(target_root)
            elif spec.stage_id == "browser_capture":
                stage_selected_files = (BROWSER_CAPTURE_SPEC_PATH,)
            elif spec.stage_id == "unit":
                stage_selected_files = tuple(
                    path
                    for path in selected_test_files
                    if qa_policy is None or qa_policy.normalize_test_path(path)
                )
            elif spec.stage_id == "browser_e2e":
                stage_selected_files = tuple(
                    path
                    for path in selected_test_files
                    if qa_policy is not None and qa_policy.normalize_browser_test_path(path)
                )
            else:
                stage_selected_files = ()
            logical_argv = spec.argv
            executable_argv = [executable, *spec.argv[1:]]
            report_directory: tempfile.TemporaryDirectory[str] | None = None
            report_path: Path | None = None
            if stage_selected_files and spec.stage_id == "unit":
                if (
                    not developer_unit_scope
                    and (qa_policy is None or qa_policy.framework != spec.required_test_framework)
                ):
                    raise HarnessError(
                        "qa_test_policy_invalid",
                        "QA framework does not match the reviewed unit-stage reporter.",
                        run_id=run_id,
                    )
                if not spec.machine_reporter_args:
                    raise HarnessError(
                        "verification_stage_policy_invalid",
                        "The profiled unit stage has no machine-readable reporter contract.",
                        run_id=run_id,
                    )
                report_directory = tempfile.TemporaryDirectory(prefix="sdlc-harness-vitest-")
                report_path = Path(report_directory.name) / "report.json"
                logical_reporter_args = tuple(
                    item.replace("{report_path}", "<temporary-vitest-json-report>")
                    for item in spec.machine_reporter_args
                )
                executable_reporter_args = tuple(
                    item.replace("{report_path}", str(report_path))
                    for item in spec.machine_reporter_args
                )
                logical_argv = (
                    *spec.argv,
                    "--",
                    *logical_reporter_args,
                    *stage_selected_files,
                )
                executable_argv = [
                    executable,
                    *spec.argv[1:],
                    "--",
                    *executable_reporter_args,
                    *stage_selected_files,
                ]
            elif stage_selected_files and spec.stage_id in {"browser_e2e", "repository_e2e", "runtime_probe", "browser_capture"}:
                logical_argv = (*spec.argv, "--", *stage_selected_files)
                executable_argv = [executable, *spec.argv[1:], "--", *stage_selected_files]
            start = time.monotonic()
            try:
                command_steps = [
                    (logical_argv, executable_argv),
                    *[
                        (argv, [executable, *argv[1:]])
                        for argv in spec.additional_argv
                    ],
                ]
                completed_steps: list[subprocess.CompletedProcess[str]] = []
                for _, step_argv in command_steps:
                    completed_step = _run_bounded(
                        step_argv,
                        cwd=str(stage_cwd),
                        env=env,
                        timeout=spec.timeout_seconds,
                    )
                    completed_steps.append(completed_step)
                    if completed_step.returncode != 0:
                        break
                completed = completed_steps[-1]
                logical_parts: list[str] = []
                for index, (step, _) in enumerate(command_steps[: len(completed_steps)]):
                    if index:
                        logical_parts.append("&&")
                    logical_parts.extend(step)
                logical_argv = tuple(logical_parts)
            except subprocess.TimeoutExpired as exc:
                stage_result = VerificationStageResult(
                    stage_id=spec.stage_id,
                    status="failed",
                    argv=logical_argv,
                    cwd=spec.cwd,
                    timeout_seconds=spec.timeout_seconds,
                    exit_code=None,
                    duration_seconds=time.monotonic() - start,
                    stdout=_truncate_output(_decode_timeout_output(exc.stdout)),
                    stderr=_truncate_output(_decode_timeout_output(exc.stderr)),
                    reason=f"Verification stage timed out after {spec.timeout_seconds}s: {spec.stage_id}",
                    failure_kind="environment",
                    timed_out=True,
                    selected_test_files=stage_selected_files,
                )
            except OSError as exc:
                stage_result = VerificationStageResult(
                    stage_id=spec.stage_id,
                    status="failed",
                    argv=logical_argv,
                    cwd=spec.cwd,
                    timeout_seconds=spec.timeout_seconds,
                    exit_code=None,
                    duration_seconds=time.monotonic() - start,
                    stdout="",
                    stderr=_truncate_output(str(exc)),
                    reason=f"Verification stage could not start: {spec.stage_id}",
                    failure_kind="environment",
                    selected_test_files=stage_selected_files,
                )
            else:
                combined_stdout = "\n".join(
                    item.stdout for item in completed_steps if item.stdout
                )
                combined_stderr = "\n".join(
                    item.stderr for item in completed_steps if item.stderr
                )
                verified_test_files: tuple[str, ...] = ()
                report_error = ""
                report_failure_kind = ""
                probe_results: tuple[dict[str, object], ...] = ()
                capture_results: tuple[dict[str, object], ...] = ()
                if report_path is not None:
                    verified_test_files, report_error, report_failure_kind = _vitest_verified_test_files(
                        report_path=report_path,
                        target_root=target_root,
                        selected_test_files=stage_selected_files,
                    )
                elif spec.report_kind == "playwright" and stage_selected_files:
                    verified_test_files, report_error, report_failure_kind = _playwright_verified_test_files(
                        report_path=target_root / spec.report_path,
                        target_root=target_root,
                        selected_test_files=stage_selected_files,
                    )
                    if spec.stage_id == "runtime_probe" and runtime_probe is not None:
                        probe_results, probe_error = _runtime_probe_outcomes(
                            report_path=target_root / spec.report_path,
                            target_root=target_root,
                            probe_ids=runtime_probe.probe_ids,
                        )
                        if probe_error:
                            # A missing or duplicated probe is a Harness/policy fault, never a code defect.
                            report_error, report_failure_kind = probe_error, "policy"
                    if spec.stage_id == "browser_capture" and browser_capture is not None:
                        # Read and copy evidence even when a test failed: it is the failure's evidence.
                        capture_results, capture_error = _browser_capture_outcomes(
                            report_path=target_root / spec.report_path,
                            target_root=target_root,
                            capture=browser_capture,
                        )
                        if capture_error and not report_error:
                            report_error, report_failure_kind = capture_error, "policy"
                stage_passed = completed.returncode == 0 and not report_error
                failure_kind = ""
                if not stage_passed:
                    failure_kind = (
                        report_failure_kind
                        if report_error and report_failure_kind
                        else _command_failure_kind(stdout=combined_stdout, stderr=combined_stderr)
                    )
                stage_result = VerificationStageResult(
                    stage_id=spec.stage_id,
                    status="passed" if stage_passed else "failed",
                    argv=logical_argv,
                    cwd=spec.cwd,
                    timeout_seconds=spec.timeout_seconds,
                    exit_code=completed.returncode,
                    duration_seconds=time.monotonic() - start,
                    stdout=_truncate_output(combined_stdout),
                    stderr=_truncate_output(combined_stderr),
                    reason=(
                        f"Verification stage completed successfully: {spec.stage_id}"
                        if stage_passed
                        else report_error
                        or f"Verification stage exited with code {completed.returncode}: {spec.stage_id}"
                    ),
                    failure_kind=failure_kind,
                    selected_test_files=stage_selected_files,
                    verified_test_files=verified_test_files,
                    probe_results=probe_results,
                    capture_results=capture_results,
                )
            finally:
                if report_directory is not None:
                    report_directory.cleanup()
            if (
                spec.stage_id == "frontend_build"
                and stage_result.status == "passed"
                and "browser_capture" in planned
                and browser_capture is not None
            ):
                # R3: the production build must not ship demo fixtures; later browser stages rebuild with the flag.
                stage_result = _with_build_literal_scan(stage_result, target_root=target_root, capture=browser_capture)
            stage_result = replace(stage_result, env_keys=env_keys_by_stage[spec.stage_id])
            stage_results.append(stage_result)
            if stage_result.status != "passed":
                first_failure = first_failure or stage_result

        terminal = first_failure or stage_results[-1]
        return _profiled_result(
            target_path=target_root,
            planned_stage_ids=planned_stage_ids,
            stage_results=stage_results,
            preparation_results=preparation_results,
            terminal=terminal,
            reason=(
                first_failure.reason
                if first_failure is not None
                else "All profile-owned verification stages completed successfully."
            ),
            selected_test_files=selected_test_files,
        )

def _validate_profiled_work_packet(
    work_packet: dict[str, object],
    *,
    verification_policy: VerificationStagePolicy,
    run_id: str | None,
) -> None:
    if not is_profiled_work_packet_schema(work_packet.get("schema_version")):
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Profiled verification requires the profiled Developer Work Packet schema.",
            run_id=run_id,
        )
    actual_stage_ids = tuple(_string_items(work_packet.get("verification_stage_ids")))
    if actual_stage_ids != verification_policy.stage_ids:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Developer Work Packet verification stages do not match the immutable run policy.",
            run_id=run_id,
        )
    test_intents = work_packet.get("test_intents")
    if not isinstance(test_intents, list) or not test_intents:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Profiled Developer Work Packet has no test intent block.",
            run_id=run_id,
        )
    prohibited = profiled_execution_field_paths(test_intents, path="$.test_intents")
    if prohibited:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Profiled Developer Work Packet contains prohibited execution fields: " + ", ".join(prohibited),
            run_id=run_id,
        )
    allowed_stage_ids = set(verification_policy.stage_ids)
    for item in test_intents:
        if not isinstance(item, dict):
            raise HarnessError(
                "verification_stage_policy_invalid",
                "Profiled Developer Work Packet contains an invalid test intent.",
                run_id=run_id,
            )
        stage_id = str(item.get("stage_id") or "").strip()
        if not stage_id or stage_id not in allowed_stage_ids:
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Profiled test intent references an unsupported verification stage: {stage_id}",
                run_id=run_id,
            )
        if verification_policy.owner_for_stage(stage_id) != "developer_intent":
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Developer test intent references a non-Developer stage: {stage_id}",
                run_id=run_id,
            )
    qa_test_intents = work_packet.get("qa_test_intents", [])
    if not isinstance(qa_test_intents, list):
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Profiled Developer Work Packet has an invalid QA intent block.",
            run_id=run_id,
        )
    prohibited = profiled_execution_field_paths(qa_test_intents, path="$.qa_test_intents")
    if prohibited:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Profiled Developer Work Packet QA intents contain prohibited execution fields: "
            + ", ".join(prohibited),
            run_id=run_id,
        )
    for item in qa_test_intents:
        if not isinstance(item, dict) or str(item.get("stage_id") or "").strip() != "browser_e2e":
            raise HarnessError(
                "verification_stage_policy_invalid",
                "Profiled QA intents may reference only the browser_e2e stage.",
                run_id=run_id,
            )
        if "browser_e2e" not in allowed_stage_ids:
            raise HarnessError(
                "verification_stage_policy_invalid",
                "Profiled QA intent references browser_e2e outside the immutable run policy.",
                run_id=run_id,
            )
        if verification_policy.owner_for_stage("browser_e2e") != "qa_intent":
            raise HarnessError(
                "verification_stage_policy_invalid",
                "Profiled browser_e2e intent is not owned by QA in the immutable run policy.",
                run_id=run_id,
            )


def _package_scripts(target_root: Path) -> tuple[set[str], str]:
    manifest_path = target_root / "package.json"
    if not manifest_path.is_file():
        return set(), "Verification target is missing package.json."
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set(), "Verification target package.json is not valid UTF-8 JSON."
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return set(), "Verification target package.json has no scripts object."
    return {str(key) for key, value in scripts.items() if isinstance(value, str) and value.strip()}, ""


def _profiled_required_test_files(
    *,
    target_root: Path,
    required_test_files: list[str],
    work_packet: dict[str, object],
    verification_policy: VerificationStagePolicy,
    qa_policy: QaTestPolicy | None,
    run_id: str | None,
    developer_unit_scope: bool = False,
) -> tuple[str, ...]:
    if not required_test_files:
        return ()
    if qa_policy is None and not developer_unit_scope:
        raise HarnessError(
            "qa_test_policy_invalid",
            "Profiled required test execution requires the immutable QA test policy.",
            run_id=run_id,
        )
    if qa_policy is not None and qa_policy.project_profile_id != verification_policy.project_profile_id:
        raise HarnessError(
            "qa_test_policy_invalid",
            "QA test policy does not belong to the verification project profile.",
            run_id=run_id,
        )
    selected: list[str] = []
    allowed_paths = {
        str(path).strip().replace("\\", "/")
        for path in (
            work_packet.get("output_contract", {}).get("allowed_paths", [])
            if isinstance(work_packet.get("output_contract"), dict)
            else []
        )
        if isinstance(path, str) and path.strip()
    }
    for raw_path in required_test_files:
        path = (
            str(raw_path).strip().replace("\\", "/")
            if developer_unit_scope
            else (
                qa_policy.normalize_test_path(raw_path)
                or qa_policy.normalize_browser_test_path(raw_path)
                if qa_policy is not None
                else ""
            )
        )
        if developer_unit_scope and (
            not path
            or path.startswith("/")
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path not in allowed_paths
            or not path.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
        ):
            raise HarnessError(
                "developer_unit_test_out_of_scope",
                f"Developer unit test path is outside the approved packet scope: {raw_path}",
                run_id=run_id,
            )
        if not path:
            raise HarnessError(
                "qa_test_out_of_scope",
                f"Required QA test path is outside the profiled QA policy: {raw_path}",
                run_id=run_id,
            )
        if path in selected:
            raise HarnessError(
                "qa_test_path_duplicate",
                f"Required QA test path is duplicated: {path}",
                run_id=run_id,
            )
        target_file = (target_root / path).resolve()
        try:
            target_file.relative_to(target_root)
        except ValueError as exc:
            raise HarnessError(
                "qa_test_unsafe_path",
                f"Required QA test path escapes target repository: {path}",
                run_id=run_id,
            ) from exc
        if not target_file.is_file():
            raise HarnessError(
                "qa_test_file_missing",
                f"Required QA test file does not exist: {path}",
                run_id=run_id,
            )
        selected.append(path)
    return tuple(selected)


def _vitest_verified_test_files(
    *,
    report_path: Path,
    target_root: Path,
    selected_test_files: tuple[str, ...],
) -> tuple[tuple[str, ...], str, str]:
    """Đọc Vitest JSON và đối chiếu đúng tập file QA đã chọn."""

    if not report_path.is_file():
        return (), "Vitest did not produce the required machine-readable JSON report.", "policy"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (), "Vitest machine-readable JSON report is missing or invalid.", "policy"
    test_results = payload.get("testResults") if isinstance(payload, dict) else None
    if not isinstance(test_results, list):
        return (), "Vitest JSON report does not contain a testResults array.", "policy"

    reported: list[str] = []
    assertion_failures: list[str] = []
    for item in test_results:
        if not isinstance(item, dict):
            return (), "Vitest JSON report contains an invalid test result entry.", "policy"
        raw_path = str(item.get("name") or item.get("testFilePath") or "").strip()
        if not raw_path:
            return (), "Vitest JSON report contains a test result without a file path.", "policy"
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = target_root / candidate
        try:
            relative = candidate.resolve().relative_to(target_root).as_posix()
        except (OSError, ValueError):
            return (), "Vitest JSON report references a test file outside the target repository.", "policy"
        if relative in reported:
            return tuple(reported), "Vitest JSON report contains a duplicate test file entry.", "policy"
        reported.append(relative)
        assertion_results = item.get("assertionResults")
        if not isinstance(assertion_results, list) or not assertion_results:
            return (
                tuple(reported),
                "Vitest JSON report does not prove that each selected file executed a test.",
                "policy",
            )
        for assertion in assertion_results:
            if not isinstance(assertion, dict) or not str(assertion.get("status") or "").strip():
                return tuple(reported), "Vitest JSON report contains an invalid assertion entry.", "policy"
            if str(assertion.get("status") or "").strip().lower() != "passed":
                assertion_failures.append(_vitest_assertion_diagnostic(assertion))

    if set(reported) != set(selected_test_files) or len(reported) != len(selected_test_files):
        return (
            tuple(reported),
            "Vitest JSON report does not match the exact selected QA test files: "
            f"selected={list(selected_test_files)}, reported={reported}.",
            "policy",
        )
    ordered_reported = tuple(path for path in selected_test_files if path in reported)
    if payload.get("success") is not True:
        reason = "Vitest test failure"
        if assertion_failures:
            reason += ": " + " | ".join(assertion_failures[:3])
        else:
            reason += ": the machine-readable report records an unsuccessful test run."
        return ordered_reported, reason, "implementation"
    if assertion_failures:
        return (
            ordered_reported,
            "Vitest test failure: " + " | ".join(assertion_failures[:3]),
            "implementation",
        )
    return ordered_reported, "", ""


def _vitest_assertion_diagnostic(assertion: dict[str, object]) -> str:
    """Return one bounded, human/agent-readable Vitest assertion diagnostic."""

    title = str(assertion.get("fullName") or assertion.get("title") or "unnamed assertion").strip()
    messages = assertion.get("failureMessages")
    message = ""
    if isinstance(messages, list):
        message = next((str(item).strip() for item in messages if str(item).strip()), "")
    if not message:
        message = str(assertion.get("failureMessage") or "").strip()
    message = re.sub(r"\s+", " ", message)[:1200]
    return f"{title}: {message}" if message else title


def _playwright_report_relative_file(raw_file: str, *, payload: object, target_root: Path) -> str | None:
    """Target-relative POSIX path of a report suite file, or None when it is outside the target.

    Playwright writes suite files relative to ``config.rootDir`` (the configured testDir), not
    to the repository root; an absolute ``rootDir`` is therefore the base for relative files.
    """

    config = payload.get("config") if isinstance(payload, dict) else None
    root_dir = str(config.get("rootDir") or "") if isinstance(config, dict) else ""
    base = Path(root_dir) if root_dir and Path(root_dir).is_absolute() else target_root
    candidate = Path(raw_file)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve().relative_to(target_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _playwright_verified_test_files(
    *,
    report_path: Path,
    target_root: Path,
    selected_test_files: tuple[str, ...],
) -> tuple[tuple[str, ...], str, str]:
    """Read Playwright JSON and prove the exact reviewed browser files ran."""

    if not report_path.is_file():
        return (), "Playwright did not produce the required JSON report.", "policy"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (), "Playwright JSON report is missing or invalid.", "policy"
    suites = payload.get("suites") if isinstance(payload, dict) else None
    if not isinstance(suites, list):
        return (), "Playwright JSON report does not contain a suites array.", "policy"

    reported: list[str] = []
    assertion_failures: list[str] = []
    executed_count: dict[str, int] = {}

    def visit_suite(suite: object, inherited_file: str = "") -> str:
        if not isinstance(suite, dict):
            return "Playwright JSON report contains an invalid suite entry."
        raw_file = str(suite.get("file") or inherited_file).strip()
        relative_file = ""
        if raw_file:
            resolved = _playwright_report_relative_file(raw_file, payload=payload, target_root=target_root)
            if resolved is None:
                return "Playwright JSON report references a test file outside the target repository."
            relative_file = resolved
            if relative_file not in reported:
                reported.append(relative_file)

        specs = suite.get("specs", [])
        if not isinstance(specs, list):
            return "Playwright JSON report contains an invalid specs entry."
        for spec in specs:
            if not isinstance(spec, dict):
                return "Playwright JSON report contains an invalid spec entry."
            tests = spec.get("tests")
            if not isinstance(tests, list) or not tests:
                return "Playwright JSON report does not prove that each selected file executed a test."
            for test in tests:
                if not isinstance(test, dict):
                    return "Playwright JSON report contains an invalid test entry."
                results = test.get("results")
                if not isinstance(results, list) or not results:
                    return "Playwright JSON report contains a test without an execution result."
                if relative_file:
                    executed_count[relative_file] = executed_count.get(relative_file, 0) + 1
                for result in results:
                    if not isinstance(result, dict):
                        return "Playwright JSON report contains an invalid execution result."
                    status = str(result.get("status") or "").strip().lower()
                    if not status:
                        return "Playwright JSON report contains a result without status."
                    if status != "passed":
                        title = str(spec.get("title") or "unnamed browser assertion").strip()
                        message = _playwright_error_message(result)
                        assertion_failures.append(f"{title}: {status}: {message}" if message else f"{title}: {status}")

        children = suite.get("suites", [])
        if not isinstance(children, list):
            return "Playwright JSON report contains an invalid nested suites entry."
        for child in children:
            # Children inherit the file as Playwright reported it, so it resolves against the same base.
            error = visit_suite(child, raw_file)
            if error:
                return error
        return ""

    for suite in suites:
        error = visit_suite(suite)
        if error:
            return tuple(reported), error, "policy"

    selected_set = set(selected_test_files)
    relevant_reported = [path for path in reported if path in selected_set]
    if set(relevant_reported) != selected_set or any(
        executed_count.get(path, 0) == 0 for path in selected_test_files
    ):
        return (
            tuple(relevant_reported),
            "Playwright JSON report does not prove the exact selected QA browser files: "
            f"selected={list(selected_test_files)}, reported={relevant_reported}.",
            "policy",
        )
    ordered_reported = tuple(path for path in selected_test_files if path in relevant_reported)
    stats = payload.get("stats") if isinstance(payload, dict) else None
    unexpected = stats.get("unexpected") if isinstance(stats, dict) else None
    if assertion_failures or (isinstance(unexpected, int) and unexpected > 0):
        detail = " | ".join(assertion_failures)[:MAX_FAILURE_DETAIL_CHARS] or f"{unexpected} unexpected result(s)"
        return ordered_reported, f"Playwright test failure: {detail}", "implementation"
    return ordered_reported, "", ""


def _runtime_probe_outcomes(
    *,
    report_path: Path,
    target_root: Path,
    probe_ids: tuple[str, ...],
) -> tuple[tuple[dict[str, object], ...], str]:
    """Per-probe status and Harness annotation from the Playwright JSON report of the probe spec."""

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (), "Playwright JSON report is missing or invalid for the runtime probe spec."
    outcomes: dict[str, dict[str, object]] = {}
    problems: list[str] = []

    def visit(suite: object, inherited_file: str) -> None:
        if not isinstance(suite, dict):
            return
        raw_file = str(suite.get("file") or inherited_file)
        relative = ""
        if raw_file:
            relative = _playwright_report_relative_file(raw_file, payload=payload, target_root=target_root) or ""
        for spec in suite.get("specs") or []:
            if not isinstance(spec, dict) or relative != RUNTIME_PROBE_SPEC_PATH:
                continue
            title = str(spec.get("title") or "").strip()
            if title not in probe_ids:
                problems.append(f"unexpected test `{title}` in the probe spec")
                continue
            if title in outcomes:
                problems.append(f"probe `{title}` ran more than once")
                continue
            statuses: list[str] = []
            observation: dict[str, object] = {}
            for test in spec.get("tests") or []:
                if not isinstance(test, dict):
                    continue
                annotations = list(test.get("annotations") or [])
                for result in test.get("results") or []:
                    if isinstance(result, dict):
                        statuses.append(str(result.get("status") or "").strip().lower())
                        annotations.extend(result.get("annotations") or [])
                for annotation in annotations:
                    if isinstance(annotation, dict) and annotation.get("type") == RUNTIME_PROBE_ANNOTATION_TYPE:
                        try:
                            parsed = json.loads(str(annotation.get("description") or ""))
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict):
                            observation = parsed
            outcomes[title] = {
                "probe_id": title,
                "status": "passed" if statuses and all(item == "passed" for item in statuses) else "failed",
                "observation": observation,
            }
        for child in suite.get("suites") or []:
            visit(child, raw_file)

    suites = payload.get("suites") if isinstance(payload, dict) else None
    for suite in suites if isinstance(suites, list) else []:
        visit(suite, "")
    missing = [probe_id for probe_id in probe_ids if probe_id not in outcomes]
    if missing:
        problems.append(f"no result for probe(s) {missing}")
    ordered = tuple(outcomes[probe_id] for probe_id in probe_ids if probe_id in outcomes)
    if problems:
        return ordered, "Runtime probe report does not match the approved probes: " + "; ".join(problems)
    return ordered, ""


def _runtime_probe_stand_in_env(runtime_probe: RuntimeProbeRun | None, *, run_id: str | None) -> dict[str, str]:
    """Run-scoped loopback URLs for approved stand-ins; keys come from the contract, values from Harness."""

    if runtime_probe is None:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "The runtime_probe stage requires the approved probe set of the runtime closure contract.",
            run_id=run_id,
        )
    invalid = [probe_id for probe_id in runtime_probe.probe_ids if not _PROBE_ID.fullmatch(probe_id)]
    if invalid or len(set(runtime_probe.probe_ids)) != len(runtime_probe.probe_ids):
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Runtime probe ids must be unique RP-<n> ids: {list(runtime_probe.probe_ids)}",
            run_id=run_id,
        )
    return _stand_in_env(runtime_probe.stand_in_env_keys, run_id=run_id)


def _browser_capture_env(capture: BrowserCaptureRun | None, *, run_id: str | None) -> dict[str, str]:
    """Stand-in URLs (same values the probe stage gets) plus the demo build flag, for browser_capture only."""

    if capture is None:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "The browser_capture stage requires the Harness-rendered view set.",
            run_id=run_id,
        )
    for label, values, pattern in (
        ("view", capture.view_ids, _VIEW_ID),
        ("viewport", capture.viewport_ids, _VIEWPORT_ID),
    ):
        if not values or len(set(values)) != len(values) or any(not pattern.fullmatch(item) for item in values):
            raise HarnessError(
                "verification_stage_policy_invalid",
                f"Browser capture {label} ids must be unique and well-formed: {list(values)}",
                run_id=run_id,
            )
    env = _stand_in_env(capture.stand_in_env_keys, run_id=run_id)
    if capture.demo_flag_env_key:
        _require_harness_env_key(capture.demo_flag_env_key, taken=env, run_id=run_id)
        env[capture.demo_flag_env_key] = "1"
    return env


def _stand_in_env(keys: tuple[str, ...], *, run_id: str | None) -> dict[str, str]:
    """Run-scoped loopback URLs for approved stand-ins; keys come from the contract, values from Harness."""

    env: dict[str, str] = {}
    for key in keys:
        _require_harness_env_key(key, taken=env, run_id=run_id)
        digest = hashlib.sha256(f"{run_id or ''}\0{key}".encode("utf-8")).hexdigest()
        env[key] = f"http://127.0.0.1:{40000 + int(digest[:8], 16) % 10000}"
    return env


def _require_harness_env_key(key: str, *, taken: dict[str, str], run_id: str | None) -> None:
    folded = key.casefold()
    if (
        not _STAND_IN_ENV_KEY.fullmatch(key)
        or folded in _RESERVED_ENV_KEYS
        or folded.startswith(_RESERVED_ENV_PREFIXES)
        or key in taken
    ):
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Harness stage env key is reserved, duplicated or invalid: {key}",
            run_id=run_id,
        )


def _browser_capture_outcomes(
    *,
    report_path: Path,
    target_root: Path,
    capture: BrowserCaptureRun,
) -> tuple[tuple[dict[str, object], ...], str]:
    """Per-step captures of the Harness capture spec; screenshots are copied out of test-results/."""

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (), "Playwright JSON report is missing or invalid for the browser capture spec."
    expected = [f"{view_id}@{viewport_id}" for view_id in capture.view_ids for viewport_id in capture.viewport_ids]
    seen: dict[str, tuple[str, list[dict[str, object]]]] = {}
    problems: list[str] = []

    def visit(suite: object, inherited_file: str) -> None:
        if not isinstance(suite, dict):
            return
        raw_file = str(suite.get("file") or inherited_file)
        relative = ""
        if raw_file:
            relative = _playwright_report_relative_file(raw_file, payload=payload, target_root=target_root) or ""
        for spec in suite.get("specs") or []:
            if not isinstance(spec, dict) or relative != BROWSER_CAPTURE_SPEC_PATH:
                continue
            title = str(spec.get("title") or "").strip()
            if title not in expected:
                problems.append(f"unexpected test `{title}` in the capture spec")
                continue
            if title in seen:
                problems.append(f"test `{title}` ran more than once")
                continue
            view_id, viewport_id = title.split("@", 1)
            captures: dict[int, dict[str, object]] = {}
            test_status = "passed"
            for test in spec.get("tests") or []:
                if not isinstance(test, dict):
                    continue
                annotations = list(test.get("annotations") or [])
                attachments: dict[str, str] = {}
                for result in test.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    status = str(result.get("status") or "").strip().lower()
                    if status != "passed":
                        test_status = status or "unknown"
                    annotations.extend(result.get("annotations") or [])
                    for item in result.get("attachments") or []:
                        if isinstance(item, dict) and item.get("name") and item.get("path"):
                            attachments[str(item["name"])] = str(item["path"])
                for annotation in annotations:
                    if not isinstance(annotation, dict) or annotation.get("type") != BROWSER_CAPTURE_ANNOTATION_TYPE:
                        continue
                    try:
                        parsed = json.loads(str(annotation.get("description") or ""))
                    except json.JSONDecodeError:
                        parsed = None
                    step = parsed.get("step") if isinstance(parsed, dict) else None
                    if (
                        not isinstance(parsed, dict)
                        or parsed.get("view_id") != view_id
                        or parsed.get("viewport_id") != viewport_id
                        or not isinstance(step, int)
                        or isinstance(step, bool)
                        or step < 0
                        or parsed.get("status") not in {"passed", "step_failed"}
                    ):
                        problems.append(f"invalid capture annotation in `{title}`")
                        continue
                    # Playwright may report the same runtime annotation on the test and on its result.
                    captures.setdefault(
                        step,
                        {
                            **parsed,
                            # Playwright call logs keep terminal colour codes even with NO_COLOR.
                            "error": _ANSI_ESCAPE.sub("", parsed["error"]) if isinstance(parsed.get("error"), str) else None,
                            "attachment_path": attachments.get(str(parsed.get("attachment") or ""), ""),
                        },
                    )
            steps = sorted(captures)
            if not steps:
                problems.append(f"no capture for `{title}`")
            elif steps != list(range(len(steps))):
                problems.append(f"capture steps of `{title}` are not contiguous: {steps}")
            seen[title] = (test_status, [captures[step] for step in steps])
        for child in suite.get("suites") or []:
            visit(child, raw_file)

    suites = payload.get("suites") if isinstance(payload, dict) else None
    for suite in suites if isinstance(suites, list) else []:
        visit(suite, "")
    missing = [title for title in expected if title not in seen]
    if missing:
        problems.append(f"no result for view/viewport pair(s) {missing}")

    records: list[dict[str, object]] = []
    for title in expected:
        test_status, captures = seen.get(title, ("", []))
        for item in captures:
            screenshot, error = _capture_screenshot(item, target_root=target_root, capture=capture)
            if error:
                problems.append(error)
            records.append(
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "view_id",
                            "viewport_id",
                            "step",
                            "status",
                            "error",
                            "aria_snapshot",
                            "aria_truncated",
                            "focus",
                            "layout",
                            "styles",
                            "network",
                            "network_truncated",
                        )
                    },
                    **_capture_observe(item.get("observe")),
                    "test_status": test_status,
                    "screenshot": screenshot,
                }
            )
    if problems:
        return tuple(records), "Browser capture report does not match the Harness view set: " + "; ".join(problems)
    return tuple(records), ""


def _capture_observe(value: object) -> dict[str, object]:
    """Outcome of a step's `observe` locator; absent for a step without one."""

    if not isinstance(value, dict) or value.get("status") not in {"observed", "missed"}:
        return {}
    error = value.get("error")
    return {
        "observe": {
            "status": value["status"],
            "error": _ANSI_ESCAPE.sub("", error) if isinstance(error, str) else None,
            "network_held": value.get("network_held") is True,
        }
    }


def _capture_screenshot(
    item: dict[str, object],
    *,
    target_root: Path,
    capture: BrowserCaptureRun,
) -> tuple[dict[str, object] | None, str]:
    label = f"{item.get('view_id')}@{item.get('viewport_id')} step {item.get('step')}"
    raw = str(item.get("attachment_path") or "")
    source = Path(raw) if raw else None
    if source is not None and not source.is_absolute():
        source = target_root / source
    source_io = _windows_io_path(source) if source is not None else None
    if source is None or not _is_within(source, target_root) or source_io is None or not source_io.is_file():
        return None, f"capture {label} has no screenshot attachment inside the target"
    if capture.evidence_dir is None:
        return {
            "file": source.resolve().relative_to(target_root.resolve()).as_posix(),
            "sha256": _file_sha256(source_io),
        }, ""
    name = f"{item.get('view_id')}__{item.get('viewport_id')}__s{int(item.get('step') or 0):02d}.png"
    destination = capture.evidence_dir / name
    try:
        _windows_io_path(capture.evidence_dir).mkdir(parents=True, exist_ok=True)
        destination_io = _windows_io_path(destination)
        shutil.copyfile(source_io, destination_io)
    except OSError as exc:
        return None, f"capture {label} screenshot could not be copied: {exc}"
    return {"file": name, "sha256": _file_sha256(destination_io)}, ""


def _with_build_literal_scan(
    stage_result: VerificationStageResult,
    *,
    target_root: Path,
    capture: BrowserCaptureRun,
) -> VerificationStageResult:
    """R3: fail frontend_build when its production output contains a Harness-extracted demo literal."""

    literals = tuple(dict.fromkeys(item for item in capture.forbidden_build_literals if item))
    if not literals:
        return stage_result
    output = target_root / capture.build_output_dir if capture.build_output_dir else None
    files = (
        sorted(path for path in output.rglob("*") if path.is_file())
        if output is not None and _is_within(output, target_root) and output.is_dir()
        else []
    )
    scan: dict[str, object] = {
        "rule": "R3",
        "output_dir": capture.build_output_dir,
        "files_scanned": len(files),
        "literal_count": len(literals),
        "hits": [],
    }
    if not files:
        return replace(
            stage_result,
            status="failed",
            failure_kind="policy",
            reason=f"R3 build scan found no production build output in `{capture.build_output_dir or '<unset>'}`.",
            build_scan=scan,
        )
    hits: dict[str, list[str]] = {}
    for path in files:
        text = path.read_bytes().decode("utf-8", errors="replace")
        for literal in literals:
            if literal in text:
                hits.setdefault(literal, []).append(path.resolve().relative_to(target_root.resolve()).as_posix())
    scan["hits"] = [{"literal": literal, "files": hits[literal]} for literal in literals if literal in hits]
    if not hits:
        return replace(stage_result, build_scan=scan)
    found = "; ".join(f"`{literal}` in {files}" for literal, files in hits.items())
    return replace(
        stage_result,
        status="failed",
        failure_kind="implementation",
        reason=f"R3: the production build contains demo literal(s): {found}"[:MAX_FAILURE_DETAIL_CHARS],
        build_scan=scan,
    )


def _playwright_error_message(result: dict[str, object]) -> str:
    listed = result.get("errors")
    errors = [result.get("error"), *(listed if isinstance(listed, list) else [])]
    for error in errors:
        if isinstance(error, dict) and str(error.get("message") or "").strip():
            text = _ANSI_ESCAPE.sub("", str(error["message"]))
            return " ".join(text.split())[:MAX_FAILURE_MESSAGE_CHARS]
    return ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _repository_e2e_specs(target_root: Path) -> tuple[str, ...]:
    """The repository's own Playwright specs: what CI runs, without the two Harness-rendered specs.

    shortcut: mirrors the profile's playwright.config (`testDir: tests/e2e`, `testMatch: **/*.spec.ts`);
    read them from the repository once repo discovery exists (weakness backlog item 2).
    """

    root = target_root / REPOSITORY_E2E_ROOT
    if not root.is_dir():
        return ()
    harness_specs = {RUNTIME_PROBE_SPEC_PATH, BROWSER_CAPTURE_SPEC_PATH}
    return tuple(
        sorted(
            path
            for path in (item.relative_to(target_root).as_posix() for item in root.rglob("*.spec.ts"))
            if path not in harness_specs and "node_modules" not in path.split("/")
        )
    )


def _windows_io_path(path: Path) -> Path:
    """Use Win32 extended paths for generated evidence that can exceed MAX_PATH."""

    resolved = path.resolve()
    if os.name != "nt":
        return resolved
    raw = str(resolved)
    if raw.startswith("\\\\?\\"):
        return resolved
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profiled_verification_env(
    spec: VerificationStageSpec | VerificationPreparationSpec,
    *,
    run_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    allowed = {key.casefold() for key in spec.inherited_env_keys}
    env = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    env.update(dict(spec.fixed_env))
    env.update(extra_env or {})
    if run_id and getattr(spec, "stage_id", "") in _RUN_SCOPED_STAGE_IDS:
        env.update(_run_scoped_env(run_id))
    return env


def _run_scoped_env(run_id: str) -> dict[str, str]:
    """Per-run database isolation for the run-scoped stages."""

    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    port = 50000 + (int(digest[:8], 16) % 10000)
    return {
        "COMPOSE_PROJECT_NAME": f"sdlc_{digest[:10]}",
        "SDLC_POSTGRES_PORT": str(port),
        # Compose reads these to create the database DATABASE_URL names; without them it
        # falls back to the target's own defaults and authentication fails.
        "POSTGRES_USER": _RUN_DATABASE_USER,
        "POSTGRES_PASSWORD": _RUN_DATABASE_PASSWORD,
        "POSTGRES_DB": _RUN_DATABASE_NAME,
        "DATABASE_URL": (
            f"postgresql://{_RUN_DATABASE_USER}:{_RUN_DATABASE_PASSWORD}"
            f"@127.0.0.1:{port}/{_RUN_DATABASE_NAME}"
        ),
    }


def _stage_env_keys(
    spec: VerificationStageSpec,
    *,
    run_id: str | None,
    extra_env: dict[str, str],
    constant_keys: set[str],
) -> dict[str, object]:
    """Which env a stage ran with, by source. Values are kept only for reviewed constants: the spec's fixed
    env and Harness flags whose value is fixed in code (the demo flag is always "1"); every other value may be
    machine-specific or secret."""

    allowed = {key.casefold() for key in spec.inherited_env_keys}
    return {
        "inherited": sorted(key for key in os.environ if key.casefold() in allowed),
        "fixed": dict(spec.fixed_env),
        "run_scoped": (
            sorted(_run_scoped_env(run_id)) if run_id and spec.stage_id in _RUN_SCOPED_STAGE_IDS else []
        ),
        "harness": sorted(extra_env),
        "harness_constants": {key: extra_env[key] for key in sorted(extra_env) if key in constant_keys},
    }


def _environment_value(env: dict[str, str], key: str) -> str:
    expected = key.casefold()
    return next((value for name, value in env.items() if name.casefold() == expected), "")


def _profiled_preflight_failure(
    *,
    target_path: Path,
    planned_stage_ids: tuple[str, ...],
    reason: str,
    selected_test_files: tuple[str, ...] = (),
    failure_kind: str = "environment",
) -> VerificationResult:
    return VerificationResult(
        status="failed",
        command="",
        cwd=str(target_path),
        exit_code=None,
        duration_seconds=0.0,
        stdout="",
        stderr="",
        reason=reason,
        failure_kind=failure_kind,
        planned_stage_ids=planned_stage_ids,
        selected_test_files=selected_test_files,
    )


def _profiled_result(
    *,
    target_path: Path,
    planned_stage_ids: tuple[str, ...],
    stage_results: list[VerificationStageResult],
    preparation_results: list[VerificationStageResult] | None = None,
    terminal: VerificationStageResult,
    reason: str | None = None,
    selected_test_files: tuple[str, ...] = (),
) -> VerificationResult:
    return VerificationResult(
        status=terminal.status,
        command="",
        cwd=str(target_path),
        exit_code=terminal.exit_code,
        duration_seconds=sum(
            item.duration_seconds for item in [*(preparation_results or []), *stage_results]
        ),
        stdout=terminal.stdout,
        stderr=terminal.stderr,
        reason=reason or terminal.reason,
        failure_kind=terminal.failure_kind,
        timed_out=terminal.timed_out,
        planned_stage_ids=planned_stage_ids,
        executed_stage_ids=tuple(item.stage_id for item in stage_results),
        stage_results=tuple(stage_results),
        preparation_results=tuple(preparation_results or []),
        selected_test_files=selected_test_files,
        verified_test_files=tuple(
            path
            for item in stage_results
            for path in item.verified_test_files
        ),
    )


def _run_profiled_preparation(
    *,
    target_root: Path,
    spec: VerificationPreparationSpec | None,
) -> VerificationStageResult | None:
    """Chạy một preparation theo fingerprint và trả evidence tách biệt."""

    if spec is None:
        return None
    trigger_path = (target_root / spec.trigger_path).resolve()
    try:
        trigger_path.relative_to(target_root)
    except ValueError:
        return _preparation_failure(spec, reason="Verification preparation trigger escapes target repository.")
    if not trigger_path.is_file():
        return None

    fingerprint = _preparation_fingerprint(target_root=target_root, spec=spec)
    if not fingerprint:
        return _preparation_failure(spec, reason="Verification preparation fingerprint could not be computed.")
    marker_path = (target_root / spec.success_marker_path).resolve()
    try:
        marker_path.relative_to(target_root)
    except ValueError:
        return _preparation_failure(spec, reason="Verification preparation marker escapes target repository.")
    if _preparation_marker_matches(marker_path, fingerprint=fingerprint):
        return VerificationStageResult(
            stage_id=spec.preparation_id,
            status="passed",
            argv=spec.argv,
            cwd=spec.cwd,
            timeout_seconds=spec.timeout_seconds,
            exit_code=0,
            duration_seconds=0.0,
            stdout="",
            stderr="",
            reason="Verification preparation already completed for the current inputs.",
        )

    stage_cwd = (target_root / spec.cwd).resolve()
    try:
        stage_cwd.relative_to(target_root)
    except ValueError:
        return _preparation_failure(spec, reason="Verification preparation cwd escapes target repository.")
    if not stage_cwd.is_dir():
        return _preparation_failure(spec, reason=f"Verification preparation cwd does not exist: {spec.cwd}")
    env = _profiled_verification_env(spec)
    executable = shutil.which(spec.argv[0], path=_environment_value(env, "PATH") or "")
    if not executable:
        return _preparation_failure(
            spec,
            reason=f"Verification preparation executable was not found: {spec.argv[0]}",
        )

    started = time.monotonic()
    try:
        completed = _run_bounded(
            [executable, *spec.argv[1:]],
            cwd=str(stage_cwd),
            env=env,
            timeout=spec.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationStageResult(
            stage_id=spec.preparation_id,
            status="failed",
            argv=spec.argv,
            cwd=spec.cwd,
            timeout_seconds=spec.timeout_seconds,
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            stdout=_truncate_output(_decode_timeout_output(exc.stdout)),
            stderr=_truncate_output(_decode_timeout_output(exc.stderr)),
            reason=f"Verification preparation timed out after {spec.timeout_seconds}s.",
            failure_kind="environment",
            timed_out=True,
        )
    except OSError as exc:
        return VerificationStageResult(
            stage_id=spec.preparation_id,
            status="failed",
            argv=spec.argv,
            cwd=spec.cwd,
            timeout_seconds=spec.timeout_seconds,
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            stdout="",
            stderr=_truncate_output(str(exc)),
            reason="Verification preparation could not start.",
            failure_kind="environment",
        )

    stdout = _truncate_output(completed.stdout)
    stderr = _truncate_output(completed.stderr)
    if completed.returncode != 0:
        return VerificationStageResult(
            stage_id=spec.preparation_id,
            status="failed",
            argv=spec.argv,
            cwd=spec.cwd,
            timeout_seconds=spec.timeout_seconds,
            exit_code=completed.returncode,
            duration_seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            reason=f"Verification preparation exited with code {completed.returncode}.",
            failure_kind=_dependency_setup_failure_kind(stdout=stdout, stderr=stderr),
        )
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"fingerprint": fingerprint}, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return _preparation_failure(
            spec,
            reason="Verification preparation succeeded but its success marker could not be written.",
            stderr=str(exc),
        )
    return VerificationStageResult(
        stage_id=spec.preparation_id,
        status="passed",
        argv=spec.argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        exit_code=0,
        duration_seconds=time.monotonic() - started,
        stdout=stdout,
        stderr=stderr,
        reason="Verification preparation completed successfully.",
    )


def _preparation_failure(
    spec: VerificationPreparationSpec,
    *,
    reason: str,
    stderr: str = "",
) -> VerificationStageResult:
    return VerificationStageResult(
        stage_id=spec.preparation_id,
        status="failed",
        argv=spec.argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        exit_code=None,
        duration_seconds=0.0,
        stdout="",
        stderr=_truncate_output(stderr),
        reason=reason,
        failure_kind="environment",
    )


def _preparation_fingerprint(*, target_root: Path, spec: VerificationPreparationSpec) -> str:
    digest = hashlib.sha256()
    for relative_path in spec.fingerprint_paths:
        path = (target_root / relative_path).resolve()
        try:
            path.relative_to(target_root)
            payload = path.read_bytes()
        except (OSError, ValueError):
            return ""
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _preparation_marker_matches(path: Path, *, fingerprint: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("fingerprint") == fingerprint


def _command_failure_kind(*, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".casefold()
    environment_markers = (
        "is not recognized as an internal or external command",
        "command not found",
        "could not determine executable to run",
        # Busy ports and an unreachable database are machine state, not code defects.
        "eaddrinuse",
        "address already in use",
        "port is already allocated",
        "is already used, make sure that nothing is running on the port",
        "can't reach database server",
        # Database credentials come from the run-scoped env, never from target code.
        "authentication failed against database server",
    )
    return "environment" if any(marker in combined for marker in environment_markers) else "implementation"


def _dependency_setup_failure_kind(*, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".casefold()
    environment_markers = (
        "enotfound",
        "eai_again",
        "econnreset",
        "etimedout",
        "unable to get local issuer certificate",
        "network request to",
        "proxy",
    )
    if any(marker in combined for marker in environment_markers):
        return "environment"
    return _command_failure_kind(stdout=stdout, stderr=stderr)


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _truncate_output(value: str) -> str:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS].rstrip() + "\n... truncated ..."


_KILL_GRACE_SECONDS = 10


def _run_bounded(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` whose timeout also ends the command's descendants.

    npm starts the real tool (Playwright, its webServers, Prisma) as a grandchild. Killing only
    npm leaves that grandchild holding the output pipes, and ``subprocess.run`` then waits for
    them forever after its own kill. Raises ``TimeoutExpired`` with the output read so far.
    """

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # POSIX: own process group so the whole tree can be signalled at once.
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant outlived the tree kill and still holds the pipes; give up on the output
            # rather than wait: the stage is already over.
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        _kill_process_tree(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        # shortcut: taskkill /T follows parent PIDs, so a descendant whose parent already exited
        # escapes it; use a Job Object if verification ever leaks such processes.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=_KILL_GRACE_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _decode_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
