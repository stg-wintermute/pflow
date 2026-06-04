"""Definite-assignment analysis + use-before-def pass (RFC-0002 §4 Tier 1).

Forward MUST analysis on the generic solver: a name is *definitely assigned*
at a point iff it is assigned on every path reaching it (meet = intersection).
A use of a local name that is not definitely assigned is a use-before-def /
possibly-unbound — the UnboundLocalError class, including the read-in-`finally`
case that no mainstream Python tool solves (pyflakes #236, mypy #17387),
because pflow models the exception edges into the finally region.

Only names that are assigned somewhere in the function (real locals) are
considered — a use of a never-assigned name is a global/builtin/free var, not
use-before-def. Attributes/subscripts and `_` conventions are skipped.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from ..ir import FunctionGraph
from .solver import solve
from .opportunities import Opportunity


def _params(graph: FunctionGraph) -> Set[str]:
    if not graph.blocks:
        return set()
    out: Set[str] = set()
    for op in graph.blocks[0].ops:
        if op.attrs.get("kind") == "args":
            out.update(op.targets)
    return out


def compute_definite_assignment(graph: FunctionGraph) -> Tuple[Dict[int, Set[str]], Set[str], Set[str]]:
    """Return (definite_in per block, universe of local names, params)."""
    universe: Set[str] = set()
    assigned_in: Dict[int, Set[str]] = {}
    for blk in graph.blocks:
        s: Set[str] = set()
        for op in blk.ops:
            s.update(op.targets)
        assigned_in[blk.id] = s
        universe |= s
    params = _params(graph)

    def transfer(blk, in_set: Set[str]) -> Set[str]:
        return in_set | assigned_in[blk.id]

    def meet(vals):
        it = iter(vals)
        acc = set(next(it))
        for v in it:
            acc &= v
        return acc

    definite_in, _ = solve(
        graph, "forward",
        init=lambda: set(universe),       # top for a MUST/intersection analysis
        boundary=lambda: set(params),     # params are bound on entry
        meet=meet, transfer=transfer,
    )
    return definite_in, universe, params


def _is_simple_local(name: str) -> bool:
    return "." not in name and "[" not in name and not name.startswith("_")


def find_use_before_def(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    definite_in, universe, params = compute_definite_assignment(graph)
    global_decls = graph.attrs.get("global_decls", set())
    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}
    # per (op_id, name) reaching defs, to tell "never assigned" from "not on all paths"
    reaching = {(u.op_id, u.name): u.reaching_defs for u in graph.uses}

    out: List[Opportunity] = []
    for blk in graph.blocks:
        assigned = set(definite_in.get(blk.id, set()))
        for op in blk.ops:
            for u in op.uses:
                if (u in universe and u not in params and u not in global_decls
                        and _is_simple_local(u) and u not in assigned):
                    ln = op_line.get(op.id)
                    ref = f"{graph.qualname}:use:{u}@{ln}" if ln else f"{graph.qualname}:op:{op.id}"
                    if not reaching.get((op.id, u)):
                        out.append(Opportunity(
                            pass_name="use-before-def", kind="unbound",
                            title=f"`{u}` is used before it is ever assigned (UnboundLocalError)",
                            ref=ref, op_id=op.id, block_id=blk.id, line=ln,
                            modality="must", soundness="heuristic"))
                    else:
                        out.append(Opportunity(
                            pass_name="use-before-def", kind="maybe_unbound",
                            title=f"`{u}` may be unbound here — assigned on some but not all "
                                  f"paths reaching this use",
                            ref=ref, op_id=op.id, block_id=blk.id, line=ln,
                            modality="may", soundness="heuristic"))
            assigned.update(op.targets)
    return out
