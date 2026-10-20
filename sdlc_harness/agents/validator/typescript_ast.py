"""Bridge fail-closed từ Code Trace sang TypeScript Compiler AST của target."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from ...core.errors import HarnessError
from ...profiles import CodeTracePolicy


TYPESCRIPT_AST_EVIDENCE_VERSION = "typescript_ast_evidence_v1"
TYPESCRIPT_AST_REQUEST_VERSION = "typescript_ast_request_v1"
DEFAULT_TYPESCRIPT_AST_TIMEOUT_SECONDS = 30
MAX_EXTRACTOR_ERROR_CHARS = 4000
_ALLOWED_SYMBOL_KINDS = {"interface", "class", "function", "method"}
_ENV_ALLOWLIST = {
    "path",
    "pathext",
    "systemroot",
    "comspec",
    "temp",
    "tmp",
    "tmpdir",
    "home",
    "userprofile",
    "appdata",
    "localappdata",
}


@dataclass(frozen=True)
class TypeScriptAstEvidence:
    """AST evidence đã validate, không chứa absolute target path."""

    extractor_id: str
    profile: dict[str, object]
    files: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": TYPESCRIPT_AST_EVIDENCE_VERSION,
            "extractor_id": self.extractor_id,
            "profile": self.profile,
            "files": list(self.files),
        }

    def symbols_for(self, path: str) -> set[str]:
        names: set[str] = set()
        for item in self.files:
            if item.get("path") != path:
                continue
            for symbol in _dict_items(item.get("symbols")):
                for key in ("name", "qualified_name"):
                    value = str(symbol.get(key) or "").strip()
                    if value:
                        names.add(value)
        return names


class TypeScriptAstExtractor:
    """Chạy helper code-reviewed bằng Node và package TypeScript của target."""

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_TYPESCRIPT_AST_TIMEOUT_SECONDS,
        run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[..., str | None] = shutil.which,
    ):
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._run_process = run_process
        self._which = which

    def extract(
        self,
        *,
        target_path: Path,
        source_paths: list[str],
        policy: CodeTracePolicy,
        run_id: str | None = None,
    ) -> TypeScriptAstEvidence:
        """Trích xuất evidence cho đúng source paths đã qua policy."""

        target_root = target_path.resolve()
        normalized: list[str] = []
        for raw_path in source_paths:
            path = policy.normalize_source_path(raw_path)
            if not path:
                raise HarnessError(
                    "code_trace_source_out_of_scope",
                    f"Code Trace source path is outside the profile policy: {raw_path}",
                    run_id=run_id,
                )
            if path in normalized:
                continue
            source_file = (target_root / path).resolve()
            try:
                source_file.relative_to(target_root)
            except ValueError as exc:
                raise HarnessError(
                    "code_trace_source_out_of_scope",
                    f"Code Trace source path escapes target repository: {path}",
                    run_id=run_id,
                ) from exc
            if source_file.is_file():
                normalized.append(path)

        if not normalized:
            return TypeScriptAstEvidence(
                extractor_id=policy.symbol_extractor_id,
                profile=policy.metadata(),
                files=(),
            )

        env = {key: value for key, value in os.environ.items() if key.casefold() in _ENV_ALLOWLIST}
        node = self._which("node", path=_environment_value(env, "PATH") or "")
        if not node:
            raise HarnessError(
                "typescript_ast_extractor_unavailable",
                "Node executable is unavailable for the TypeScript AST extractor.",
                run_id=run_id,
            )
        helper_path = Path(__file__).with_name("typescript_ast_extractor.cjs")
        request = {
            "version": TYPESCRIPT_AST_REQUEST_VERSION,
            "extractor_id": policy.symbol_extractor_id,
            "profile": policy.metadata(),
            "files": normalized,
        }
        try:
            completed = self._run_process(
                [node, str(helper_path)],
                cwd=str(target_root),
                env=env,
                shell=False,
                input=json.dumps(request, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessError(
                "typescript_ast_extraction_failed",
                f"TypeScript AST extractor could not complete: {exc}",
                run_id=run_id,
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown extractor error").strip()
            if "TypeScript package is unavailable" in detail:
                error_code = "typescript_ast_extractor_unavailable"
            else:
                error_code = "typescript_ast_extraction_failed"
            raise HarnessError(
                error_code,
                "TypeScript AST extractor failed: " + detail[:MAX_EXTRACTOR_ERROR_CHARS],
                run_id=run_id,
            )
        try:
            payload = json.loads(completed.stdout or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise HarnessError(
                "typescript_ast_extraction_failed",
                "TypeScript AST extractor returned invalid JSON.",
                run_id=run_id,
            ) from exc
        return _validate_evidence(
            payload,
            expected_paths=normalized,
            policy=policy,
            run_id=run_id,
        )


def _validate_evidence(
    value: object,
    *,
    expected_paths: list[str],
    policy: CodeTracePolicy,
    run_id: str | None,
) -> TypeScriptAstEvidence:
    if not isinstance(value, dict):
        raise _invalid_evidence("Extractor response must be a JSON object.", run_id=run_id)
    if value.get("version") != TYPESCRIPT_AST_EVIDENCE_VERSION:
        raise _invalid_evidence("Extractor evidence version is invalid.", run_id=run_id)
    if value.get("extractor_id") != policy.symbol_extractor_id:
        raise _invalid_evidence("Extractor identity does not match Code Trace policy.", run_id=run_id)
    if value.get("profile") != policy.metadata():
        raise _invalid_evidence("Extractor profile metadata does not match Code Trace policy.", run_id=run_id)
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise _invalid_evidence("Extractor evidence files must be an array.", run_id=run_id)

    files: list[dict[str, object]] = []
    seen: list[str] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise _invalid_evidence("Extractor evidence contains an invalid file entry.", run_id=run_id)
        path = policy.normalize_source_path(raw_file.get("path"))
        if not path or path in seen:
            raise _invalid_evidence("Extractor evidence contains an invalid or duplicate path.", run_id=run_id)
        seen.append(path)
        diagnostics = raw_file.get("diagnostics")
        if not isinstance(diagnostics, list):
            raise _invalid_evidence("Extractor diagnostics must be an array.", run_id=run_id)
        if diagnostics:
            raise HarnessError(
                "typescript_ast_parse_failed",
                f"TypeScript AST parser reported syntax diagnostics for {path}.",
                run_id=run_id,
            )
        symbols = _validated_symbols(raw_file.get("symbols"), path=path, run_id=run_id)
        files.append({"path": path, "diagnostics": [], "symbols": symbols})
    if seen != expected_paths:
        raise _invalid_evidence(
            f"Extractor evidence paths do not match the request: expected={expected_paths}, actual={seen}.",
            run_id=run_id,
        )
    return TypeScriptAstEvidence(
        extractor_id=policy.symbol_extractor_id,
        profile=policy.metadata(),
        files=tuple(files),
    )


def _validated_symbols(value: object, *, path: str, run_id: str | None) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _invalid_evidence(f"Extractor symbols must be an array for {path}.", run_id=run_id)
    symbols: list[dict[str, object]] = []
    for raw_symbol in value:
        if not isinstance(raw_symbol, dict):
            raise _invalid_evidence(f"Extractor returned an invalid symbol for {path}.", run_id=run_id)
        kind = str(raw_symbol.get("kind") or "").strip()
        name = str(raw_symbol.get("name") or "").strip()
        qualified_name = str(raw_symbol.get("qualified_name") or "").strip()
        parameters = raw_symbol.get("parameters")
        type_parameters = raw_symbol.get("type_parameters")
        if (
            kind not in _ALLOWED_SYMBOL_KINDS
            or not name
            or not qualified_name
            or not isinstance(parameters, list)
            or not isinstance(type_parameters, list)
            or not all(isinstance(item, dict) for item in parameters)
            or not all(isinstance(item, str) for item in type_parameters)
        ):
            raise _invalid_evidence(f"Extractor symbol schema is invalid for {path}.", run_id=run_id)
        normalized_parameters: list[dict[str, object]] = []
        for parameter in parameters:
            parameter_name = str(parameter.get("name") or "").strip()
            parameter_type = str(parameter.get("type") or "").strip()
            if not parameter_name or not parameter_type:
                raise _invalid_evidence(f"Extractor parameter schema is invalid for {path}.", run_id=run_id)
            normalized_parameters.append(
                {
                    "name": parameter_name,
                    "type": parameter_type,
                    "optional": parameter.get("optional") is True,
                    "rest": parameter.get("rest") is True,
                }
            )
        symbols.append(
            {
                "kind": kind,
                "name": name,
                "qualified_name": qualified_name,
                "parameters": normalized_parameters,
                "return_type": str(raw_symbol.get("return_type") or ""),
                "type_parameters": list(type_parameters),
                "async": raw_symbol.get("async") is True,
                "exported": raw_symbol.get("exported") is True,
            }
        )
    return symbols


def _invalid_evidence(message: str, *, run_id: str | None) -> HarnessError:
    return HarnessError("typescript_ast_evidence_invalid", message, run_id=run_id)


def _environment_value(env: dict[str, str], key: str) -> str:
    expected = key.casefold()
    return next((value for name, value in env.items() if name.casefold() == expected), "")


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
