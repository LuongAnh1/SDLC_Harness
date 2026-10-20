"""Chạy compile/unit test command của target repo cho Phase 4.

Verification Runner là lớp công cụ, không phải AI Tester. Nó nhận
DeveloperWorkPacket đã được Phase 4 chuẩn bị hoặc refresh, chọn command kiểm thử
gần code nhất, chạy trong target repo và trả kết quả có cấu trúc để workflow ghi
`test_report_v{n}.md`. Trên Windows, runner cố gắng thêm JDK phổ biến vào PATH
để các lần chạy sau không kẹt vì shell chưa nhận `javac`.

Sau command baseline, runner khám phá mọi Java test entrypoint có `main()` dưới
`src/test/java`, hợp nhất chúng với `required_test_files` của QA và chạy trực tiếp
từng class. Chỉ compile test source không còn đủ để báo pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from ..events import emit_current

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
    discovered_test_entrypoints: tuple[str, ...] = ()
    executed_test_entrypoints: tuple[str, ...] = ()

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
            "discovered_test_entrypoints": list(self.discovered_test_entrypoints),
            "executed_test_entrypoints": list(self.executed_test_entrypoints),
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
        """Run verification and publish bounded result metadata."""

        result = self._run_internal(
            target_path=target_path,
            work_packet=work_packet,
            required_test_files=required_test_files,
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
            },
        )
        return result

    def _run_internal(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
    ) -> VerificationResult:
        """Chạy baseline command rồi chạy mọi Java test entrypoint đã khám phá."""

        if _has_profiled_verification_contract(work_packet):
            return VerificationResult(
                status="failed",
                command="",
                cwd=str(target_path),
                exit_code=None,
                duration_seconds=0.0,
                stdout="",
                stderr="",
                reason=(
                    "Profile-owned verification stages require the A3 stage executor; "
                    "the legacy Java command runner will not execute them."
                ),
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
