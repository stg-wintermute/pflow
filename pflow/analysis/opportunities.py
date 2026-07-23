"""Opportunity model + pass runner (RFC-0002 §2, §8 A4).

A pass over the IR emits Opportunities: ref-tagged, soundness-tagged change
suggestions. The tool never edits — it reports; the agent decides. Every
opportunity is tagged MUST/MAY (does it hold on all paths) and SOUND/HEURISTIC
(is it exact, or best-effort over Python's dynamic tail), per RFC-0002 §2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..ir import FunctionGraph


@dataclass
class Opportunity:
    pass_name: str          # 'dead-store', ...
    kind: str               # finer label within the pass
    title: str              # one-line, human/agent readable
    ref: str                # e.g. "func:def:x@42" — quotable, re-dereferenceable
    op_id: int
    block_id: int
    line: Optional[int]
    modality: str           # 'must' | 'may'
    soundness: str          # 'sound' | 'heuristic'
    detail: str = ""

    def render(self, path: str = "") -> str:
        tag = f"[{self.soundness}/{self.modality}]"
        ref = full_ref(self, path)
        loc = f"  ref {ref}" if ref else ""
        extra = f"  — {self.detail}" if self.detail else ""
        return f"{tag} {self.pass_name}: {self.title}{loc}{extra}"


# Static catalog (name, one-line description), in run order. No imports here —
# keeps module load free of the Opportunity import cycle. Each name equals the
# pass_name its finder emits, so `--pass NAME` and the rendered prefix agree.
# NOTE: dependence.find_overloaded_variables ('overloaded-var') is intentionally
# NOT listed — dogfooding showed a high false-positive rate (transformation
# chains `x = f(x)` and loop-var reuse both look like disjoint def-use groups).
# Kept in the tree for later refinement. (RFC-0002 §4)
PASS_CATALOG: List[tuple] = [
    ("unreachable",      "blocks/branches with no path from entry"),
    ("redundant-branch", "conditions that re-test an already-decided guard"),
    ("const-branch",     "branches on a constant condition"),
    ("use-before-def",   "names read before they are definitely assigned"),
    ("dead-store",       "assignments never read before overwrite/exit"),
    ("complexity",       "cyclomatic / cognitive / nesting hotspots"),
    ("decomposition",    "functions splittable along output slices"),
    ("scope-coupling",   "closures that capture many enclosing locals (hidden fan-in)"),
    ("lossy-projection", "open record narrowed to a closed dict (a key is dropped)"),
    ("input-mutation",   "function mutates a parameter in place (writes through the caller's argument)"),
]
PASS_NAMES: List[str] = [name for name, _ in PASS_CATALOG]

_RANK = {("sound", "must"): 0, ("sound", "may"): 1,
         ("heuristic", "must"): 2, ("heuristic", "may"): 3}


def _registry():
    """[(name, finder)] in run order. Lazy imports: passes import Opportunity."""
    from .reachability import find_unreachable
    from .redundant_branch import find_redundant_branches
    from .const_prop import find_constant_branches
    from .definite_assignment import find_use_before_def
    from .liveness import find_dead_stores
    from .complexity import find_complexity_hotspots
    from .decomposition import find_decomposition
    from .scope_coupling import find_scope_coupling
    from .projection import find_lossy_projection
    from .input_mutation import find_input_mutation
    fns = {
        "unreachable": find_unreachable,
        "redundant-branch": find_redundant_branches,
        "const-branch": find_constant_branches,
        "use-before-def": find_use_before_def,
        "dead-store": find_dead_stores,
        "complexity": find_complexity_hotspots,
        "decomposition": find_decomposition,
        "scope-coupling": find_scope_coupling,
        "lossy-projection": find_lossy_projection,
        "input-mutation": find_input_mutation,
    }
    return [(name, fns[name]) for name in PASS_NAMES]


def _sort(opps: List[Opportunity]) -> List[Opportunity]:
    """sound+must first, then by source line (the shared ranking)."""
    return sorted(opps, key=lambda o: (_RANK.get((o.soundness, o.modality), 9),
                                        o.line or 0))


def run_passes(graph: FunctionGraph) -> List[Opportunity]:
    """Run all passes over one function graph; one ranked, merged list."""
    opps: List[Opportunity] = []
    for _, fn in _registry():
        opps.extend(fn(graph))
    return _sort(opps)


def run_passes_grouped(graph: FunctionGraph, only=None) -> List[tuple]:
    """[(pass_name, ranked findings)] for the selected passes (all if `only`
    is None). Within-pass ranking only — no cross-pass merge — so each pass's
    feedback stays separable. Empty passes are included so a selected pass that
    found nothing still reports that explicitly."""
    sel = only if only is not None else PASS_NAMES
    by_name = dict(_registry())
    return [(name, _sort(by_name[name](graph))) for name in sel if name in by_name]


_PRAGMA = None  # compiled lazily; module must import without `re` cost


def filter_suppressed(opps: List[Opportunity], source_lines: List[str]):
    """Drop findings whose source line carries `# pflow: ok` (all passes) or
    `# pflow: ok(pass-name[, pass-name])` (those passes only). Returns
    (kept, n_suppressed). Accepted heuristics stop re-firing on every run —
    without this, agents re-read the same known-intentional findings forever."""
    global _PRAGMA
    if _PRAGMA is None:
        import re
        _PRAGMA = re.compile(r"pflow:\s*ok(?:\(([^)]*)\))?")
    kept: List[Opportunity] = []
    n = 0
    for o in opps:
        if o.line and 1 <= o.line <= len(source_lines):
            m = _PRAGMA.search(source_lines[o.line - 1])
            if m and (not m.group(1)
                      or o.pass_name in {s.strip() for s in m.group(1).split(",")}):
                n += 1
                continue
        kept.append(o)
    return kept, n


def full_ref(o: Opportunity, path: str = "") -> str:
    """File-qualified, runnable ref: `path:qualname:def:x@42`. Refs emitted by
    passes start at the qualname; without the path prefix they cannot be fed
    back into walk/slice/show — which silently broke the confirm-by-walking
    loop everywhere the path was dropped."""
    return f"{path}:{o.ref}" if path and o.ref else o.ref


# Per-pass verification commands — the exact IR-layer invocation that shows the
# structure behind a finding. Rendered with every finding: a verdict an agent
# can check in one command is trusted; one it can't is dismissed.
_VERIFY = {
    "unreachable":      lambda t, r: f"pflow cfg {t} -a" if t else None,
    "redundant-branch": lambda t, r: f"pflow slice --from '{r}' --backward",
    "const-branch":     lambda t, r: f"pflow slice --from '{r}' --backward",
    "use-before-def":   lambda t, r: f"pflow slice --from '{r}' --backward",
    "lossy-projection": lambda t, r: f"pflow slice --from '{r}' --backward",
    "dead-store":       lambda t, r: f"pflow walk --from '{r}' --edges data --direction forward",
    "scope-coupling":   lambda t, r: f"pflow walk --from '{r}' --edges data --direction forward",
    "input-mutation":   lambda t, r: f"pflow show '{r}'",
    "complexity":       lambda t, r: f"pflow report {t}" if t else None,
    "decomposition":    lambda t, r: f"pflow report {t}" if t else None,
}


def verify_hint(o: Opportunity, target: str = "", path: str = "") -> Optional[str]:
    fn = _VERIFY.get(o.pass_name)
    if fn is None:
        return None
    r = full_ref(o, path)
    return fn(target, r) if r else None


def _path_and_target(graph: FunctionGraph, target: str = ""):
    """(file path, file:qualname target) for building runnable refs/hints."""
    if target and not target.startswith("live:") and ":" in target:
        return target.rsplit(":", 1)[0], target
    path = graph.source_path or ""
    return path, (f"{path}:{graph.qualname}" if path else "")


_SECTION = {"sound": "sound (holds by construction):",
            "heuristic": "heuristic (hypotheses — confirm against source):"}


def _render_ranked(opps: List[Opportunity], path: str, target: str,
                   indent: str = "  ") -> List[str]:
    """Ranked findings with soundness section headers + verify hints."""
    lines: List[str] = []
    current = None
    for o in opps:
        if o.soundness != current:
            current = o.soundness
            lines.append(f"{indent}{_SECTION.get(current, current + ':')}")
        lines.append(f"{indent}  {o.render(path)}")
        hint = verify_hint(o, target, path)
        if hint:
            lines.append(f"{indent}      verify → {hint}")
    return lines


def format_opportunities(graph: FunctionGraph, opps: List[Opportunity],
                         target: str = "") -> str:
    path, tgt = _path_and_target(graph, target)
    if not opps:
        walk_to = tgt or graph.qualname
        return (f"{graph.qualname}: no findings — the passes are a cross-check, "
                f"not the review; walk the graph: pflow report {walk_to}")
    n_sound = sum(1 for o in opps if o.soundness == "sound")
    lines = [f"{graph.qualname}: {len(opps)} finding(s) "
             f"({n_sound} sound · {len(opps) - n_sound} heuristic) — "
             f"cross-check layer: confirm each ref in the graph before acting"]
    lines += _render_ranked(opps, path, tgt)
    return "\n".join(lines)


def format_grouped(graph: FunctionGraph, groups: List[tuple],
                   target: str = "") -> str:
    """Per-pass sections: one header per pass, its findings beneath it."""
    path, tgt = _path_and_target(graph, target)
    total = sum(len(f) for _, f in groups)
    lines = [f"{graph.qualname}: {total} finding(s) across {len(groups)} pass(es)"]
    for name, findings in groups:
        lines.append(f"  [{name}] {len(findings)} finding(s)" if findings
                     else f"  [{name}] none")
        for o in findings:
            lines.append("    " + o.render(path))
            hint = verify_hint(o, tgt, path)
            if hint:
                lines.append(f"        verify → {hint}")
    return "\n".join(lines)
