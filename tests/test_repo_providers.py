import json
from pathlib import Path

from sdlc_harness.repo_context.providers import provider_files


def _write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


def _workspace_repo(root: Path) -> None:
    _write(root, "package.json", json.dumps({"name": "repo", "workspaces": ["apps/*", "packages/*"]}))
    _write(root, "packages/db/package.json", json.dumps({"name": "@t/db", "exports": {".": "./src/index.ts"}}))
    _write(
        root,
        "packages/db/src/index.ts",
        'export { default as articleUpdate } from "./article/articleUpdate";\nexport * from "./user";\n',
    )
    _write(root, "packages/db/src/article/articleUpdate.ts", "export default async function articleUpdate() {}\n")
    _write(root, "packages/db/src/user.ts", "export const userGet = () => null;\n")
    _write(root, "apps/api/package.json", json.dumps({"name": "@t/api"}))
    _write(root, "apps/api/src/view/viewer.ts", "export default function viewer() {}\n")
    _write(
        root,
        "apps/api/src/update.ts",
        'import { Request } from "express";\n'
        "import {\n  articleUpdate,\n  type userGet,\n} from \"@t/db\";\n"
        'import viewer from "./view/viewer.js";\n'
        'import "./side-effect";\n',
    )


def test_follows_relative_imports_workspace_entries_and_barrels(tmp_path: Path) -> None:
    _workspace_repo(tmp_path)

    rows = provider_files(target_path=tmp_path, source_paths=["apps/api/src/update.ts"])

    # `express` is an external package and the side-effect import names nothing: both are skipped.
    assert rows == [
        {
            "path": "packages/db/src/article/articleUpdate.ts",
            "imported_by": ["apps/api/src/update.ts"],
            "names": ["articleUpdate"],
        },
        {"path": "packages/db/src/user.ts", "imported_by": ["apps/api/src/update.ts"], "names": ["userGet"]},
        {"path": "apps/api/src/view/viewer.ts", "imported_by": ["apps/api/src/update.ts"], "names": ["default"]},
    ]


def test_skips_excluded_paths_and_stops_at_the_limit(tmp_path: Path) -> None:
    _workspace_repo(tmp_path)

    rows = provider_files(
        target_path=tmp_path,
        source_paths=["apps/api/src/update.ts"],
        exclude=["packages/db/src/article/articleUpdate.ts"],
        limit=1,
    )

    assert [row["path"] for row in rows] == ["packages/db/src/user.ts"]


def test_unknown_name_in_a_barrel_falls_back_to_the_entry_file(tmp_path: Path) -> None:
    _write(tmp_path, "src/index.ts", 'export * from "./missing";\n')
    _write(tmp_path, "src/app.ts", 'import { thing } from "./index";\n')

    rows = provider_files(target_path=tmp_path, source_paths=["src/app.ts"])

    assert [row["path"] for row in rows] == ["src/index.ts"]
