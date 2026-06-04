"""Basic dominance analysis for pflow (RFC direction).

This is a first-cut implementation using the standard iterative dataflow approach.
It is intentionally simple for now.
"""

from ..ir import FunctionGraph
from typing import Dict, Set


def compute_dominators(g: FunctionGraph) -> Dict[int, Set[int]]:
    """Return a mapping: block_id -> set of dominator block_ids (including self)."""
    if not g.blocks:
        return {}

    all_blocks = {b.id for b in g.blocks}
    dom: Dict[int, Set[int]] = {}

    entry = g.entry
    if entry not in dom:
        dom[entry] = {entry}

    for b in g.blocks:
        if b.id != entry:
            dom[b.id] = set(all_blocks)

    # No iteration cap: dom sets only shrink (intersection) over a finite
    # lattice, so the round-robin solver is guaranteed to terminate. A cap
    # would silently return a half-converged (wrong) result on graphs whose
    # block order is far from flow order (mirrors compute_post_dominators).
    changed = True
    while changed:
        changed = False
        for b in g.blocks:
            if b.id == entry:
                continue
            if not b.preds:
                new_d = {b.id}
            else:
                new_d = set(dom.get(b.preds[0], set()))
                for p in b.preds[1:]:
                    new_d &= dom.get(p, set())
                new_d.add(b.id)

            if new_d != dom[b.id]:
                dom[b.id] = new_d
                changed = True

    return dom


def compute_post_dominators(g: FunctionGraph) -> Dict[int, Set[int]]:
    """Return post-dominators (dominators in the reverse graph).

    In the reverse graph an edge b -> a exists iff a forward edge a -> b
    exists, so the *predecessors of b in the reverse graph* are exactly the
    forward successors of b (normal + exceptional). With multiple exits we
    treat them all as roots seeded with {self}; this is the conventional
    single-virtual-exit behaviour without materializing the virtual node.
    """
    if not g.blocks:
        return {}

    # reverse-graph predecessors of b == forward successors of b
    ids = {b.id for b in g.blocks}
    rev_preds: Dict[int, list] = {
        b.id: [s for s in (*b.succs, *b.except_succs) if s in ids]
        for b in g.blocks
    }

    exits = set(g.exit_blocks) or {g.blocks[-1].id}

    all_blocks = {b.id for b in g.blocks}
    pdom: Dict[int, Set[int]] = {}
    for b in g.blocks:
        pdom[b.id] = {b.id} if b.id in exits else set(all_blocks)

    changed = True
    while changed:
        changed = False
        for b in g.blocks:
            if b.id in exits:
                continue
            preds = rev_preds.get(b.id, [])
            if not preds:
                new_d = {b.id}
            else:
                new_d = set(all_blocks)
                for p in preds:
                    new_d &= pdom.get(p, set())
                new_d.add(b.id)
            if new_d != pdom[b.id]:
                pdom[b.id] = new_d
                changed = True

    return pdom