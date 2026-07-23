"""Module-level import graph: the architecture layer above the call graph.

Nodes are the program's modules (relpaths), edges are import statements that
resolve to an in-program module. Reveals the structure questions the call
graph is too fine-grained for: layering (who is allowed to know about whom),
import cycles (the classic packaging failure), fan-in hubs (the de-facto core
modules), and the external dependency surface.

Resolution is name-based like everything interprocedural in pflow: relative
imports resolve against the importer's package; absolute imports match a
module either exactly or by dotted suffix (a program rooted at `pkg/` sees
its own modules as `sub.mod`, while the source says `pkg.sub.mod`). `from m
import symbol` strips trailing components until a module matches.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Set, Tuple

from .program import ProgramGraph


def _resolve(dotted: str, importer_pkg: str, by_name: Dict[str, str]) -> str | None:
    """dotted import -> relpath of an in-program module, or None (external)."""
    if dotted.startswith("."):
        n = len(dotted) - len(dotted.lstrip("."))
        rest = dotted.lstrip(".")
        base = importer_pkg.split(".") if importer_pkg else []
        base = base[:len(base) - (n - 1)] if n > 1 else base
        cand = ".".join(base + (rest.split(".") if rest else []))
        candidates = [cand]
    else:
        candidates = [dotted]

    for cand in candidates:
        parts = cand.split(".")
        # strip trailing components: `from a.b import c` binds c but the
        # module might be a.b (or a.b.c if c is a submodule) — try longest first
        for cut in range(len(parts), 0, -1):
            name = ".".join(parts[:cut])
            if name in by_name:
                return by_name[name]
            # absolute path written from outside the program root:
            # `pkg.sub.mod` should match in-program `sub.mod`
            hits = [rp for m, rp in by_name.items()
                    if name.endswith("." + m)]
            if len(hits) == 1:
                return hits[0]
    return None


def module_import_graph(pg: ProgramGraph):
    """(eager edges, lazy edges, external Counter).

    Eager edges run at import time (top-level statements) — they order module
    loading and can deadlock in cycles. Lazy edges (imports inside functions)
    are architectural dependencies that dodge load order; a lazy edge whose
    reverse path exists eagerly is usually a deliberately broken cycle."""
    by_name = {m.module_name: m.relpath for m in pg.modules.values() if m.module_name}
    eager: Dict[str, Set[str]] = {rp: set() for rp in pg.modules}
    lazy: Dict[str, Set[str]] = {rp: set() for rp in pg.modules}
    external: Counter = Counter()
    for rp, mod in pg.modules.items():
        pkg = (mod.module_name.rsplit(".", 1)[0]
               if "." in mod.module_name else
               (mod.module_name if mod.relpath.endswith("__init__.py") else ""))
        for dotted in set(mod.imports.values()):
            target = _resolve(dotted, pkg, by_name)
            if target is not None and target != rp:
                eager[rp].add(target)
            elif target is None and not dotted.startswith("."):
                external[dotted.split(".")[0]] += 1
        for dotted in set(getattr(mod, "lazy_imports", {}).values()):
            target = _resolve(dotted, pkg, by_name)
            if target is not None and target != rp and target not in eager[rp]:
                lazy[rp].add(target)
            elif target is None and not dotted.startswith("."):
                external[dotted.split(".")[0]] += 1
    return eager, lazy, external


def _reaches(edges: Dict[str, Set[str]], src: str, dst: str) -> bool:
    seen = {src}
    stack = [src]
    while stack:
        for n in edges.get(stack.pop(), ()):
            if n == dst:
                return True
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return False


def broken_cycles(eager: Dict[str, Set[str]], lazy: Dict[str, Set[str]]):
    """Lazy edges a→b where b already reaches a eagerly: the import cycle that
    WOULD exist, deliberately deferred out of load order."""
    out = []
    for a, tgts in lazy.items():
        for b in tgts:
            if _reaches(eager, b, a):
                out.append((a, b))
    return sorted(out)


def sccs(edges: Dict[str, Set[str]]) -> List[List[str]]:
    """Strongly-connected components of size > 1 (iterative Tarjan)."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on: Set[str] = set()
    stack: List[str] = []
    out: List[List[str]] = []
    counter = [0]

    def connect(root: str) -> None:
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in edges:
                    continue
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                pv = work[-1][0]
                low[pv] = min(low[pv], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))

    for v in edges:
        if v not in index:
            connect(v)
    return out


def layers(edges: Dict[str, Set[str]]) -> Dict[str, int]:
    """Longest-path depth of each module over the import DAG: depth 0 imports
    nothing in-program (the foundation); a module sits one above its deepest
    import. Cycle members share the max depth reachable ignoring back-edges
    (computed with memoized DFS that treats in-cycle re-entry as 0)."""
    memo: Dict[str, int] = {}
    visiting: Set[str] = set()

    def depth(m: str) -> int:
        if m in memo:
            return memo[m]
        if m in visiting:
            return 0
        visiting.add(m)
        d = max((depth(x) + 1 for x in edges.get(m, ())), default=0)
        visiting.discard(m)
        memo[m] = d
        return d

    return {m: depth(m) for m in edges}


def fan(edges: Dict[str, Set[str]]) -> List[Tuple[str, int, int]]:
    """(relpath, fan_in, fan_out) sorted by fan_in."""
    fin: Counter = Counter()
    for src, tgts in edges.items():
        for t in tgts:
            fin[t] += 1
    rows = [(m, fin.get(m, 0), len(tgts)) for m, tgts in edges.items()]
    rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
    return rows
