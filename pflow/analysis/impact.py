"""Impact analysis: the blast radius of changing a function.

Answers the two questions a reviewer/debugger asks about a change before
reading a line of it:

  upstream   — who can observe this change? (reverse call closure: every
               function with a call path INTO the changed code, plus every
               function that READS state the changed code writes)
  downstream — what does the changed code stand on? (forward call closure +
               state it reads that others write — the assumptions that, if
               someone else changes them, break THIS code)

Focus functions come either from explicit --focus fqnames or from a git diff
(`--diff [REV]`): changed line ranges are mapped to the innermost enclosing
function via block line spans. Everything is name-based may-analysis (RFC
§5.6): caller edges carry the resolution ambiguity mark (`~k`) so smeared
edges are visible, and untracked (brand-new) files are not in `git diff` —
the census, not impact, is the view for those.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .program import ProgramGraph
from .interproc import CallGraph, program_callgraph, state_map


# -- source spans ---------------------------------------------------------

def function_spans(pg: ProgramGraph) -> Dict[str, Tuple[int, int]]:
    """fq -> (first line, last line) from block line ranges."""
    spans: Dict[str, Tuple[int, int]] = {}
    for fq, g in pg.functions.items():
        start = g.first_line or 0
        end = start
        for b in g.blocks:
            lr = b.attrs.get("line_range")
            if lr:
                end = max(end, lr[1])
        spans[fq] = (start, end)
    return spans


def functions_at(pg: ProgramGraph, relpath: str, lines: Set[int],
                 spans: Optional[Dict[str, Tuple[int, int]]] = None) -> Set[str]:
    """Innermost function(s) whose span covers each changed line."""
    spans = spans if spans is not None else function_spans(pg)
    in_file = [(fq, s, e) for fq, (s, e) in spans.items()
               if fq.split(":", 1)[0] == relpath]
    hits: Set[str] = set()
    for ln in lines:
        covering = [(e - s, fq) for fq, s, e in in_file if s <= ln <= e]
        if covering:
            hits.add(min(covering)[1])       # smallest span = innermost def
    return hits


# -- git diff -> changed functions -----------------------------------------

def _git(root: str, *argv: str) -> str:
    p = subprocess.run(["git", "-C", os.path.abspath(root), *argv],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"git {' '.join(argv)} failed")
    return p.stdout


def changed_lines_from_git(root: str, rev: str = "HEAD") -> Dict[str, Set[int]]:
    """{relpath (program-relative): changed line numbers in the NEW file}
    for the working tree vs `rev`. Uses -U0 hunks; deletions map to the line
    they now touch. Raises RuntimeError with git's stderr on failure."""
    absroot = os.path.abspath(root)
    git = lambda *argv: _git(root, *argv)

    top = git("rev-parse", "--show-toplevel").strip()
    out = git("diff", "-U0", rev, "--", ".")

    changed: Dict[str, Set[int]] = {}
    cur: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            cur = None
            if path.startswith("b/"):
                ap = os.path.join(top, path[2:])
                if ap.endswith(".py") and (ap == absroot or
                                           ap.startswith(absroot + os.sep)):
                    cur = (os.path.basename(ap) if os.path.isfile(absroot)
                           else os.path.relpath(ap, absroot))
        elif line.startswith("@@") and cur is not None:
            # @@ -a,b +c,d @@ — take the new-file range (c, len d; d omitted = 1)
            try:
                new = line.split("+", 1)[1].split(" ", 1)[0]
                start, _, count = new.partition(",")
                c, d = int(start), int(count) if count else 1
            except ValueError:
                continue
            changed.setdefault(cur, set()).update(range(c, c + max(d, 1)))
    return changed


def changed_functions_from_git(pg: ProgramGraph, rev: str = "HEAD") -> Dict[str, Set[int]]:
    """{fq: changed lines inside it} for the working tree vs `rev`."""
    spans = function_spans(pg)
    per_fn: Dict[str, Set[int]] = {}
    for relpath, lines in changed_lines_from_git(pg.root, rev).items():
        for fq in functions_at(pg, relpath, lines, spans):
            s, e = spans[fq]
            per_fn.setdefault(fq, set()).update(ln for ln in lines if s <= ln <= e)
    return per_fn


_DELTA_KEYS = ("blocks", "branches", "exits", "cyclomatic", "cognitive",
               "max_live_span")


def structural_deltas(pg: ProgramGraph, rev: str = "HEAD"):
    """Per-function structural metrics REV → worktree, for every function in
    a file the diff touches. Rows: (fq, old metrics|None, new metrics|None) —
    None on one side means the function was added/removed. Only rows whose
    metrics actually differ (or that appeared/vanished) are returned; a
    reformat that leaves the flow graph identical reports nothing."""
    from .program import _build_file
    from .complexity import compute_metrics

    absroot = os.path.abspath(pg.root)
    top = _git(pg.root, "rev-parse", "--show-toplevel").strip()

    rows = []
    for relpath in sorted(changed_lines_from_git(pg.root, rev)):
        abspath = absroot if os.path.isfile(absroot) else os.path.join(absroot, relpath)
        repo_rel = os.path.relpath(abspath, top)
        try:
            old_src = _git(pg.root, "show", f"{rev}:{repo_rel}")
        except RuntimeError:
            old_src = None                        # file added since rev
        old_funcs = {}
        if old_src is not None:
            try:
                _, old_funcs = _build_file(relpath, old_src)
            except SyntaxError:
                old_funcs = {}
        new_funcs = {fq: g for fq, g in pg.functions.items()
                     if fq.split(":", 1)[0] == relpath}
        for fq in sorted(set(old_funcs) | set(new_funcs)):
            og, ng = old_funcs.get(fq), new_funcs.get(fq)
            om = compute_metrics(og) if og is not None else None
            nm = compute_metrics(ng) if ng is not None else None
            if om is not None and nm is not None and \
                    all(om[k] == nm[k] for k in _DELTA_KEYS):
                continue
            rows.append((fq, om, nm))
    return rows


# -- the impact closure -----------------------------------------------------

@dataclass
class ImpactReport:
    focus: List[str]
    focus_lines: Dict[str, Set[int]]                 # only for --diff mode
    callers_by_depth: Dict[int, List[str]]           # upstream closure
    entry_reached: List[str]                         # entrypoints in the closure
    callees_by_depth: Dict[int, List[str]]           # downstream closure
    writes_read_by: List[Tuple[str, List[str]]]      # (cell, outside readers)
    reads_written_by: List[Tuple[str, List[str]]]    # (cell, outside writers)
    smeared_edges: int = 0                           # ambiguous (~k>1) edges used
    total_edges: int = 0
    cg: Optional[CallGraph] = field(default=None, repr=False)


def _closure(edges: Dict[str, Set[str]], seeds: Set[str], depth: int,
             cg: CallGraph, counters: List[int], reverse: bool) -> Dict[int, List[str]]:
    seen: Set[str] = set(seeds)
    frontier = set(seeds)
    by_depth: Dict[int, List[str]] = {}
    for d in range(1, depth + 1):
        nxt: Set[str] = set()
        for fq in frontier:
            for n in edges.get(fq, ()):
                counters[1] += 1
                pair = (n, fq) if reverse else (fq, n)   # ambiguity keyed caller->callee
                if cg.ambiguity.get(pair, 1) > 1:
                    counters[0] += 1
                if n not in seen:
                    seen.add(n)
                    nxt.add(n)
        if not nxt:
            break
        by_depth[d] = sorted(nxt)
        frontier = nxt
    return by_depth


def impact(pg: ProgramGraph, focus: List[str], depth: int = 4) -> ImpactReport:
    cg = program_callgraph(pg)
    seeds = set(focus)
    counters = [0, 0]                                # [smeared, total] edges walked
    up = _closure(cg.rev, seeds, depth, cg, counters, reverse=True)
    down = _closure(cg.edges, seeds, min(depth, 2), cg, counters, reverse=False)

    up_all = seeds | {fq for fqs in up.values() for fq in fqs}
    entry = sorted(fq for fq in up_all if not cg.rev.get(fq))

    cells = state_map(pg)
    writes_read_by: List[Tuple[str, List[str]]] = []
    reads_written_by: List[Tuple[str, List[str]]] = []
    for cell in cells.values():
        if cell.writers & seeds:
            outside = sorted(cell.readers - seeds)
            if outside:
                writes_read_by.append((cell.name, outside))
        if cell.readers & seeds:
            outside = sorted(cell.writers - seeds)
            if outside:
                reads_written_by.append((cell.name, outside))
    writes_read_by.sort(key=lambda r: -len(r[1]))
    reads_written_by.sort(key=lambda r: -len(r[1]))

    return ImpactReport(
        focus=sorted(seeds), focus_lines={},
        callers_by_depth=up, entry_reached=entry, callees_by_depth=down,
        writes_read_by=writes_read_by, reads_written_by=reads_written_by,
        smeared_edges=counters[0], total_edges=counters[1], cg=cg)
