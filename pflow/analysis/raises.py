"""Interprocedural exception escape: what can calling this function raise?

The debugging complement to `catches`: `catches` maps where errors stop,
`raises` maps what gets OUT. Per function, the escape set is

    E(f) = local raises(f)  ∪  ⋃ over call sites c in f:
           (E(callee(c)) − types caught at c's block)

computed to a fixpoint over the call graph (worklist; monotone, bounded by
the finite name universe).

Precision notes (all name-based, RFC §5.6 spirit):
  - `raise X(...)` contributes the leftmost name of the exception expression
    (`X`, `self.Error`); a bare `raise` inside a handler re-raises that
    handler's caught types, at top level contributes nothing.
  - A call's caught types = exc_types of the handlers its block has
    exceptional edges to. Matching uses a small builtin hierarchy table
    (`except OSError` catches FileNotFoundError; `except Exception` catches
    everything except the BaseException-only trio; unknown names are assumed
    Exception-derived, so `except Exception` swallows user exceptions).
  - Propagation only follows SHARP call edges (resolution fan-out == 1).
    Smeared edges (`obj.method()` matching many same-named methods) would
    spray exceptions across the program; the count of skipped smeared edges
    is reported instead of silently propagating through them.
  - Implicit raises (KeyError from subscripts, AttributeError, MemoryError…)
    are out of scope: this maps EXPLICIT raise statements and their travel.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from ..ir import FunctionGraph
from .program import ProgramGraph
from .interproc import CallGraph, program_callgraph

# child -> ancestors (transitive, minimal builtin table)
_PARENTS: Dict[str, Set[str]] = {
    "FileNotFoundError": {"OSError"}, "PermissionError": {"OSError"},
    "FileExistsError": {"OSError"}, "IsADirectoryError": {"OSError"},
    "NotADirectoryError": {"OSError"}, "InterruptedError": {"OSError"},
    "BlockingIOError": {"OSError"}, "ChildProcessError": {"OSError"},
    "BrokenPipeError": {"ConnectionError", "OSError"},
    "ConnectionAbortedError": {"ConnectionError", "OSError"},
    "ConnectionRefusedError": {"ConnectionError", "OSError"},
    "ConnectionResetError": {"ConnectionError", "OSError"},
    "ConnectionError": {"OSError"}, "TimeoutError": {"OSError"},
    "IOError": {"OSError"},
    "IndexError": {"LookupError"}, "KeyError": {"LookupError"},
    "ModuleNotFoundError": {"ImportError"},
    "UnboundLocalError": {"NameError"},
    "ZeroDivisionError": {"ArithmeticError"}, "OverflowError": {"ArithmeticError"},
    "FloatingPointError": {"ArithmeticError"},
    "IndentationError": {"SyntaxError"}, "TabError": {"IndentationError", "SyntaxError"},
    "UnicodeDecodeError": {"UnicodeError", "ValueError"},
    "UnicodeEncodeError": {"UnicodeError", "ValueError"},
    "UnicodeError": {"ValueError"},
    "NotImplementedError": {"RuntimeError"}, "RecursionError": {"RuntimeError"},
    "StopIteration": set(), "StopAsyncIteration": set(),
}
_BASE_ONLY = {"KeyboardInterrupt", "SystemExit", "GeneratorExit", "BaseException"}


def catches_type(caught: str, raised: str) -> bool:
    caught = caught.strip()
    if caught == raised or caught == "BaseException":
        return True
    if caught == "Exception":
        return raised not in _BASE_ONLY
    simple = raised.split(".")[-1]
    return caught in _PARENTS.get(simple, set()) or caught == simple


def _split_types(exc_type: Optional[str]) -> List[str]:
    """'(OSError, SyntaxError)' -> ['OSError', 'SyntaxError']; None (bare) -> ['BaseException']."""
    if exc_type is None:
        return ["BaseException"]
    return [t.strip() for t in exc_type.strip("()").split(",") if t.strip()]


def _covered_by(caught: List[str], raised: str) -> bool:
    return any(catches_type(c, raised) for c in caught)


@dataclass
class FnRaises:
    local: Set[str]                              # raised right here, uncaught
    local_lines: Dict[str, int]                  # exc -> a witness line
    caught_local: Set[str]                       # raised here but caught by own try
    calls: List[Tuple[int, List[str], List[str]]]  # (line, sharp callee fqs, caught types)
    smeared_calls: int = 0
    local_desc: Dict[str, str] = None            # exc -> witness text (set by analyze)


def _handler_types_of_block(g: FunctionGraph, block) -> List[str]:
    out: List[str] = []
    for hid in block.except_succs:
        try:
            hb = g.get_block(hid)      # raises KeyError for unknown ids —
        except KeyError:               # found by `pflow raises` on itself
            continue
        for op in hb.ops:
            if op.kind == "enter_except":
                out.extend(_split_types(op.attrs.get("exc_type")))
    return out


def analyze_function(pg: Optional[ProgramGraph], cg: Optional[CallGraph],
                     fq: str, g: FunctionGraph,
                     implicit: bool = False) -> FnRaises:
    local: Set[str] = set()
    caught_local: Set[str] = set()
    lines: Dict[str, int] = {}
    desc: Dict[str, str] = {}
    calls: List[Tuple[int, List[str], List[str]]] = []
    smeared = 0

    # `except X as e: ... raise e` re-raises the CAUGHT types — without this
    # map the literal name `e` leaked into escape sets (seeker's cmd_use
    # reported an escape type of `e`). Function-wide by binds name; collisions
    # across handlers are rare and merely widen (the safe direction).
    binds_types: Dict[str, List[str]] = {}
    for b in g.blocks:
        if b.attrs.get("is_exception_handler"):
            ee = next((op for op in b.ops if op.kind == "enter_except"), None)
            bn = next((op.targets[0] for op in b.ops
                       if op.attrs.get("kind") == "except_as"), None)
            if ee is not None and bn:
                binds_types.setdefault(bn, []).extend(
                    _split_types(ee.attrs.get("exc_type")))

    for b in g.blocks:
        caught_here = _handler_types_of_block(g, b)
        handler_types = None
        if b.attrs.get("is_exception_handler"):
            ee = next((op for op in b.ops if op.kind == "enter_except"), None)
            if ee is not None:
                handler_types = _split_types(ee.attrs.get("exc_type"))
        for op in b.ops:
            if op.kind == "raise":
                ln = op.source[0] if op.source else 0
                if op.uses:
                    first = op.uses[0]
                    if len(op.uses) == 1 and first in binds_types:
                        raised = binds_types[first]       # `raise e` re-raise
                    else:
                        # normalize dotted spellings: scheduler.AdmissionError
                        # and AdmissionError are the same class
                        raised = [first.split(".")[-1]]
                    for exc in raised:
                        if _covered_by(caught_here, exc):
                            caught_local.add(exc)
                        else:
                            local.add(exc)
                            lines.setdefault(exc, ln)
                elif handler_types is not None:      # bare raise: re-raise
                    for t in handler_types:
                        if _covered_by(caught_here, t):
                            caught_local.add(t)
                        else:
                            local.add(t)
                            lines.setdefault(t, ln)
            if pg is not None and cg is not None:
                for c in op.attrs.get("calls", ()):
                    name = c.get("func")
                    if not name:
                        continue
                    targets = [t for t in pg.resolve_call(fq, name)
                               if t in pg.functions]
                    if not targets:
                        continue
                    if len(targets) > 1:
                        smeared += 1
                        continue                     # smear: do not propagate
                    ln = op.source[0] if op.source else 0
                    calls.append((ln, targets, caught_here))
    short = fq.split(":")[-1]
    for exc in local:
        desc[exc] = f"raise {exc} @{short} L{lines.get(exc, 0)}"

    if implicit:
        # Language-level sources, function-granular (opt-in — these would
        # bury explicit raises if always on). Filtered by ANY handler in the
        # function catching the type (coarser than block-level; documented).
        fn_caught: List[str] = []
        for b in g.blocks:
            for op in b.ops:
                if op.kind == "enter_except":
                    fn_caught.extend(_split_types(op.attrs.get("exc_type")))
        n_sub = g.attrs.get("subscript_loads", 0)
        n_attr = sum(1 for op in g.iter_ops() for u in op.uses if "." in u)
        for exc, n, what in (("KeyError", n_sub, "subscript load"),
                             ("IndexError", n_sub, "subscript load"),
                             ("AttributeError", n_attr, "attribute access")):
            if n and not _covered_by(fn_caught, exc) and exc not in local:
                local.add(exc)
                lines.setdefault(exc, g.first_line or 0)
                desc[exc] = f"implicit: {n} {what}(s) in {short}"

    return FnRaises(local=local, local_lines=lines, caught_local=caught_local,
                    calls=calls, smeared_calls=smeared, local_desc=desc)


@dataclass
class ProgramRaises:
    escapes: Dict[str, Set[str]]                     # fq -> escaping type names
    origin: Dict[Tuple[str, str], Tuple[str, str, Optional[str]]]
    # (fq, exc) -> (kind 'local'|'via', detail, parent fq or None)
    smeared_calls: int = 0

    def chain(self, fq: str, exc: str, limit: int = 6) -> List[str]:
        out: List[str] = []
        cur: Optional[str] = fq
        for _ in range(limit):
            o = self.origin.get((cur, exc))
            if o is None:
                break
            kind, detail, parent = o
            out.append(detail)
            if kind == "local" or parent is None:
                break
            cur = parent
        return out


def program_raises(pg: ProgramGraph, implicit: bool = False) -> ProgramRaises:
    cg = program_callgraph(pg)
    info = {fq: analyze_function(pg, cg, fq, g, implicit=implicit)
            for fq, g in pg.functions.items()}
    escapes: Dict[str, Set[str]] = {fq: set(i.local) for fq, i in info.items()}
    origin: Dict[Tuple[str, str], Tuple[str, str, Optional[str]]] = {}
    for fq, i in info.items():
        for exc in i.local:
            origin[(fq, exc)] = ("local", i.local_desc.get(
                exc, f"raise {exc} @{fq.split(':')[-1]}"), None)

    # reverse dependency: which callers must be re-evaluated when fq changes
    callers_of: Dict[str, Set[str]] = {fq: set() for fq in info}
    for fq, i in info.items():
        for _, targets, _ in i.calls:
            for t in targets:
                callers_of.setdefault(t, set()).add(fq)

    work = deque(info)
    while work:
        fq = work.popleft()
        i = info[fq]
        changed = False
        for ln, targets, caught in i.calls:
            callee = targets[0]
            for exc in escapes.get(callee, ()):
                if _covered_by(caught, exc) or exc in escapes[fq]:
                    continue
                escapes[fq].add(exc)
                origin[(fq, exc)] = ("via",
                                     f"{fq.split(':')[-1]} L{ln} → "
                                     f"{callee.split(':')[-1]}", callee)
                changed = True
        if changed:
            for caller in callers_of.get(fq, ()):
                if caller not in work:
                    work.append(caller)

    return ProgramRaises(escapes=escapes, origin=origin,
                         smeared_calls=sum(i.smeared_calls for i in info.values()))
