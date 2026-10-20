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
  return {
    kind,
    name,
    qualified_name: owner ? `${owner}.${name}` : name,
    parameters: parameters(node, sourceFile),
    return_type: kind === "class" || kind === "interface" ? "" : typeText(node, sourceFile),
    type_parameters: typeParameters(node, sourceFile),
    async: hasModifier(ts, node, ts.SyntaxKind.AsyncKeyword),
    exported: hasModifier(ts, node, ts.SyntaxKind.ExportKeyword),
  };
}

function collectSymbols(ts, sourceFile) {
  const symbols = [];
  for (const statement of sourceFile.statements) {
    const top = declarationSymbol(ts, statement, sourceFile, "");
    if (top) symbols.push(top);
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
  fail(`Target TypeScript package is unavailable: ${error.code || error.message}`);
}

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
  });
}

process.stdout.write(JSON.stringify({
  version: "typescript_ast_evidence_v1",
  extractor_id: request.extractor_id,
  profile: request.profile,
  files,
}));
