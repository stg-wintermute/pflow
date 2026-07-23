"""Interprocedural (whole-program) analyses built on ProgramGraph (v1).

Three views, all conservative may-analyses (RFC §5.6):
  - program_callgraph: cross-module call graph + control architecture
    (entry points, hubs, recursion cycles, layers).
  - state_map: program-wide writers/readers of each self.<attr> and module
    global — surfaces shared-mutable communication channels.
  - trace_value: follow a value across function boundaries (arg forwarding,
    store-escape via shared state, return-flow to callers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .program import ProgramGraph


# ----------------------------------------------------------------------
# Control architecture: cross-module call graph
# ----------------------------------------------------------------------

@dataclass
class CallGraph:
    edges: Dict[str, Set[str]]          # fqname -> callees (in-program)
    rev: Dict[str, Set[str]]            # fqname -> callers
    unresolved: Dict[str, int]          # fqname -> count of calls that resolved to nothing
    # (caller, callee) -> how many candidates the resolving call name fanned
    # out to. 1 = sharp (import/self/same-module match); >1 = simple-name smear
    # (`obj.method()` matching every same-named method). Rendered as `~k` so an
    # agent can tell load-bearing edges from artifacts of name-based resolution.
    ambiguity: Dict[Tuple[str, str], int] = field(default_factory=dict)
    # higher-order edges: the callee was PASSED as an argument, not called by
    # name (`_validated(validate_service_payload, payload)`). Without these,
    # every helper-mediated call path is invisible (seeker's production
    # routes to its validators only existed through tests).
    via_arg: Set[Tuple[str, str]] = field(default_factory=set)
    # edges whose ONLY evidence is the simple-name global fallback ('global'
    # resolution kind) — k can be 1 and still be a guess (`app.deploy(...)` on
    # a local hitting ModalClient.deploy). Marked `~`, excluded from cycle
    # verdicts and exception propagation.
    soft: Set[Tuple[str, str]] = field(default_factory=set)

    def fan_in(self, fq: str) -> int:
        return len(self.rev.get(fq, ()))

    def fan_out(self, fq: str) -> int:
        return len(self.edges.get(fq, ()))

    def edge_mark(self, caller: str, callee: str) -> str:
        k = self.ambiguity.get((caller, callee), 1)
        if k > 1:
            return f"~{k}"
        return "~" if (caller, callee) in self.soft else ""

    def edge_is_soft(self, caller: str, callee: str) -> bool:
        key = (caller, callee)
        return self.ambiguity.get(key, 1) > 1 or key in self.soft


def program_callgraph(pg: ProgramGraph) -> CallGraph:
    """Build (or fetch) the program call graph. Memoized on the ProgramGraph
    and persisted beside the program cache keyed by its file digest — at vllm
    scale the edge resolution costs ~60s, which every interprocedural command
    (at --in, raises, impact, callgraph, census) was paying per invocation."""
    memo = getattr(pg, "_cg_memo", None)
    if memo is not None:
        return memo
    cg = _load_cg_cache(pg) if pg.cache_enabled else None
    if cg is None:
        cg = _build_callgraph(pg)
        if pg.cache_enabled:
            _save_cg_cache(pg, cg)
    pg._cg_memo = cg
    return cg


def _cg_cache_path(pg: ProgramGraph) -> str:
    from .program import _cache_path
    return _cache_path(pg.root) + ".cg"


def _load_cg_cache(pg: ProgramGraph) -> Optional[CallGraph]:
    import pickle
    from .program import _pflow_fingerprint
    try:
        with open(_cg_cache_path(pg), "rb") as f:
            blob = pickle.load(f)
        # gate on BOTH identities: the analyzed files AND the analyzer.
        # Keying on files alone served yesterday's resolver's edges after a
        # pflow upgrade (external audit finding).
        if (blob.get("digest") == pg.cache_digest
                and blob.get("fp") == _pflow_fingerprint()):
            return blob["cg"]
    except Exception:
        pass
    return None


def _save_cg_cache(pg: ProgramGraph, cg: CallGraph) -> None:
    import pickle
    from .program import _pflow_fingerprint
    try:
        with open(_cg_cache_path(pg), "wb") as f:
            pickle.dump({"digest": pg.cache_digest, "fp": _pflow_fingerprint(),
                         "cg": cg}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"pflow: callgraph cache not saved ({type(e).__name__})",
              file=sys.stderr)


def _build_callgraph(pg: ProgramGraph) -> CallGraph:
    edges: Dict[str, Set[str]] = {fq: set() for fq in pg.functions}
    rev: Dict[str, Set[str]] = {fq: set() for fq in pg.functions}
    unresolved: Dict[str, int] = {}
    ambiguity: Dict[Tuple[str, str], int] = {}
    via_arg: Set[Tuple[str, str]] = set()
    soft: Set[Tuple[str, str]] = set()

    def add(fq: str, targets, kind: str, is_arg: bool) -> None:
        k = len(targets)
        for t in targets:
            if t in edges:
                edges[fq].add(t)
                rev[t].add(fq)
                key = (fq, t)
                # keep the SHARPEST evidence: one unambiguous call site
                # outweighs any number of smeared ones.
                ambiguity[key] = min(ambiguity.get(key, k), k)
                if is_arg:
                    via_arg.add(key)
                if kind == "global":
                    soft.add(key)
                elif key in soft:
                    soft.discard(key)     # sharper evidence supersedes

    for fq, g in pg.functions.items():
        local_names = {d.name for d in g.defs}
        for op in g.iter_ops():
            for c in op.attrs.get("calls", ()):
                name = c.get("func")
                if name:
                    targets, kind = pg.resolve_call_kind(fq, name)
                    if targets:
                        add(fq, targets, kind, is_arg=False)
                    else:
                        unresolved[fq] = unresolved.get(fq, 0) + 1
                # address-taken: a BARE name passed as an argument that
                # resolves sharply to an in-program function is a potential
                # call-through (k<=2 — smear would fabricate edges; local
                # variables are values, not function references)
                for a in c.get("args", ()):
                    if a and "." not in a and a not in local_names:
                        targets, kind = pg.resolve_call_kind(fq, a)
                        if targets and len(targets) <= 2:
                            add(fq, targets, kind, is_arg=True)
    return CallGraph(edges=edges, rev=rev, unresolved=unresolved,
                     ambiguity=ambiguity, via_arg=via_arg, soft=soft)


def call_paths(cg: CallGraph, to: str, origin: Optional[str] = None,
               max_depth: int = 8, max_paths: int = 10) -> List[List[str]]:
    """Call chains that can reach `to` — the "how does execution get here"
    debugging question. Walks caller edges backward from `to`; a chain ends at
    `origin` (if given) or at any entrypoint (no in-program callers). BFS, so
    shortest chains come first; cycles are not re-entered within one chain."""
    roots = set(entrypoints(cg)) if origin is None else {origin}
    out: List[List[str]] = []
    from collections import deque
    q = deque([[to]])
    while q and len(out) < max_paths:
        chain = q.popleft()
        head = chain[0]
        if (head in roots and len(chain) > 1) or (origin is None and not cg.rev.get(head)):
            out.append(chain)
            continue
        if len(chain) > max_depth:
            continue
        for caller in sorted(cg.rev.get(head, ())):
            if caller not in chain:            # no cycle re-entry
                q.append([caller] + chain)
    return out


def entrypoints(cg: CallGraph) -> List[str]:
    """Functions nobody in-program calls (handlers, CLI commands, dead code)."""
    return sorted(fq for fq in cg.edges if not cg.rev.get(fq))


def leaves(cg: CallGraph) -> List[str]:
    return sorted(fq for fq in cg.edges if not cg.edges.get(fq))


def hubs(cg: CallGraph, top: int = 10) -> List[Tuple[str, int, int]]:
    """(fqname, fan_in, fan_out) sorted by fan_in then fan_out."""
    rows = [(fq, cg.fan_in(fq), cg.fan_out(fq)) for fq in cg.edges]
    rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
    return rows[:top]


def cycles(cg: CallGraph) -> List[List[str]]:
    """Strongly-connected components of size > 1, plus self-recursive nodes."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    counter = [0]
    sccs: List[List[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in cg.edges.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    # iterative-safe for big graphs: raise recursion limit modestly via chunking
    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, len(cg.edges) * 4 + 1000))
    try:
        for v in cg.edges:
            if v not in index:
                strongconnect(v)
    finally:
        sys.setrecursionlimit(old)

    out = [c for c in sccs if len(c) > 1]
    # self-recursion: only via a SHARP self-edge. A smeared one (`self._client
    # .create()` inside Sandbox.create matching its own name) is a resolution
    # artifact, not recursion.
    out += [[v] for v in cg.edges
            if v in cg.edges.get(v, ()) and not cg.edge_is_soft(v, v)]
    return out


def cycle_is_smeared(cg: CallGraph, comp: List[str]) -> bool:
    """True when the cycle cannot be closed without at least one SOFT edge
    (ambiguous k>1 or global-fallback) — it may exist only in name-resolution
    space. A cycle whose every member is reachable around the loop on sharp
    edges alone is real."""
    members = set(comp)
    sharp_edges: Dict[str, List[str]] = {m: [] for m in comp}
    any_edge = False
    for a in comp:
        for b in cg.edges.get(a, ()):
            if b in members:
                any_edge = True
                if not cg.edge_is_soft(a, b):
                    sharp_edges[a].append(b)
    if not any_edge:
        return False
    # is `comp` still strongly connected using sharp edges only?
    start = comp[0]
    seen = {start}
    stack = [start]
    while stack:
        for n in sharp_edges.get(stack.pop(), ()):
            if n not in seen:
                seen.add(n)
                stack.append(n)
    if seen != members:
        return True
    # reverse reachability too (strong connectivity, not just reach)
    rev_sharp: Dict[str, List[str]] = {m: [] for m in comp}
    for a, bs in sharp_edges.items():
        for b in bs:
            rev_sharp[b].append(a)
    seen = {start}
    stack = [start]
    while stack:
        for n in rev_sharp.get(stack.pop(), ()):
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return seen != members


def layers(cg: CallGraph, roots: Optional[List[str]] = None) -> Dict[str, int]:
    """Min call-depth of each function from the entry points (BFS)."""
    from collections import deque
    if roots is None:
        roots = entrypoints(cg)
    depth: Dict[str, int] = {r: 0 for r in roots}
    q = deque(roots)
    while q:
        v = q.popleft()
        for w in cg.edges.get(v, ()):
            if w not in depth:
                depth[w] = depth[v] + 1
                q.append(w)
    return depth


def reachable_from(cg: CallGraph, root: str, max_depth: int = 6) -> Dict[str, int]:
    from collections import deque
    depth = {root: 0}
    q = deque([root])
    while q:
        v = q.popleft()
        if depth[v] >= max_depth:
            continue
        for w in cg.edges.get(v, ()):
            if w not in depth:
                depth[w] = depth[v] + 1
                q.append(w)
    return depth


# ----------------------------------------------------------------------
# Shared-state / coupling map
# ----------------------------------------------------------------------

@dataclass
class StateCell:
    name: str                       # readable key, e.g. "server/state.py:State._db" or "cli.py:GLOBAL:_cache"
    kind: str                       # "attr" | "global"
    writers: Set[str] = field(default_factory=set)
    readers: Set[str] = field(default_factory=set)

    @property
    def flags(self) -> List[str]:
        out = []
        external_readers = self.readers - self.writers
        if len(self.writers) >= 2 and external_readers:
            out.append("shared-mutable-channel")
        if self.kind == "global" and self.writers and len(self.readers) >= 2:
            out.append("global-channel")
        if len(self.writers) >= 3:
            out.append("many-writers")
        if len(self.readers) >= 6:
            out.append("widely-read")
        return out


def _attr_root(path: str) -> Optional[str]:
    """`self.a.b` / `self.a[i]` -> `a`; non-self paths -> None."""
    if not path.startswith("self."):
        return None
    rest = path[5:]
    return rest.split(".")[0].split("[")[0] or None


def _owner_methods(pg: ProgramGraph, owner: Optional[str], mod) -> Set[str]:
    if owner is None or mod is None:
        return set()
    cls_name = owner.split(":", 1)[1]
    ci = mod.classes.get(cls_name)
    return set(ci.methods) if ci else set()


def state_map(pg: ProgramGraph) -> Dict[str, StateCell]:
    """Program-wide writers/readers of each self.<attr> and module global."""
    cells: Dict[str, StateCell] = {}

    def cell(key: str, kind: str) -> StateCell:
        if key not in cells:
            cells[key] = StateCell(name=key, kind=kind)
        return cells[key]

    for fq, g in pg.functions.items():
        owner = pg.class_of(fq)          # "relpath:Class" or None
        mod = pg.module_of(fq)
        gdecls = g.attrs.get("global_decls", set())
        methods = _owner_methods(pg, owner, mod)
        for op in g.iter_ops():
            # instance attributes via self.<path> — normalize to the instance
            # attribute root (`self.a.b` -> `a`) and skip method references
            # (`self.method(...)` is a call, not state).
            if owner is not None:
                for t in op.targets:
                    a = _attr_root(t)
                    if a and a not in methods:
                        cell(f"{owner}.{a}", "attr").writers.add(fq)
                for u in op.uses:
                    a = _attr_root(u)
                    if a and a not in methods:
                        cell(f"{owner}.{a}", "attr").readers.add(fq)
            # module globals
            if mod is not None:
                for t in op.targets:
                    if t in mod.globals and t in gdecls:
                        cell(f"{mod.relpath}:GLOBAL:{t}", "global").writers.add(fq)
                for u in op.uses:
                    if u in mod.globals:
                        cell(f"{mod.relpath}:GLOBAL:{u}", "global").readers.add(fq)
    return cells


def state_rows(pg: ProgramGraph, focus: Optional[str] = None) -> List[StateCell]:
    """state_map sorted for display: flagged + most-coupled first."""
    cells = list(state_map(pg).values())
    if focus:
        cells = [c for c in cells if focus in c.name]
    cells.sort(key=lambda c: (bool(c.flags), len(c.writers) + len(c.readers),
                              len(c.writers)), reverse=True)
    return cells


# ----------------------------------------------------------------------
# Interprocedural value tracing (conservative may-flow)
# ----------------------------------------------------------------------

@dataclass
class TraceStep:
    depth: int
    fq: str
    var: str
    kind: str          # origin | assign | arg | return | store | read | use
    detail: str = ""


def _param_at(pg: ProgramGraph, callee_fq: str, idx: int) -> Optional[str]:
    """Name of the idx-th positional parameter of callee (accounting for self)."""
    g = pg.functions.get(callee_fq)
    if g is None or not g.blocks:
        return None
    args_op = next((op for op in g.blocks[0].ops
                    if op.kind == "assign" and op.attrs.get("kind") == "args"), None)
    if args_op is None:
        return None
    params = list(args_op.targets)
    if pg.class_of(callee_fq) and params and params[0] in ("self", "cls"):
        idx += 1
    return params[idx] if 0 <= idx < len(params) else None


def _callers_binding(pg: ProgramGraph, cg: CallGraph, callee_fq: str):
    """Yield (caller_fq, target_var) where caller assigns callee's return value."""
    for caller in cg.rev.get(callee_fq, ()):
        g = pg.functions.get(caller)
        if g is None:
            continue
        for op in g.iter_ops():
            calls = op.attrs.get("calls", ())
            if not calls or not op.targets:
                continue
            for c in calls:
                if callee_fq in pg.resolve_call(caller, c.get("func") or ""):
                    yield caller, op.targets[0]
                    break


def trace_value(pg: ProgramGraph, value: str, origin: Optional[str] = None,
                max_depth: int = 6, max_steps: int = 300) -> List[TraceStep]:
    """Follow `value` across function boundaries (arg forwarding, store-escape
    through shared state, return-flow to callers). Conservative may-flow."""
    cg = program_callgraph(pg)
    sm = state_map(pg)

    # seeds
    seeds: List[Tuple[str, str]] = []
    if origin:
        seeds.append((origin, value))
    else:
        for fq, g in pg.functions.items():
            if any(d.name == value for d in g.defs) or \
               any(value in op.targets for op in g.iter_ops()):
                seeds.append((fq, value))

    steps: List[TraceStep] = []
    visited: Set[Tuple[str, str]] = set()
    from collections import deque
    q: deque = deque()
    for fq, var in seeds:
        if (fq, var) not in visited:
            visited.add((fq, var))
            q.append((fq, var, 0, "origin", ""))

    while q and len(steps) < max_steps:
        fq, var, depth, kind, detail = q.popleft()
        steps.append(TraceStep(depth, fq, var, kind, detail))
        if depth >= max_depth:
            continue
        g = pg.functions.get(fq)
        if g is None:
            continue
        owner = pg.class_of(fq)

        def enqueue(nfq, nvar, nkind, ndetail):
            key = (nfq, nvar)
            if key not in visited:
                visited.add(key)
                q.append((nfq, nvar, depth + 1, nkind, ndetail))

        for op in g.iter_ops():
            if var not in op.uses:
                continue
            # return-flow: value leaves via return -> caller's binding var
            if op.attrs.get("is_return"):
                for caller, tvar in _callers_binding(pg, cg, fq):
                    enqueue(caller, tvar, "return",
                            f"{fq.split(':')[-1]} returns -> {caller.split(':')[-1]}:{tvar}")
            # argument forwarding: value passed into a callee parameter
            for c in op.attrs.get("calls", ()):
                args = c.get("args", ())
                if var in args:
                    idx = args.index(var)
                    for callee in pg.resolve_call(fq, c.get("func") or ""):
                        pname = _param_at(pg, callee, idx)
                        if pname:
                            enqueue(callee, pname, "arg",
                                    f"arg{idx} -> {callee.split(':')[-1]}({pname})")
            # assignment / store
            for t in op.targets:
                if t == var:
                    continue
                if t.startswith("self.") and owner is not None:
                    root = _attr_root(t)
                    cellkey = f"{owner}.{root}"
                    cell = sm.get(cellkey)
                    detail2 = f"stored {fq.split(':')[-1]} -> self.{root}"
                    if cell:
                        for reader in cell.readers:
                            enqueue(reader, f"self.{root}", "store", detail2)
                    # also continue intra-function under the attr name
                    enqueue(fq, f"self.{root}", "store", detail2)
                else:
                    enqueue(fq, t, "assign", f"{var} -> {t}")
    return steps
