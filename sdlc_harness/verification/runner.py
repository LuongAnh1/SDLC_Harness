"""Chạy compile/unit test command của target repo cho Phase 4.

Verification Runner là lớp công cụ, không phải AI Tester. Nó nhận
DeveloperWorkPacket đã được Phase 4 chuẩn bị hoặc refresh, chọn command kiểm thử
gần code nhất, chạy trong target repo và trả kết quả có cấu trúc để workflow ghi
`test_report_v{n}.md`. Trên Windows, runner cố gắng thêm JDK phổ biến vào PATH
để các lần chạy sau không kẹt vì shell chưa nhận `javac`.

Khi QA truyền `required_test_files`, runner còn chạy trực tiếp từng Java class vừa sinh sau command baseline; chỉ compile file QA không còn đủ để báo pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

DEFAULT_TEST_COMMAND = "powershell -ExecutionPolicy Bypass -File scripts/test.ps1"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20000
JAVA_HOME_ENV_KEYS = ("SDLC_JAVA_HOME", "JAVA_HOME")


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
    timed_out: bool = False

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
            "timed_out": self.timed_out,
        }


class VerificationRunner:
    """Chạy command kiểm thử của target repo và phân loại pass/fail/skipped."""

    def __init__(self, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = max(1, timeout_seconds)

    def run(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
    ) -> VerificationResult:
        """Chạy command test trong target repo và các class QA bắt buộc nếu có."""

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
        if baseline.status != "passed" or not required_test_files:
            return baseline
        return self._run_required_java_tests(
            target_path=target_path,
            required_test_files=required_test_files,
            baseline=baseline,
        )

    def _run_required_java_tests(
        self,
        *,
        target_path: Path,
        required_test_files: list[str],
        baseline: VerificationResult,
    ) -> VerificationResult:
        """Chạy trực tiếp từng class Java do QA sinh sau command baseline."""

        class_names: list[str] = []
        for path in required_test_files:
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
                    reason=f"Cannot derive Java test class from QA-generated path: {path}",
                )
            if class_name not in class_names:
                class_names.append(class_name)

        stdout_parts = [baseline.stdout.rstrip()] if baseline.stdout.strip() else []
        stderr_parts = [baseline.stderr.rstrip()] if baseline.stderr.strip() else []
        total_duration = baseline.duration_seconds
        commands = [baseline.command]
        for class_name in class_names:
            command = ["java", "-cp", "build", class_name]
            commands.append(" ".join(command))
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
                    reason=f"QA-generated test class timed out: {class_name}",
                    timed_out=True,
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
                    reason=f"QA-generated test class failed: {class_name}",
                )
        return VerificationResult(
            status="passed",
            command=" && ".join(commands),
            cwd=str(target_path),
            exit_code=0,
            duration_seconds=total_duration,
            stdout=_truncate_output("\n".join(stdout_parts)),
            stderr=_truncate_output("\n".join(stderr_parts)),
            reason=f"Baseline verification and {len(class_names)} required QA test class(es) passed.",
        )


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
