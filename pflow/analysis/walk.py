"""Graph traversal from a ref — the primary "focus attention" primitive.

Per RFC §15.4 this is "one of the highest-leverage ways a flow-graph-reviewer
will use the tool": start at a ref (block, op, def, or use), walk control
and/or data edges outward under a direction + edge filter, and render the
result as an indented tree with per-hop location, reaching-defs, and region
context — so an agent can see a value's neighborhood without reading the
whole function.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from ..ir.graph import FunctionGraph
from ..ir.refs import resolve_node


@dataclass
class Step:
    depth: int
    kind: str              # 'block' | 'op' | 'def' | 'use'
    id: int
    label: str             # stable token: bbN / opN / def:name@line / use:name
    via: str               # 'start' | 'control' | 'except' | 'data'
    parent: int = -1       # index into the steps list, -1 for the root
    loc: str = ""          # "(bb2,op4)"
    note: str = ""         # "reaching: [state@91]" / "in finally region"
    detail: str = ""       # callee name for calls, etc.
    frontier: bool = False  # depth-limited: has unexplored neighbours (more graph this way)


def walk(
    graph: FunctionGraph,
    start,
    direction: str = "forward",
    edges: str = "both",
    max_depth: int = 3,
    kinds: Optional[Set[str]] = None,
) -> List[Step]:
    if not graph.blocks:
        return []

    resolved = _resolve_start(graph, start)
    if resolved is None:
        raise ValueError(f"could not resolve start ref: {start!r}")
    start_type, start_id = resolved

    visited: Set[Tuple[str, int]] = {(start_type, start_id)}
    steps: List[Step] = []
    # queue items: (type, id, depth, via, parent_index)
    q: deque = deque([(start_type, start_id, 0, "start", -1)])

    while q:
        ntype, nid, depth, via, parent = q.popleft()
        idx = len(steps)
        step = _make_step(graph, ntype, nid, depth, via, parent)
        steps.append(step)
        neighbors = _neighbors(graph, ntype, nid, direction, edges, kinds)
        if depth >= max_depth:
            # Don't expand, but record that the walk stopped here with graph
            # left unexplored — these become the `targets →` to continue from.
            if neighbors:
                step.frontier = True
            continue
        for ntyp, nidx, via_edge in neighbors:
            key = (ntyp, nidx)
            if key not in visited:
                visited.add(key)
                q.append((ntyp, nidx, depth + 1, via_edge, idx))

    return steps


def _resolve_start(graph, start):
    if isinstance(start, int):
        return ("block", start) if any(b.id == start for b in graph.blocks) else None
    return resolve_node(graph, str(start))


# -- node description ----------------------------------------------------

def _make_step(graph, ntype, nid, depth, via, parent) -> Step:
    label, loc, note, detail = _describe(graph, ntype, nid)
    return Step(depth=depth, kind=ntype, id=nid, label=label, via=via,
                parent=parent, loc=loc, note=note, detail=detail)


def _describe(graph, ntype, nid):
    """Return (label, loc, note, detail) for a node."""
    if ntype == "block":
        blk = graph.get_block(nid)
        kinds = ",".join(dict.fromkeys(o.kind for o in blk.ops)) or "empty"
        return f"bb{nid}", "", _region_note(blk), kinds

    if ntype == "op":
        op = _find_op(graph, nid)
        if op is None:
            return f"op{nid}", "", "", ""
        bb = _op_block_id(graph, nid)
        loc = f"(bb{bb},op{nid})"
        detail = ""
        if op.kind == "call":
            calls = op.attrs.get("calls", ())
            detail = calls[0]["func"] if calls and calls[0].get("func") else ""
        label = f"op{nid}:{op.kind}"
        return label, loc, _region_note(graph.get_block(bb)) if bb is not None else "", detail

    if ntype == "def":
        d = next((x for x in graph.defs if x.id == nid), None)
        if d is None:
            return f"def{nid}", "", "", ""
        ln = f"@{d.source[0]}" if d.source else ""
        return f"def:{d.name}{ln}", f"(bb{d.block_id},op{d.op_id})", \
               _region_note(graph.get_block(d.block_id)), ""

    if ntype == "use":
        u = next((x for x in graph.uses if x.id == nid), None)
        if u is None:
            return f"use{nid}", "", "", ""
        note = _reaching_note(graph, u)
        region = _region_note(graph.get_block(u.block_id))
        if region:
            note = f"{note}  {region}" if note else region
        return f"use:{u.name}", f"(bb{u.block_id},op{u.op_id})", note, ""

    return str(nid), "", "", ""


def _reaching_note(graph, use) -> str:
    if not use.reaching_defs:
        return ""
    by_id = {d.id: d for d in graph.defs}
    parts = []
    for did in use.reaching_defs:
        d = by_id.get(did)
        if d:
            ln = f"@{d.source[0]}" if d.source else ""
            parts.append(f"{d.name}{ln}")
    return "reaching: [" + ", ".join(parts) + "]" if parts else ""


def _region_note(blk) -> str:
    if blk is None:
        return ""
    if blk.attrs.get("is_exception_handler"):
        return "in handler"
    if blk.attrs.get("is_finally_region"):
        # with-cleanup blocks carry an exit_with op; finally blocks an enter_finally
        if any(o.kind == "exit_with" for o in blk.ops):
            return "in with-cleanup"
        return "in finally region"
    return ""


# -- neighbours (direction- and edge-aware) ------------------------------

def _neighbors(graph, ntype, nid, direction, edges, kinds):
    out: List[Tuple[str, int, str]] = []
    fwd = direction in ("forward", "both")
    bwd = direction in ("backward", "both")

    if edges in ("control", "both") and ntype == "block":
        blk = graph.get_block(nid)
        if fwd:
            for s in blk.succs:
                out.append(("block", s, "control"))
            for s in blk.except_succs:
                out.append(("block", s, "except"))
        if bwd:
            for p in blk.preds:
                out.append(("block", p, "control"))

    if edges in ("data", "both"):
        # Control dependence (PDG): backward from any node, include the
        # branch(es) that decide whether its block executes — so a slice
        # contains the controlling predicates, not just data ancestors.
        if bwd:
            blk_id = _node_block_id(graph, ntype, nid)
            if blk_id is not None:
                for c in _control_deps(graph).get(blk_id, ()):
                    cblk = graph.get_block(c)
                    bop = next((o.id for o in cblk.ops if o.kind == "branch"), None)
                    if bop is not None and bop != nid:
                        out.append(("op", bop, "ctrl-dep"))

        if ntype == "def":
            d = next((x for x in graph.defs if x.id == nid), None)
            if d is not None:
                if fwd:
                    for u in graph.uses:
                        if nid in u.reaching_defs:
                            out.append(("use", u.id, "data"))
                if bwd:
                    out.append(("op", d.op_id, "data"))
        elif ntype == "use":
            u = next((x for x in graph.uses if x.id == nid), None)
            if u is not None:
                if bwd:
                    for did in u.reaching_defs:
                        out.append(("def", did, "data"))
                if fwd:
                    out.append(("op", u.op_id, "data"))
        elif ntype == "op":
            if fwd:
                for d in graph.defs:
                    if d.op_id == nid:
                        out.append(("def", d.id, "data"))
            if bwd:
                for u in graph.uses:
                    if u.op_id == nid:
                        out.append(("use", u.id, "data"))

    if kinds:
        filtered = []
        for ntyp, nidx, via in out:
            if ntyp == "op":
                op = _find_op(graph, nidx)
                if op is None or op.kind not in kinds:
                    continue
            filtered.append((ntyp, nidx, via))
        out = filtered

    return out


def _find_op(graph, op_id):
    for op in graph.iter_ops():
        if op.id == op_id:
            return op
    return None


def _node_block_id(graph, ntype, nid):
    if ntype == "block":
        return nid
    if ntype == "def":
        d = next((x for x in graph.defs if x.id == nid), None)
        return d.block_id if d else None
    if ntype == "use":
        u = next((x for x in graph.uses if x.id == nid), None)
        return u.block_id if u else None
    if ntype == "op":
        return _op_block_id(graph, nid)
    return None


def _control_deps(graph):
    cd = graph.attrs.get("control_deps")
    if cd is None:
        from .control_dependence import compute_control_dependence
        cd = compute_control_dependence(graph)
        graph.attrs["control_deps"] = cd
    return cd


def _op_block_id(graph, op_id):
    for b in graph.blocks:
        if any(o.id == op_id for o in b.ops):
            return b.id
    return None


# -- rendering -----------------------------------------------------------

_EDGE_WORD = {"data": "->", "control": "->", "except": "~>",
              "ctrl-dep": "c>", "start": ""}

_ABBR = {"block": "bb", "op": "op", "def": "def", "use": "use"}


def _walk_targets(steps, children, graph) -> List[str]:
    """Refs to continue traversal from — positions, not advice. A target is a
    leaf where the walk hit its depth limit (more graph this way) or a value
    that crosses a call boundary (an unexpanded call op). Fully-resolved leaves
    (params, literals) are terminals, not targets, and are omitted."""
    prefix = (graph.source_path + ":") if getattr(graph, "source_path", None) else ""
    out: List[str] = []
    seen: Set[str] = set()
    for i, s in enumerate(steps):
        is_leaf = not children.get(i)
        crosses_call = s.kind == "op" and bool(s.detail)
        if not (s.frontier or (is_leaf and crosses_call)):
            continue
        abbr = _ABBR.get(s.kind)
        if abbr is None:
            continue
        # Prefer the RFC-canonical name@line tail for defs (matches `report`);
        # ops/uses/blocks use the unambiguous numeric id.
        tail = s.label if s.kind == "def" and s.label.startswith("def:") else f"{abbr}:{s.id}"
        ref = f"{prefix}{graph.qualname}:{tail}"
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
        if len(out) >= 6:
            break
    return out


def format_walk(steps, dense=True, graph: Optional[FunctionGraph] = None) -> str:
    """Render the walk as an indented tree (RFC §15.4 shape)."""
    if not steps:
        return "(no steps)"

    children = defaultdict(list)
    for i, s in enumerate(steps):
        if s.parent >= 0:
            children[s.parent].append(i)

    lines: List[str] = []
    if graph is not None:
        root = steps[0]
        lines.append(f"[{graph.qualname}:{root.label}]")

    def emit(i: int, indent: int, is_root: bool) -> None:
        s = steps[i]
        arrow = "" if is_root else _EDGE_WORD.get(s.via, "->") + " "
        # edge-kind prefix from node kind (use:/def:/call:/branch:/bb)
        head = s.label
        if s.kind == "op" and s.detail:
            head = head + f" {s.detail}"
        pad = "  " * indent
        bits = [p for p in (s.loc, s.note) if p]
        tail = ("   " + "  ".join(bits)) if bits else ""
        if graph is not None:
            lines.append(f"{pad}{arrow}{head}{tail}")
        else:  # legacy dense single-line form (no header), still tree-indented
            lines.append(f"{pad}{arrow}{head}{tail}".rstrip())
        for c in children[i]:
            emit(c, indent + 1, False)

    # roots = steps with parent -1 (normally just steps[0])
    start_indent = 1 if graph is not None else 0
    for i, s in enumerate(steps):
        if s.parent < 0:
            emit(i, start_indent, is_root=(graph is None))

    if graph is not None:
        targets = _walk_targets(steps, children, graph)
        if targets:
            lines.append("targets → " + "  ".join(targets))

    return "\n".join(lines)
