"""Registry thực thi đã review cho các stage kiểm chứng theo project profile.

Project profile chỉ công bố stage ID bất biến. Module này thuộc tầng
Verification Runner và là nơi duy nhất ánh xạ stage ID sang argv/cwd/env/timeout
có thể thực thi; output của LLM hoặc Developer Work Packet không được phép ghi
đè các giá trị này.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import HarnessError
from ..profiles import VERIFICATION_STAGE_POLICY_VERSION, VerificationStagePolicy


DEFAULT_STAGE_TIMEOUT_SECONDS = 120
PROFILED_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "ComSpec",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)
PROFILED_FIXED_ENV = (
    ("CI", "1"),
    ("NO_COLOR", "1"),
    ("FORCE_COLOR", "0"),
)


@dataclass(frozen=True)
class VerificationStageSpec:
    """Cấu hình thực thi bất biến của một stage đã được review trong code."""

    stage_id: str
    argv: tuple[str, ...]
    cwd: str
    inherited_env_keys: tuple[str, ...]
    fixed_env: tuple[tuple[str, str], ...]
    timeout_seconds: int
    required_package_script: str
    required_test_framework: str
    machine_reporter_args: tuple[str, ...]

    def evidence(self) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "required_test_framework": self.required_test_framework,
            "machine_reporter_args": list(self.machine_reporter_args),
        }


_TYPESCRIPT_FULLSTACK_V1_SPECS = (
    VerificationStageSpec(
        stage_id="typecheck",
        argv=("npm", "run", "typecheck"),
        cwd=".",
        inherited_env_keys=PROFILED_ENV_ALLOWLIST,
        fixed_env=PROFILED_FIXED_ENV,
        timeout_seconds=DEFAULT_STAGE_TIMEOUT_SECONDS,
        required_package_script="typecheck",
        required_test_framework="",
        machine_reporter_args=(),
    ),
    VerificationStageSpec(
        stage_id="unit",
        argv=("npm", "run", "test:unit"),
        cwd=".",
        inherited_env_keys=PROFILED_ENV_ALLOWLIST,
        fixed_env=PROFILED_FIXED_ENV,
        timeout_seconds=DEFAULT_STAGE_TIMEOUT_SECONDS,
        required_package_script="test:unit",
        required_test_framework="vitest",
        machine_reporter_args=("--reporter=json", "--outputFile={report_path}"),
    ),
)

_STAGE_SPECS_BY_PROJECT = {
    "typescript_fullstack_v1": _TYPESCRIPT_FULLSTACK_V1_SPECS,
}


def resolve_verification_stage_specs(
    policy: VerificationStagePolicy,
    *,
    run_id: str | None = None,
) -> tuple[VerificationStageSpec, ...]:
    """Resolve exact ordered specs và chặn policy bị đổi sau khi snapshot."""

    if policy.policy_version != VERIFICATION_STAGE_POLICY_VERSION:
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Unsupported verification stage policy version: {policy.policy_version}",
            run_id=run_id,
        )
    specs = _STAGE_SPECS_BY_PROJECT.get(policy.project_profile_id)
    if not specs:
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Project profile has no executable verification registry: {policy.project_profile_id}",
            run_id=run_id,
        )
    expected_stage_ids = tuple(spec.stage_id for spec in specs)
    if policy.stage_ids != expected_stage_ids:
        raise HarnessError(
            "verification_stage_policy_invalid",
            "Verification stage order does not match the reviewed executable registry.",
            run_id=run_id,
        )
    return specs
