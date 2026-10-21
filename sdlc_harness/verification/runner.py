"""Chạy các stage kiểm chứng của target repo cho Phase 4.

Verification Runner là lớp công cụ, không phải AI Tester. Nó nhận
DeveloperWorkPacket đã được Phase 4 chuẩn bị hoặc refresh. Profiled run chỉ dùng
stage policy từ snapshot và executable registry đã review; artifact legacy vẫn
dùng command Java cũ. Kết quả có cấu trúc được ghi vào `test_report_v{n}.md`.

Sau command baseline, runner khám phá mọi Java test entrypoint có `main()` dưới
`src/test/java`, hợp nhất chúng với `required_test_files` của QA và chạy trực tiếp
từng class. Chỉ compile test source không còn đủ để báo pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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
    VerificationPreparationSpec,
    VerificationStageSpec,
    resolve_verification_preparation_specs,
    resolve_verification_stage_specs,
)

DEFAULT_TEST_COMMAND = "powershell -ExecutionPolicy Bypass -File scripts/test.ps1"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20000
JAVA_HOME_ENV_KEYS = ("SDLC_JAVA_HOME", "JAVA_HOME")
FATAL_OUTPUT_MARKERS = (
    "could not find or load main class",
    "classnotfoundexception",
    "invalidpathexception",
)
JAVA_MAIN_METHOD_PATTERN = re.compile(
    r"\bpublic\s+static\s+void\s+main\s*\(\s*"
    r"(?:final\s+)?(?:java\.lang\.)?String\s*"
    r"(?:\[\s*\]\s*[A-Za-z_$][\w$]*|\.\.\.\s*[A-Za-z_$][\w$]*|"
    r"[A-Za-z_$][\w$]*\s*\[\s*\])\s*\)",
    re.MULTILINE,
)


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

    def to_dict(self) -> dict[str, object]:
        return {
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
    """Chạy profile stages hoặc command legacy và phân loại kết quả."""

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
    ) -> VerificationResult:
        """Run verification and publish bounded result metadata."""

        result = self._run_internal(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=required_test_files,
            verification_policy=verification_policy,
            qa_policy=qa_policy,
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

    def _run_internal(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
        verification_policy: VerificationStagePolicy | None = None,
        qa_policy: QaTestPolicy | None = None,
    ) -> VerificationResult:
        """Chọn runner profile-owned hoặc nhánh Java compatibility."""

        if verification_policy is not None or _has_profiled_verification_contract(work_packet):
            if verification_policy is None:
                raise HarnessError(
                    "verification_stage_policy_invalid",
                    "Profiled verification requires the immutable run stage policy.",
                    run_id=str(work_packet.get("run_id") or "") or None,
                )
            return self._run_profiled_stages(
                target_path=target_path,
                work_packet=work_packet,
                required_test_files=required_test_files,
                verification_policy=verification_policy,
                qa_policy=qa_policy,
            )

        command = _test_command_from_work_packet(work_packet)
        if not command:
            return VerificationResult(
                status="skipped",
                command="",
                cwd=str(target_path),
                exit_code=None,
                duration_seconds=0.0,
                stdout="",
                stderr="",
                reason="No test command found in DeveloperWorkPacket.",
            )

        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(target_path),
                env=_verification_env(),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return VerificationResult(
                status="failed",
                command=command,
                cwd=str(target_path),
                exit_code=None,
                duration_seconds=duration,
                stdout=_truncate_output(_decode_timeout_output(exc.stdout)),
                stderr=_truncate_output(_decode_timeout_output(exc.stderr)),
                reason=f"Test command timed out after {self.timeout_seconds}s.",
                timed_out=True,
            )
        duration = time.monotonic() - start
        stdout = _truncate_output(completed.stdout)
        stderr = _truncate_output(completed.stderr)
        status, reason = _classify_result(exit_code=completed.returncode, stdout=stdout, stderr=stderr)
        baseline = VerificationResult(
            status=status,
            command=command,
            cwd=str(target_path),
            exit_code=completed.returncode,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            reason=reason,
        )
        if baseline.status != "passed":
            return baseline

        discovered_test_files = _discover_java_main_test_files(target_path)
        test_files: list[str] = []
        for discovered_path in [*discovered_test_files, *(required_test_files or [])]:
            normalized = discovered_path.strip().replace("\\", "/")
            if normalized and normalized not in test_files:
                test_files.append(normalized)
        if not test_files:
            return baseline

        # Discovery execution uses ``java -cp build`` below. A non-Java or
        # placeholder baseline may coexist with fixture source files without
        # producing bytecode; treating those sources as executable would turn
        # a successful baseline into a synthetic ClassNotFoundException. Real
        # Java verification remains strict once the baseline has populated the
        # build directory.
        if not _has_compiled_java_output(target_path):
            if required_test_files:
                return VerificationResult(
                    status="failed",
                    command=baseline.command,
                    cwd=str(target_path),
                    exit_code=1,
                    duration_seconds=baseline.duration_seconds,
                    stdout=baseline.stdout,
                    stderr=baseline.stderr,
                    reason=(
                        "Baseline verification did not produce Java bytecode in build; "
                        "required QA test entrypoints cannot be executed."
                    ),
                    discovered_test_entrypoints=tuple(
                        filter(None, (_java_test_class_name(path) for path in test_files))
                    ),
                )
            return baseline

        return self._run_java_tests(
            target_path=target_path,
            test_files=test_files,
            baseline=baseline,
        )

    def _run_profiled_stages(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None,
        verification_policy: VerificationStagePolicy,
        qa_policy: QaTestPolicy | None,
    ) -> VerificationResult:
        """Chạy ordered smoke suite bằng registry, không đọc executable data từ packet."""

        run_id = str(work_packet.get("run_id") or "") or None
        specs = resolve_verification_stage_specs(verification_policy, run_id=run_id)
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
            verification_policy=verification_policy,
            qa_policy=qa_policy,
            run_id=run_id,
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
            if spec.required_package_script not in scripts:
                return _profiled_preflight_failure(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    reason=f"package.json is missing required script: {spec.required_package_script}",
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
            env = _profiled_verification_env(spec)
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

        stage_results: list[VerificationStageResult] = []
        for spec, stage_cwd, executable, env in prepared:
            stage_selected_files = selected_test_files if spec.stage_id == "unit" else ()
            logical_argv = spec.argv
            executable_argv = [executable, *spec.argv[1:]]
            report_directory: tempfile.TemporaryDirectory[str] | None = None
            report_path: Path | None = None
            if stage_selected_files:
                if qa_policy is None or qa_policy.framework != spec.required_test_framework:
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
            start = time.monotonic()
            try:
                completed = subprocess.run(
                    executable_argv,
                    cwd=str(stage_cwd),
                    env=env,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=spec.timeout_seconds,
                )
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
                verified_test_files: tuple[str, ...] = ()
                report_error = ""
                if report_path is not None:
                    verified_test_files, report_error = _vitest_verified_test_files(
                        report_path=report_path,
                        target_root=target_root,
                        selected_test_files=stage_selected_files,
                    )
                stage_passed = completed.returncode == 0 and not report_error
                failure_kind = ""
                if not stage_passed:
                    failure_kind = (
                        "policy"
                        if report_error
                        else _command_failure_kind(stdout=completed.stdout, stderr=completed.stderr)
                    )
                stage_result = VerificationStageResult(
                    stage_id=spec.stage_id,
                    status="passed" if stage_passed else "failed",
                    argv=logical_argv,
                    cwd=spec.cwd,
                    timeout_seconds=spec.timeout_seconds,
                    exit_code=completed.returncode,
                    duration_seconds=time.monotonic() - start,
                    stdout=_truncate_output(completed.stdout),
                    stderr=_truncate_output(completed.stderr),
                    reason=(
                        f"Verification stage completed successfully: {spec.stage_id}"
                        if stage_passed
                        else report_error
                        or f"Verification stage exited with code {completed.returncode}: {spec.stage_id}"
                    ),
                    failure_kind=failure_kind,
                    selected_test_files=stage_selected_files,
                    verified_test_files=verified_test_files,
                )
            finally:
                if report_directory is not None:
                    report_directory.cleanup()
            stage_results.append(stage_result)
            if stage_result.status != "passed":
                return _profiled_result(
                    target_path=target_root,
                    planned_stage_ids=planned_stage_ids,
                    stage_results=stage_results,
                    preparation_results=preparation_results,
                    terminal=stage_result,
                    selected_test_files=selected_test_files,
                )

        terminal = stage_results[-1]
        return _profiled_result(
            target_path=target_root,
            planned_stage_ids=planned_stage_ids,
            stage_results=stage_results,
            preparation_results=preparation_results,
            terminal=terminal,
            reason="All profile-owned verification stages completed successfully.",
            selected_test_files=selected_test_files,
        )

    def _run_java_tests(
        self,
        *,
        target_path: Path,
        test_files: list[str],
        baseline: VerificationResult,
    ) -> VerificationResult:
        """Chạy trực tiếp mọi Java test class đã khám phá hoặc được QA yêu cầu."""

        class_names: list[str] = []
        for path in test_files:
            class_name = _java_test_class_name(path)
            if not class_name:
                return VerificationResult(
                    status="failed",
                    command=baseline.command,
                    cwd=str(target_path),
                    exit_code=1,
                    duration_seconds=baseline.duration_seconds,
                    stdout=baseline.stdout,
                    stderr=baseline.stderr,
                    reason=f"Cannot derive Java test class from discovered test path: {path}",
                )
            if class_name not in class_names:
                class_names.append(class_name)

        stdout_parts = [baseline.stdout.rstrip()] if baseline.stdout.strip() else []
        stderr_parts = [baseline.stderr.rstrip()] if baseline.stderr.strip() else []
        total_duration = baseline.duration_seconds
        commands = [baseline.command]
        executed_class_names: list[str] = []
        stdout_parts.append(f"[verification-discovery] discovered={len(class_names)} executed=0")
        for class_name in class_names:
            command = ["java", "-cp", "build", class_name]
            commands.append(" ".join(command))
            executed_class_names.append(class_name)
            start = time.monotonic()
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(target_path),
                    env=_verification_env(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                total_duration += time.monotonic() - start
                stderr_parts.append(_decode_timeout_output(exc.stderr))
                return VerificationResult(
                    status="failed",
                    command=" && ".join(commands),
                    cwd=str(target_path),
                    exit_code=None,
                    duration_seconds=total_duration,
                    stdout=_truncate_output("\n".join(stdout_parts)),
                    stderr=_truncate_output("\n".join(stderr_parts)),
                    reason=f"Discovered Java test class timed out: {class_name}",
                    timed_out=True,
                    discovered_test_entrypoints=tuple(class_names),
                    executed_test_entrypoints=tuple(executed_class_names),
                )
            total_duration += time.monotonic() - start
            if completed.stdout.strip():
                stdout_parts.append(f"[{class_name}]\n{completed.stdout.rstrip()}")
            if completed.stderr.strip():
                stderr_parts.append(f"[{class_name}]\n{completed.stderr.rstrip()}")
            if completed.returncode != 0:
                return VerificationResult(
                    status="failed",
                    command=" && ".join(commands),
                    cwd=str(target_path),
                    exit_code=completed.returncode,
                    duration_seconds=total_duration,
                    stdout=_truncate_output("\n".join(stdout_parts)),
                    stderr=_truncate_output("\n".join(stderr_parts)),
                    reason=f"Discovered Java test class failed: {class_name}",
                    discovered_test_entrypoints=tuple(class_names),
                    executed_test_entrypoints=tuple(executed_class_names),
                )
        stdout_parts.append(
            f"[verification-discovery] discovered={len(class_names)} "
            f"executed={len(executed_class_names)} passed={len(executed_class_names)}"
        )
        return VerificationResult(
            status="passed",
            command=" && ".join(commands),
            cwd=str(target_path),
            exit_code=0,
            duration_seconds=total_duration,
            stdout=_truncate_output("\n".join(stdout_parts)),
            stderr=_truncate_output("\n".join(stderr_parts)),
            reason=f"Baseline verification and {len(class_names)} discovered Java test entrypoint(s) passed.",
            discovered_test_entrypoints=tuple(class_names),
            executed_test_entrypoints=tuple(executed_class_names),
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
    verification_policy: VerificationStagePolicy,
    qa_policy: QaTestPolicy | None,
    run_id: str | None,
) -> tuple[str, ...]:
    if not required_test_files:
        return ()
    if qa_policy is None:
        raise HarnessError(
            "qa_test_policy_invalid",
            "Profiled required test execution requires the immutable QA test policy.",
            run_id=run_id,
        )
    if qa_policy.project_profile_id != verification_policy.project_profile_id:
        raise HarnessError(
            "qa_test_policy_invalid",
            "QA test policy does not belong to the verification project profile.",
            run_id=run_id,
        )
    selected: list[str] = []
    for raw_path in required_test_files:
        path = qa_policy.normalize_test_path(raw_path)
        if not path:
            raise HarnessError(
                "qa_test_out_of_scope",
                f"Required QA test path is outside the profiled Vitest policy: {raw_path}",
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
) -> tuple[tuple[str, ...], str]:
    """Đọc Vitest JSON và đối chiếu đúng tập file QA đã chọn."""

    if not report_path.is_file():
        return (), "Vitest did not produce the required machine-readable JSON report."
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (), "Vitest machine-readable JSON report is missing or invalid."
    test_results = payload.get("testResults") if isinstance(payload, dict) else None
    if not isinstance(test_results, list):
        return (), "Vitest JSON report does not contain a testResults array."

    reported: list[str] = []
    assertion_error = ""
    for item in test_results:
        if not isinstance(item, dict):
            return (), "Vitest JSON report contains an invalid test result entry."
        raw_path = str(item.get("name") or item.get("testFilePath") or "").strip()
        if not raw_path:
            return (), "Vitest JSON report contains a test result without a file path."
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = target_root / candidate
        try:
            relative = candidate.resolve().relative_to(target_root).as_posix()
        except (OSError, ValueError):
            return (), "Vitest JSON report references a test file outside the target repository."
        if relative in reported:
            return tuple(reported), "Vitest JSON report contains a duplicate test file entry."
        reported.append(relative)
        assertion_results = item.get("assertionResults")
        if not isinstance(assertion_results, list) or not assertion_results:
            assertion_error = "Vitest JSON report does not prove that each selected file executed a test."
            continue
        if not all(
            isinstance(assertion, dict)
            and str(assertion.get("status") or "").strip().lower() == "passed"
            for assertion in assertion_results
        ):
            assertion_error = "Vitest JSON report records a non-passing or invalid assertion."

    if set(reported) != set(selected_test_files) or len(reported) != len(selected_test_files):
        return (
            tuple(reported),
            "Vitest JSON report does not match the exact selected QA test files: "
            f"selected={list(selected_test_files)}, reported={reported}.",
        )
    ordered_reported = tuple(path for path in selected_test_files if path in reported)
    if payload.get("success") is not True:
        return ordered_reported, "Vitest JSON report records an unsuccessful test run."
    if assertion_error:
        return ordered_reported, assertion_error
    return ordered_reported, ""


def _profiled_verification_env(spec: VerificationStageSpec | VerificationPreparationSpec) -> dict[str, str]:
    allowed = {key.casefold() for key in spec.inherited_env_keys}
    env = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    env.update(dict(spec.fixed_env))
    return env


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
        completed = subprocess.run(
            [executable, *spec.argv[1:]],
            cwd=str(stage_cwd),
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    missing_command_markers = (
        "is not recognized as an internal or external command",
        "command not found",
        "could not determine executable to run",
    )
    return "environment" if any(marker in combined for marker in missing_command_markers) else "implementation"


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


def _has_compiled_java_output(target_path: Path) -> bool:
    build_path = target_path / "build"
    return build_path.is_dir() and any(build_path.rglob("*.class"))


def _discover_java_main_test_files(target_path: Path) -> list[str]:
    """Tìm Java test source có main() để fallback runner không bỏ sót test."""

    test_root = target_path / "src" / "test" / "java"
    if not test_root.is_dir():
        return []

    discovered: list[str] = []
    for source_path in sorted(test_root.rglob("*.java")):
        if not source_path.is_file():
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        source_without_comments = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", source, flags=re.DOTALL)
        if not JAVA_MAIN_METHOD_PATTERN.search(source_without_comments):
            continue
        discovered.append(source_path.relative_to(target_path).as_posix())
    return discovered


def _java_test_class_name(path: str) -> str:
    normalized = path.strip().replace("\\", "/").lstrip("/")
    prefix = "src/test/java/"
    if not normalized.startswith(prefix) or not normalized.endswith(".java"):
        return ""
    relative = normalized[len(prefix) : -len(".java")]
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        return ""
    return relative.replace("/", ".")


def _test_command_from_work_packet(work_packet: dict[str, object]) -> str:
    intents = work_packet.get("test_intents")
    if isinstance(intents, list):
        for item in intents:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            if command:
                return command
    return DEFAULT_TEST_COMMAND


def _has_profiled_verification_contract(work_packet: dict[str, object]) -> bool:
    stage_ids = work_packet.get("verification_stage_ids")
    if isinstance(stage_ids, list) and any(str(item).strip() for item in stage_ids):
        return True
    intents = work_packet.get("test_intents")
    if not isinstance(intents, list):
        return False
    return any(
        isinstance(item, dict) and bool(str(item.get("stage_id") or "").strip())
        for item in intents
    )


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _verification_env() -> dict[str, str]:
    """Tạo env cho test command, tự thêm JDK vào PATH nếu máy đã cài nhưng shell chưa nhận."""

    env = os.environ.copy()
    java_bin = _resolve_java_bin(env)
    if not java_bin:
        return env

    java_bin_text = str(java_bin)
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    if java_bin_text.lower() not in {part.lower() for part in path_parts}:
        env["PATH"] = os.pathsep.join([java_bin_text, *path_parts])
    env.setdefault("JAVA_HOME", str(java_bin.parent))
    return env


def _resolve_java_bin(env: dict[str, str]) -> Path | None:
    existing = shutil.which("javac", path=env.get("PATH"))
    if existing:
        return Path(existing).resolve().parent

    for key in JAVA_HOME_ENV_KEYS:
        java_bin = _java_bin_from_home(env.get(key))
        if java_bin:
            return java_bin

    for home in _common_java_home_candidates():
        java_bin = _java_bin_from_home(str(home))
        if java_bin:
            return java_bin
    return None


def _java_bin_from_home(home: str | None) -> Path | None:
    if not home:
        return None
    javac = Path(home) / "bin" / _javac_executable_name()
    if javac.exists():
        return javac.parent
    return None


def _javac_executable_name() -> str:
    return "javac.exe" if os.name == "nt" else "javac"


def _common_java_home_candidates() -> list[Path]:
    if os.name != "nt":
        return []

    roots = [
        Path("C:/Program Files/Eclipse Adoptium"),
        Path("C:/Program Files/Java"),
        Path("C:/Program Files/Microsoft"),
        Path("C:/Program Files/BellSoft"),
        Path("C:/Program Files/Zulu"),
        Path("C:/Program Files/Amazon Corretto"),
        Path("C:/Program Files (x86)/Java"),
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir() and ("jdk" in child.name.lower() or "corretto" in child.name.lower()):
                candidates.append(child)
    return sorted(candidates, key=lambda path: path.name.lower(), reverse=True)


def _classify_result(*, exit_code: int, stdout: str, stderr: str) -> tuple[str, str]:
    combined = f"{stdout}\n{stderr}".lower()
    if exit_code != 0:
        return "failed", f"Test command exited with code {exit_code}."
    if re.search(r"(?im)^\s*(?:javac:\s*)?error:", combined) or any(
        marker in combined for marker in FATAL_OUTPUT_MARKERS
    ):
        return "failed", "Test command output contains a fatal compile/runtime error despite exit code 0."
    if "skipped" in combined or "javac not found" in combined or "compiler 'javac' was not found" in combined:
        return "skipped", "Test command reported skipped verification."
    return "passed", "Test command completed successfully."


def _truncate_output(value: str) -> str:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS].rstrip() + "\n... truncated ..."


def _decode_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
