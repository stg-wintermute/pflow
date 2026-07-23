"""Redundant-branch opportunity pass (RFC-0002 §4 Tier 1).

Two detectors:

1. constant_condition — an `if` on a literal constant is always taken one
   way; the other arm is dead. Restricted to `if` (the `while True:` idiom is
   intentional) and literal `Constant` conditions. SOUND/MUST.

2. implied_condition — an `if C` where a DOMINATING branch tests the same
   `C` and this block sits inside one decided arm: the re-test re-asks an
   answered question. Sound only when (a) no definition of any name C reads
   lies on ANY path between the deciding arm and the re-test (checked over
   the paths-between region, cycles included — a loop that redefines and
   comes back rejects the claim), and (b) C is a pure name expression (no
   calls — impure; no attributes — aliasing). Otherwise emitted as
   HEURISTIC/MUST for the agent to confirm.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Set

from ..ir import FunctionGraph
from .dominance import compute_dominators
from .opportunities import Opportunity


def find_redundant_branches(graph: FunctionGraph) -> List[Opportunity]:
    out: List[Opportunity] = []
    for blk in graph.blocks:
        for op in blk.ops:
            if op.kind != "branch" or op.attrs.get("construct") != "if":
                continue
            cond = op.attrs.get("condition")
            if not cond:
                continue
            try:
                node = ast.parse(cond, mode="eval").body
            except SyntaxError:
                continue
            if not isinstance(node, ast.Constant):
                continue
            truthy = bool(node.value)
            ln = op.source[0] if op.source else None
            ref = f"{graph.qualname}:op:{op.id}"
            out.append(Opportunity(
                pass_name="redundant-branch", kind="constant_condition",
                title=f"condition `{cond}` is constant (always {truthy}) — "
                      f"the {'else' if truthy else 'then'} branch is dead",
                ref=ref, op_id=op.id, block_id=blk.id, line=ln,
                modality="must", soundness="sound"))
    out.extend(_implied_branches(graph))
    return out


def _condition_purity(cond: str) -> str:
    """'sound' for pure name/compare/bool expressions; 'heuristic' when the
    condition calls anything (impure) or dereferences attributes (aliasing —
    a mutation changes the value without any def appearing in the IR)."""
    try:
        tree = ast.parse(cond, mode="eval")
    except SyntaxError:
        return "heuristic"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Attribute, ast.Subscript,
                             ast.Await, ast.NamedExpr)):
            return "heuristic"
    return "sound"


def _paths_between(graph: FunctionGraph, start: int, end: int) -> Set[int]:
    """Blocks lying on SOME path start→end, cycles included (so a loop body
    that leaves `end` and comes back is part of the region)."""
    succs: Dict[int, tuple] = {b.id: tuple(b.succs) + tuple(b.except_succs)
                               for b in graph.blocks}
    preds: Dict[int, Set[int]] = {b.id: set() for b in graph.blocks}
    for b, ss in succs.items():
        for s in ss:
            if s in preds:
                preds[s].add(b)

    def bfs(seed: int, edges) -> Set[int]:
        seen = {seed}
        stack = [seed]
        while stack:
            for n in edges.get(stack.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return seen

    return bfs(start, succs) & bfs(end, preds)


def _norm_condition(cond: str):
    """(key, polarity): `not x` -> ('x', False); anything else -> (cond, True).
    Lets `if x` and `if not x` participate in the same implication group."""
    try:
        node = ast.parse(cond, mode="eval").body
    except SyntaxError:
        return cond, True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ast.unparse(node.operand), False
    return cond, True


def _implied_branches(graph: FunctionGraph) -> List[Opportunity]:
    branches = [(blk, op) for blk in graph.blocks for op in blk.ops
                if op.kind == "branch" and op.attrs.get("construct") == "if"
                and op.attrs.get("condition")]
    by_cond: Dict[str, list] = {}
    for blk, op in branches:
        key, pol = _norm_condition(op.attrs["condition"])
        by_cond.setdefault(key, []).append((blk, op, pol))
    if not any(len(v) > 1 for v in by_cond.values()):
        return []

    from .dominance import compute_post_dominators
    doms = graph.attrs.get("dominators") or compute_dominators(graph)
    pdoms = graph.attrs.get("post_dominators") or compute_post_dominators(graph)
    nonlocal_writes = graph.attrs.get("nonlocal_writes", frozenset())
    block = {b.id: b for b in graph.blocks}
    out: List[Opportunity] = []

    for key, sites in by_cond.items():
        if len(sites) < 2:
            continue
        for bx, ox, px in sites:
            cond = ox.attrs["condition"]
            if any(n in nonlocal_writes for n in ox.uses):
                continue        # a closure can rebind this behind our back
            for ba, oa, pa in sites:
                if ba.id == bx.id or ba.id not in doms.get(bx.id, set()):
                    continue
                arms = ba.succs
                if len(arms) < 2:
                    continue
                key_value = None
                for truth, arm in ((True, arms[0]), (False, arms[1])):
                    # An arm that POST-dominates the branch is the merge
                    # point, not a decided side: for `if X: body` with no
                    # else, succs[1] IS the merge and dominates everything
                    # after — treating it as "X was False" flagged every
                    # later re-test of X (all five seeker-dev sound findings
                    # were this false positive).
                    if arm in pdoms.get(ba.id, set()):
                        continue
                    if arm == bx.id or arm in doms.get(bx.id, set()):
                        # then-arm means the ancestor's WHOLE condition was
                        # true; the shared key's value follows its polarity.
                        key_value, arm_block = (truth == pa), arm
                        break
                if key_value is None:
                    continue                    # re-test sits after the merge
                decided = (key_value == px)     # value of the re-test's condition

                names = set(ox.uses)
                region = _paths_between(graph, arm_block, bx.id)
                between_ops = [o for rid in region for o in block[rid].ops]
                between_ops += [o for o in bx.ops if o.id < ox.id]
                redefined = any(
                    t in names or (("." in t) and t.split(".", 1)[0] in names)
                    for o in between_ops for t in o.targets)
                if redefined:
                    continue

                calls_between = any(o.kind == "call" or o.attrs.get("calls")
                                    for o in between_ops)
                soundness = _condition_purity(cond)
                if soundness != "sound" and calls_between:
                    # an attribute/call condition can be invalidated by ANY
                    # intervening call (other threads, reentrancy, mutation) —
                    # the wait()-then-recheck idiom lives here; stay silent.
                    continue
                if soundness == "sound" and calls_between and any(
                        op.attrs.get("kind") == "nested_def"
                        for op in graph.iter_ops()):
                    soundness = "heuristic"     # a closure could nonlocal-write

                la = oa.source[0] if oa.source else None
                lx = ox.source[0] if ox.source else None
                out.append(Opportunity(
                    pass_name="redundant-branch", kind="implied_condition",
                    title=f"condition `{cond}` was already decided {decided} at "
                          f"bb{ba.id}" + (f" (line {la})" if la else "") +
                          f" — the {'else' if decided else 'then'} arm here is dead",
                    ref=f"{graph.qualname}:op:{ox.id}", op_id=ox.id,
                    block_id=bx.id, line=lx,
                    modality="must", soundness=soundness,
                    detail=f"dominating guard bb{ba.id} pins `{cond}`; no def of "
                           f"{{{', '.join(sorted(names))}}} on any path between"))
                break                           # one deciding ancestor is enough
    return out
