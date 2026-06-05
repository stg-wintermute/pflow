"""Enumerate control-flow paths between two refs (DFS, depth-limited)."""

from __future__ import annotations

from typing import List

from ..ir import FunctionGraph
from ..ir.refs import resolve_node


def _to_block_id(g: FunctionGraph, ref) -> int | None:
    node = resolve_node(g, ref) if not isinstance(ref, int) else ("block", ref)
    if node is None:
        return None
    ntype, nid = node
    if ntype == "block":
        return nid
    # Map op/def/use refs onto the block they live in.
    if ntype == "op":
        for b in g.blocks:
            if any(o.id == nid for o in b.ops):
                return b.id
    if ntype == "def":
        d = next((d for d in g.defs if d.id == nid), None)
        return d.block_id if d else None
    if ntype == "use":
        u = next((u for u in g.uses if u.id == nid), None)
        return u.block_id if u else None
    return None


def find_paths(g: FunctionGraph, start, goal, max_depth: int = 8,
               include_except: bool = True) -> List[List[int]]:
    """Return simple control paths (lists of block ids) from start to goal."""
    start_id = _to_block_id(g, start)
    goal_id = _to_block_id(g, goal)
    if start_id is None or goal_id is None:
        return []

    paths: List[List[int]] = []
    seen: set = set()
    stack = [(start_id, [start_id])]
    while stack:
        node, path = stack.pop()
        if node == goal_id:
            key = tuple(path)
            if key not in seen:  # collapse normal/except edges to the same blocks
                seen.add(key)
                paths.append(path)
            continue
        if len(path) > max_depth:
            continue
        blk = g.get_block(node)
        succs = list(blk.succs) + (list(blk.except_succs) if include_except else [])
        for succ in dict.fromkeys(succs):
            if succ not in path:  # simple paths only (no cycles)
                stack.append((succ, path + [succ]))

    return paths[:20]
