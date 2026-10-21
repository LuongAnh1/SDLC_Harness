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


@dataclass(frozen=True)
class VerificationPreparationSpec:
    """Bước chuẩn bị cố định, tách khỏi ordered stage contract của plan."""

    preparation_id: str
    argv: tuple[str, ...]
    cwd: str
    inherited_env_keys: tuple[str, ...]
    fixed_env: tuple[tuple[str, str], ...]
    timeout_seconds: int
    trigger_path: str
    fingerprint_paths: tuple[str, ...]
    success_marker_path: str

    def evidence(self) -> dict[str, object]:
        return {
            "preparation_id": self.preparation_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "trigger_path": self.trigger_path,
            "fingerprint_paths": list(self.fingerprint_paths),
            "success_marker_path": self.success_marker_path,
        }


_TYPESCRIPT_FULLSTACK_V1_PREPARATIONS = (
    VerificationPreparationSpec(
        preparation_id="dependency_setup",
        argv=("npm", "ci"),
        cwd=".",
        inherited_env_keys=PROFILED_ENV_ALLOWLIST,
        fixed_env=PROFILED_FIXED_ENV,
        timeout_seconds=300,
        trigger_path="package-lock.json",
        fingerprint_paths=("package-lock.json",),
        success_marker_path="node_modules/.sdlc-harness-dependency-setup.json",
    ),
    VerificationPreparationSpec(
        preparation_id="generated_source_setup",
        argv=("npm", "run", "db:generate"),
        cwd=".",
        inherited_env_keys=PROFILED_ENV_ALLOWLIST,
        fixed_env=PROFILED_FIXED_ENV,
        timeout_seconds=120,
        trigger_path="packages/db/prisma/schema.prisma",
        fingerprint_paths=(
            "package-lock.json",
            "packages/db/package.json",
            "packages/db/prisma/schema.prisma",
            "packages/db/prisma7.config.ts",
        ),
        success_marker_path="node_modules/.sdlc-harness-generated-source-setup.json",
    ),
)


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

_PREPARATION_SPECS_BY_PROJECT = {
    "typescript_fullstack_v1": _TYPESCRIPT_FULLSTACK_V1_PREPARATIONS,
}


def resolve_verification_preparation_specs(
    policy: VerificationStagePolicy,
    *,
    run_id: str | None = None,
) -> tuple[VerificationPreparationSpec, ...]:
    """Resolve ordered preparation đã review, không nhận argv từ LLM hay work packet."""

    if policy.policy_version != VERIFICATION_STAGE_POLICY_VERSION:
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Unsupported verification stage policy version: {policy.policy_version}",
            run_id=run_id,
        )
    return _PREPARATION_SPECS_BY_PROJECT.get(policy.project_profile_id, ())


def resolve_verification_preparation_spec(
    policy: VerificationStagePolicy,
    *,
    run_id: str | None = None,
) -> VerificationPreparationSpec | None:
    """Giữ API tương thích cho caller cũ cần dependency preparation đầu tiên."""

    specs = resolve_verification_preparation_specs(policy, run_id=run_id)
    return specs[0] if specs else None


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
