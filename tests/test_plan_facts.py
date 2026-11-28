"""Repository facts for the H-OC Phase 3 Plan graph, read from a small temporary workspace (no LLM)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.repo_context.plan_facts import RepoFacts, build_repo_facts, is_barrel

MANIFESTS = {
    "package.json": {
        "name": "root",
        "workspaces": ["apps/*", "packages/*"],
        "scripts": {"db:smoke": "npm run smoke --workspace @t/db", "test:e2e": "playwright test"},
    },
    "apps/api/package.json": {"name": "@t/api", "dependencies": {"@t/db": "*", "@t/contracts": "*"}},
    "apps/web/package.json": {"name": "@t/web", "dependencies": {"@t/contracts": "*"}},
    "packages/db/package.json": {"name": "@t/db", "main": "src/index.ts", "scripts": {"smoke": "tsx src/smoke.ts"}},
    "packages/contracts/package.json": {"name": "@t/contracts", "main": "src/index.ts"},
}
FILES = {
    "packages/db/prisma/schema.prisma": "model Thing { id String @id }\n",
    "packages/db/src/index.ts": (
        "// barrel\n"
        'export { default as readThing } from "./read";\n'
        'export type { Row } from "./types";\n'
    ),
    "packages/db/src/read.ts": "export default async function readThing() {}\n",
    "packages/db/src/types.ts": "export type Row = { id: string };\n",
    "packages/db/src/smoke.ts": 'import { readThing } from "./index";\nreadThing();\n',
    "packages/contracts/src/index.ts": "export interface Thing { id: string }\n",
    "apps/api/src/app.ts": (
        'import thingsRouter from "./routes/things";\n'
        'import errorHandler from "./middleware/errorHandler";\n'
        'app.use("/api/things", thingsRouter);\n'
        "app.use(errorHandler);\n"
    ),
    "apps/api/src/routes/things.ts": (
        'import { getThing } from "../controllers";\n'
        'router.get("/:id", getThing);\n'
        "export default router;\n"
    ),
    "apps/api/src/controllers/index.ts": 'export { getThing } from "./getThing";\n',
    "apps/api/src/controllers/getThing.ts": (
        'import { readThing } from "@t/db";\n'
        'import type { Thing } from "@t/contracts";\n'
        "export async function getThing() {}\n"
    ),
    "apps/api/src/middleware/errorHandler.ts": "export default function errorHandler(err, _req, res) {}\n",
    "tests/e2e/things.spec.ts": "test('x', async () => {});\n",
    # Existing tests: one imports a name, one only mocks a module, one e2e spec, one touches no unit.
    "apps/api/src/controllers/getThing.test.ts": 'import { getThing } from "./getThing";\ntest("x", () => getThing());\n',
    "apps/api/src/controllers/handler.test.ts": 'vi.mock("../middleware/errorHandler");\ntest("y", () => {});\n',
    "tests/e2e/extra.spec.ts": 'import type { Thing } from "@t/contracts";\ntest("z", async () => {});\n',
    "apps/web/src/lonely.test.ts": 'test("w", () => {});\n',
}
UNITS = {
    "U-1": ["packages/db/prisma/schema.prisma"],
    "U-2": ["packages/contracts/src/index.ts"],
    "U-3": ["packages/db/src/read.ts", "packages/db/src/index.ts"],
    "U-4": ["apps/api/src/controllers/getThing.ts"],
    "U-5": ["apps/api/src/middleware/errorHandler.ts"],
    "U-6": ["packages/db/src/smoke.ts"],
    "U-7": ["tests/e2e/things.spec.ts"],
    "U-8": ["apps/api/src/controllers/newThing.ts (new)"],
}


class PlanFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for path, payload in MANIFESTS.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(json.dumps(payload), encoding="utf-8")
        for path, text in FILES.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text, encoding="utf-8")
        self.facts = build_repo_facts(
            self.root,
            UNITS,
            component_roots={"api": ["apps/api/src"], "database": ["packages/db/src", "packages/db/prisma"],
                             "contracts": ["packages/contracts/src"], "e2e": ["tests/e2e"]},
            protected_paths=["package.json"],
            stage_argvs=[(("npm", "run", "db:smoke"), "."), (("npm", "run", "test:e2e"), ".")],
            generate_triggers=["packages/db/prisma/schema.prisma"],
            test_suffixes=(".test.ts", ".test.tsx", ".spec.ts"),
            schema_roots=["packages/db/prisma/"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_packages_and_imports_come_from_the_manifests(self) -> None:
        self.assertEqual(self.facts.package_dirs, ("", "apps/api", "apps/web", "packages/contracts", "packages/db"))
        self.assertEqual(self.facts.imports["apps/api"], frozenset({"apps/api", "packages/db", "packages/contracts"}))
        self.assertEqual(self.facts.imports["apps/web"], frozenset({"apps/web", "packages/contracts"}))
        self.assertEqual(self.facts.package_of("apps/api/src/app.ts"), "apps/api")
        self.assertEqual(self.facts.package_of("tests/e2e/things.spec.ts"), "")

    def test_generated_code_consumers_and_check_files(self) -> None:
        self.assertEqual(self.facts.generate_inputs, frozenset({"packages/db/prisma/schema.prisma"}))
        self.assertEqual(self.facts.generated_consumers, frozenset({"packages/db", "apps/api"}))
        # smoke.ts through `npm run db:smoke` -> `npm run smoke --workspace @t/db` -> `tsx src/smoke.ts`
        self.assertEqual(self.facts.check_files, frozenset({"packages/db/src/smoke.ts", "tests/e2e/things.spec.ts"}))

    def test_barrels(self) -> None:
        self.assertEqual(self.facts.barrels, frozenset({"packages/db/src/index.ts"}))
        self.assertTrue(is_barrel('export * from "./a";\n/* c */ export { b as c } from "./b";'))
        self.assertFalse(is_barrel('export { a } from "./a";\nexport const b = 1;'))
        self.assertFalse(is_barrel(""))

    def test_reliance_candidates_follow_runtime_imports_to_the_mounting_router(self) -> None:
        pairs = {(c["from"], c["to"]) for c in self.facts.reliance_candidates}
        # controller -> barrel -> router mounted by app.ts before errorHandler; the db read is imported at runtime too
        self.assertEqual(pairs, {("U-3", "U-5"), ("U-4", "U-5")})
        evidence = next(c for c in self.facts.reliance_candidates if c["from"] == "U-4")["evidence"][0]
        self.assertEqual(evidence["registered_by"], "apps/api/src/app.ts")
        self.assertEqual(evidence["middleware"], "errorHandler")
        self.assertEqual(evidence["mounted_file"], "apps/api/src/routes/things.ts")

    def test_existing_tests_are_attached_to_the_units_they_import_or_mock(self) -> None:
        self.assertEqual(
            list(self.facts.tests),
            [
                {"path": "apps/api/src/controllers/getThing.test.ts", "units": ["U-4"], "check": False},
                {"path": "apps/api/src/controllers/handler.test.ts", "units": ["U-5"], "check": False},
                {"path": "tests/e2e/extra.spec.ts", "units": ["U-2"], "check": True},
            ],
        )  # unit files (things.spec.ts, smoke.ts) and tests touching no unit are left out
        self.assertEqual(self.facts.check_roots, ("tests/e2e",))

    def test_round_trip(self) -> None:
        self.assertEqual(self.facts.schema_roots, ("packages/db/prisma",))
        self.assertEqual(RepoFacts.from_dict(self.facts.to_dict()).to_dict(), self.facts.to_dict())


if __name__ == "__main__":
    unittest.main()
