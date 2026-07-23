"""Constant propagation + always-taken branch (RFC-0002 §4 Tier 2).

Forward analysis on the generic solver. Each name maps to a constant literal,
TOP (not constant), or is absent (BOTTOM / no info). A branch whose condition is
a single name that is a known constant is always taken one way — extends the
literal-only redundant-branch pass to constant-valued variables.

Sound (it reports only facts true on all paths) but incomplete: it tracks only
direct literal assignments (`x = 5`), not computed/copied/mutated values — so it
under-reports, never over-reports. (Non-distributivity: MFP < MOP at merges.)
"""

from __future__ import annotations

from typing import Dict, List

from ..ir import FunctionGraph
from .solver import solve
from .opportunities import Opportunity

_TOP = object()  # "not a constant"


def constant_values(graph: FunctionGraph) -> Dict[int, Dict[str, object]]:
    """block_id -> {name: literal} of names definitely constant at block entry."""
    # A name any nested def rebinds via `nonlocal` is never constant here:
    # every call may flip it (seeker-dev's `stop` flag, set by a signal
    # handler closure, was reported constant-False — a sound-tier FP).
    volatile = graph.attrs.get("nonlocal_writes", frozenset())

    def transfer(blk, in_fact):
        out = dict(in_fact)
        for op in blk.ops:
            for t in op.targets:
                if t in volatile:
                    out[t] = _TOP
                elif "const_value" in op.attrs and len(op.targets) == 1:
                    out[t] = op.attrs["const_value"]
                else:
                    out[t] = _TOP            # computed/loop/except/param -> unknown
        return out

    def meet(facts):
        names = set().union(*facts) if facts else set()
        merged = {}
        for nm in names:
            # a fact lacking nm contributes BOTTOM (no info), the meet identity —
            # NOT "unknown". Treating absence as TOP poisoned constants at any
            # join fed by a not-yet-converged back-edge (loops).
            present = [f[nm] for f in facts if nm in f]
            if any(v is _TOP for v in present):
                merged[nm] = _TOP
            elif all(v == present[0] for v in present):
                merged[nm] = present[0]
            else:
                merged[nm] = _TOP            # constant differs across paths
        return merged

    in_f, _ = solve(graph, "forward", init=dict, boundary=dict,
                    meet=meet, transfer=transfer)
    # keep only genuine constants (drop TOP)
    return {bid: {n: v for n, v in f.items() if v is not _TOP}
            for bid, f in in_f.items()}


def find_constant_branches(graph: FunctionGraph) -> List[Opportunity]:
    consts = constant_values(graph)
    out: List[Opportunity] = []
    for blk in graph.blocks:
        cur = dict(consts.get(blk.id, {}))
        for op in blk.ops:
            if op.kind == "branch" and op.attrs.get("construct") == "if":
                cond = op.attrs.get("condition")
                if cond in cur:               # condition is exactly a constant var
                    val = cur[cond]
                    ln = op.source[0] if op.source else None
                    out.append(Opportunity(
                        pass_name="const-branch", kind="constant_var_condition",
                        title=f"condition `{cond}` is always {val!r} (constant) — "
                              f"the {'else' if val else 'then'} branch is dead",
                        ref=f"{graph.qualname}:op:{op.id}", op_id=op.id,
                        block_id=blk.id, line=ln, modality="must", soundness="sound"))
            # local update so a const assigned earlier in the block is visible
            for t in op.targets:
                if "const_value" in op.attrs and len(op.targets) == 1:
                    cur[t] = op.attrs["const_value"]
                else:
                    cur.pop(t, None)
    return out
