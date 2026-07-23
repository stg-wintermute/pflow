"""Flow-complexity metrics + hotspot opportunities (RFC-0002 §4 Tier 2).

Metrics that locate and quantify where simplification helps, computed from the
CFG + dominators + def-use pflow already has (flow-complexity-metrics.miner):
  - cyclomatic        decision count + 1 (McCabe)
  - cognitive         nesting-weighted branch count (SonarSource proxy)
  - max_nesting       deepest branch nesting (dominator-based)
  - depdegree         total data dependencies (Beck-Diehl) = sum of reaching defs
  - max_live_span     longest def->last-use distance (state the reader must hold)

Note: McCabe *essential* complexity (unstructuredness) is omitted — Python has
no goto, so source-built CFGs are essentially always reducible (essential ~1);
cognitive complexity carries the "tangled" signal here.
"""

from __future__ import annotations

from typing import Dict, List

from ..ir import FunctionGraph
from .dominance import compute_dominators, compute_post_dominators
from .control_dependence import _ipdom
from .opportunities import Opportunity

# Thresholds. cognitive raised 15->25: even with accurate nesting, intrinsically
# branchy code (parsers/dispatch/API clients) legitimately scores high, so the
# old 15 was noisy. max_live_span (data density) added — the cleanest
# "decomposable" signal (it separates state-juggling from mere branch width).
THRESHOLDS = {"cyclomatic": 12, "cognitive": 25, "max_nesting": 4, "max_live_span": 35}


def compute_metrics(graph: FunctionGraph) -> Dict[str, int]:
    cached = graph.attrs.get("_metrics")
    if cached is not None:
        return cached
    blocks = graph.blocks
    n = len(blocks)
    bb = {b.id: b for b in blocks}
    out_edges = sum(len(b.succs) + len(b.except_succs) for b in blocks)
    # Cyclomatic counts SOURCE decisions: normal-successor fan-out (if/while/
    # for/match branches) plus one per exception handler. It must NOT count
    # pflow's conservative per-block may-throw edges, which otherwise inflate
    # it massively (a branchless `with` reported 8; wait_for_ssh 12 vs ~4).
    # Decision-count form, always >= 1.
    n_handlers = sum(1 for op in graph.iter_ops() if op.kind == "enter_except")
    cyclomatic = sum(max(0, len(b.succs) - 1) for b in blocks) + n_handlers + 1

    doms = graph.attrs.get("dominators") or compute_dominators(graph)
    pdoms = graph.attrs.get("post_dominators") or compute_post_dominators(graph)
    branch_blocks = {b.id for b in blocks if any(o.kind == "branch" for o in b.ops)}
    # each branch's merge point (immediate post-dominator)
    merge = {x: _ipdom(pdoms, x) for x in branch_blocks}

    def nesting(bid: int) -> int:
        # X lexically CONTAINS bid only if bid is inside X's TRUE/body branch
        # (X's first successor dominates bid) and has not passed X's merge.
        # Counting the true-branch only keeps real nesting (loops, then-body
        # ifs) while flattening elif/else-if chains and match dispatch, which
        # are nested in the CFG but conceptually flat (the dispatch artifact).
        d = doms.get(bid, set())
        count = 0
        for x in branch_blocks:
            succs = bb[x].succs
            then_succ = succs[0] if succs else None
            if then_succ is None or then_succ not in d:
                continue            # bid not in X's true branch -> not nested in X
            m = merge.get(x)
            if m is None or m not in d:
                count += 1
        return count

    max_nesting = max((nesting(b.id) for b in blocks), default=0)
    cognitive = sum(1 + nesting(bid) for bid in branch_blocks)
    depdegree = sum(len(u.reaching_defs) for u in graph.uses)

    max_span = 0
    for d in graph.defs:
        use_ops = [u.op_id for u in graph.uses if d.id in u.reaching_defs]
        if use_ops:
            max_span = max(max_span, max(use_ops) - d.op_id)

    graph.attrs["_metrics"] = {
        "blocks": n, "control_edges": out_edges, "branches": len(branch_blocks),
        "cyclomatic": cyclomatic, "cognitive": cognitive, "max_nesting": max_nesting,
        "depdegree": depdegree, "max_live_span": max_span,
        "defs": len(graph.defs), "uses": len(graph.uses), "exits": len(graph.exit_blocks),
    }
    return graph.attrs["_metrics"]


def program_percentiles(functions) -> Dict[str, "callable"]:
    """{metric: value -> percentile} over every function in the program.
    Gives complexity findings a relative footing: `cyclomatic 16` means little
    in the abstract, `p97 in this program` is a defensible outlier claim."""
    import bisect
    dists: Dict[str, List[int]] = {k: [] for k in THRESHOLDS}
    for g in functions.values():
        m = compute_metrics(g)
        for k in dists:
            dists[k].append(m[k])
    for k in dists:
        dists[k].sort()

    def make(k):
        vals = dists[k]
        def pct(v: int) -> int:
            if not vals:
                return 0
            return round(100 * bisect.bisect_left(vals, v) / len(vals))
        return pct
    return {k: make(k) for k in dists}


def find_complexity_hotspots(graph: FunctionGraph) -> List[Opportunity]:
    """ONE finding per function, however many thresholds it crosses. The four
    measures describe the same structural mass; emitting them separately
    triple-counted every hotspot and drowned the merged ranking in near-
    duplicate lines (the main 'excessive' complaint from dogfooding agents)."""
    m = compute_metrics(graph)
    over = [(k, m[k], thr) for k, thr in THRESHOLDS.items() if m[k] > thr]
    if not over:
        return []
    short = {"cyclomatic": "cyclomatic", "cognitive": "cognitive",
             "max_nesting": "nesting", "max_live_span": "live-span"}
    measures = " · ".join(f"{short[k]} {v} (>{thr})" for k, v, thr in over)
    # The branch/value split is the actionable part: branch mass alone is often
    # intrinsic (dispatch, parsers); long live spans are the decomposition signal.
    span_over = any(k == "max_live_span" for k, _, _ in over)
    branch_over = any(k != "max_live_span" for k, _, _ in over)
    if span_over and branch_over:
        reading = "branch-heavy and value-heavy — strongest split signal"
    elif span_over:
        reading = "value-heavy (long live spans); branching is within bounds"
    else:
        reading = "branch-heavy; may be intrinsic (dispatch/parser) — check live-span before splitting"
    ctx = (f"cyclomatic={m['cyclomatic']} cognitive={m['cognitive']} "
           f"nesting={m['max_nesting']} depdegree={m['depdegree']} "
           f"live_span={m['max_live_span']}")
    return [Opportunity(
        pass_name="complexity", kind="hotspot",
        title=f"{measures} — {reading}",
        ref=f"{graph.qualname}:bb:{graph.entry}", op_id=-1,
        block_id=graph.entry, line=graph.first_line,
        modality="may", soundness="heuristic", detail=ctx)]
