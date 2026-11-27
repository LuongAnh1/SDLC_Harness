"""H-OC Phase 3: the Plan is a task graph the Harness builds and schedules from the Design and repository facts.

Nodes are the Design's units (`U-n`). An edge `u -> v` means u uses v; its type comes from repository facts only
(`repo_context/plan_facts.py`), never from names:

- `build`: v writes an input of the code generator and u's package compiles against the generated code (not cuttable);
- `import`: u's package may import v's package (cuttable only by a typed stub committed in the base);
- `http`: u's package cannot import v's package; the two meet at runtime through the shared contract (cut, owner
  decision 2026-11-27; the contract units start in the base);
- `verify`: u only writes whole-system check files (e2e specs, stage scripts); u runs after every merge;
- `reliance`: u relies on v's behaviour without using it (the Planner keeps it from a Harness candidate, with a reason).

Strongly connected components are atomic. The base is a down-set (build sources, contract units and the hub units the
search adds), the integration task an up-set (check units), and the groups are the weakly connected components of the
middle joined by hard edges and shared non-barrel files, so they all run in one wave. The base is chosen by the
estimated makespan `T = w(base) + C0 + LPT max load over k (each group w + C0) + w(integration) + C0`; a split that adds
base units or stubs is kept only if it lowers T by at least the threshold. Nothing here calls an LLM or judges meaning
(Harness/Validator boundary).

Plan `docs/maintenance/2026-11-27-h-oc-phase3-plan-graph.md` sections 2, 3, 3c, 5 and 10.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import itertools
import re

from ...repo_context.plan_facts import RepoFacts, clean_path

PLAN_GRAPH_SCHEMA_VERSION = "plan_graph_v1"
EDGE_TYPES = ("build", "import", "http", "verify", "reliance")
HARD_EDGES = frozenset({"build", "import", "verify", "reliance"})
DEFAULT_K = 3
DEFAULT_C0 = 2.0
DEFAULT_THRESHOLD = 0.15
DEFAULT_TURN_BUDGET = 25
STUB_COST = 0.5
TEST_WEIGHT = 0.5  # an existing test a task may edit (many need a small change or none; owner 2026-11-27)
NEW_TEST_WEIGHT = 1.0  # a new test file from the Planner's intents
MAX_BASE_CANDIDATES = 12
MAX_STUB_CANDIDATES = 8
RUNTIME_CHECKS = ("G8", "G9", "G10")


def _num(unit: str) -> int:
    digits = re.sub(r"\D", "", unit)
    return int(digits) if digits else 0


def _sorted(units: Iterable[str]) -> list[str]:
    return sorted(units, key=_num)


def unit_files(trace: Mapping[str, object]) -> dict[str, list[str]]:
    """Design unit -> its files without the ``(new)`` marker, in Design order."""

    return {
        str(unit["id"]): [clean_path(str(path)) for path in unit.get("files") or []]
        for unit in trace.get("units") or []
        if isinstance(unit, dict)
    }


def classify_edges(
    trace: Mapping[str, object], facts: RepoFacts, reliance: Sequence[Mapping[str, object]] = ()
) -> list[dict[str, object]]:
    """Every `Uses` edge with its type, then the kept `reliance` edges (each with its reason and evidence)."""

    files = unit_files(trace)
    packages = {unit: {facts.package_of(path) for path in paths} for unit, paths in files.items()}
    checks = check_units(files, facts)
    edges: list[dict[str, object]] = []
    for unit in trace.get("units") or []:
        source = str(unit["id"])
        for target in unit.get("uses") or []:
            target = str(target)
            if source in checks:
                kind = "verify"
            elif set(files.get(target, ())) & facts.generate_inputs and packages[source] & facts.generated_consumers:
                kind = "build"
            elif any(pv in facts.imports.get(pu, frozenset({pu})) for pu in packages[source] for pv in packages.get(target, ())):
                kind = "import"
            else:
                kind = "http"
            edges.append({"from": source, "to": target, "type": kind, "origin": "uses"})
    for item in reliance:
        edges.append(
            {
                "from": str(item.get("from") or ""),
                "to": str(item.get("to") or ""),
                "type": "reliance",
                "origin": "reliance",
                "reason": str(item.get("reason") or ""),
                "evidence": list(item.get("evidence") or []),
            }
        )
    return edges


def check_units(files: Mapping[str, Sequence[str]], facts: RepoFacts) -> set[str]:
    """Units whose every file is a whole-system check: they belong to the integration task."""

    return {unit for unit, paths in files.items() if paths and all(path in facts.check_files for path in paths)}


def strongly_connected(nodes: Iterable[str], adjacency: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components (iterative), each sorted, in reverse topological order."""

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0
    for start in _sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(_sorted(adjacency.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(_sorted(adjacency.get(child, ())))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(_sorted(component))
    return result


def down_closure(seeds: Iterable[str], adjacency: Mapping[str, Iterable[str]]) -> set[str]:
    """`seeds` and everything they reach through `adjacency`."""

    found = set(seeds)
    todo = list(found)
    while todo:
        for nxt in adjacency.get(todo.pop(), ()):
            if nxt not in found:
                found.add(nxt)
                todo.append(nxt)
    return found


class _Graph:
    """Units, typed edges and file writers of one Design, ready for grouping and the base search."""

    def __init__(self, trace: Mapping[str, object], facts: RepoFacts, edges: list[dict[str, object]]):
        self.files = unit_files(trace)
        self.units = _sorted(self.files)
        self.facts = facts
        self.edges = edges
        self.hard: dict[str, set[str]] = {unit: set() for unit in self.units}
        self.types: dict[tuple[str, str], set[str]] = {}
        for edge in edges:
            if edge["type"] in HARD_EDGES and edge["from"] in self.hard and edge["to"] in self.hard:
                self.hard[str(edge["from"])].add(str(edge["to"]))
                self.types.setdefault((str(edge["from"]), str(edge["to"])), set()).add(str(edge["type"]))
        self.checks = check_units(self.files, facts)
        self.late = set(self.checks)  # a normal unit that needs one of them is a G3 issue, never moved silently
        self.writers: dict[str, list[str]] = {}
        for unit in self.units:
            for path in self.files[unit]:
                self.writers.setdefault(path, []).append(unit)
        # Existing tests that touch Design units (plan 11.1) and new test files from the Planner's intents (11.3).
        self.tests = [
            {"path": str(item["path"]), "units": [u for u in item.get("units") or [] if u in self.files],
             "check": bool(item.get("check"))}
            for item in facts.tests
            if str(item.get("path") or "") not in self.writers
        ]
        self.tests = [item for item in self.tests if item["units"]]
        self.new_tests: list[dict[str, str]] = []

    def weight(self, units: Iterable[str]) -> int:
        return sum(len(self.files.get(unit, ())) for unit in units)

    def attach(self, base: set[str], groups: Sequence[Sequence[str]]) -> dict[object, dict[str, list[str]]]:
        """Which task edits each test: key "base", "integration" or a group tuple -> existing and new test paths."""

        group_of = {unit: tuple(group) for group in groups for unit in group}
        result: dict[object, dict[str, list[str]]] = {}

        def add(key: object, kind: str, path: str) -> None:
            paths = result.setdefault(key, {"tests": [], "new_tests": []})[kind]
            if path not in paths:  # several test intents may share one new file; it weighs once
                paths.append(path)

        for item in self.tests:
            middle = [unit for unit in item["units"] if unit in group_of]
            if item["check"]:
                add("integration", "tests", item["path"])
            elif middle:  # the base is done before branching, so "base + one group" belongs to that group
                add(group_of[middle[0]], "tests", item["path"])
            elif any(unit in base for unit in item["units"]):
                add("base", "tests", item["path"])
            else:
                add("integration", "tests", item["path"])
        for item in self.new_tests:
            anchor = item["anchor"]
            key = "integration" if anchor == "integration" or anchor in self.late else (
                "base" if anchor in base else group_of.get(anchor, "integration"))
            add(key, "new_tests", item["path"])
        return result

    @staticmethod
    def extra(attached: Mapping[object, Mapping[str, list[str]]], key: object) -> float:
        item = attached.get(key) or {}
        return TEST_WEIGHT * len(item.get("tests") or []) + NEW_TEST_WEIGHT * len(item.get("new_tests") or [])

    def stub_cut(self, source: str, target: str, stubs: Iterable[str]) -> bool:
        """A typed stub of `target` replaces only an `import`: build, verify and reliance edges stay hard."""

        return target in stubs and self.types.get((source, target), set()) == {"import"}

    def groups(self, base: set[str], stubs: set[str] = frozenset()) -> list[list[str]]:
        middle = [unit for unit in self.units if unit not in base and unit not in self.late]
        parent = {unit: unit for unit in middle}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for source in middle:
            for target in self.hard[source]:
                if target in parent and not self.stub_cut(source, target, stubs):
                    parent[find(source)] = find(target)
        for path, writers in self.writers.items():
            inside = [unit for unit in writers if unit in parent]
            if path in self.facts.barrels:
                continue
            for unit in inside[1:]:
                parent[find(unit)] = find(inside[0])
        for item in self.tests:  # a test touching units of two groups is a file both may edit: one group
            inside = [unit for unit in item["units"] if unit in parent] if not item["check"] else []
            for unit in inside[1:]:
                parent[find(unit)] = find(inside[0])
        components: dict[str, list[str]] = {}
        for unit in middle:
            components.setdefault(find(unit), []).append(unit)
        return sorted((_sorted(items) for items in components.values()), key=lambda g: (-self.weight(g), _num(g[0])))


def lpt(groups: Sequence[Sequence[str]], k: int, c0: float, weight_of) -> tuple[float, list[dict[str, object]]]:
    """Longest-processing-time packing onto k agents; each group costs its weight plus one opening turn."""

    agents = [{"agent_id": f"agent-{index + 1}", "groups": [], "load": 0.0} for index in range(max(k, 1))]
    for group in sorted(groups, key=lambda g: (-weight_of(g), _num(g[0]))):
        agent = min(agents, key=lambda item: (item["load"], _num(str(item["agent_id"]))))
        agent["groups"].append(list(group))
        agent["load"] = float(agent["load"]) + weight_of(group) + c0
    return max((float(agent["load"]) for agent in agents), default=0.0), agents


def turns(groups: Sequence[Sequence[str]], weight_of, budget: int, slices_of) -> list[list[list[str]]]:
    """An agent's turns: consecutive small groups share a turn while the total stays within the budget (owner,
    2026-11-27); an oversized group runs as its own slices, one turn each."""

    result: list[list[list[str]]] = []
    current: list[list[str]] = []
    load = 0
    for group in groups:
        cut = slices_of(group)
        if len(cut) > 1:
            if current:
                result.append(current)
                current, load = [], 0
            result.extend([[piece] for piece in cut])
            continue
        if current and load + weight_of(group) > budget:
            result.append(current)
            current, load = [], 0
        current.append(list(group))
        load += weight_of(group)
    if current:
        result.append(current)
    return result


def _forced_base(graph: _Graph) -> tuple[set[str], dict[str, list[str]]]:
    build_sources = {str(e["to"]) for e in graph.edges if e["type"] == "build"}
    contract_pkgs: set[str] = set()
    for edge in graph.edges:
        if edge["type"] != "http":
            continue
        for pu in {graph.facts.package_of(p) for p in graph.files.get(str(edge["from"]), ())}:
            for pv in {graph.facts.package_of(p) for p in graph.files.get(str(edge["to"]), ())}:
                both = graph.facts.imports.get(pu, frozenset()) & graph.facts.imports.get(pv, frozenset())
                contract_pkgs |= set(both) - {pu, pv}
    contract_units = {
        unit for unit, paths in graph.files.items() if {graph.facts.package_of(p) for p in paths} & contract_pkgs
    }
    seeds = (build_sources | contract_units) - graph.late
    return down_closure(seeds, graph.hard), {
        "build_sources": _sorted(build_sources),
        "contract_units": _sorted(contract_units),
        "contract_packages": sorted(contract_pkgs),
    }


def score(graph: _Graph, base: set[str], stubs: set[str], *, k: int, c0: float, turn_budget: int,
          base0: set[str]) -> dict[str, object]:
    """One base choice: its groups, LPT agents, merged turns and estimated makespan T."""

    groups = graph.groups(base, stubs)
    attached = graph.attach(base, groups)

    def task_weight(group: Sequence[str]) -> float:
        return graph.weight(group) + graph.extra(attached, tuple(group))

    _, agents = lpt(groups, k, c0, task_weight)

    def cut(group: Sequence[str]) -> list[list[str]]:  # the turn budget counts Design files only (plan 11.7 (3))
        return slices(graph, group, turn_budget) if graph.weight(group) > turn_budget else [list(group)]

    makespan = 0.0
    for agent in agents:
        agent["turns"] = turns(agent["groups"], graph.weight, turn_budget, cut)
        agent["load"] = float(sum(task_weight(g) for g in agent["groups"]) + c0 * len(agent["turns"]))
        makespan = max(makespan, float(agent["load"]))
    integration_open = bool(graph.late or attached.get("integration"))
    total = (
        graph.weight(base) + graph.extra(attached, "base") + (c0 if base else 0.0) + STUB_COST * len(stubs)
        + makespan
        + graph.weight(graph.late) + graph.extra(attached, "integration") + (c0 if integration_open else 0.0)
    )
    return {
        "base": _sorted(base),
        "stubs": _sorted(stubs),
        "complexity": len(base - base0) + len(stubs),
        "T": round(total, 2),
        "groups": groups,
        "agents": agents,
        "attached": attached,
    }


def search_base(
    graph: _Graph, *, k: int, c0: float, threshold: float, turn_budget: int = DEFAULT_TURN_BUDGET,
    stubs_enabled: bool = True,
) -> dict[str, object]:
    """Every base choice over the hub candidates (and stub cuts), scored by T; the threshold rule picks one."""

    base0, forced = _forced_base(graph)
    indegree: dict[str, int] = {}
    for source, targets in graph.hard.items():
        if source in graph.late:
            continue
        for target in targets:
            indegree[target] = indegree.get(target, 0) + 1
    reliance_targets = {str(e["to"]) for e in graph.edges if e["type"] == "reliance"}
    candidates = _sorted(
        unit
        for unit in graph.units
        if (indegree.get(unit, 0) >= 2 or unit in reliance_targets) and unit not in base0 and unit not in graph.late
    )
    options: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, object]] = {}
    pool = candidates[:MAX_BASE_CANDIDATES]
    use_stubs = stubs_enabled and len(pool) <= MAX_STUB_CANDIDATES
    choices = (None, "base", "stub") if use_stubs else (None, "base")
    for assignment in itertools.product(choices, repeat=len(pool)):
        chosen = {unit for unit, how in zip(pool, assignment) if how == "base"}
        stubs = {unit for unit, how in zip(pool, assignment) if how == "stub"}
        base = down_closure(base0 | chosen, graph.hard)
        if stubs & base:
            continue
        key = (tuple(_sorted(base)), tuple(_sorted(stubs)))
        if key not in options:
            options[key] = score(graph, base, stubs, k=k, c0=c0, turn_budget=turn_budget, base0=base0)
    # T1 (base promotion) is tried before T2 (stubs, riskier: behaviour is checked only at integration). Within each
    # tool the fewest splits come first, and a bigger split replaces the choice only if T drops by the threshold.
    def pick(pool_options: list[dict[str, object]], start: dict[str, object] | None) -> dict[str, object]:
        ordered = sorted(pool_options, key=lambda o: (o["complexity"], o["T"], graph.weight(o["base"])))
        chosen = start or ordered[0]
        for option in ordered:
            if option["complexity"] > chosen["complexity"] and option["T"] <= (1 - threshold) * chosen["T"]:
                chosen = option
        return chosen

    without_stubs = [o for o in options.values() if not o["stubs"]]
    chosen_option = pick(without_stubs, None)
    with_stubs = [o for o in options.values() if o["stubs"]]
    if with_stubs:
        chosen_option = pick(with_stubs, chosen_option)
    for option in options.values():
        option["needs_planner"] = bool(option["stubs"])
    by_t = sorted(options.values(), key=lambda o: (o["T"], o["complexity"], graph.weight(o["base"])))
    top = [chosen_option] + [o for o in by_t if o is not chosen_option][:2]
    return {
        "forced": forced,
        "base0": _sorted(base0),
        "candidates": candidates,
        "candidates_searched": pool,
        "stubs_searched": use_stubs,
        "options_considered": len(options),
        "rule": (
            f"base promotion (T1) before stubs (T2); fewest splits first; a bigger split replaces the choice only if "
            f"T drops by >= {threshold:.0%}; a stub option needs the Planner's confirmation"
        ),
        "chosen": chosen_option,
        "top": top,
    }


def slices(graph: _Graph, group: Sequence[str], budget: int) -> list[list[str]]:
    """T4: consecutive turns of one agent, cut at prefixes of the group's topological order (SCCs kept whole)."""

    members = set(group)
    adjacency = {unit: {t for t in graph.hard[unit] if t in members} for unit in group}
    components = strongly_connected(group, adjacency)  # Tarjan yields a unit's dependencies before the unit
    result: list[list[str]] = [[]]
    load = 0
    for component in components:
        weight = graph.weight(component)
        if result[-1] and load + weight > budget:
            result.append([])
            load = 0
        result[-1].extend(component)
        load += weight
    return result if len(result) > 1 else [list(group)]


def check_plan(graph: _Graph, plan: Mapping[str, object]) -> list[dict[str, object]]:
    """G0-G7 on a built plan (G8-G10 run in Phase 4)."""

    issues: list[dict[str, object]] = []

    def issue(check: str, message: str, refs: Iterable[str] = ()) -> None:
        issues.append({"check": check, "message": message, "refs": list(refs)})

    facts = graph.facts
    for unit in graph.units:
        for path in graph.files[unit]:
            if facts.editable_roots and not any(path == r or path.startswith(r + "/") for r in facts.editable_roots):
                issue("G0", f"{unit} writes {path} outside every editable root", [unit])
            if any(path == p or path.startswith(p.rstrip("/") + "/") for p in facts.protected_paths):
                issue("G0", f"{unit} writes protected path {path}", [unit])
        if not graph.files[unit]:
            issue("G1", f"{unit} declares no file", [unit])
    for edge in graph.edges:
        for end in (edge["from"], edge["to"]):
            if end not in graph.files:
                issue("G1", f"edge {edge['from']} -> {edge['to']} names unknown unit {end}", [str(end)])
        if edge["type"] == "reliance" and not str(edge.get("reason") or "").strip():
            issue("G1", f"reliance {edge['from']} -> {edge['to']} has no reason", [str(edge["from"]), str(edge["to"])])
    task_of: dict[str, str] = {}
    base = set(plan["base"]["units"])
    late = set(plan["integration"]["units"])
    for unit in base:
        task_of.setdefault(unit, "base")
    for group in plan["groups"]:
        for unit in group["units"]:
            if unit in task_of:
                issue("G2", f"{unit} is in two tasks", [unit])
            task_of[unit] = str(group["group_id"])
    for unit in late:
        if unit in task_of:
            issue("G2", f"{unit} is in two tasks", [unit])
        task_of[unit] = "integration"
    for unit in graph.units:
        if unit not in task_of:
            issue("G2", f"{unit} is in no task", [unit])
    stubs = set(plan.get("stubs") or [])
    for unit in stubs:
        if not any(graph.types.get((source, unit)) == {"import"} for source in graph.units):
            issue("G6", f"stub {unit} cuts no import edge", [unit])
    for source, targets in graph.hard.items():
        for target in targets:
            if source in base and target not in base:
                issue("G3", f"base unit {source} needs {target} outside the base", [source, target])
    for edge in graph.edges:  # any edge type: nothing outside the integration task may need a check unit
        if edge["to"] in late and edge["from"] not in late:
            issue("G3", f"{edge['from']} needs integration unit {edge['to']} ({edge['type']})",
                  [str(edge["from"]), str(edge["to"])])
    quotient: dict[str, set[str]] = {}
    for source, targets in graph.hard.items():
        for target in targets:
            a, b = task_of.get(source), task_of.get(target)
            if a and b and a != b and not (graph.stub_cut(source, target, stubs) and a.startswith("G-") and b.startswith("G-")):
                quotient.setdefault(a, set()).add(b)
                if a.startswith("G-") and b.startswith("G-"):
                    issue("G4", f"groups {a} and {b} are joined by {source} -> {target} in one wave", [source, target])
    for component in strongly_connected(set(task_of.values()), quotient):
        if len(component) > 1:
            issue("G4", f"task cycle {' <-> '.join(component)}", component)
    for path, writers in graph.writers.items():
        tasks = {task_of.get(unit) for unit in writers if str(task_of.get(unit, "")).startswith("G-")}
        if len(tasks) > 1 and path not in facts.barrels:
            issue("G5", f"{path} is written by groups {sorted(tasks)} in one wave", writers)
    for unit in plan["base"]["forced"]["contract_units"]:
        if unit not in base and any(e["type"] == "http" for e in graph.edges):
            issue("G6", f"contract unit {unit} is not in the base although http edges are cut", [unit])
    if not facts.gate_stage_ids:
        issue("G7", "no per-merge gate stage (typecheck/unit) in the pinned verification policy")
    return issues


def build_plan_graph(
    trace: Mapping[str, object],
    facts: RepoFacts,
    *,
    k: int = DEFAULT_K,
    c0: float = DEFAULT_C0,
    threshold: float = DEFAULT_THRESHOLD,
    turn_budget: int = DEFAULT_TURN_BUDGET,
    reliance: Sequence[Mapping[str, object]] = (),
    base_choice: int | None = None,
    fixed_base: Sequence[str] | None = None,
    fixed_stubs: Sequence[str] = (),
    stubs_enabled: bool = True,
    test_intents: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """The `plan_graph_v1` document: typed edges, base, groups, integration task, tests, schedule and checks.

    `reliance` holds the edges the Planner kept. `base_choice` picks one of the `search.top` options; `fixed_base` and
    `fixed_stubs` instead fix the option the Planner confirmed on the plan built with every candidate kept (that option
    stays valid for any subset of kept edges: fewer hard edges only split groups). Default: the threshold rule's choice.
    `test_intents` are the Planner's checked intents; a new test file weighs on the task of its `anchor` unit.
    """

    edges = classify_edges(trace, facts, reliance)
    graph = _Graph(trace, facts, edges)
    graph.new_tests = [
        {"path": str(item["path"]), "anchor": str(item.get("anchor") or "integration")}
        for item in test_intents
        if item.get("new")
    ]
    search = search_base(graph, k=k, c0=c0, threshold=threshold, turn_budget=turn_budget, stubs_enabled=stubs_enabled)
    if fixed_base is not None:
        base0 = set(search["base0"])
        option = score(graph, down_closure(base0 | set(fixed_base), graph.hard), set(fixed_stubs), k=k, c0=c0,
                       turn_budget=turn_budget, base0=base0)
        option["needs_planner"] = bool(option["stubs"])
    else:
        option = search["top"][base_choice] if base_choice is not None else search["chosen"]
    base = set(option["base"])
    groups = option["groups"]
    group_ids = {tuple(group): f"G-{index + 1}" for index, group in enumerate(groups)}

    def turn_ids(agent_turns: Sequence[Sequence[Sequence[str]]]) -> list[list[str]]:
        labels: list[list[str]] = []
        for turn in agent_turns:
            row = []
            for piece in turn:
                whole = group_ids.get(tuple(piece))
                if whole:
                    row.append(whole)
                else:  # a slice of an oversized group
                    owner = next(g for g in groups if piece[0] in g)
                    number = slices(graph, owner, turn_budget).index(list(piece)) + 1
                    row.append(f"{group_ids[tuple(owner)]}#{number}")
            labels.append(row)
        return labels

    attached = option["attached"]

    def task_weight(group: Sequence[str]) -> float:
        return graph.weight(group) + graph.extra(attached, tuple(group))

    def cut(group: Sequence[str]) -> list[list[str]]:  # the turn budget counts Design files only (plan 11.7 (3))
        return slices(graph, group, turn_budget) if graph.weight(group) > turn_budget else [list(group)]

    def task_label(key: object) -> str:
        return key if isinstance(key, str) else group_ids[key]

    def tests_of(key: object) -> dict[str, list[str]]:
        item = attached.get(key) or {}
        return {"tests": list(item.get("tests") or []), "new_tests": list(item.get("new_tests") or [])}

    test_task = {path: task_label(key) for key, item in attached.items() for path in item.get("tests") or []}
    new_task = {path: task_label(key) for key, item in attached.items() for path in item.get("new_tests") or []}
    mode_a_turns = turns(groups, graph.weight, turn_budget, cut)

    def intent_task(item: Mapping[str, object]) -> str:
        path = str(item.get("path"))
        anchor = item.get("anchor")
        # an existing check file (e2e spec, stage script) belongs to no attached test: its anchor names the task
        return new_task.get(path) or test_task.get(path) or (
            "integration" if anchor == "integration" or anchor in graph.late else "")
    sccs = [component for component in strongly_connected(graph.units, graph.hard) if len(component) > 1]
    conflicts = []
    for path, writers in sorted(graph.writers.items()):
        if len(writers) < 2:
            continue
        tasks = sorted({("base" if u in base else "integration" if u in graph.late else
                         next(gid for g, gid in group_ids.items() if u in g)) for u in writers})
        resolution = "barrel_union" if path in facts.barrels and len(tasks) > 1 else (
            "same_task" if len(tasks) == 1 else "sequential")
        conflicts.append({"file": path, "units": _sorted(writers), "tasks": tasks, "resolution": resolution})
    agents = [
        {
            "agent_id": agent["agent_id"],
            "groups": [group_ids[tuple(group)] for group in agent["groups"]],
            "turns": turn_ids(agent["turns"]),
            "load": agent["load"],
        }
        for agent in option["agents"]
    ]
    plan: dict[str, object] = {
        "schema_version": PLAN_GRAPH_SCHEMA_VERSION,
        "k": k,
        "settings": {"c0": c0, "threshold": threshold, "turn_budget": turn_budget, "stub_cost": STUB_COST,
                     "weight": f"Design files + {TEST_WEIGHT} per existing test + {NEW_TEST_WEIGHT:g} per new test; "
                               "the turn budget counts Design files only",
                     "test_weight": TEST_WEIGHT,
                     "new_test_weight": NEW_TEST_WEIGHT},
        "units": [
            {"id": unit, "files": graph.files[unit], "weight": len(graph.files[unit]),
             "task": "base" if unit in base else "integration" if unit in graph.late else
             next(gid for g, gid in group_ids.items() if unit in g)}
            for unit in graph.units
        ],
        "edges": edges,
        "sccs": sccs,
        "conflicts": conflicts,
        "check_units": _sorted(graph.checks),
        "base": {
            "units": _sorted(base),
            "files": [path for unit in _sorted(base) for path in graph.files[unit]],
            **tests_of("base"),
            "weight": graph.weight(base) + graph.extra(attached, "base"),
            "forced": search["forced"],
            "checkpoint": {"gate_stage_ids": list(facts.gate_stage_ids), "reviewer": True},
        },
        "integration": {
            "units": _sorted(graph.late),
            "files": [path for unit in _sorted(graph.late) for path in graph.files[unit]],
            **tests_of("integration"),
            "weight": graph.weight(graph.late) + graph.extra(attached, "integration"),
        },
        "groups": [
            {
                "group_id": group_ids[tuple(group)],
                "units": group,
                "files": [path for unit in group for path in graph.files[unit]],
                **tests_of(tuple(group)),
                "weight": task_weight(group),
                "slices": cut(group),
            }
            for group in groups
        ],
        "tests": [{**item, "task": test_task.get(item["path"], "")} for item in graph.tests],
        "test_rules": {"test_suffixes": list(facts.test_suffixes), "e2e_roots": list(facts.check_roots),
                       "ui_callers": _sorted({str(e["from"]) for e in edges if e["type"] == "http"})},
        "test_intents": [{**dict(item), "task": intent_task(item)} for item in test_intents],
        "stubs": option["stubs"],
        "search": {
            **{key: value for key, value in search.items() if key not in {"chosen", "top"}},
            "chosen_index": None if fixed_base is not None else base_choice if base_choice is not None else 0,
            "fixed": {"base": list(fixed_base), "stubs": list(fixed_stubs)} if fixed_base is not None else None,
            "top": [
                {"base": o["base"], "stubs": o["stubs"], "T": o["T"], "complexity": o["complexity"],
                 "needs_planner": o["needs_planner"],
                 "group_weights": [graph.weight(g) + graph.extra(o["attached"], tuple(g)) for g in o["groups"]]}
                for o in search["top"]
            ],
        },
        "schedule": {
            "mode_b": {"k": k, "agents": agents, "T": option["T"]},
            "mode_a": {"k": 1, "agents": [{
                "agent_id": "agent-1",
                "groups": [group_ids[tuple(g)] for g in groups],
                "turns": turn_ids(mode_a_turns),
                "load": float(sum(task_weight(g) for g in groups) + c0 * len(mode_a_turns)),
            }]},
        },
        "reliance_candidates": list(facts.reliance_candidates),
    }
    plan["checks"] = {"issues": check_plan(graph, plan), "runtime": list(RUNTIME_CHECKS)}
    return plan


def plan_markdown(plan: Mapping[str, object], *, title: str = "Plan graph") -> str:
    """The checkpoint report: edges by type, reliance candidates, base options, groups, schedule, checks."""

    lines = [f"# {title}", ""]
    lines.append(
        f"k = {plan['k']} · C0 = {plan['settings']['c0']} · threshold = {plan['settings']['threshold']:.0%} · "
        f"turn budget = {plan['settings']['turn_budget']} files · weight = {plan['settings']['weight']}"
    )
    lines += ["", "## Edges by type", "", "| Type | Count | Edges |", "| --- | --- | --- |"]
    for kind in EDGE_TYPES:
        items = [f"{e['from']}->{e['to']}" for e in plan["edges"] if e["type"] == kind]
        if items:
            lines.append(f"| {kind} | {len(items)} | {' '.join(items)} |")
    lines += ["", f"Strongly connected components: {plan['sccs'] or 'none'}"]
    lines += ["", "## Reliance candidates (Harness evidence; the Planner keeps or drops each)", ""]
    by_target: dict[str, list[str]] = {}
    for item in plan["reliance_candidates"]:
        by_target.setdefault(str(item["to"]), []).append(str(item["from"]))
    if not by_target:
        lines.append("none")
    for target, sources in sorted(by_target.items(), key=lambda kv: _num(kv[0])):
        evidence = next(c for c in plan["reliance_candidates"] if c["to"] == target).get("evidence") or [{}]
        sample = evidence[0]
        where = (
            f" (e.g. {sample['middleware']} registered by {sample['registered_by']}:{sample['line']})"
            if sample.get("middleware") else ""
        )
        lines.append(f"- {target} <- {', '.join(_sorted(sources))}{where}")
    kept = [e for e in plan["edges"] if e["type"] == "reliance"]
    lines.append(f"\nKept reliance edges: {len(kept)}")
    base = plan["base"]

    def test_count(task: Mapping[str, object]) -> str:
        return f"{len(task.get('tests') or [])} existing + {len(task.get('new_tests') or [])} new"

    lines += ["", "## Base", "", f"Units: {', '.join(base['units'])} (weight {base['weight']}; tests {test_count(base)})",
              f"Forced: build sources {base['forced']['build_sources']}, contract units {base['forced']['contract_units']}",
              "", "| # | Base | Stubs | T | Group weights |", "| --- | --- | --- | --- | --- |"]
    for index, option in enumerate(plan["search"]["top"]):
        mark = " (chosen)" if index == plan["search"]["chosen_index"] else ""
        lines.append(f"| {index}{mark} | {', '.join(option['base'])} | {', '.join(option['stubs']) or '-'} | "
                     f"{option['T']} | {option['group_weights']} |")
    lines += ["", f"Rule: {plan['search']['rule']}; {plan['search']['options_considered']} options considered."]
    if plan["search"].get("fixed"):
        fixed = plan["search"]["fixed"]
        lines.append(f"Fixed by the Planner: base {fixed['base']}, stubs {fixed['stubs'] or '-'}.")
    lines.append("")
    lines += ["## Groups", "",
              f"Weight = Design files + {plan['settings']['test_weight']} per existing test + "
              f"{plan['settings']['new_test_weight']} per new test.", "",
              "| Group | Units | Weight | Tests | Slices |", "| --- | --- | --- | --- | --- |"]
    for group in plan["groups"]:
        cut = " / ".join(",".join(s) for s in group["slices"]) if len(group["slices"]) > 1 else "-"
        lines.append(f"| {group['group_id']} | {', '.join(group['units'])} | {group['weight']} | {test_count(group)} | {cut} |")
    integration = plan["integration"]
    lines += ["", f"Integration task: {', '.join(integration['units']) or '-'} (weight {integration['weight']}; "
              f"tests {test_count(integration)})", ""]
    if plan.get("test_intents"):
        lines += ["## Test intents", "", "| Proves | Level | Drives | Test file | Task | What it checks |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for intent in plan["test_intents"]:
            marker = " (new)" if intent.get("new") else ""
            lines.append(f"| {', '.join(intent.get('proves') or [])} | {intent.get('level')} | "
                         f"{', '.join(intent.get('drives') or []) or '-'} | {intent.get('path')}{marker} | "
                         f"{intent.get('task')} | {intent.get('intent')} |")
        lines.append("")
    lines += ["## Existing tests a task may edit", ""]
    lines += [f"- {item['task']}: {item['path']} ({', '.join(item['units'])})" for item in plan.get("tests") or []] or ["none"]
    lines.append("")
    def turn_text(agent: Mapping[str, object]) -> str:
        return " ".join("[" + ", ".join(turn) + "]" for turn in agent["turns"]) or "-"

    lines += ["## Schedule (one turn per bracket; small groups of one agent share a turn within the budget)", "",
              f"Mode B (k = {plan['k']}, T = {plan['schedule']['mode_b']['T']}):"]
    for agent in plan["schedule"]["mode_b"]["agents"]:
        lines.append(f"- {agent['agent_id']}: {turn_text(agent)} (load {agent['load']})")
    mode_a = plan["schedule"]["mode_a"]["agents"][0]
    lines.append(f"\nMode A (k = 1, same base and groups): {turn_text(mode_a)} (load {mode_a['load']})")
    http = [f"{e['from']}->{e['to']}" for e in plan["edges"] if e["type"] == "http"]
    callers = (plan.get("test_rules") or {}).get("ui_callers") or []
    lines += ["", "## Accepted risk: cut http edges (checked only by ui test intents in the integration task)", "",
              " ".join(http) or "none", "",
              f"Callers of a cut edge (each driven by >= 1 ui test intent): {', '.join(callers) or 'none'}", "",
              "## Shared files", ""]
    for conflict in plan["conflicts"]:
        lines.append(f"- {conflict['file']}: {', '.join(conflict['units'])} -> {conflict['resolution']}")
    issues = plan["checks"]["issues"]
    lines += ["", "## Checks", ""]
    lines += [f"- {i['check']}: {i['message']}" for i in issues] or ["G0-G7 pass (G8-G10 run in Phase 4)."]
    return "\n".join(lines) + "\n"


def plan_mermaid(plan: Mapping[str, object]) -> str:
    """Task graph drawn by the Harness: base -> groups -> integration."""

    lines = ["flowchart LR", f'  base["Base: {", ".join(plan["base"]["units"])}"]']
    for group in plan["groups"]:
        lines.append(f'  {group["group_id"].replace("-", "")}["{group["group_id"]}: {", ".join(group["units"])}"]')
        lines.append(f'  base --> {group["group_id"].replace("-", "")}')
    if plan["integration"]["units"]:
        lines.append(f'  integration["Integration: {", ".join(plan["integration"]["units"])}"]')
        for group in plan["groups"]:
            lines.append(f'  {group["group_id"].replace("-", "")} --> integration')
    return "\n".join(lines) + "\n"
