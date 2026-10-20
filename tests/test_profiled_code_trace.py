"""Kiểm thử Code Trace discovery và TypeScript AST bridge theo profile."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

import pytest

from sdlc_harness.agents.validator import CodeTraceValidator
from sdlc_harness.agents.validator.typescript_ast import (
    TYPESCRIPT_AST_EVIDENCE_VERSION,
    TypeScriptAstEvidence,
    TypeScriptAstExtractor,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    CodeTracePolicy,
    build_profile_snapshot,
    code_trace_policy_from_snapshot,
    get_project_profile,
)
from sdlc_harness.workflows.phase4 import _code_trace_policy_for_state


class AcceptingCodeTraceLlm:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompt = prompt
        return json.dumps(
            {
                "ok": True,
                "confidence": 0.95,
                "summary": "Profiled code trace accepted.",
                "graph_edges": [],
                "missing_links": [],
                "risks": [],
            }
        )


class StaticAstExtractor:
    def __init__(self, symbols: list[dict[str, object]]) -> None:
        self.symbols = symbols
        self.calls: list[dict[str, object]] = []

    def extract(
        self,
        *,
        target_path: Path,
        source_paths: list[str],
        policy: CodeTracePolicy,
        run_id: str | None = None,
    ) -> TypeScriptAstEvidence:
        self.calls.append(
            {
                "target_path": target_path,
                "source_paths": source_paths,
                "run_id": run_id,
            }
        )
        return TypeScriptAstEvidence(
            extractor_id=policy.symbol_extractor_id,
            profile=policy.metadata(),
            files=(
                {
                    "path": source_paths[0],
                    "diagnostics": [],
                    "symbols": self.symbols,
                },
            ),
        )


@pytest.fixture
def code_trace_policy() -> CodeTracePolicy:
    project = get_project_profile("typescript_fullstack_v1")
    snapshot = build_profile_snapshot(
        project_profile_id=project.profile_id,
        target_remote="https://example.invalid/target.git",
        target_base_branch="main",
    ).bind_workspace(
        target_manifest_schema=project.target_manifest_schema,
        target_manifest_digest="sha256:" + "1" * 64,
        base_sha="2" * 40,
        resolved_component_roots=tuple(
            (component.component_id, component.editable_roots) for component in project.components
        ),
        run_id="run-profiled-code-trace",
    )
    return code_trace_policy_from_snapshot(snapshot, run_id="run-profiled-code-trace")


def _symbol(name: str, *, qualified_name: str | None = None) -> dict[str, object]:
    return {
        "kind": "class",
        "name": name,
        "qualified_name": qualified_name or name,
        "parameters": [],
        "return_type": "",
        "type_parameters": [],
        "async": False,
        "exported": True,
    }


def _work_packet(*, source_path: str, test_path: str, symbol: str) -> dict[str, object]:
    target = f"{source_path}#{symbol}"
    return {
        "run_id": "run-profiled-code-trace",
        "issue_key": "SDLC-TS-1",
        "requirement_contract": {"version": "v2", "revision": 1},
        "canonical_requirements": [{"id": "REQ-1", "content": "Delete the selected item."}],
        "requirement_coverage": [
            {
                "requirement_id": "REQ-1",
                "content": "Delete the selected item.",
                "design_targets": [symbol],
                "design_element_ids": [f"class:{symbol}"],
                "planned_code_targets": [target],
                "test_intents": ["Verify deletion."],
            }
        ],
        "design_mappings_by_file": {
            source_path: [
                {
                    "design_element_id": f"class:{symbol}",
                    "design_element_type": "class",
                    "design_element_name": symbol,
                    "planned_code_target": target,
                }
            ]
        },
        "proposed_files": [{"path": source_path}, {"path": test_path}],
        "output_contract": {"allowed_paths": [source_path, test_path]},
    }


def test_code_trace_policy_accepts_only_profiled_source_and_colocated_tests(
    code_trace_policy: CodeTracePolicy,
) -> None:
    assert code_trace_policy.normalize_source_path("apps/api/src/DeleteService.ts") == (
        "apps/api/src/DeleteService.ts"
    )
    assert code_trace_policy.normalize_test_path("apps/api/src/DeleteService.test.ts") == (
        "apps/api/src/DeleteService.test.ts"
    )
    assert code_trace_policy.normalize_test_path("apps/web/src/DeletePage.test.tsx") == (
        "apps/web/src/DeletePage.test.tsx"
    )
    assert code_trace_policy.normalize_source_path("src/main/java/DeleteService.java") == ""
    assert code_trace_policy.normalize_test_path("tests/e2e/delete.spec.ts") == ""
    assert code_trace_policy.normalize_source_path("../apps/api/src/DeleteService.ts") == ""
    assert code_trace_policy.normalize_source_path("apps\\api\\src\\DeleteService.ts") == ""


def test_phase4_resolves_code_trace_policy_only_from_bound_snapshot(
    code_trace_policy: CodeTracePolicy,
) -> None:
    project = get_project_profile("typescript_fullstack_v1")
    snapshot = build_profile_snapshot(
        project_profile_id=project.profile_id,
        target_remote="https://example.invalid/target.git",
        target_base_branch="main",
    ).bind_workspace(
        target_manifest_schema=project.target_manifest_schema,
        target_manifest_digest="sha256:" + "3" * 64,
        base_sha="4" * 40,
        resolved_component_roots=tuple(
            (component.component_id, component.editable_roots) for component in project.components
        ),
    )

    resolved = _code_trace_policy_for_state(
        {"profile_snapshot": snapshot.to_dict()},
        run_id="run-profiled-code-trace",
    )

    assert resolved is not None
    assert resolved.metadata() == code_trace_policy.metadata()
    assert _code_trace_policy_for_state({}, run_id="run-legacy") is None


def test_typescript_ast_bridge_uses_fixed_helper_and_validates_evidence(
    code_trace_policy: CodeTracePolicy,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        source.parent.mkdir(parents=True)
        source.write_text("export class DeleteService {}\n", encoding="utf-8")
        calls: list[dict[str, object]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append({"argv": argv, **kwargs})
            payload = {
                "version": TYPESCRIPT_AST_EVIDENCE_VERSION,
                "extractor_id": code_trace_policy.symbol_extractor_id,
                "profile": code_trace_policy.metadata(),
                "files": [
                    {
                        "path": source_path,
                        "diagnostics": [],
                        "symbols": [
                            _symbol("DeleteService"),
                            {
                                "kind": "method",
                                "name": "delete",
                                "qualified_name": "DeleteService.delete",
                                "parameters": [
                                    {"name": "ids", "type": "string[]", "optional": True, "rest": True}
                                ],
                                "return_type": "Promise<void>",
                                "type_parameters": ["T extends string"],
                                "async": True,
                                "exported": False,
                            },
                        ],
                    }
                ],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        evidence = TypeScriptAstExtractor(
            run_process=fake_run,
            which=lambda command, **kwargs: "C:/Program Files/nodejs/node.exe",
        ).extract(
            target_path=target,
            source_paths=[source_path],
            policy=code_trace_policy,
            run_id="run-profiled-code-trace",
        )

    assert evidence.symbols_for(source_path) == {
        "DeleteService",
        "delete",
        "DeleteService.delete",
    }
    assert len(calls) == 1
    assert calls[0]["argv"][0] == "C:/Program Files/nodejs/node.exe"
    assert str(calls[0]["argv"][1]).endswith("typescript_ast_extractor.cjs")
    assert calls[0]["shell"] is False
    assert calls[0]["cwd"] == str(target.resolve())
    request = json.loads(str(calls[0]["input"]))
    assert request["files"] == [source_path]
    assert request["profile"] == code_trace_policy.metadata()
    assert str(target.resolve()) not in json.dumps(evidence.to_dict())


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "error_code"),
    [
        (2, "", "Target TypeScript package is unavailable: MODULE_NOT_FOUND", "typescript_ast_extractor_unavailable"),
        (0, "not-json", "", "typescript_ast_extraction_failed"),
    ],
)
def test_typescript_ast_bridge_fails_closed_on_runtime_or_contract_errors(
    code_trace_policy: CodeTracePolicy,
    returncode: int,
    stdout: str,
    stderr: str,
    error_code: str,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        source.parent.mkdir(parents=True)
        source.write_text("export class DeleteService {}\n", encoding="utf-8")
        extractor = TypeScriptAstExtractor(
            run_process=lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout, stderr),
            which=lambda command, **kwargs: "node",
        )

        with pytest.raises(HarnessError) as caught:
            extractor.extract(
                target_path=target,
                source_paths=[source_path],
                policy=code_trace_policy,
                run_id="run-profiled-code-trace",
            )

    assert caught.value.error_code == error_code
    assert caught.value.run_id == "run-profiled-code-trace"


def test_typescript_ast_bridge_fails_closed_when_node_is_unavailable(
    code_trace_policy: CodeTracePolicy,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        source.parent.mkdir(parents=True)
        source.write_text("export class DeleteService {}\n", encoding="utf-8")

        with pytest.raises(HarnessError) as caught:
            TypeScriptAstExtractor(which=lambda command, **kwargs: None).extract(
                target_path=target,
                source_paths=[source_path],
                policy=code_trace_policy,
                run_id="run-profiled-code-trace",
            )

    assert caught.value.error_code == "typescript_ast_extractor_unavailable"


@pytest.mark.integration
def test_real_typescript_ast_helper_fails_closed_without_target_package(
    code_trace_policy: CodeTracePolicy,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        source.parent.mkdir(parents=True)
        source.write_text("export class DeleteService {}\n", encoding="utf-8")

        with pytest.raises(HarnessError) as caught:
            TypeScriptAstExtractor().extract(
                target_path=target,
                source_paths=[source_path],
                policy=code_trace_policy,
                run_id="run-profiled-code-trace",
            )

    assert caught.value.error_code == "typescript_ast_extractor_unavailable"


def test_typescript_ast_bridge_rejects_parse_diagnostics_and_mismatched_paths(
    code_trace_policy: CodeTracePolicy,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        source.parent.mkdir(parents=True)
        source.write_text("export class DeleteService {}\n", encoding="utf-8")

        def response(*, path: str, diagnostics: list[dict[str, object]]) -> str:
            return json.dumps(
                {
                    "version": TYPESCRIPT_AST_EVIDENCE_VERSION,
                    "extractor_id": code_trace_policy.symbol_extractor_id,
                    "profile": code_trace_policy.metadata(),
                    "files": [{"path": path, "diagnostics": diagnostics, "symbols": []}],
                }
            )

        for output, error_code in (
            (response(path=source_path, diagnostics=[{"code": 1005}]), "typescript_ast_parse_failed"),
            (response(path="apps/api/src/OtherService.ts", diagnostics=[]), "typescript_ast_evidence_invalid"),
        ):
            extractor = TypeScriptAstExtractor(
                run_process=lambda argv, output=output, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
                which=lambda command, **kwargs: "node",
            )
            with pytest.raises(HarnessError) as caught:
                extractor.extract(
                    target_path=target,
                    source_paths=[source_path],
                    policy=code_trace_policy,
                    run_id="run-profiled-code-trace",
                )
            assert caught.value.error_code == error_code


def test_profiled_code_trace_uses_exact_ast_symbol_evidence(
    code_trace_policy: CodeTracePolicy,
) -> None:
    source_path = "apps/api/src/DeleteService.ts"
    test_path = "apps/api/src/DeleteService.test.ts"
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir)
        source = target / source_path
        test = target / test_path
        source.parent.mkdir(parents=True)
        source.write_text(
            "// DeleteService is mentioned, but the declaration has another name.\nexport class OtherService {}\n",
            encoding="utf-8",
        )
        test.write_text("export {};\n", encoding="utf-8")
        packet = _work_packet(source_path=source_path, test_path=test_path, symbol="DeleteService")

        missing = CodeTraceValidator(
            AcceptingCodeTraceLlm(),
            symbol_extractor=StaticAstExtractor([_symbol("OtherService")]),
        ).validate(
            target_path=target,
            work_packet=packet,
            code_trace_policy=code_trace_policy,
        )

        llm = AcceptingCodeTraceLlm()
        exact = CodeTraceValidator(
            llm,
            symbol_extractor=StaticAstExtractor([_symbol("DeleteService")]),
        ).validate(
            target_path=target,
            work_packet=packet,
            code_trace_policy=code_trace_policy,
        )

    assert missing.ok is False
    assert any(item.get("type") == "missing_code_symbol" for item in missing.missing_links)
    assert exact.ok is True
    assert exact.mappings[0]["code_evidence"][0]["symbol_evidence_source"] == (
        "typescript_compiler_ast_v1"
    )
    assert exact.symbol_evidence["version"] == TYPESCRIPT_AST_EVIDENCE_VERSION
    prompt = json.loads(llm.prompt.split("\n\n", 1)[1])
    assert prompt["symbol_evidence"]["extractor_id"] == "typescript_compiler_ast_v1"


def test_profiled_code_trace_rejects_browser_e2e_as_unit_test_evidence(
    code_trace_policy: CodeTracePolicy,
) -> None:
    packet = _work_packet(
        source_path="apps/api/src/DeleteService.ts",
        test_path="tests/e2e/delete.spec.ts",
        symbol="DeleteService",
    )

    with tempfile.TemporaryDirectory() as temp_dir, pytest.raises(HarnessError) as caught:
        CodeTraceValidator(
            AcceptingCodeTraceLlm(),
            symbol_extractor=StaticAstExtractor([_symbol("DeleteService")]),
        ).validate(
            target_path=Path(temp_dir),
            work_packet=packet,
            code_trace_policy=code_trace_policy,
        )

    assert caught.value.error_code == "code_trace_test_out_of_scope"


def test_profiled_code_trace_rejects_source_outside_profile(
    code_trace_policy: CodeTracePolicy,
) -> None:
    packet = _work_packet(
        source_path="src/main/java/DeleteService.java",
        test_path="apps/api/src/DeleteService.test.ts",
        symbol="DeleteService",
    )

    with tempfile.TemporaryDirectory() as temp_dir, pytest.raises(HarnessError) as caught:
        CodeTraceValidator(
            AcceptingCodeTraceLlm(),
            symbol_extractor=StaticAstExtractor([_symbol("DeleteService")]),
        ).validate(
            target_path=Path(temp_dir),
            work_packet=packet,
            code_trace_policy=code_trace_policy,
        )

    assert caught.value.error_code == "code_trace_source_out_of_scope"
