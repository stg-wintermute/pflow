"""Input-mutation detector (RFC-0002): does the function mutate its arguments?

A function that writes through a parameter — `p.attr = v`, `p[k] = v`,
`del p[k]`, or a mutating method like `p.append(...)` / `p.update(...)` — has
a side effect on an object it does not own. That coupling is what makes the
flow hard to follow: the caller's value changes out from under it, so reading
the function tells you only half the story. This pass flags those sites; the
agent decides whether the mutation is an intentional in-place API or an
accidental impurity to refactor into a returned value.

The precision rule that makes this a flow analysis and not a grep: a write only
counts if the ORIGINAL parameter binding (the args op's def) still reaches the
write site. The defensive-copy idiom

    def f(p):
        p = dict(p)        # rebind kills the args def
        p["k"] = v         # ...so this is NOT input mutation — `p` is local now

is therefore not flagged. `self` / `cls` are excluded: mutating the receiver is
the whole point of a method.

Heuristic / may throughout (RFC-0002 §2): intentional in-place APIs are common
(Rice), and this is name-based with no aliasing — `x = p; x.append(1)` is a
known, deliberate under-report (the safe direction). Likewise plain-name
augmented assignment `p += e` is not flagged: on an int param it is pure
rebinding, on a list it mutates, and telling them apart needs type info.

Consumes the `store_bases` op attr (subscript writes, attached at lowering in
pflow/ir/cfg.py), the dotted attribute targets, and the `calls` op attr, plus
the reaching_defs attached to each Use by the dataflow pass.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from ..ir import FunctionGraph
from .opportunities import Opportunity

# Receivers whose in-place mutation is idiomatic, not a smell.
_RECEIVERS = frozenset({"self", "cls"})

# Methods that mutate their receiver in place (stdlib containers + common
# idioms). Read-only accessors (get/keys/values/items/copy/...) are absent by
# design, so `p.get(k)` / `sorted(p)` never match.
_MUTATORS = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "update", "add", "discard", "setdefault", "popitem",
    "intersection_update", "difference_update", "symmetric_difference_update",
    "__setitem__", "__delitem__",
})

# The full method surface of the builtin containers: a receiver that gets
# called with anything OUTSIDE this set is not a plain container.
_CONTAINER_METHODS = frozenset(
    m for t in (dict, list, set, frozenset, tuple, str, bytes, bytearray)
    for m in dir(t) if not m.startswith("_"))


def _args_op(graph: FunctionGraph):
    if not graph.blocks:
        return None
    return next((op for op in graph.blocks[0].ops
                 if op.attrs.get("kind") == "args"), None)


def _origin_def_ids(graph: FunctionGraph, args_op) -> Dict[str, int]:
    """{param_name: def_id} for the args-op definitions — the caller-supplied
    bindings we gate against."""
    return {d.name: d.id for d in graph.defs if d.op_id == args_op.id}


def _uses_index(graph: FunctionGraph) -> Dict[Tuple[int, str], list]:
    idx: Dict[Tuple[int, str], list] = {}
    for u in graph.uses:
        idx.setdefault((u.op_id, u.name), []).append(u)
    return idx


def find_input_mutation(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    args_op = _args_op(graph)
    if args_op is None:
        return []
    params: Set[str] = set(args_op.targets) - _RECEIVERS
    if not params:
        return []

    # Dataflow is normally run by _enrich / build_program before any pass; run
    # it here too so the pass is correct when called on a bare CFG (e.g. tests).
    if not graph.uses or not graph.defs:
        from .dataflow import run as run_dataflow
        run_dataflow(graph)

    origin = _origin_def_ids(graph, args_op)
    uses = _uses_index(graph)
    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}
    block_of = {op.id: b.id for b in graph.blocks for op in b.ops}

    def caller_object_reaches(op_id: int, root: str) -> bool:
        """Does the param's ORIGINAL binding still reach this write? False once
        the name was rebound to a fresh local (defensive copy)."""
        origin_def = origin.get(root)
        if origin_def is None:
            return False
        us = uses.get((op_id, root))
        if not us:               # base not recorded as a read here — be safe, skip
            return False
        return any(origin_def in u.reaching_defs for u in us)

    # One finding per (param, mutation kind), all sites merged. Emitting one
    # finding per write site produced N identically-worded lines for one
    # accumulator param (e.g. `out.append` in a recursive collector) — the
    # duplication read as tool noise, not as N pieces of evidence.
    groups: Dict[Tuple[str, str], dict] = {}
    seen: Set[Tuple[int, str, str]] = set()

    def emit(op, root: str, label: str, kind: str, verb: str) -> None:
        if root not in params or not caller_object_reaches(op.id, root):
            return
        key = (op.id, label, kind)
        if key in seen:
            return
        seen.add(key)
        slot = groups.setdefault((root, kind), {"verb": verb, "sites": []})
        slot["sites"].append((op, label, op_line.get(op.id)))

    # Receiver-kind evidence: `.add()`/`.update()` only signal CONTAINER
    # mutation if the receiver is a container. A param that also receives
    # non-container methods (`db.commit()`, `db.execute()`, `db.get(Node,...)`)
    # is a service/handle object whose mutating-looking methods are just its
    # API — seeker-dev's `db` session produced a dozen such false positives.
    api_like: Set[str] = set()
    for op in graph.iter_ops():
        for call in op.attrs.get("calls", ()):
            func = call.get("func")
            if func and "." in func:
                base, method = func.rsplit(".", 1)
                root = base.split(".", 1)[0]
                if root in params and method not in _CONTAINER_METHODS:
                    api_like.add(root)

    for op in graph.iter_ops():
        # (a) attribute store: target like `p.name` (root is a param).
        for t in op.targets:
            if "." in t:
                emit(op, t.split(".", 1)[0], t, "attr-store", "assigns")

        # (b) subscript store / del: `p[k] = v`, `del p[k]` (store_bases attr).
        for root, base_path in op.attrs.get("store_bases", ()):
            kind = "subscript-del" if op.kind == "delete" else "subscript-store"
            verb = "deletes from" if op.kind == "delete" else "writes"
            emit(op, root, f"{base_path}[...]", kind, verb)

        # (c) mutating method call: `p.append(...)`, `p.update(...)` — only on
        # receivers that look like plain containers (see api_like above).
        for call in op.attrs.get("calls", ()):
            func = call.get("func")
            if not func or "." not in func:
                continue
            base_path, method = func.rsplit(".", 1)
            root = base_path.split(".", 1)[0]
            if method in _MUTATORS and root not in api_like:
                emit(op, root, f"{base_path}.{method}()",
                     "method-mutation", "calls")

    # Contract discriminator: a function that mutates a param AND returns a
    # value has a mixed contract (the suspicious shape); one that returns
    # nothing is likely an intentional in-place procedure.
    returns_value = any(op.kind == "return" and op.uses for op in graph.iter_ops())

    found: List[Opportunity] = []
    for (root, kind), slot in groups.items():
        sites = sorted(slot["sites"], key=lambda s: s[0].id)
        first_op, first_label, first_line = sites[0]
        if len(sites) == 1:
            what = f"{slot['verb']} `{first_label}`"
        else:
            where = ", ".join(f"`{lb}`@{ln}" if ln else f"`{lb}`"
                              for _, lb, ln in sites[:4])
            what = f"{len(sites)} sites: {where}" + (" …" if len(sites) > 4 else "")
        detail = ("also returns a value — mixed contract; consider returning "
                  "the new value instead of writing through the caller's argument"
                  if returns_value else
                  "returns nothing (procedure-style) — in-place mutation is "
                  "likely the contract; confirm callers expect it")
        found.append(Opportunity(
            pass_name="input-mutation", kind=kind,
            title=f"mutates parameter `{root}` in place — {what}",
            ref=f"{graph.qualname}:op:{first_op.id}", op_id=first_op.id,
            block_id=block_of.get(first_op.id, graph.entry), line=first_line,
            modality="may", soundness="heuristic", detail=detail))
    return found
