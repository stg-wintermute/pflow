"""Exception-flow census: every handler, what it catches, and how it exits.

The debugging question this answers is "where does my error disappear?".
For each `except` handler in the program it reports, from the IR alone:

  exc_type   what the handler catches (None = bare `except:`)
  binds      the `as e` name, if any
  exit       how the handler leaves:
               re-raise   — bare `raise` or `raise <bound name>` (propagates)
               raise-new  — raises a different/wrapped exception (transforms)
               return     — converts the error into a return value
               swallow    — neither: control falls through, the error is gone
  sets       names assigned in the handler (the default-on-error idiom:
             `except KeyError: y = None`)
  calls      calls made inside the handler (log/print/cleanup visibility)

Handler region = blocks dominated by the handler entry (a handler's body has
no other entry). Exit classes are structural facts read off the ops, not
judgments; `swallow` in particular is a *position* to inspect, since silencing
may be exactly what the code means to do (build loops, cache probes).

Caveats (RFC §5.6 spirit): `raise ValueError(str(e))` is raise-new even though
it mentions the bound name (rule: re-raise iff uses is empty or exactly the
bound name); a handler that exits via break/continue counts as swallow (the
error stops propagating either way).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..ir import FunctionGraph
from .dominance import compute_dominators

# direct process/interpreter terminators — a handler calling one does not
# swallow the error, it converts it into an exit
_TERMINATORS = frozenset({"sys.exit", "os._exit", "os.abort", "exit", "quit"})


@dataclass
class HandlerInfo:
    fq: str                     # relpath:qualname (or bare qualname in single mode)
    block_id: int               # handler entry block — quotable as fq:bb:N
    line: Optional[int]
    exc_type: Optional[str]     # source text of the except clause; None = bare
    binds: Optional[str]        # `as e` name
    exit: str                   # 're-raise' | 'raise-new' | 'return' | 'swallow' | 'mixed'
    sets: List[str]             # names assigned in the handler region
    calls: List[str]            # call names made in the handler region

    @property
    def broad(self) -> bool:
        t = self.exc_type
        return t is None or t.strip("()") in ("Exception", "BaseException")


def function_handlers(fq: str, g: FunctionGraph) -> List[HandlerInfo]:
    handlers = [b for b in g.blocks if b.attrs.get("is_exception_handler")
                and any(op.kind == "enter_except" for op in b.ops)]
    if not handlers:
        return []
    doms = g.attrs.get("dominators") or compute_dominators(g)

    out: List[HandlerInfo] = []
    for hb in handlers:
        ee = next(op for op in hb.ops if op.kind == "enter_except")
        binds = next((op.targets[0] for op in hb.ops
                      if op.attrs.get("kind") == "except_as"), None)
        region = [b for b in g.blocks
                  if b.id == hb.id or hb.id in doms.get(b.id, set())]

        exits: set = set()
        sets: List[str] = []
        calls: List[str] = []
        for b in region:
            for op in b.ops:
                if op.kind == "raise":
                    uses = tuple(op.uses)
                    exits.add("re-raise" if not uses or uses == (binds,) else "raise-new")
                elif op.kind == "return":
                    exits.add("return")
                if op.kind == "assign" and not op.attrs.get("is_synthetic") \
                        and op.attrs.get("kind") != "except_as":
                    sets.extend(t for t in op.targets if t not in sets)
                for c in op.attrs.get("calls", ()):
                    if c.get("func") and c["func"] not in calls:
                        calls.append(c["func"])
                    if c.get("func") in _TERMINATORS:
                        # sys.exit() raises SystemExit — classifying the
                        # handler as "swallow" misread every CLI error path
                        exits.add("terminates")

        exit_ = ("swallow" if not exits
                 else exits.pop() if len(exits) == 1 else "mixed")
        out.append(HandlerInfo(
            fq=fq, block_id=hb.id,
            line=ee.source[0] if ee.source else None,
            exc_type=ee.attrs.get("exc_type"), binds=binds,
            exit=exit_, sets=sets, calls=calls))
    return out


def program_handlers(functions: Dict[str, FunctionGraph]) -> List[HandlerInfo]:
    out: List[HandlerInfo] = []
    for fq, g in functions.items():
        out.extend(function_handlers(fq, g))
    out.sort(key=lambda h: (h.fq, h.line or 0))
    return out


def census_counts(hs: List[HandlerInfo]) -> Dict[str, int]:
    c = {"swallow": 0, "return": 0, "re-raise": 0, "raise-new": 0, "mixed": 0,
         "terminates": 0, "broad": 0, "bare": 0}
    for h in hs:
        c[h.exit] = c.get(h.exit, 0) + 1
        if h.exc_type is None:
            c["bare"] += 1
        elif h.broad:
            c["broad"] += 1
    return c
