"use strict";

// UI syntax facts only. Whether a component/provider satisfies a requirement is a semantic decision.
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

module.exports = function collectUiFacts(ts, relativePaths) {
  const root = process.cwd();
  const configPath = ts.findConfigFile(root, ts.sys.fileExists);
  let options = { jsx: ts.JsxEmit.Preserve, target: ts.ScriptTarget.Latest, noEmit: true };
  if (configPath) {
    const config = ts.readConfigFile(configPath, ts.sys.readFile);
    if (!config.error) options = { ...ts.parseJsonConfigFileContent(config.config, ts.sys, path.dirname(configPath)).options, noEmit: true };
  }
  const program = ts.createProgram(relativePaths.map((p) => path.resolve(root, p)), options);
  const checker = program.getTypeChecker();
  const records = [];
  const declarationRefs = new Map();
  const sources = new Map();

  function callable(node) {
    if (!node) return null;
    if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return node;
    if (ts.isParenthesizedExpression(node)) return callable(node.expression);
    // React wrappers retain the callback's syntax; unknown HOCs are left unresolved.
    if (ts.isCallExpression(node) && /^(?:React\.)?(?:memo|forwardRef)$/.test(node.expression.getText())) {
      return callable(node.arguments[0]);
    }
    return null;
  }

  for (const relative of relativePaths) {
    const source = program.getSourceFile(path.resolve(root, relative));
    if (!source) continue;
    sources.set(relative, source);
    for (const statement of source.statements) {
      const candidates = [];
      if (ts.isFunctionDeclaration(statement) && statement.body) {
        candidates.push({ declaration: statement, fn: statement, name: statement.name?.text || "default" });
      } else if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (ts.isIdentifier(declaration.name) && callable(declaration.initializer)) {
            candidates.push({ declaration, fn: callable(declaration.initializer), name: declaration.name.text });
          }
        }
      }
      for (const candidate of candidates) {
        if (!/^[A-Z]/.test(candidate.name) && candidate.name !== "default") continue;
        const ref = `${relative}#${candidate.name}`;
        const record = { ...candidate, ref, relative, source };
        records.push(record);
        declarationRefs.set(candidate.declaration, ref);
      }
    }
  }

  function propFacts(record) {
    const parameter = record.fn.parameters[0];
    let annotation = parameter?.type;
    if (!annotation && record.declaration.type && ts.isTypeReferenceNode(record.declaration.type)) {
      const declared = record.declaration.type;
      if (/^(?:React\.)?(?:FC|FunctionComponent|PropsWithChildren)$/.test(declared.typeName.getText(record.source))) {
        annotation = declared.typeArguments?.[0];
      }
    }
    const defaults = new Set();
    const binding = parameter?.name;
    if (binding && ts.isObjectBindingPattern(binding)) {
      for (const element of binding.elements) {
        if (element.initializer) defaults.add((element.propertyName || element.name).getText(record.source));
      }
    }
    if (!annotation) {
      if (!parameter) return { props_status: "known", props: [] };
      if (binding && ts.isObjectBindingPattern(binding) && binding.elements.every((e) => !e.dotDotDotToken)) {
        return {
          props_status: "known",
          props: binding.elements.map((element) => {
            const name = (element.propertyName || element.name).getText(record.source);
            return { name, optional: false, has_default: defaults.has(name) };
          }),
        };
      }
      return { props_status: "unknown", props: null, uncertainty: "The props parameter has no resolvable type or complete object binding." };
    }
    const type = checker.getTypeFromTypeNode(annotation);
    if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.TypeParameter | ts.TypeFlags.Union)) {
      return { props_status: "unknown", props: null, uncertainty: "The props type is unresolved or has alternative shapes." };
    }
    if (checker.getIndexInfosOfType(type).length) {
      return { props_status: "unknown", props: null, uncertainty: "An open props index signature cannot be enumerated completely." };
    }
    return {
      props_status: "known",
      props: checker.getPropertiesOfType(type).map((symbol) => ({
        name: symbol.name,
        optional: Boolean(symbol.flags & ts.SymbolFlags.Optional),
        has_default: defaults.has(symbol.name),
      })),
    };
  }

  function tagRef(tag) {
    let symbol = checker.getSymbolAtLocation(tag);
    if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
      try { symbol = checker.getAliasedSymbol(symbol); } catch (_) { return null; }
    }
    for (const declaration of symbol?.declarations || []) {
      if (declarationRefs.has(declaration)) return declarationRefs.get(declaration);
    }
    return null;
  }

  function rendered(body) {
    const children = new Map();
    const unresolved = new Set();
    function visit(node) {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName;
        if (/^[A-Z]/.test(tag.getText())) {
          const ref = tagRef(tag);
          if (ref) {
            const names = new Set(children.get(ref) || []);
            for (const attr of node.attributes.properties) {
              if (ts.isJsxAttribute(attr)) names.add(attr.name.getText());
              else if (ts.isJsxSpreadAttribute(attr)) {
                const type = checker.getTypeAtLocation(attr.expression);
                if (!(type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown))) {
                  for (const prop of checker.getPropertiesOfType(type)) names.add(prop.name);
                } else unresolved.add(`spread props to ${ref}`);
              }
            }
            children.set(ref, [...names].sort());
          } else {
            // External wrappers are not repository components. Unresolved relative imports remain uncertainty.
            const symbol = checker.getSymbolAtLocation(tag);
            const declaration = symbol?.declarations?.[0];
            let parent = declaration;
            while (parent && !ts.isImportDeclaration(parent)) parent = parent.parent;
            if (!parent || parent.moduleSpecifier.text.startsWith(".")) unresolved.add(tag.getText());
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(body);
    return { renders: [...children].map(([ref, props]) => ({ ref, props })), unresolved_tags: [...unresolved].sort() };
  }

  const result = {};
  for (const [relative, source] of sources) {
    const diagnostics = source.parseDiagnostics || [];
    const components = records.filter((r) => r.relative === relative).map((record) => {
      const facts = propFacts(record);
      return {
        symbol: record.name,
        start_line: source.getLineAndCharacterOfPosition(record.declaration.getStart(source)).line + 1,
        end_line: source.getLineAndCharacterOfPosition(record.declaration.getEnd()).line + 1,
        ...facts,
        ...rendered(record.fn.body),
        ...(diagnostics.length ? { props_status: "unknown", props: null, uncertainty: "Source has TypeScript parse diagnostics." } : {}),
      };
    });
    const entrypoints = [];
    function findMount(node) {
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === "render") {
        for (const argument of node.arguments) {
          for (const item of rendered(argument).renders) if (!entrypoints.includes(item.ref)) entrypoints.push(item.ref);
        }
      }
      ts.forEachChild(node, findMount);
    }
    findMount(source);
    result[relative] = {
      content_sha256: "sha256:" + crypto.createHash("sha256").update(fs.readFileSync(path.resolve(root, relative))).digest("hex"),
      components,
      entrypoint_candidates: entrypoints,
      diagnostics: diagnostics.map((d) => ts.flattenDiagnosticMessageText(d.messageText, " ")),
    };
  }
  return result;
};
