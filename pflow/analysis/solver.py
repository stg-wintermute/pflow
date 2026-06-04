"""Generic monotone-dataflow solver (RFC-0002 §3.1, the structural keystone).

One worklist driver parameterized by a lattice + transfer function, so every
flow analysis (reaching-defs, liveness, definite-assignment, constant-prop, ...)
is a ~small client instead of a bespoke fixpoint. Replaces the hand-rolled
loops that used to live in each pass.

    forward:  in[b]  = meet(out[p] for p in preds(b)), boundary at entry
              out[b] = transfer(b, in[b])
    backward: out[b] = meet(in[s] for s in succs(b)), boundary at exits
              in[b]  = transfer(b, out[b])

`init()` is the bottom fact for interior nodes; `boundary()` the boundary fact;
`meet(values)` combines (union for may-analyses, intersection for must);
`transfer(block, fact) -> fact`; `eq` compares facts (defaults to ==).
Returns (in_facts, out_facts), each {block_id: fact}.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Dict, Tuple


def solve(graph, direction: str, init: Callable, boundary: Callable,
          meet: Callable, transfer: Callable, eq: Callable | None = None
          ) -> Tuple[Dict[int, object], Dict[int, object]]:
    eq = eq or (lambda x, y: x == y)
    if not graph.blocks:
        return {}, {}
    forward = direction == "forward"
    if direction not in ("forward", "backward"):
        raise ValueError(f"direction must be forward|backward, got {direction!r}")

    bb = {b.id: b for b in graph.blocks}
    ids = [b.id for b in graph.blocks]
    succ = {i: list(dict.fromkeys((*bb[i].succs, *bb[i].except_succs))) for i in ids}
    pred = {i: list(bb[i].preds) for i in ids}

    if forward:
        upstream, downstream, boundary_ids = pred, succ, {graph.entry}
    else:
        upstream, downstream = succ, pred
        boundary_ids = set(graph.exit_blocks) or {ids[-1]}

    # `combined` = the upstream-meet side (in for forward, out for backward);
    # `derived` = transfer(combined) = the side that flows to neighbours.
    combined = {i: (boundary() if i in boundary_ids else init()) for i in ids}
    derived = {i: init() for i in ids}

    work = deque(ids)
    inq = set(ids)
    cap = len(ids) * len(ids) + 4 * len(ids) + 100  # convergence backstop
    steps = 0
    while work and steps < cap:
        steps += 1
        i = work.popleft()
        inq.discard(i)
        vals = [derived[u] for u in upstream[i]]
        if i in boundary_ids:
            vals.append(boundary())
        new_combined = meet(vals) if vals else (
            boundary() if i in boundary_ids else init())
        new_derived = transfer(bb[i], new_combined)
        if not eq(new_combined, combined[i]) or not eq(new_derived, derived[i]):
            combined[i] = new_combined
            derived[i] = new_derived
            for d in downstream[i]:
                if d not in inq:
                    work.append(d)
                    inq.add(d)

    if forward:
        return combined, derived          # (in, out)
    return derived, combined              # (in = transfer(out), out)
