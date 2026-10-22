"""Resolve ngữ cảnh sinh code từ DeveloperWorkPacket đã được phê duyệt."""

from __future__ import annotations

from pathlib import PurePosixPath

from ...core.errors import HarnessError
from ...profiles import (
    IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION,
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    get_language_profile,
    implementation_signature_contract_digest,
    is_profiled_work_packet_schema,
    resolve_language_prompt_directive,
)


def developer_generation_context(work_packet: dict[str, object]) -> dict[str, object]:
    """Tạo context prompt bất biến; không cho LLM tự chọn language/profile."""

    allowed_paths = _allowed_paths(work_packet)
    if not allowed_paths:
        raise HarnessError(
            "developer_work_packet_allowed_paths_missing",
            "DeveloperWorkPacket has no allowed paths.",
        )

    schema_version = str(work_packet.get("schema_version") or "")
    if not is_profiled_work_packet_schema(schema_version):
        raise _profile_error(
            "Developer execution requires a profiled DeveloperWorkPacket; legacy batch packets are retired."
        )

    output_contract = _dict_value(work_packet.get("output_contract"))
    path_policy = _dict_value(output_contract.get("path_policy"))
    if not path_policy and schema_version != PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION:
        path_policy = _dict_value(work_packet.get("planning_profile"))
    project_profile_id = str(path_policy.get("project_profile_id") or "").strip()
    language_profile_id = str(path_policy.get("language_profile_id") or "").strip()
    policy_version = str(
        path_policy.get("planning_policy_version") or path_policy.get("policy_version") or ""
    ).strip()
    if not project_profile_id or not language_profile_id or not policy_version:
        raise _profile_error("Profiled DeveloperWorkPacket has incomplete editable path policy metadata.")

    try:
        language = get_language_profile(language_profile_id)
        directive = resolve_language_prompt_directive(language)
    except HarnessError as exc:
        raise _profile_error(exc.message) from exc

    signature_contract = _dict_value(work_packet.get("implementation_signature_contract"))
    if schema_version == PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION:
        if (
            signature_contract.get("version") != IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION
            or signature_contract.get("digest")
            != implementation_signature_contract_digest(signature_contract)
        ):
            raise _profile_error("Developer signature contract version or digest is invalid.")
        expected_profile = {
            "project_profile_id": project_profile_id,
            "language_profile_id": language_profile_id,
            "policy_version": policy_version,
        }
        if signature_contract.get("profile") != expected_profile:
            raise _profile_error(
                "Developer signature contract does not match the frozen editable path policy."
            )

    signatures_by_file: dict[str, list[dict[str, object]]] = {}
    for entry in _dict_items(signature_contract.get("entries")):
        source_path = str(entry.get("source_path") or "").strip().replace("\\", "/")
        if source_path not in allowed_paths:
            raise _profile_error(
                f"Developer signature contract references a path outside allowed_paths: {source_path}"
            )
        signatures_by_file.setdefault(source_path, []).append(entry)

    return {
        "mode": "profiled_v1",
        "profiled": True,
        "project_profile_id": project_profile_id,
        "language_profile_id": language_profile_id,
        "planning_policy_version": policy_version,
        "prompt_directive": directive.developer_plan_context(),
        "language_name": directive.language_name,
        "example_path": _example_path(allowed_paths),
        "rules": list(directive.developer_plan_rules),
        "implementation_signature_contract": signature_contract,
        "signatures_by_file": signatures_by_file,
    }


def developer_prompt_context(work_packet: dict[str, object]) -> dict[str, object]:
    """Context gọn cho prompt; exact signatures vẫn nằm trong packet/item tương ứng."""

    resolved = developer_generation_context(work_packet)
    signature_contract = _dict_value(resolved.pop("implementation_signature_contract", {}))
    resolved.pop("signatures_by_file", None)
    resolved["implementation_signature_contract_digest"] = str(
        signature_contract.get("digest") or ""
    )
    return resolved


def developer_path_is_test(path: str) -> bool:
    """Nhận diện test path trong các component TypeScript đã profile hóa."""

    normalized = "/" + path.strip().replace("\\", "/").casefold().lstrip("/")
    name = PurePosixPath(normalized).name
    return (
        "/src/test/" in normalized
        or normalized.startswith("/test/")
        or normalized.startswith("/tests/")
        or "/__tests__/" in normalized
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def _example_path(paths: list[str]) -> str:
    production_typescript = [
        path
        for path in paths
        if path.casefold().endswith((".ts", ".tsx")) and not developer_path_is_test(path)
    ]
    production = [path for path in paths if not developer_path_is_test(path)]
    return (production_typescript or production or paths)[0]


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    contract = _dict_value(work_packet.get("output_contract"))
    values = contract.get("allowed_paths")
    if not isinstance(values, list):
        return []
    return [
        str(value).strip().replace("\\", "/")
        for value in values
        if str(value).strip()
    ]


def _profile_error(message: str) -> HarnessError:
    return HarnessError("developer_prompt_profile_invalid", message)


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
