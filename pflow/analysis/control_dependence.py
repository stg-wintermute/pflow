"""Control dependence (the missing PDG edge), RFC-0002 §3.2.

A node Y is control-dependent on a branch X when X decides whether Y
executes: there is a CFG edge X->Z with Y post-dominating Z but Y not
post-dominating X. Computed from the post-dominator tree pflow already
has (Ferrante-Ottenstein-Warren 1987). Unioned with def-use, this turns
pflow's data graph into a Program Dependence Graph, so a backward slice
includes the predicates that select a value — not just its data ancestors.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

from ..ir import FunctionGraph
from .dominance import compute_post_dominators


def _ipdom(pdom: Dict[int, Set[int]], n: int) -> Optional[int]:
    """Immediate post-dominator: the closest strict post-dominator.

    Among n's strict post-dominators (which form a chain to the exit),
    the immediate one has the largest post-dominator set."""
    strict = pdom.get(n, set()) - {n}
    if not strict:
        return None
    return max(strict, key=lambda m: len(pdom.get(m, set())))


def compute_control_dependence(g: FunctionGraph) -> Dict[int, Set[int]]:
    """Return {block_id: set of branch-block ids it is control-dependent on}."""
    pdom = g.attrs.get("post_dominators") or compute_post_dominators(g)
    cd: Dict[int, Set[int]] = {b.id: set() for b in g.blocks}
    if not pdom:
        return cd
    n = len(g.blocks)

    for a in g.blocks:
        ia = _ipdom(pdom, a.id)
        for b in (*a.succs, *a.except_succs):
            if b not in cd:
                continue
            if b in pdom.get(a.id, set()):
                continue  # b post-dominates a -> edge is not control-deciding
            # Walk up the post-dom tree from b to ipdom(a) (exclusive),
            # marking each node control-dependent on a.
            y: Optional[int] = b
            guard = 0
            while y is not None and y != ia and guard <= n:
                cd[y].add(a.id)
                ny = _ipdom(pdom, y)
                if ny == y:
                    break
                y = ny
                guard += 1
    return cd
