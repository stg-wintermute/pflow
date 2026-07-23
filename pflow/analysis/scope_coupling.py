"""Scope-coupling detector (RFC-0002): closures that capture many enclosing locals.

A nested `def` whose body reads several names from the enclosing function has no
interface you can read — its real inputs are implicit captures, invisible at each
call site. `_check(g, None, "global")` tells you nothing about the `state`,
`new_rate`, and `_spent` it also depends on. That hidden fan-in is the flow smell:
the parent looks simple to a per-function analyzer precisely because the work —
and the coupling — is hidden inside the closure's separate graph.

The signal is capture COUNT, not nesting. A well-scoped closure (a one-capture
helper) is fine and stays silent. And the remedy is deliberately NOT "hoist it":
turning N captures into an N-parameter module function trades hidden coupling for
a sprawling signature — the same oversized interface, now spelled out. Well-scoped
means a SMALL interface (params + captures) for what the function does; this pass
flags the closures that fail that on the capture side and leaves the fix — rescope,
inline if single-use, or accept it — to the agent.

Consumes the `nested_def` op emitted at lowering (pflow/ir/cfg.py): an `assign`
whose `attrs['kind'] == 'nested_def'`, binding the closure name, with `uses`
carrying the closure's free variables. Intersecting those uses with the enclosing
function's own bound names isolates real enclosing-scope captures from ordinary
global / builtin references (`HTTPException`, `len`, imported helpers) — without
that intersection the pass would fire on every closure that calls a library.

Heuristic / may (RFC-0002 §2): whether a capturing closure is worth rescoping is a
design call, and capture counting is name-based (no aliasing).
"""

from __future__ import annotations

from typing import List, Set

from ..ir import FunctionGraph
from .opportunities import Opportunity

# A closure reading this many enclosing locals has an interface you can't see at
# its call sites. Below 3, hidden fan-in is incidental; at 3+ it's the flow smell
# (dogfooded on cloud._enforce_limits — its _check and _spent each capture 3).
_MIN_CAPTURES = 3


def _enclosing_locals(graph: FunctionGraph) -> Set[str]:
    """Every name bound in this function's own scope: params (the args op),
    assignments, and the nested-def bindings themselves — a closure can capture a
    sibling closure (in cloud._enforce_limits, `_check` captures `_spent`)."""
    return {t for op in graph.iter_ops() for t in op.targets}


def find_scope_coupling(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    local = _enclosing_locals(graph)
    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}
    block_of = {op.id: b.id for b in graph.blocks for op in b.ops}

    returned: Set[str] = {u for op in graph.iter_ops()
                          if op.kind == "return" for u in op.uses}

    found: List[Opportunity] = []
    for op in graph.iter_ops():
        if op.attrs.get("kind") != "nested_def":
            continue
        name = op.targets[0] if op.targets else "<closure>"
        captures = sorted(u for u in op.uses if u in local and u != name)
        if len(captures) < _MIN_CAPTURES:
            continue
        ln = op_line.get(op.id)
        ref = f"{graph.qualname}:def:{name}@{ln}" if ln else f"{graph.qualname}:op:{op.id}"
        # a closure the function RETURNS is a factory product: capturing is
        # its construction mechanism, not hidden coupling at local call sites
        if name in returned:
            detail = ("returned to the caller (factory pattern) — the captures "
                      "are its constructor arguments; fan-in is by design")
        else:
            detail = ("hidden inputs at its call sites; give it a parameter "
                      "interface, or inline it if it's used once — don't just "
                      "hoist N captures into N params")
        found.append(Opportunity(
            pass_name="scope-coupling", kind="closure-capture",
            title=f"closure `{name}` captures {len(captures)} enclosing locals "
                  f"{{{', '.join(captures)}}}",
            ref=ref, op_id=op.id, block_id=block_of.get(op.id, graph.entry), line=ln,
            modality="may", soundness="heuristic", detail=detail))
    return found
