"""Chạy compile/unit test command của target repo cho Phase 4.

Verification Runner là lớp công cụ, không phải AI Tester. Nó nhận
DeveloperWorkPacket đã được Phase 4 chuẩn bị, chọn command kiểm thử gần code
nhất, chạy trong target repo và trả kết quả có cấu trúc để workflow ghi
`test_report_v1.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Any

DEFAULT_TEST_COMMAND = "powershell -ExecutionPolicy Bypass -File scripts/test.ps1"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20000


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

    def run(self, *, target_path: Path, work_packet: dict[str, object]) -> VerificationResult:
        """Chạy command test trong target repo."""

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
        return VerificationResult(
            status=status,
            command=command,
            cwd=str(target_path),
            exit_code=completed.returncode,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            reason=reason,
        )


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
