"""Class-level structure: inheritance forest + method overrides.

The type layer between the module graph and the call graph. For each
in-program class: its resolved in-program bases (external bases kept by
name), its methods, and which of them OVERRIDE a method of an in-program
base — the sites where dynamic dispatch actually forks, which is exactly
where name-based call resolution smears (`~k`) come from.

Base resolution is name-based: same module first, then a unique simple-name
match program-wide; ambiguous or unknown names stay external. No MRO — the
override check is against each direct base's own methods (shallow, like the
rest of pflow's interprocedural layer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .program import ProgramGraph


@dataclass
class ClassNode:
    key: str                        # "relpath:ClassName"
    relpath: str
    name: str
    methods: Set[str]
    bases_in: List[str] = field(default_factory=list)     # resolved keys
    bases_out: List[str] = field(default_factory=list)    # external names
    children: List[str] = field(default_factory=list)
    overrides: Dict[str, str] = field(default_factory=dict)  # method -> base key


def class_graph(pg: ProgramGraph) -> Dict[str, ClassNode]:
    nodes: Dict[str, ClassNode] = {}
    by_simple: Dict[str, List[str]] = {}
    for relpath, mod in pg.modules.items():
        for name, ci in mod.classes.items():
            key = f"{relpath}:{name}"
            nodes[key] = ClassNode(key=key, relpath=relpath, name=name,
                                   methods=set(ci.methods))
            by_simple.setdefault(name, []).append(key)

    for relpath, mod in pg.modules.items():
        for name, ci in mod.classes.items():
            node = nodes[f"{relpath}:{name}"]
            for base in ci.bases:
                same_mod = f"{relpath}:{base}"
                if same_mod in nodes:
                    target: Optional[str] = same_mod
                else:
                    hits = by_simple.get(base, [])
                    target = hits[0] if len(hits) == 1 else None
                if target is not None and target != node.key:
                    node.bases_in.append(target)
                    nodes[target].children.append(node.key)
                    for m in node.methods & nodes[target].methods:
                        if m != "__init__":
                            node.overrides.setdefault(m, target)
                else:
                    node.bases_out.append(base)
    return nodes


def roots(nodes: Dict[str, ClassNode]) -> List[str]:
    """In-program hierarchy roots that actually have subclasses."""
    return sorted(k for k, n in nodes.items() if not n.bases_in and n.children)


def render_tree(nodes: Dict[str, ClassNode], key: str, depth: int = 0,
                seen: Optional[Set[str]] = None) -> List[Tuple[int, str, bool]]:
    """(depth, key, is_revisit) rows, depth-first. A revisit row marks a
    diamond (or cycle) — the node was already printed elsewhere; the caller
    should reference, not recurse. Pass ONE `seen` across all roots so shared
    subtrees render once."""
    seen = seen if seen is not None else set()
    if key in seen:
        return [(depth, key, True)]
    seen.add(key)
    out: List[Tuple[int, str, bool]] = [(depth, key, False)]
    for c in sorted(nodes[key].children):
        out.extend(render_tree(nodes, c, depth + 1, seen))
    return out
