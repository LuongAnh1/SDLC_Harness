"""Deterministic fake Developer output dùng chung cho unit test và fake LLM."""

from __future__ import annotations

from collections import defaultdict
import re

from ...core.errors import HarnessError
from .profile_context import developer_generation_context, developer_path_is_test


def fake_developer_files(work_packet: dict[str, object]) -> list[dict[str, object]]:
    context = developer_generation_context(work_packet)
    allowed_paths = _allowed_paths(work_packet)
    mappings_by_file = _dict_value(work_packet.get("design_mappings_by_file"))
    files: list[dict[str, object]] = []
    if not bool(context.get("profiled")):
        raise HarnessError(
            "developer_profile_context_required",
            "Fake Developer generation requires the immutable project profile context.",
            run_id=str(work_packet.get("run_id") or "") or None,
        )
    for path in allowed_paths:
        content = _profiled_content(
            path,
            signatures=_dict_items(_dict_value(context.get("signatures_by_file")).get(path)),
            mappings=_dict_items(mappings_by_file.get(path)),
        )
        files.append(
            {
                "path": path,
                "operation": "create_or_update",
                "content": content,
                "reason": "Deterministic fake Developer generated this approved file.",
            }
        )
    if not files:
        raise HarnessError("developer_act_failed", "DeveloperWorkPacket has no allowed paths.")
    return files


def _profiled_content(
    path: str,
    *,
    signatures: list[dict[str, object]],
    mappings: list[dict[str, object]],
) -> str:
    lowered = path.casefold()
    if developer_path_is_test(path):
        return _typescript_test_content(path)
    if lowered.endswith(".prisma"):
        return "// Fake profiled Developer leaves schema definition to an approved database slice.\n"
    if lowered.endswith(".sql"):
        return "-- Fake profiled Developer migration placeholder for the approved slice.\n"
    if not lowered.endswith((".ts", ".tsx")):
        return "// Fake profiled Developer output.\n"
    if not signatures:
        declarations = _mapped_declarations(mappings)
        return declarations or "export {};\n"
    return _typescript_declarations(signatures)


def _mapped_declarations(mappings: list[dict[str, object]]) -> str:
    declarations: list[str] = []
    seen: set[str] = set()
    for mapping in mappings:
        design_id = str(mapping.get("design_element_id") or "").strip()
        kind, separator, name = design_id.partition(":")
        if not separator or not name or "." in name or name in seen:
            continue
        seen.add(name)
        if kind == "interface":
            declarations.append(f"export interface {name} {{}}")
        elif kind in {"class", "abstract"}:
            prefix = "abstract " if kind == "abstract" else ""
            declarations.append(f"export {prefix}class {name} {{}}")
    return "\n\n".join(declarations).rstrip() + "\n" if declarations else ""


def _typescript_declarations(signatures: list[dict[str, object]]) -> str:
    functions: list[dict[str, object]] = []
    owners: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for entry in signatures:
        if str(entry.get("symbol_kind") or "") == "function":
            functions.append(entry)
            continue
        qualified = str(entry.get("qualified_name") or "")
        owner, _, _ = qualified.rpartition(".")
        owners[(owner or "GeneratedService", str(entry.get("owner_kind") or "class"))].append(entry)

    lines: list[str] = [f"export type {name} = unknown;" for name in _placeholder_types(signatures)]
    if lines:
        lines.append("")
    for entry in functions:
        name = str(entry.get("qualified_name") or "generated").split(".")[-1]
        lines.extend(_render_callable(entry, name=name, indent="", prefix="export "))
        lines.append("")
    for (owner, owner_kind), entries in owners.items():
        if owner_kind == "interface":
            lines.append(f"export interface {owner} {{")
            for entry in entries:
                name = str(entry.get("qualified_name") or "generated").split(".")[-1]
                lines.append(f"  {_signature(entry, name=name)};")
            lines.append("}")
        else:
            keyword = "abstract class" if owner_kind == "abstract" else "class"
            lines.append(f"export {keyword} {owner} {{")
            for entry in entries:
                name = str(entry.get("qualified_name") or "generated").split(".")[-1]
                if owner_kind == "abstract":
                    lines.append(f"  abstract {_signature(entry, name=name)};")
                else:
                    lines.extend(_render_callable(entry, name=name, indent="  "))
            lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _placeholder_types(signatures: list[dict[str, object]]) -> list[str]:
    builtins = {
        "Array",
        "BigInt",
        "Boolean",
        "Date",
        "Error",
        "Map",
        "Number",
        "Object",
        "Promise",
        "ReadonlyArray",
        "Record",
        "Set",
        "String",
        "Symbol",
        "Uint8Array",
    }
    owners = {
        str(entry.get("qualified_name") or "").split(".")[0]
        for entry in signatures
    }
    type_parameters = {
        re.split(r"\s+extends\s+|\s*=\s*", value, maxsplit=1)[0].strip()
        for entry in signatures
        for value in _string_items(entry.get("type_parameters"))
    }
    referenced: set[str] = set()
    for entry in signatures:
        values = [str(entry.get("return_type") or "")]
        values.extend(str(item.get("type") or "") for item in _dict_items(entry.get("parameters")))
        for value in values:
            referenced.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", value))
    return sorted(referenced - builtins - owners - type_parameters)


def _render_callable(
    entry: dict[str, object],
    *,
    name: str,
    indent: str,
    prefix: str = "",
) -> list[str]:
    async_prefix = "async " if entry.get("async") is True else ""
    result = [f"{indent}{prefix}{async_prefix}{_signature(entry, name=name)} {{"]
    for parameter in _dict_items(entry.get("parameters")):
        parameter_name = str(parameter.get("name") or "value").strip()
        if parameter_name:
            result.append(f"{indent}  void {parameter_name};")
    result.append(f'{indent}  throw new Error("Deterministic fake implementation");')
    result.append(f"{indent}}}")
    return result


def _signature(entry: dict[str, object], *, name: str) -> str:
    type_parameters = _string_items(entry.get("type_parameters"))
    generics = f"<{', '.join(type_parameters)}>" if type_parameters else ""
    parameters = ", ".join(_render_parameter(item) for item in _dict_items(entry.get("parameters")))
    return_type = str(entry.get("return_type") or "void").strip()
    return f"{name}{generics}({parameters}): {return_type}"


def _render_parameter(parameter: dict[str, object]) -> str:
    name = str(parameter.get("name") or "value").strip()
    type_name = str(parameter.get("type") or "unknown").strip()
    rest = "..." if parameter.get("rest") is True else ""
    optional = "?" if parameter.get("optional") is True and not rest else ""
    return f"{rest}{name}{optional}: {type_name}"


def _typescript_test_content(path: str) -> str:
    suite = re.sub(r"[^A-Za-z0-9]+", " ", path.rsplit("/", 1)[-1]).strip()
    return f'''import {{ describe, expect, it }} from "vitest";

describe("{suite}", () => {{
  it("keeps the deterministic profiled fixture executable", () => {{
    expect(true).toBe(true);
  }});
}});
'''


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    output = _dict_value(work_packet.get("output_contract"))
    values = output.get("allowed_paths")
    if not isinstance(values, list):
        return []
    return [str(value).strip().replace("\\", "/") for value in values if str(value).strip()]


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
