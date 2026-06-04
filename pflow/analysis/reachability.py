"""Unreachable-code opportunity pass (RFC-0002 §4 Tier 1).

A block with no path from the entry (e.g. code after an unconditional
return/raise/break/continue) is dead. Pure CFG reachability — structurally
sound on Python (no dynamic feature creates a hidden CFG entry to a dead
block), so this is one of the few SOUND/MUST findings.
"""

from __future__ import annotations

from typing import List

from ..ir import FunctionGraph
from .opportunities import Opportunity


def find_unreachable(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    bb = {b.id: b for b in graph.blocks}
    seen = {graph.entry}
    stack = [graph.entry]
    while stack:
        b = bb.get(stack.pop())
        if b is None:
            continue
        for s in (*b.succs, *b.except_succs):
            if s not in seen:
                seen.add(s)
                stack.append(s)

    out: List[Opportunity] = []
    for blk in graph.blocks:
        if blk.id in seen:
            continue
        # Point only at a real, removable user statement. A block whose only
        # sourced ops are SYNTHETIC (e.g. the `exit_with` cleanup pflow emits
        # for a `with`, or `enter_finally`) is not deletable code — flagging it
        # would be a false positive in this SOUND/MUST tier. The lock-guarded
        # `with self._lock: return X` idiom is the canonical trigger: its
        # __exit__ cleanup is unreachable once the body returns, but there is
        # nothing for a human to remove. Require a non-synthetic op.
        op = next((o for o in blk.ops
                   if o.source and not o.attrs.get("is_synthetic")), None)
        if op is None:
            continue  # empty/synthetic dead block — nothing to point at
        ln = op.source[0]
        out.append(Opportunity(
            pass_name="unreachable", kind="dead_code",
            title=f"unreachable code (bb{blk.id} cannot be reached from entry) — remove it",
            ref=f"{graph.qualname}:bb:{blk.id}", op_id=op.id, block_id=blk.id,
            line=ln, modality="must", soundness="sound"))
    return out
