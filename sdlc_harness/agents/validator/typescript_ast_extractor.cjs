"use strict";

// Helper cố định: dùng TypeScript Compiler API của target để tạo symbol evidence.

const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

function fail(message) {
  process.stderr.write(String(message));
  process.exit(2);
}

function canonicalRelativePath(value) {
  if (typeof value !== "string" || !value || value.trim() !== value || value.includes("\\")) {
    fail("Invalid TypeScript source path.");
  }
  if (path.posix.isAbsolute(value) || value.split("/").some((part) => !part || part === "." || part === "..")) {
    fail("Unsafe TypeScript source path.");
  }
  return value;
}

function hasModifier(ts, node, kind) {
  return Boolean(node.modifiers && node.modifiers.some((modifier) => modifier.kind === kind));
}

function typeText(node, sourceFile) {
  return node && node.type ? node.type.getText(sourceFile) : "inferred";
}

function parameters(node, sourceFile) {
  return (node.parameters || []).map((parameter) => ({
    name: parameter.name.getText(sourceFile),
    type: typeText(parameter, sourceFile),
    optional: Boolean(parameter.questionToken || parameter.initializer),
    rest: Boolean(parameter.dotDotDotToken),
  }));
}

function typeParameters(node, sourceFile) {
  return (node.typeParameters || []).map((parameter) => parameter.getText(sourceFile));
}

function declarationSymbol(ts, node, sourceFile, owner) {
  if (!node.name) {
    return null;
  }
  const name = node.name.getText(sourceFile);
  let kind = "";
  if (ts.isInterfaceDeclaration(node)) kind = "interface";
  else if (ts.isClassDeclaration(node)) kind = "class";
  else if (ts.isFunctionDeclaration(node)) kind = "function";
  else if (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) kind = "method";
  if (!kind) {
    return null;
  }
  const start = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
  const end = sourceFile.getLineAndCharacterOfPosition(node.getEnd());
  return {
    kind,
    name,
    qualified_name: owner ? `${owner}.${name}` : name,
    parameters: parameters(node, sourceFile),
    return_type: kind === "class" || kind === "interface" ? "" : typeText(node, sourceFile),
    type_parameters: typeParameters(node, sourceFile),
    async: hasModifier(ts, node, ts.SyntaxKind.AsyncKeyword),
    exported: hasModifier(ts, node, ts.SyntaxKind.ExportKeyword),
    start_line: start.line + 1,
    end_line: end.line + 1,
  };
}

// `export const handler = async (req) => ...` is a function; any other value (`jwt({...})`) is a variable.
function variableSymbols(ts, statement, sourceFile) {
  if (!ts.isVariableStatement(statement)) {
    return [];
  }
  const exported = hasModifier(ts, statement, ts.SyntaxKind.ExportKeyword);
  const symbols = [];
  for (const declaration of statement.declarationList.declarations) {
    if (!ts.isIdentifier(declaration.name)) {
      continue;
    }
    const initializer = declaration.initializer;
    const callable =
      initializer && (ts.isArrowFunction(initializer) || ts.isFunctionExpression(initializer)) ? initializer : null;
    const name = declaration.name.getText(sourceFile);
    const start = sourceFile.getLineAndCharacterOfPosition(declaration.getStart(sourceFile));
    const end = sourceFile.getLineAndCharacterOfPosition(declaration.getEnd());
    symbols.push({
      kind: callable ? "function" : "variable",
      name,
      qualified_name: name,
      parameters: callable ? parameters(callable, sourceFile) : [],
      return_type: typeText(callable || declaration, sourceFile),
      type_parameters: callable ? typeParameters(callable, sourceFile) : [],
      async: callable ? hasModifier(ts, callable, ts.SyntaxKind.AsyncKeyword) : false,
      exported,
      start_line: start.line + 1,
      end_line: end.line + 1,
    });
  }
  return symbols;
}

function collectSymbols(ts, sourceFile) {
  const symbols = [];
  for (const statement of sourceFile.statements) {
    const top = declarationSymbol(ts, statement, sourceFile, "");
    if (top) symbols.push(top);
    symbols.push(...variableSymbols(ts, statement, sourceFile));
    if (ts.isClassDeclaration(statement) || ts.isInterfaceDeclaration(statement)) {
      const owner = statement.name ? statement.name.getText(sourceFile) : "";
      for (const member of statement.members || []) {
        const nested = declarationSymbol(ts, member, sourceFile, owner);
        if (nested) symbols.push(nested);
      }
    }
  }
  return symbols;
}

// Opt-in (request.include_module_facts): value string literals and module references, so the
// Harness can find demo values in a build and static imports of a demo module (L5.4 readiness).
function collectModuleFacts(ts, sourceFile) {
  const literals = [];
  const seenLiterals = new Set();
  const references = [];
  const specifiers = new Set();

  function reference(node, kind) {
    if (node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))) {
      specifiers.add(node);
      references.push({ specifier: node.text, kind });
    }
  }

  function visit(node) {
    if (ts.isImportDeclaration(node)) {
      reference(node.moduleSpecifier, node.importClause && node.importClause.isTypeOnly ? "type_only" : "static");
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
      reference(node.moduleSpecifier, node.isTypeOnly ? "type_only" : "static");
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      reference(node.moduleReference.expression, node.isTypeOnly ? "type_only" : "static");
    } else if (ts.isCallExpression(node) && node.arguments.length > 0) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
        reference(node.arguments[0], "dynamic");
      } else if (ts.isIdentifier(node.expression) && node.expression.text === "require") {
        reference(node.arguments[0], "static");
      }
    } else if (ts.isModuleDeclaration(node) && ts.isStringLiteral(node.name)) {
      specifiers.add(node.name);
    }
    const parent = node.parent;
    if (
      (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) &&
      !specifiers.has(node) &&
      // Type positions are erased by the build; property names and directives are not values.
      !(parent && (ts.isLiteralTypeNode(parent) || parent.name === node || ts.isExpressionStatement(parent))) &&
      !seenLiterals.has(node.text)
    ) {
      seenLiterals.add(node.text);
      literals.push(node.text);
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return { string_literals: literals, module_references: references };
}

let request;
try {
  request = JSON.parse(fs.readFileSync(0, "utf8"));
} catch (error) {
  fail(`Invalid extractor request: ${error.message}`);
}
if (request.version !== "typescript_ast_request_v1" || request.extractor_id !== "typescript_compiler_ast_v1") {
  fail("Unsupported extractor request contract.");
}

let ts;
try {
  const targetRequire = createRequire(path.join(process.cwd(), "package.json"));
  ts = targetRequire("typescript");
} catch (error) {
  // Design runs before dependency installation in a worktree. Only the Harness-bound canonical repo
  // may supply its compiler; source paths and cwd remain the run's worktree.
  if (request.include_ui_facts === true && request.typescript_package_root) {
    try {
      ts = createRequire(path.join(request.typescript_package_root, "package.json"))("typescript");
    } catch (fallbackError) {
      fail(`Target TypeScript package is unavailable: ${fallbackError.code || fallbackError.message}`);
    }
  } else fail(`Target TypeScript package is unavailable: ${error.code || error.message}`);
}

const uiFacts = request.include_ui_facts === true
  ? require("./typescript_ui_facts.cjs")(ts, (request.files || []).map(canonicalRelativePath))
  : {};
const files = [];
for (const rawPath of request.files || []) {
  const relativePath = canonicalRelativePath(rawPath);
  const absolutePath = path.resolve(process.cwd(), ...relativePath.split("/"));
  const relativeCheck = path.relative(process.cwd(), absolutePath);
  if (!relativeCheck || relativeCheck.startsWith("..") || path.isAbsolute(relativeCheck)) {
    fail("TypeScript source path escapes target repository.");
  }
  const content = fs.readFileSync(absolutePath, "utf8");
  const scriptKind = relativePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(
    relativePath,
    content,
    ts.ScriptTarget.Latest,
    true,
    scriptKind,
  );
  files.push({
    path: relativePath,
    diagnostics: (sourceFile.parseDiagnostics || []).map((diagnostic) => ({
      code: diagnostic.code,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, " "),
      start: diagnostic.start ?? null,
      length: diagnostic.length ?? null,
    })),
    symbols: collectSymbols(ts, sourceFile),
    ...(request.include_ui_facts === true ? { ui_facts: uiFacts[relativePath] } : {}),
    ...(request.include_module_facts === true ? collectModuleFacts(ts, sourceFile) : {}),
  });
}

process.stdout.write(JSON.stringify({
  version: "typescript_ast_evidence_v2",
  extractor_id: request.extractor_id,
  profile: request.profile,
  files,
}));
