"""Dependence-based opportunity: overloaded variables (RFC-0002 §4 Tier 2).

NOT WIRED into run_passes: dogfooding found a high false-positive rate — a
variable threaded through a transformation pipeline (`x = f(x); x = g(x)`)
fragments into N disjoint def-use groups, and a loop-var name reused across two
loops looks the same. Distinguishing those from genuine overloading needs
semantic judgment beyond def-use, so this is kept for later refinement only.


If a local name's def-use relationships partition into >=2 disjoint groups
(each with its own defs and uses), the name is reused for unrelated values —
a readability simplification: give each purpose its own name. Loop accumulators
(`total = total + x`) form a single connected group via the loop-carried use,
so they are not flagged. Conservative; heuristic/may (renaming is a judgment
call). Grounded in RAW/WAR/WAW false-dependence classification.
"""

from __future__ import annotations

from typing import Dict, List

from ..ir import FunctionGraph
from .opportunities import Opportunity


def find_overloaded_variables(graph: FunctionGraph) -> List[Opportunity]:
    defs_by_name: Dict[str, list] = {}
    uses_by_name: Dict[str, list] = {}
    for d in graph.defs:
        defs_by_name.setdefault(d.name, []).append(d)
    for u in graph.uses:
        uses_by_name.setdefault(u.name, []).append(u)
    global_decls = graph.attrs.get("global_decls", set())
    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}

    out: List[Opportunity] = []
    for name, defs in defs_by_name.items():
        if len(defs) < 2 or "." in name or "[" in name or name.startswith("_"):
            continue
        if name in global_decls:
            continue
        uses = uses_by_name.get(name, [])
        nodes = [("d", d.id) for d in defs] + [("u", u.id) for u in uses]
        idx = {n: i for i, n in enumerate(nodes)}
        parent = list(range(len(nodes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for u in uses:
            for did in u.reaching_defs:
                a, b = idx.get(("d", did)), idx.get(("u", u.id))
                if a is not None and b is not None:
                    parent[find(a)] = find(b)

        comps: Dict[int, list] = {}
        for i in range(len(nodes)):
            comps.setdefault(find(i), []).append(nodes[i])
        groups = [c for c in comps.values()
                  if any(k == "d" for k, _ in c) and any(k == "u" for k, _ in c)]
        if len(groups) >= 2:
            ln = op_line.get(defs[0].op_id)
            ref = (f"{graph.qualname}:def:{name}@{ln}" if ln
                   else f"{graph.qualname}:op:{defs[0].op_id}")
            out.append(Opportunity(
                pass_name="overloaded-var", kind="reuse",
                title=f"`{name}` is reused for {len(groups)} unrelated values "
                      f"(disjoint def-use groups) — give each purpose its own name",
                ref=ref, op_id=defs[0].op_id, block_id=defs[0].block_id,
                line=ln, modality="may", soundness="heuristic"))
    return out
