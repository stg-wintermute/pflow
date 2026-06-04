"""Reference system for pflow (RFC-0001).

Stable, parseable references that agents can quote and later expand.

Supported forms (v1):
  - bb:3
  - op:5
  - def:2
  - use:7
  - def:state@87          (name + line, best effort)
  - file.py:func:bb:3
  - file.py:func:def:foo@12

Refs are meant to be round-trippable within the same source.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class Ref:
    path: Optional[str] = None
    qualname: Optional[str] = None
    kind: str = ""          # 'bb', 'op', 'def', 'use'
    ident: str | int = ""   # number or "name@line"
    raw: str = ""

    def __str__(self):
        parts = []
        if self.path:
            parts.append(self.path)
        if self.qualname:
            parts.append(self.qualname)
        parts.append(f"{self.kind}:{self.ident}")
        return ":".join(parts)

    def as_agent_ref(self) -> str:
        return f"[{self}]"


def format_agent_ref(path=None, qualname=None, kind=None, ident=None) -> str:
    """Convenience to build the bracketed ref style agents like to quote."""
    parts = []
    if path:
        parts.append(str(path))
    if qualname:
        parts.append(str(qualname))
    if kind and ident is not None:
        parts.append(f"{kind}:{ident}")
    inner = ":".join(parts)
    return f"[{inner}]" if inner else "[]"


def parse_ref(s: str) -> Ref:
    s = s.strip()
    r = Ref(raw=s)

    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]

    parts = s.split(":")

    if len(parts) >= 3:
        kind = parts[-2]
        if kind in ("bb", "op", "def", "use"):
            r.kind = kind
            r.ident = parts[-1]
            prefix = parts[:-2]
            if prefix:
                r.path = prefix[0]
                if len(prefix) > 1:
                    r.qualname = ":".join(prefix[1:])
            return r

    if len(parts) == 1:
        val = parts[0]
        if val.startswith("bb"):
            r.kind = "bb"
            r.ident = val[2:]
        elif val.startswith("def"):
            r.kind = "def"
            r.ident = val[3:] or val
        elif val.startswith("use"):
            r.kind = "use"
            r.ident = val[3:] or val
        elif val.startswith("op"):
            r.kind = "op"
            r.ident = val[2:] or val
        else:
            r.kind = "bb"
            r.ident = val
        return r

    if len(parts) == 2:
        kind, ident = parts
        if kind in ("bb", "op", "def", "use"):
            r.kind = kind
            r.ident = ident
            return r

    r.kind = "raw"
    r.ident = s
    return r


def _parse_detail(ident) -> tuple:
    """Split a ref detail into (name, lineno, num).

    '3'          -> (None, None, 3)
    'lease@87'   -> ('lease', 87, None)
    'lease'      -> ('lease', None, None)
    """
    s = str(ident).strip()
    if "@" in s:
        name, _, line = s.partition("@")
        line_no = int(line) if line.isdigit() else None
        return (name or None, line_no, None)
    if s.lstrip("-").isdigit():
        return (None, None, int(s))
    return (s or None, None, None)


def _best_by_name_line(items, name, lineno, line_of):
    """Pick the item whose .name matches and whose source line is closest."""
    candidates = [it for it in items if it.name == name]
    if not candidates:
        return None
    if lineno is None:
        return candidates[0]

    exact = [it for it in candidates if line_of(it) == lineno]
    if exact:
        return exact[0]
    with_line = [it for it in candidates if line_of(it) is not None]
    if with_line:
        return min(with_line, key=lambda it: abs(line_of(it) - lineno))
    return candidates[0]


def resolve_node(g, ref: Ref | str):
    """Resolve a ref to a concrete graph node.

    Returns (node_type, node_id) where node_type is one of
    'block' | 'op' | 'def' | 'use', or None if it cannot be resolved.
    """
    if isinstance(ref, str):
        ref = parse_ref(ref)
    name, lineno, num = _parse_detail(ref.ident)

    if ref.kind == "bb":
        if num is None:
            return None
        return ("block", num) if any(b.id == num for b in g.blocks) else None

    if ref.kind == "op":
        if num is None:
            return None
        return ("op", num) if any(o.id == num for o in g.iter_ops()) else None

    if ref.kind == "def":
        if num is not None:
            return ("def", num) if any(d.id == num for d in g.defs) else None
        def_line = lambda d: d.source[0] if d.source else None
        d = _best_by_name_line(g.defs, name, lineno, def_line) if name else None
        return ("def", d.id) if d else None

    if ref.kind == "use":
        if num is not None:
            return ("use", num) if any(u.id == num for u in g.uses) else None
        op_line = {o.id: (o.source[0] if o.source else None) for o in g.iter_ops()}
        use_line = lambda u: op_line.get(u.op_id)
        u = _best_by_name_line(g.uses, name, lineno, use_line) if name else None
        return ("use", u.id) if u else None

    return None


def resolve(g, ref: Ref | str) -> dict:
    """Resolve a ref against a FunctionGraph into a descriptive dict.

    Canonical result keys: 'found', 'type', and one of
    'block' | 'op' | 'def' | 'use' holding the resolved object.
    """
    if isinstance(ref, str):
        ref = parse_ref(ref)

    out = {"ref": str(ref), "found": False, "type": ref.kind}
    node = resolve_node(g, ref)
    if node is None:
        if ref.ident in ("", None):
            out["error"] = "could not parse ref detail"
        else:
            out["error"] = "stale ref: not found in current graph"
        return out

    ntype, nid = node
    out["found"] = True
    if ntype == "block":
        blk = g.get_block(nid)
        out.update(block=blk, ops=[o.kind for o in blk.ops], succs=blk.succs)
    elif ntype == "op":
        out["op"] = next(o for o in g.iter_ops() if o.id == nid)
    elif ntype == "def":
        out["def"] = next(d for d in g.defs if d.id == nid)
    elif ntype == "use":
        out["use"] = next(u for u in g.uses if u.id == nid)
    return out
