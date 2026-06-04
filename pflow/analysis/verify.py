"""Transform verification — the round-trip near-term form (RFC-0002 §6, Phase D).

The agent proposes a refactor; pflow checks two things about old vs new:
  1. behavior preserved (conservatively): every output (return / self.attr /
     global) depends on the SAME set of external inputs (params, globals, free
     vars, self.attrs) in both versions. This is a flow-equivalence proxy, NOT a
     proof — it cannot see value changes or reordered side effects; it catches
     the common "the refactor changed what feeds an output" mistake.
  2. simpler: the complexity metrics did not increase, and at least one dropped.

So a green verdict means "same input->output dependence structure, measurably
simpler flow" — exactly what an opinionated simplifying refactor should be.
"""

from __future__ import annotations

from typing import Dict, Set

from ..ir import FunctionGraph
from .complexity import compute_metrics
from .control_dependence import compute_control_dependence
from .decomposition import _backward_slice_ops


def _output_input_sets(g: FunctionGraph) -> Dict[str, Set[str]]:
    op_by_id = {op.id: op for op in g.iter_ops()}
    block_of_op = {op.id: b.id for b in g.blocks for op in b.ops}
    def_op = {d.id: d.op_id for d in g.defs}
    branch_ops: Dict[int, list] = {}
    for b in g.blocks:
        bops = [o.id for o in b.ops if o.kind == "branch"]
        if bops:
            branch_ops[b.id] = bops
    reaching: Dict = {}
    for u in g.uses:
        reaching.setdefault((u.op_id, u.name), set()).update(u.reaching_defs)
    cd = g.attrs.get("control_deps") or compute_control_dependence(g)

    params: Set[str] = set()
    if g.blocks:
        for op in g.blocks[0].ops:
            if op.attrs.get("kind") == "args":
                params |= set(op.targets)
    local_defs = {d.name for d in g.defs}
    globals_ = g.attrs.get("global_decls", set())

    def inputs(slice_ops) -> Set[str]:
        names: Set[str] = set()
        for oid in slice_ops:
            op = op_by_id.get(oid)
            if not op:
                continue
            for u in op.uses:
                root = u.split(".")[0].split("[")[0]
                if root in params or root not in local_defs or root in globals_:
                    names.add(u)
        return names

    def bslice(starts):
        return _backward_slice_ops(g, starts, cd, def_op, op_by_id,
                                   block_of_op, branch_ops, reaching)

    out: Dict[str, Set[str]] = {}
    ret_ops = {op.id for op in g.iter_ops() if op.attrs.get("is_return") and op.uses}
    if ret_ops:
        out["<return>"] = inputs(bslice(ret_ops))
    for op in g.iter_ops():
        for t in op.targets:
            if t.startswith("self.") or t in globals_:
                out.setdefault(t, set()).update(inputs(bslice({op.id})))
    return out


_METRICS = ["cyclomatic", "cognitive", "max_nesting", "max_live_span", "depdegree"]


def verify_refactor(old: FunctionGraph, new: FunctionGraph) -> dict:
    oi, ni = _output_input_sets(old), _output_input_sets(new)
    diffs = []
    for label in sorted(set(oi) | set(ni)):
        o, n = oi.get(label, set()), ni.get(label, set())
        if o != n:
            diffs.append((label, sorted(o - n), sorted(n - o)))
    mo, mn = compute_metrics(old), compute_metrics(new)
    deltas = {k: (mo[k], mn[k]) for k in _METRICS}
    simpler = all(mn[k] <= mo[k] for k in _METRICS) and any(mn[k] < mo[k] for k in _METRICS)
    return {"behavior_ok": not diffs, "diffs": diffs, "deltas": deltas, "simpler": simpler}
