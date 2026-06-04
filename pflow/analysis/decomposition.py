"""Function decomposition / split-point pass (RFC-0002 §5, the flagship).

Slice each output of a function over the PDG (control + data dependence); group
outputs whose slices overlap. If a function computes several output groups whose
slices are disjoint, it does several weakly-related things -> a split candidate,
with the seams being the group boundaries (decomposition slicing — Gallagher &
Lyle; slice-based cohesion — Bieman & Ott; slice-cohesion-refactoring.miner).

Outputs considered: each `return` and each write to a `self.<attr>` / module
global. LIMITATION: a `return (p, q)` of independent tuple elements is one
output here (op granularity is the whole return expression), so same-return
independence isn't split — multi-output / multi-side-effect functions are.
Heuristic/may: a suggestion the agent confirms.
"""

from __future__ import annotations

from typing import Dict, List, Set

from ..ir import FunctionGraph
from .control_dependence import compute_control_dependence
from .opportunities import Opportunity


def _backward_slice_ops(graph, start_ops, cd, def_op, op_by_id,
                        block_of_op, branch_ops_by_block, reaching) -> Set[int]:
    seen: Set[int] = set(start_ops)
    stack = list(start_ops)
    while stack:
        oid = stack.pop()
        op = op_by_id.get(oid)
        if op is None:
            continue
        for name in op.uses:                       # data dependence
            for did in reaching.get((oid, name), ()):
                doid = def_op.get(did)
                if doid is not None and doid not in seen:
                    seen.add(doid)
                    stack.append(doid)
        for cblk in cd.get(block_of_op.get(oid), ()):   # control dependence
            for bop in branch_ops_by_block.get(cblk, ()):
                if bop not in seen:
                    seen.add(bop)
                    stack.append(bop)
    return seen


def find_decomposition(graph: FunctionGraph, min_ops: int = 10,
                       min_group_ops: int = 4) -> List[Opportunity]:
    # Conservative bar (the FP-magnet otherwise): only larger functions, never
    # dunders (__init__ legitimately sets many independent attrs), and each
    # independent group must do real work (>= min_group_ops). Split suggestions
    # are judgment calls, so few-but-precise per the <10% false-positive rule.
    if len(graph.ops) < min_ops:
        return []
    last = graph.qualname.split(".")[-1]
    if last.startswith("__") and last.endswith("__"):
        return []

    op_by_id = {op.id: op for op in graph.iter_ops()}
    block_of_op = {op.id: b.id for b in graph.blocks for op in b.ops}
    def_op = {d.id: d.op_id for d in graph.defs}
    branch_ops_by_block: Dict[int, List[int]] = {}
    for b in graph.blocks:
        bops = [o.id for o in b.ops if o.kind == "branch"]
        if bops:
            branch_ops_by_block[b.id] = bops
    reaching: Dict = {}
    for u in graph.uses:
        reaching.setdefault((u.op_id, u.name), set()).update(u.reaching_defs)
    cd = graph.attrs.get("control_deps") or compute_control_dependence(graph)
    globals_ = graph.attrs.get("global_decls", set())
    args_op = next((op.id for op in (graph.blocks[0].ops if graph.blocks else [])
                    if op.attrs.get("kind") == "args"), None)

    outputs = []  # (label, slice_ops)
    for op in graph.iter_ops():
        starts = {op.id}
        if op.attrs.get("is_return") and op.uses:
            label = f"return@{op.source[0]}" if op.source else f"return:op{op.id}"
            outputs.append((label, _backward_slice_ops(
                graph, starts, cd, def_op, op_by_id, block_of_op,
                branch_ops_by_block, reaching)))
        for t in op.targets:
            if t.startswith("self.") or t in globals_:
                outputs.append((t, _backward_slice_ops(
                    graph, starts, cd, def_op, op_by_id, block_of_op,
                    branch_ops_by_block, reaching)))

    if len(outputs) < 2:
        return []

    # exclude the params op: every slice contains it, but params are shared
    # inputs, not coupling between outputs.
    drop = {args_op} if args_op is not None else set()
    labels = [l for l, _ in outputs]
    slices = [s - drop for _, s in outputs]

    # union-find: outputs are coupled if their slices share an op.
    parent = list(range(len(slices)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(slices)):
        for j in range(i + 1, len(slices)):
            if slices[i] & slices[j]:
                parent[find(i)] = find(j)

    comps: Dict[int, List[int]] = {}
    for i in range(len(slices)):
        comps.setdefault(find(i), []).append(i)

    # keep only SUBSTANTIAL groups (real computation, not a bare store)
    substantial = [idxs for idxs in comps.values()
                   if len(set().union(*(slices[i] for i in idxs))) >= min_group_ops]
    if len(substantial) < 2:
        return []

    sub_slices = [set().union(*(slices[i] for i in idxs)) for idxs in substantial]
    union_all = set().union(*sub_slices)
    inter_all = set(sub_slices[0])
    for s in sub_slices[1:]:
        inter_all &= s
    tightness = len(inter_all) / len(union_all) if union_all else 1.0
    groups = " | ".join(", ".join(labels[i] for i in idxs) for idxs in substantial)
    return [Opportunity(
        pass_name="decomposition", kind="low_cohesion",
        title=f"computes {len(substantial)} weakly-related output groups "
              f"(tightness={tightness:.2f}) — split candidate: {groups}",
        ref=f"{graph.qualname}:bb:{graph.entry}", op_id=-1,
        block_id=graph.entry, line=graph.first_line,
        modality="may", soundness="heuristic")]
