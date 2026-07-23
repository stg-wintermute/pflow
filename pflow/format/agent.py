"""Dense agent-friendly formatting for pflow.

Designed for low token usage while still being highly informative
for code review agents (especially flow-graph-reviewer style).
"""

from collections import Counter
from typing import List
from ..ir import FunctionGraph


def format_cfg_agent(g: FunctionGraph, include_dataflow: bool = True, include_dominance: bool = False) -> str:
    """Produce a compact, dense representation of the CFG suitable for agents."""
    lines: List[str] = []

    # Header
    lines.append(f"CFG {g.qualname} ({len(g.blocks)} blocks, {len(g.ops)} ops)")

    # Dataflow stats
    if g.defs:
        lines.append(f"  defs={len(g.defs)} uses={len(g.uses)}")

    # Defs that reach each block's entry (true reaching_in from dataflow).
    reaching_by_block = {}
    if include_dataflow and g.defs:
        for b in g.blocks:
            rin = b.attrs.get("reaching_in")
            if rin:
                reaching_by_block[b.id] = set(rin)

    # Dominators: prefer attached data (from analyze_*), otherwise compute if requested
    doms = g.attrs.get("dominators", {})
    if not doms and include_dominance:
        try:
            from ..analysis.dominance import compute_dominators
            doms = compute_dominators(g)
            g.attrs["dominators"] = doms
        except Exception:
            pass

    for b in g.blocks:
        # Very aggressive filtering for dense agent output
        if not b.ops:
            if b.succs and b.id != g.entry and not any(op.kind in ("return", "raise") for op in b.ops):
                continue
            if not b.succs and b.id != g.entry:
                continue

        # Hide pure trailing return blocks (implicit or dead successors of explicit return) with no lines
        if b.ops and all(op.kind == "return" for op in b.ops):
            if not b.attrs.get("line_range") and b.id != g.entry:
                if not reaching_by_block.get(b.id):
                    continue

        # In very dense mode, hide pure synthetic handler blocks unless they have
        # interesting incoming dataflow or are the only way to see cleanup.
        if b.attrs.get("is_exception_handler"):
            if include_dataflow and not reaching_by_block.get(b.id):
                if not b.attrs.get("possible_exception_handler"):
                    continue

        parts = [f"bb{b.id}"]

        line_range = b.attrs.get("line_range")
        if line_range:
            parts.append(f"lines={line_range[0]}-{line_range[1]}")

        if b.ops:
            op_strs = []
            for op in b.ops:
                s = f"{op.id}:{op.kind}"      # id first: every op addressable as :op:N
                if op.targets:
                    s += f"→{','.join(op.targets)}"
                if op.uses:
                    s += f"←{','.join(op.uses)}"
                op_strs.append(s)
            parts.append("ops=[" + " ".join(op_strs) + "]")
        else:
            parts.append("ops=[]")

        parts.append(f"succs={b.succs}")
        if b.except_succs:
            parts.append(f"except={b.except_succs}")
        if b.attrs.get("is_exception_handler"):
            parts.append("handler=True")
        if b.attrs.get("suggested_except_succs") and not b.except_succs:
            parts.append("except_corrected=True")

        if include_dataflow and reaching_by_block.get(b.id):
            reaching_names = set()
            for did in reaching_by_block[b.id]:
                for d in g.defs:
                    if d.id == did:
                        reaching_names.add(d.name)
                        break
            if reaching_names:
                parts.append(f"reaches_in={sorted(reaching_names)}")

        if doms and b.id in doms:
            strict_doms = sorted(doms[b.id] - {b.id})
            if strict_doms:
                parts.append(f"dom_by={strict_doms}")

        # Post-dominators: prefer attached, otherwise compute if requested
        pdoms = g.attrs.get("post_dominators", {})
        if not pdoms and include_dominance:
            try:
                from ..analysis.dominance import compute_post_dominators
                pdoms = compute_post_dominators(g)
                g.attrs["post_dominators"] = pdoms
            except Exception:
                pass

        if pdoms and b.id in pdoms:
            strict_pdoms = sorted(pdoms[b.id] - {b.id})
            if strict_pdoms:
                parts.append(f"pdom_by={strict_pdoms}")

        lines.append("  " + " | ".join(parts))

    return "\n".join(lines)


def format_report_agent(g: FunctionGraph) -> str:
    """Positional census of a function — the IR layer's on-ramp.

    Reports WHERE the structure is and WHAT connects to what — entry, exits,
    branch points (and which names they read), loop headers, the spine that
    post-dominates entry, and defs ranked by fan-out — then ends with a
    `targets →` list of refs to walk. It renders no verdicts: "13-way
    dispatch", "split this", "runs on every path" are interpretations the
    agent draws from these coordinates. Judgments live in `opportunities`.
    """
    lines: List[str] = []
    path = g.source_path or ""
    loc = path if path else g.qualname
    lines.append(f"{g.qualname}  ({loc}:{g.first_line})  "
                 f"{len(g.blocks)} bb · {len(g.ops)} op · {len(g.exit_blocks)} exits")
    if not g.blocks:
        return lines[0]

    doms = g.attrs.get("dominators")
    pdoms = g.attrs.get("post_dominators")
    if doms is None or pdoms is None:
        from ..analysis.dominance import compute_dominators, compute_post_dominators
        doms = doms if doms is not None else compute_dominators(g)
        pdoms = pdoms if pdoms is not None else compute_post_dominators(g)

    def bbs(ids):
        return " ".join(f"bb{i}" for i in ids)

    lines.append(f"  {'entry':<9}bb{g.entry}")

    if g.exit_blocks:
        # split by exit kind: with many exits (validators, dispatchers) the
        # question is always "which are the error paths vs the real returns"
        by_id = {b.id: b for b in g.blocks}
        rets, raises, falls = [], [], []
        for bid in g.exit_blocks:
            ops = by_id[bid].ops if bid in by_id else ()
            last = ops[-1] if ops else None
            (rets if last is not None and last.kind == "return" else
             raises if last is not None and last.kind == "raise" else
             falls).append(bid)
        parts = []
        if rets:
            parts.append("return: " + bbs(rets))
        if raises:
            tail = f" (+{len(raises) - 8})" if len(raises) > 8 else ""
            parts.append("raise: " + bbs(raises[:8]) + tail)
        if falls:
            parts.append("fall-off: " + bbs(falls))
        lines.append(f"  {'exits':<9}({len(g.exit_blocks)})  " + " · ".join(parts))

    branch_blocks = [b for b in g.blocks if any(o.kind == "branch" for o in b.ops)]
    if branch_blocks:
        read = Counter()
        for b in branch_blocks:
            for o in b.ops:
                if o.kind == "branch":
                    for nm in o.uses:
                        read[nm] += 1
        line = f"  {'branches':<9}{bbs(b.id for b in branch_blocks)} ({len(branch_blocks)})"
        if read:
            line += "   read: " + " ".join(f"{nm}×{k}" for nm, k in read.most_common(6))
        lines.append(line)

    headers = sorted({s for b in g.blocks for s in b.succs if s in doms.get(b.id, set())})
    if headers:
        lines.append(f"  {'headers':<9}{bbs(headers)}   (back-edge targets)")

    spine = sorted(pdoms.get(g.entry, set()) - {g.entry})
    if spine:
        tail = f" +{len(spine) - 10}" if len(spine) > 10 else ""
        lines.append(f"  {'spine':<9}{bbs(spine[:10])}{tail}   (pdom of entry)")

    # state footprint: the self.*/global surface this function touches — the
    # part of its behavior invisible to its signature. `self.m(...)` roots are
    # method calls, not attribute reads: exclude them from reads, and count
    # only DEEP call paths (`self.attr.m()`) as possible mutation of attr.
    called_roots = {(c.get("func") or "").split(".")[1]
                    for op in g.iter_ops() for c in op.attrs.get("calls", ())
                    if (c.get("func") or "").startswith("self.")}
    sreads = sorted({u.split(".")[1] for op in g.iter_ops() for u in op.uses
                     if u.startswith("self.") and u.count(".") >= 1}
                    - called_roots)
    swrites = sorted({t.split(".")[1] for op in g.iter_ops() for t in op.targets
                      if t.startswith("self.")})
    scalls = sorted({c["func"].split(".")[1]
                     for op in g.iter_ops() for c in op.attrs.get("calls", ())
                     if (c.get("func") or "").startswith("self.")
                     and c["func"].count(".") >= 2})
    if sreads or swrites or scalls:
        bits = []
        if swrites:
            bits.append("writes self." + ",".join(swrites[:6])
                        + (f" (+{len(swrites) - 6})" if len(swrites) > 6 else ""))
        if scalls:
            bits.append("mutates? self." + ",".join(scalls[:5])
                        + (f" (+{len(scalls) - 5})" if len(scalls) > 5 else ""))
        reads_only = [r for r in sreads if r not in swrites]
        if reads_only:
            bits.append("reads self." + ",".join(reads_only[:8])
                        + (f" (+{len(reads_only) - 8})" if len(reads_only) > 8 else ""))
        lines.append(f"  {'state':<9}" + " · ".join(bits))

    fan = []
    for d in g.defs:
        n = sum(1 for u in g.uses if d.id in u.reaching_defs)
        if n:
            fan.append((d, n, d.source[0] if d.source else g.first_line))
    fan.sort(key=lambda r: r[1], reverse=True)
    if fan:
        cells = " · ".join(f"{d.name}@{ln}×{n}" for d, n, ln in fan[:5])
        lines.append(f"  {'fan-out':<9}{cells}")

    pre = (path + ":") if path else ""
    targets, seen = [], set()
    for d, _, ln in fan[:4]:
        ref = f"{pre}{g.qualname}:def:{d.name}@{ln}"
        if ref not in seen:
            seen.add(ref)
            targets.append(ref)
    for h in headers[:2]:
        ref = f"{pre}{g.qualname}:bb:{h}"
        if ref not in seen:
            seen.add(ref)
            targets.append(ref)
    # value-returning exits: slicing backward from these is the canonical
    # "what does this function's result depend on"
    for bid in rets[:2] if g.exit_blocks else ():
        last = by_id[bid].ops[-1]
        if last.uses:
            ref = f"{pre}{g.qualname}:op:{last.id}"
            if ref not in seen:
                seen.add(ref)
                targets.append(ref)
    if targets:
        lines.append("  targets →  " + "  ".join(targets))

    return "\n".join(lines)


def _fn_size(g: FunctionGraph):
    """Cheap structural size for the program census: (blocks, branch blocks,
    exits, longest def→use span). No dominators — must stay fast over hundreds
    of functions."""
    br = sum(1 for b in g.blocks if any(o.kind == "branch" for o in b.ops))
    last_use = {}
    for u in g.uses:
        for d in u.reaching_defs:
            last_use[d] = max(last_use.get(d, 0), u.op_id)
    span = max((last_use[d.id] - d.op_id for d in g.defs if d.id in last_use),
               default=0)
    return len(g.blocks), br, len(g.exit_blocks), span


def format_program_report_agent(pg) -> str:
    """Whole-program positional census — the front door for 'review this repo'.

    Same contract as the per-function report: coordinates and connections, no
    verdicts. Aggregates the three interprocedural views (callgraph, state,
    per-function structure) into one orientation screen and ends with
    `targets →` — runnable file:qualname handles for the per-function IR
    commands (report/cfg/slice/walk). Judgments live in `opportunities`.
    """
    from ..analysis.interproc import program_callgraph, entrypoints, hubs, cycles, state_rows

    cg = program_callgraph(pg)
    n_edges = sum(len(v) for v in cg.edges.values())
    import os
    name = os.path.basename(pg.root.rstrip(os.sep)) if pg.out_of_tree else pg.display_root()
    lines = [f"PROGRAM census: {name}  ·  {len(pg.modules)} files · "
             f"{len(pg.functions)} fn · {n_edges} call edges · "
             f"{len(pg.errors)} parse error(s)"]
    if pg.out_of_tree:
        # rows below use in-root relpaths; this line carries the prefix once
        lines.append(f"  {'root':<9}{pg.root}")

    def disp(fq: str) -> str:
        if pg.out_of_tree:
            return fq
        relpath, qual = fq.split(":", 1)
        return f"{pg.display_path(relpath)}:{qual}"

    def runnable(fq: str) -> str:
        relpath, qual = fq.split(":", 1)
        return f"{pg.display_path(relpath)}:{qual}"

    hub_rows = [(fq, fi, fo) for fq, fi, fo in hubs(cg, 8) if fi or fo]
    if hub_rows:
        def hub_cell(fq, fi, fo):
            callers = cg.rev.get(fq, ())
            smeared = sum(1 for c in callers if cg.edge_is_soft(c, fq))
            mark = "~" if fi and smeared * 2 >= fi else ""
            return f"{fi}{mark}←{fo}→ {disp(fq)}"
        cells = " · ".join(hub_cell(fq, fi, fo) for fq, fi, fo in hub_rows[:6])
        note = ("   (~ = fan-in mostly name-resolution smear)"
                if "~←" in cells else "")
        lines.append(f"  {'hubs':<9}{cells}{note}")

    cy = cycles(cg)
    if cy:
        from ..analysis.interproc import cycle_is_smeared
        sharp = [c for c in cy if not cycle_is_smeared(cg, c)]
        smeared = [c for c in cy if cycle_is_smeared(cg, c)]

        def label(c, mark=""):
            names = [x.split(":")[-1] for x in c]
            return ("recursive: " + names[0] if len(c) == 1
                    else " ↔ ".join(names[:4])) + mark

        head = (f"({len(sharp)} sharp · {len(smeared)} smeared~)"
                if smeared else f"({len(cy)})")
        shown = [label(c) for c in sharp[:4]] + \
                [label(c, "~") for c in smeared[:2]]
        rest = len(cy) - len(shown)
        tail = f" (+{rest})" if rest > 0 else ""
        lines.append(f"  {'cycles':<9}{head}  " + " · ".join(shown) + tail)

    rows = state_rows(pg)
    flagged = [c for c in rows if c.flags]
    if rows:
        cells = " · ".join(f"{c.name} W{len(c.writers)}/R{len(c.readers)} "
                           f"[{','.join(c.flags)}]" for c in flagged[:3])
        line = f"  {'state':<9}{len(rows)} cells · {len(flagged)} flagged"
        if cells:
            line += "   " + cells
        lines.append(line)

    eps = entrypoints(cg)
    if eps:
        shown = " · ".join(e.split(":")[-1] for e in eps[:8])
        tail = f" (+{len(eps) - 8})" if len(eps) > 8 else ""
        lines.append(f"  {'entry':<9}({len(eps)} uncalled in-program)  {shown}{tail}")

    sized = sorted(((fq, *_fn_size(g)) for fq, g in pg.functions.items()),
                   key=lambda r: r[1], reverse=True)
    cap = len(sized) if len(sized) <= 20 else 12
    if sized:
        lines.append(f"  {'largest':<9}(top {cap} of {len(sized)} fn:  bb · branch · exit · span)")
        for fq, bb, br, ex, span in sized[:cap]:
            lines.append(f"    {disp(fq):<58} {bb:>3} · {br:>2} · {ex:>2} · {span:>3}")

    targets, seen = [], set()
    cap = 3 if pg.out_of_tree else 5           # absolute paths are long
    for fq, *_ in sized[:3]:
        t = runnable(fq)
        if t not in seen:
            seen.add(t)
            targets.append(t)
    for fq, _, _ in hub_rows[:2]:
        t = runnable(fq)
        if t not in seen:
            seen.add(t)
            targets.append(t)
    if targets:
        lines.append("  targets →  " + "  ".join(targets[:cap]))
    return "\n".join(lines)
