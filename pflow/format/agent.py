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
                s = op.kind
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
        lines.append(f"  {'exits':<9}{bbs(g.exit_blocks)} ({len(g.exit_blocks)})")

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
    for d, n, ln in fan[:4]:
        ref = f"{pre}{g.qualname}:def:{d.name}@{ln}"
        if ref not in seen:
            seen.add(ref)
            targets.append(ref)
    for h in headers[:2]:
        ref = f"{pre}{g.qualname}:bb:{h}"
        if ref not in seen:
            seen.add(ref)
            targets.append(ref)
    if targets:
        lines.append("  targets →  " + "  ".join(targets))

    return "\n".join(lines)
