"""Redundant-branch opportunity pass (RFC-0002 §4 Tier 1).

An `if` whose condition is a literal constant is always taken one way — the
other arm is dead. Restricted to `if` (the `while True:` idiom is intentional)
and to literal `Constant` conditions, which makes it SOUND/MUST.
"""

from __future__ import annotations

import ast
from typing import List

from ..ir import FunctionGraph
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
    return out
