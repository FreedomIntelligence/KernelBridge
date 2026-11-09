import ts from "typescript";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export interface ContinuationViolation {
  readonly file: string;
  readonly line: number;
  readonly reason: string;
}

type FunctionBody = ts.FunctionLikeDeclaration & { readonly body: ts.ConciseBody };
type CallOwner = FunctionBody | ts.SourceFile;
type CallbackBindings = ReadonlyMap<ts.ParameterDeclaration, ReadonlySet<ts.Node>>;
interface CallFacts {
  readonly targets: Set<CallOwner>;
  managed: boolean;
}

function isFunctionBody(node: ts.Node): node is FunctionBody {
  return ts.isFunctionLike(node) && "body" in node && node.body !== undefined;
}

function isManagedDeclaration(node: ts.Node): boolean {
  const path = node.getSourceFile().fileName.replaceAll("\\", "/");
  if (ts.isFunctionDeclaration(node)) {
    return path.endsWith("/run/managed.ts") && node.name?.text === "runManaged";
  }
  if (!ts.isMethodDeclaration(node) && !ts.isMethodSignature(node)) return false;
  if (node.name.getText() !== "run") return false;
  const parent = node.parent;
  if (!ts.isClassDeclaration(parent) && !ts.isInterfaceDeclaration(parent)) return false;
  return (path.endsWith("/sdk/session.ts") && parent.name?.text === "KernelCatSession") ||
    (path.endsWith("/sdk/goal.ts") && parent.name?.text === "GoalManagedSession");
}

function callableReferences(expression: ts.Expression, checker: ts.TypeChecker,
  seen = new Set<ts.Symbol>(), bindings: CallbackBindings = new Map()): ts.Node[] {
  if (ts.isParenthesizedExpression(expression)) return callableReferences(expression.expression, checker, seen, bindings);
  if (ts.isPropertyAccessExpression(expression) && ["bind", "call", "apply"].includes(expression.name.text)) {
    return callableReferences(expression.expression, checker, seen, bindings);
  }
  if (ts.isCallExpression(expression) && ts.isPropertyAccessExpression(expression.expression) &&
      expression.expression.name.text === "bind") {
    return callableReferences(expression.expression.expression, checker, seen, bindings);
  }
  if (isFunctionBody(expression)) return [expression];
  let symbol = checker.getSymbolAtLocation(expression);
  if (!symbol && ts.isPropertyAccessExpression(expression)) symbol = checker.getSymbolAtLocation(expression.name);
  if (!symbol || seen.has(symbol)) return [];
  seen.add(symbol);
  if (symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
  const nodes: ts.Node[] = [];
  for (const declaration of symbol.declarations ?? []) {
    nodes.push(declaration);
    if (ts.isParameter(declaration)) nodes.push(...(bindings.get(declaration) ?? []));
    if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
      nodes.push(...callableReferences(declaration.initializer, checker, seen, bindings));
    }
  }
  return nodes;
}

function callFacts(call: ts.CallExpression, checker: ts.TypeChecker, bindings: CallbackBindings): CallFacts {
  const facts: CallFacts = { targets: new Set(), managed: false };
  // Binding a method creates a reference; invoking that reference is checked later.
  if (ts.isPropertyAccessExpression(call.expression) && call.expression.name.text === "bind") return facts;
  const signature = checker.getResolvedSignature(call)?.getDeclaration();
  const declarations = callableReferences(call.expression, checker, new Set(), bindings);
  if (signature) declarations.push(signature);
  for (const declaration of declarations) {
    if (isManagedDeclaration(declaration)) facts.managed = true;
    if (isFunctionBody(declaration)) facts.targets.add(declaration);
  }
  // A callback may schedule work even when its invocation is hidden in an API.
  for (const argument of call.arguments) {
    for (const declaration of callableReferences(argument, checker, new Set(), bindings)) {
      if (isManagedDeclaration(declaration)) facts.managed = true;
      if (isFunctionBody(declaration)) facts.targets.add(declaration);
    }
  }
  return facts;
}

function isLoop(node: ts.Node): boolean {
  return ts.isForStatement(node) || ts.isForOfStatement(node) || ts.isForInStatement(node) ||
    ts.isWhileStatement(node) || ts.isDoStatement(node);
}

function isRepeatedCallback(call: ts.CallExpression): boolean {
  if (ts.isIdentifier(call.expression) && call.expression.text === "setInterval") return true;
  return ts.isPropertyAccessExpression(call.expression) &&
    ["forEach", "map", "flatMap", "reduce", "reduceRight", "some", "every", "filter", "setInterval", "from"].includes(call.expression.name.text);
}

function ownerOf(node: ts.Node): CallOwner {
  let parent = node.parent;
  while (!ts.isSourceFile(parent) && !isFunctionBody(parent)) parent = parent.parent;
  return parent;
}

function reaches(start: CallOwner, target: CallOwner, graph: ReadonlyMap<CallOwner, CallFacts>): boolean {
  const pending = [...(graph.get(start)?.targets ?? [])];
  const seen = new Set<CallOwner>();
  while (pending.length) {
    const current = pending.pop()!;
    if (current === target) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    pending.push(...(graph.get(current)?.targets ?? []));
  }
  return false;
}

/** Bind callback arguments to callee parameters, including chains of higher-order helpers. */
function callbackBindings(sources: readonly ts.SourceFile[], checker: ts.TypeChecker): CallbackBindings {
  const calls: ts.CallExpression[] = [];
  const collect = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) calls.push(node);
    ts.forEachChild(node, collect);
  };
  sources.forEach(collect);
  const bindings = new Map<ts.ParameterDeclaration, Set<ts.Node>>();
  let changed = true;
  while (changed) {
    changed = false;
    for (const call of calls) {
      const declaration = checker.getResolvedSignature(call)?.getDeclaration();
      if (!declaration) continue;
      call.arguments.forEach((argument, index) => {
        const parameter = declaration.parameters[index];
        if (!parameter) return;
        const targets = bindings.get(parameter) ?? new Set<ts.Node>();
        for (const target of callableReferences(argument, checker, new Set(), bindings)) {
          if (!targets.has(target)) { targets.add(target); changed = true; }
        }
        bindings.set(parameter, targets);
      });
    }
  }
  return bindings;
}

/** Typed calls and their transitive helpers are checked; unrelated .run methods are not sinks. */
export function checkManagedContinuation(program: ts.Program, ownerFile: string): readonly ContinuationViolation[] {
  const checker = program.getTypeChecker();
  const sources = program.getRootFileNames().map(path => program.getSourceFile(path))
    .filter((source): source is ts.SourceFile => source !== undefined && !source.isDeclarationFile);
  const bindings = callbackBindings(sources, checker);
  const graph = new Map<CallOwner, CallFacts>();
  const calls = new Map<ts.CallExpression, CallFacts>();
  for (const source of sources) {
    const visit = (node: ts.Node): void => {
      if (ts.isCallExpression(node)) {
        // Parameter flow qualifies loop call sites, but must not add global
        // context-insensitive callback edges to the recursion graph.
        const facts = callFacts(node, checker, new Map());
        calls.set(node, callFacts(node, checker, bindings));
        const owner = ownerOf(node);
        const aggregate = graph.get(owner) ?? { targets: new Set<CallOwner>(), managed: false };
        aggregate.managed ||= facts.managed;
        for (const target of facts.targets) aggregate.targets.add(target);
        graph.set(owner, aggregate);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  // Monotone fixed point: each function becomes managed at most once. No runtime effects occur.
  let changed = true;
  while (changed) {
    changed = false;
    for (const facts of graph.values()) {
      if (facts.managed || ![...facts.targets].some(target => graph.get(target)?.managed)) continue;
      facts.managed = true;
      changed = true;
    }
  }
  const violations = new Map<ts.Node, ContinuationViolation>();
  const record = (node: ts.Node, reason: string): void => {
    const source = node.getSourceFile();
    if (resolve(source.fileName) === resolve(ownerFile)) return;
    const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
    violations.set(node, { file: source.fileName, line, reason });
  };
  for (const [call, facts] of calls) {
    const managed = facts.managed || [...facts.targets].some(target => graph.get(target)?.managed);
    if (!managed) continue;
    let ancestor: ts.Node = call.parent;
    while (!ts.isSourceFile(ancestor) && !isFunctionBody(ancestor)) {
      if (isLoop(ancestor)) record(ancestor, "Automatic managed execution loop must be owned by LoopXGoal");
      ancestor = ancestor.parent;
    }
    if (isRepeatedCallback(call)) record(call, "Repeated managed callbacks must be owned by LoopXGoal");
    const owner = ownerOf(call);
    if (reaches(owner, owner, graph)) record(call, "Recursive managed execution must be owned by LoopXGoal");
  }
  return [...violations.values()].sort((left, right) => left.file < right.file ? -1 :
    left.file > right.file ? 1 : left.line - right.line);
}

function main(): void {
  const directory = fileURLToPath(new URL("../", import.meta.url));
  const configPath = resolve(directory, "tsconfig.json");
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, "\n"));
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, directory);
  if (parsed.errors.length) throw new Error(ts.flattenDiagnosticMessageText(parsed.errors[0].messageText, "\n"));
  const files = parsed.fileNames.filter(path => path.startsWith(resolve(directory, "src") + "/"));
  const program = ts.createProgram(files, parsed.options);
  const violations = checkManagedContinuation(program, resolve(directory, "src/sdk/goal.ts"));
  for (const violation of violations) console.error(`${violation.file}:${violation.line}: ${violation.reason}`);
  if (violations.length) process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
