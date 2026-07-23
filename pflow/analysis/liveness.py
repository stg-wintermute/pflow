"""Liveness analysis + dead-store opportunity pass (RFC-0002 §4 Tier 1).

Liveness is a backward may-analysis on the generic solver. Dead stores — an
assignment whose value is never read before it is overwritten or the function
exits — are the standout Tier-1 finding: a real gap (pyflakes F841 / pylint
W0612 only catch "never referenced at all", not "last write before return is
unread"). A store whose target is not live immediately after it is dead.

Soundness: "not live" is a MUST property (no path uses it). We tag it
HEURISTIC, not SOUND, because Python's dynamic tail (globals()/locals()/exec/
getattr) can read a name we can't see, and we restrict to simple local names
to stay honest.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from ..ir import FunctionGraph
from .solver import solve
from .opportunities import Opportunity


def compute_liveness(graph: FunctionGraph) -> Tuple[Dict[int, Set[str]], Dict[int, Set[str]]]:
    """Return (live_in, live_out): names live entering/leaving each block.

    Hand-rolled (not on the generic solver) because exceptional edges need a
    split transfer: a block's KILLS apply only to liveness flowing in from its
    NORMAL successors — on an exception path the block's assignments may never
    have executed, so the pre-block value can still be read by the handler.
    Merging both edge kinds killed uniformly and flagged the defensive-init
    idiom (`x = None; try: x = fetch() except: pass; use(x)`) as a dead store
    (found on obol's harbor reward parsing).
    """
    use: Dict[int, Set[str]] = {}
    defs: Dict[int, Set[str]] = {}
    for blk in graph.blocks:
        defined: Set[str] = set()
        used: Set[str] = set()
        for op in blk.ops:
            for u in op.uses:
                if u not in defined:
                    used.add(u)
            defined.update(op.targets)
        use[blk.id] = used
        defs[blk.id] = defined

    ids = [b.id for b in graph.blocks]
    normal = {b.id: tuple(s for s in b.succs if s in defs) for b in graph.blocks}
    exc = {b.id: tuple(s for s in b.except_succs if s in defs) for b in graph.blocks}
    live_in: Dict[int, Set[str]] = {i: set() for i in ids}
    live_out: Dict[int, Set[str]] = {i: set() for i in ids}

    changed = True
    while changed:
        changed = False
        for bid in reversed(ids):
            out_n: Set[str] = set().union(*(live_in[s] for s in normal[bid])) \
                if normal[bid] else set()
            out_e: Set[str] = set().union(*(live_in[s] for s in exc[bid])) \
                if exc[bid] else set()
            new_out = out_n | out_e
            new_in = use[bid] | (out_n - defs[bid]) | out_e
            if new_in != live_in[bid] or new_out != live_out[bid]:
                live_in[bid], live_out[bid] = new_in, new_out
                changed = True
    return live_in, live_out


def _is_local_simple(name: str) -> bool:
    # only flag plain local names; skip attributes/subscripts (may escape) and
    # underscore conventions (intentional throwaways).
    return ("." not in name and "[" not in name
            and not name.startswith("_"))


def find_dead_stores(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    live_in, live_out = compute_liveness(graph)
    # nonlocal/global stores write through to an outer scope — never dead here
    global_decls = (set(graph.attrs.get("global_decls", set()))
                    | set(graph.attrs.get("nonlocal_decls", set())))
    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}

    out: List[Opportunity] = []
    for blk in graph.blocks:
        live: Set[str] = set(live_out.get(blk.id, set()))
        # names the handler(s) can still read: within-block kills don't apply
        # on the exception path (the killing op may never run)
        exc_keep: Set[str] = set()
        for s in blk.except_succs:
            exc_keep |= live_in.get(s, set())
        for op in reversed(blk.ops):
            is_params = op.attrs.get("kind") == "args"
            if not is_params:
                has_call = bool(op.attrs.get("calls"))
                for t in op.targets:
                    # a branch that binds AND reads the name (match-case
                    # capture consumed by its own guard) is self-consuming
                    if op.kind == "branch" and t in op.uses:
                        continue
                    if (_is_local_simple(t) and t not in global_decls
                            and t not in live and t not in exc_keep):
                        ln = op_line.get(op.id)
                        ref = f"{graph.qualname}:def:{t}@{ln}" if ln else f"{graph.qualname}:op:{op.id}"
                        if has_call:
                            out.append(Opportunity(
                                pass_name="dead-store", kind="dead_store_call",
                                title=f"`{t}` is assigned but never read; RHS calls "
                                      f"({op.attrs['calls'][0].get('func')}) — keep the call or drop the binding",
                                ref=ref, op_id=op.id, block_id=blk.id, line=ln,
                                modality="must", soundness="heuristic"))
                        else:
                            out.append(Opportunity(
                                pass_name="dead-store", kind="dead_store",
                                title=f"`{t}` is assigned but never read before exit/overwrite — remove it",
                                ref=ref, op_id=op.id, block_id=blk.id, line=ln,
                                modality="must", soundness="heuristic"))
            live = (live - set(op.targets)) | set(op.uses)
    return out
