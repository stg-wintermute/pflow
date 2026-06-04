"""Intra-file (conservative) call graph (v1).

We look at every `call`/`other` op's uses and link a caller to any
in-file function whose name appears. Matching is by full name or by the
trailing component of a dotted attribute path (e.g. `self.step` -> a
method named `step`). This is deliberately approximate per RFC §5.6.
"""

from __future__ import annotations

from typing import Dict, List, Set

from ..ir import FunctionGraph


def build_callgraph(graphs: Dict[str, FunctionGraph]) -> Dict[str, List[str]]:
    # Index: simple/last name -> set of qualnames defining it.
    by_last: Dict[str, Set[str]] = {}
    for qn in graphs:
        last = qn.split(".")[-1]
        by_last.setdefault(last, set()).add(qn)
        by_last.setdefault(qn, set()).add(qn)

    # A call's callee shows up in op.uses regardless of the op's kind
    # (e.g. `a = helper(n)` is an `assign`, `return f(x)` is a `return`),
    # so we scan every op's uses. This is a conservative may-reference graph.
    calls: Dict[str, Set[str]] = {qn: set() for qn in graphs}
    for qn, g in graphs.items():
        for op in g.ops:
            for use in op.uses:
                last = use.split(".")[-1]
                for cand in by_last.get(use, set()) | by_last.get(last, set()):
                    if cand != qn:
                        calls[qn].add(cand)

    return {k: sorted(v) for k, v in calls.items()}
