"""pflow CLI.

Implements the surface described in RFC-0001 §15 (revised).
Default output is agent-dense; pass --human for the decorated view.

Exit codes (RFC §10):
    0  success
    2  structural signal (target not found / no structure produced)
    3  partial lowering failure (some functions failed)
    4  unsupported Python feature for v1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from . import (
    build_cfg_from_source,
    build_cfg_from_live,
    run_dataflow,
    walk,
    format_walk,
    find_paths,
    attach_dis_info,
    correct_exception_edges,
    format_cfg_agent,
)
from .ir import FunctionGraph
from .analysis.dominance import compute_dominators, compute_post_dominators
from .program import build_program
from .analysis.interproc import (
    program_callgraph, entrypoints, hubs, cycles, state_rows, trace_value,
)
from .refs import parse_ref, resolve


# -- exit-code signalling ------------------------------------------------

class StructuralError(Exception):
    """Target could not be located / produced no structure (exit 2)."""


class UnsupportedFeature(Exception):
    """A v1-unsupported Python feature was hit (exit 4)."""


# -- graph loading -------------------------------------------------------

def _enrich(g: FunctionGraph) -> FunctionGraph:
    attach_dis_info(g)
    correct_exception_edges(g)
    run_dataflow(g)
    g.attrs["dominators"] = compute_dominators(g)
    g.attrs["post_dominators"] = compute_post_dominators(g)
    from .analysis.control_dependence import compute_control_dependence
    g.attrs["control_deps"] = compute_control_dependence(g)
    return g


def _load_graph(target: str) -> FunctionGraph:
    if target.startswith("live:"):
        _, rest = target.split(":", 1)
        if ":" not in rest:
            raise StructuralError("live target must be live:module:qualname")
        mod, qual = rest.rsplit(":", 1)
        import importlib
        m = importlib.import_module(mod)
        obj = m
        for part in qual.split("."):
            obj = getattr(obj, part)
        return _enrich(build_cfg_from_live(obj))

    path, qual = (target.rsplit(":", 1) if ":" in target else (target, None))
    p = Path(path)
    if not p.exists():
        raise StructuralError(f"file not found: {path}")
    if qual is None:
        raise StructuralError("module-level CFG not implemented; use file.py:func")

    src = p.read_text(encoding="utf-8")
    try:
        g = build_cfg_from_source(src, qual, source_path=str(p))
    except SyntaxError as e:
        raise UnsupportedFeature(f"could not parse {path}: {e}") from e
    except ValueError as e:
        raise StructuralError(str(e)) from e
    return _enrich(g)


def _load_program(target: str, use_cache: bool = True):
    """Build a ProgramGraph from a directory/package or a single file."""
    path = target
    if not os.path.exists(path) and ":" in target:
        path = target.split(":", 1)[0]
    if not os.path.exists(path):
        raise StructuralError(f"path not found: {path}")
    pg = build_program(path, use_cache=use_cache)
    if not pg.functions:
        detail = f" ({len(pg.errors)} parse error(s))" if pg.errors else ""
        raise StructuralError(f"no functions found in {path}{detail}")
    return pg


# -- argument parsing ----------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pflow",
        description="Explicit program flow graphs for Python code review agents.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_agent_flags(sp):
        sp.add_argument("-a", "--agent", action="store_true",
                        help="dense agent output (default)")
        sp.add_argument("--human", action="store_true",
                        help="decorated human-readable output")

    p = sub.add_parser("cfg", help="Emit control flow graph")
    p.add_argument("target", help="file.py:func or live:module:qualname")
    p.add_argument("--view", choices=["source", "bytecode"], default="source")
    add_agent_flags(p)

    p = sub.add_parser("dataflow", help="Reaching definitions / def-use")
    p.add_argument("target")
    p.add_argument("--var", help="focus on this name")
    add_agent_flags(p)

    tgt_help = "file.py:func (optional if the --from ref embeds path:qualname)"
    p = sub.add_parser("walk", help="Graph traversal from a ref")
    p.add_argument("target", nargs="?", help=tgt_help)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--direction", choices=["forward", "backward", "both"], default="forward")
    p.add_argument("--edges", choices=["control", "data", "both"], default="both")
    add_agent_flags(p)

    p = sub.add_parser("slice", help="Program slice from a ref")
    p.add_argument("target", nargs="?", help=tgt_help)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--backward", action="store_true")
    p.add_argument("--forward", action="store_true")
    p.add_argument("--depth", type=int, default=5)
    add_agent_flags(p)

    p = sub.add_parser("metrics", help="Structural metrics")
    p.add_argument("target")
    add_agent_flags(p)

    p = sub.add_parser("paths", help="Find paths between two refs")
    p.add_argument("target", nargs="?", help=tgt_help)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="goal", required=True)
    p.add_argument("--depth", type=int, default=8)

    p = sub.add_parser("callgraph", help="Cross-file call graph + control architecture")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--focus", help="show the call subtree reachable from this fqname")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--no-cache", action="store_true", help="ignore the on-disk program-graph cache")
    add_agent_flags(p)

    p = sub.add_parser("state", help="Program-wide shared-state / coupling map")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--name", help="filter to cells whose key contains this string")
    p.add_argument("--no-cache", action="store_true", help="ignore the on-disk program-graph cache")
    add_agent_flags(p)

    p = sub.add_parser("trace", help="Trace a value's flow across functions")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--value", required=True, help="the variable/param name to trace")
    p.add_argument("--from", dest="origin", help="fqname (relpath:qualname) to start at")
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--no-cache", action="store_true", help="ignore the on-disk program-graph cache")
    add_agent_flags(p)

    p = sub.add_parser("show", help="Expand a reference")
    p.add_argument("ref")
    p.add_argument("target", nargs="?", help=tgt_help)

    p = sub.add_parser("report",
                       help="Positional census: structural coordinates + targets to walk (no verdicts)")
    p.add_argument("target")

    p = sub.add_parser("verify",
                       help="Check a proposed refactor preserves flow + reduces complexity")
    p.add_argument("old", help="original  file.py:func")
    p.add_argument("new", help="refactored file.py:func")

    p = sub.add_parser("opportunities",
                       help="Run simplification passes; emit ranked, tagged opportunities")
    p.add_argument("target", nargs="?", help="file.py:func, a file.py, a directory, or live:module:qualname")
    p.add_argument("--pass", dest="passes", action="append", metavar="NAME",
                   help="run only this pass (repeatable; NAME,NAME also ok). "
                        "Implies --by-pass. See --list-passes")
    p.add_argument("--by-pass", action="store_true",
                   help="group findings under each pass instead of one merged ranked list")
    p.add_argument("--list-passes", action="store_true", help="list available passes and exit")
    p.add_argument("--no-cache", action="store_true", help="ignore the program-graph cache (dir/file mode)")
    add_agent_flags(p)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dense = not getattr(args, "human", False)

    try:
        return _dispatch(args, dense)
    except StructuralError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except UnsupportedFeature as e:
        print(f"unsupported: {e}", file=sys.stderr)
        return 4
    except Exception as e:  # noqa: BLE001 — surface anything else as a generic error
        print(f"error: {e}", file=sys.stderr)
        return 1


def _target_from_ref(ref_str: str) -> Optional[str]:
    """Derive a file.py:qualname target from a ref that embeds path+qualname."""
    r = parse_ref(ref_str)
    if r.path and r.qualname:
        return f"{r.path}:{r.qualname}"
    if r.path:
        return r.path
    return None


def _resolve_target(explicit: Optional[str], *ref_strs: str) -> str:
    if explicit:
        return explicit
    for ref_str in ref_strs:
        derived = _target_from_ref(ref_str)
        if derived:
            return derived
    raise StructuralError(
        "no target given and the ref has no path:qualname prefix; "
        "pass file.py:func explicitly")


def _dispatch(args, dense: bool) -> int:
    cmd = args.cmd

    if cmd == "cfg":
        g = _load_graph(args.target)
        if args.view == "bytecode":
            _print_bytecode_view(g, args.target)
        elif dense:
            print(format_cfg_agent(g, include_dataflow=True, include_dominance=True))
        else:
            _print_cfg_human(g)

    elif cmd == "dataflow":
        g = _load_graph(args.target)
        _print_dataflow(g, args.var)

    elif cmd == "walk":
        g = _load_graph(_resolve_target(args.target, args.start))
        steps = walk(g, start=args.start, direction=args.direction,
                     edges=args.edges, max_depth=args.depth)
        print(format_walk(steps, dense=dense, graph=g))

    elif cmd == "slice":
        g = _load_graph(_resolve_target(args.target, args.start))
        direction = "forward" if args.forward and not args.backward else "backward"
        steps = walk(g, start=args.start, direction=direction,
                     edges="data", max_depth=args.depth)
        print(format_walk(steps, dense=dense, graph=g))

    elif cmd == "metrics":
        g = _load_graph(args.target)
        _print_metrics(g)

    elif cmd == "paths":
        g = _load_graph(_resolve_target(args.target, args.start, args.goal))
        paths = find_paths(g, start=args.start, goal=args.goal, max_depth=args.depth)
        if not paths:
            print("no paths found (within depth limit)")
        else:
            print(f"{len(paths)} path(s):")
            for pth in paths:
                print("  " + " -> ".join(f"bb{b}" for b in pth))

    elif cmd == "callgraph":
        pg = _load_program(args.target, use_cache=not args.no_cache)
        _print_callgraph(pg, focus=args.focus, depth=args.depth)

    elif cmd == "state":
        pg = _load_program(args.target, use_cache=not args.no_cache)
        _print_state(pg, args.name)

    elif cmd == "trace":
        pg = _load_program(args.target, use_cache=not args.no_cache)
        _print_trace(pg, args.value, args.origin, args.depth)

    elif cmd == "show":
        g = _load_graph(_resolve_target(args.target, args.ref))
        _print_show(args.ref, g)

    elif cmd == "report":
        g = _load_graph(args.target)
        _print_report(g)

    elif cmd == "verify":
        _print_verify(_load_graph(args.old), _load_graph(args.new))

    elif cmd == "opportunities":
        from .analysis.opportunities import (
            run_passes, run_passes_grouped, format_opportunities,
            format_grouped, PASS_CATALOG, PASS_NAMES)
        if args.list_passes:
            for name, desc in PASS_CATALOG:
                print(f"  {name:18} {desc}")
            return 0
        only = None
        if args.passes:
            only = [s.strip() for chunk in args.passes for s in chunk.split(",") if s.strip()]
            unknown = [n for n in only if n not in PASS_NAMES]
            if unknown:
                print(f"unknown pass(es): {', '.join(unknown)}", file=sys.stderr)
                print(f"available: {', '.join(PASS_NAMES)}", file=sys.stderr)
                return 2
        if not args.target:
            print("opportunities: a target is required (or use --list-passes)", file=sys.stderr)
            return 2
        grouped = args.by_pass or only is not None
        if _is_single_function_target(args.target):
            g = _load_graph(args.target)
            if grouped:
                print(format_grouped(g, run_passes_grouped(g, only)))
            else:
                print(format_opportunities(g, run_passes(g)))
        else:
            pg = _load_program(args.target, use_cache=not args.no_cache)
            _print_program_opportunities(pg, only=only)

    return 0


def _is_single_function_target(target: str) -> bool:
    if target.startswith("live:"):
        return True
    if ":" in target:
        path = target.split(":", 1)[0]
        return os.path.isfile(path)  # file.py:func -> single; bare dir/file -> program
    return False


# -- renderers -----------------------------------------------------------

def _print_cfg_human(g: FunctionGraph) -> None:
    print(g)
    for b in g.blocks:
        print(f"  bb{b.id}: ops={[o.kind for o in b.ops]} "
              f"succs={b.succs} except={b.except_succs} preds={b.preds}")


def _print_bytecode_view(g: FunctionGraph, target: str) -> None:
    """Show the dis-derived block view (the control-flow oracle, RFC §7.3)."""
    from .bytecode import get_basic_blocks, get_exception_table, offset_to_line_map

    code = g.attrs.get("code_object") or _compile_target_code(target)
    if code is None:
        raise UnsupportedFeature(
            "bytecode view needs a resolvable code object "
            "(use a live: target, or a top-level function/method)")

    blocks = get_basic_blocks(code)
    o2l = offset_to_line_map(code)
    print(f"BYTECODE {g.qualname} ({len(blocks)} dis-blocks)")
    for start, instrs in blocks:
        last = instrs[-1]
        line = o2l.get(start)
        print(f"  @{start} line={line} "
              f"[{instrs[0].opname}..{last.opname}] n={len(instrs)}")
    et = get_exception_table(code)
    if et:
        print("  exception_table:")
        for e in et:
            print(f"    {e}")


def _compile_target_code(target: str):
    from types import CodeType
    if target.startswith("live:"):
        return None
    path, qual = (target.rsplit(":", 1) if ":" in target else (target, None))
    try:
        src = Path(path).read_text(encoding="utf-8")
        module_code = compile(src, path, "exec")
    except (OSError, SyntaxError):
        return None
    last = qual.split(".")[-1] if qual else None

    def find(code) -> Optional[object]:
        for const in code.co_consts:
            if isinstance(const, CodeType):
                if getattr(const, "co_qualname", None) == qual or const.co_name == last:
                    return const
                nested = find(const)
                if nested is not None:
                    return nested
        return None

    return find(module_code)


def _print_dataflow(g: FunctionGraph, var: Optional[str]) -> None:
    name_of = {d.id: d.name for d in g.defs}
    if var:
        defs = [d for d in g.defs if d.name == var]
        uses = [u for u in g.uses if u.name == var]
        print(f"defs of {var}: {len(defs)}")
        for d in defs:
            ln = f"@{d.source[0]}" if d.source else ""
            print(f"  def:{d.id} {d.name}{ln} (op {d.op_id}, bb {d.block_id})")
        print(f"uses of {var}: {len(uses)}")
        for u in uses:
            reached = ", ".join(f"def:{i}" for i in u.reaching_defs) or "(none)"
            print(f"  use:{u.id} (op {u.op_id}, bb {u.block_id}) <- {reached}")
    else:
        print(f"defs={len(g.defs)} uses={len(g.uses)}")
        for u in g.uses:
            if u.reaching_defs:
                src = ", ".join(f"{name_of[i]}:def:{i}" for i in u.reaching_defs)
                print(f"  use:{u.id} {u.name} (bb{u.block_id}) <- {src}")


def _print_metrics(g: FunctionGraph) -> None:
    # Positional measurement only: numbers, no threshold verdicts. The
    # "this is a hotspot (> N)" judgment lives in `opportunities`, not here.
    from .analysis.complexity import compute_metrics
    m = compute_metrics(g)
    doms = g.attrs.get("dominators") or compute_dominators(g)
    back_edges = sum(1 for b in g.blocks for s in b.succs if s in doms.get(b.id, set()))

    print(f"blocks={m['blocks']} ops={len(g.ops)} exits={m['exits']}")
    print(f"control_edges={m['control_edges']} branches={m['branches']} "
          f"loops(back_edges)={back_edges}")
    print(f"cyclomatic={m['cyclomatic']}")
    print(f"cognitive={m['cognitive']}")
    print(f"max_nesting={m['max_nesting']}")
    print(f"depdegree={m['depdegree']}  max_live_span={m['max_live_span']}")
    print(f"defs={m['defs']} uses={m['uses']}")


def _print_report(g: FunctionGraph) -> None:
    # The IR-layer on-ramp: a positional census (coordinates + targets to
    # walk), not a verdict. See format_report_agent.
    from .format.agent import format_report_agent
    print(format_report_agent(g))


def _resolve_fqname(pg, needle: str) -> Optional[str]:
    if needle in pg.functions:
        return needle
    hits = [fq for fq in pg.functions if fq.endswith(needle) or fq.split(":")[-1] == needle]
    return hits[0] if hits else None


def _print_callgraph(pg, focus: Optional[str], depth: int) -> None:
    cg = program_callgraph(pg)
    n_edges = sum(len(v) for v in cg.edges.values())

    if focus:
        root = _resolve_fqname(pg, focus)
        if root is None:
            raise StructuralError(f"focus not found: {focus}")
        print(f"callgraph from {root} (depth {depth}):")
        seen = set()

        def walk(fq, d, prefix):
            short = fq.split(":")[-1]
            mark = " *recursion" if fq in seen else ""
            print(f"{prefix}{short}{mark}")
            if fq in seen or d >= depth:
                return
            seen.add(fq)
            for c in sorted(cg.edges.get(fq, ())):
                walk(c, d + 1, prefix + "  ")

        walk(root, 0, "  ")
        return

    print(f"PROGRAM callgraph: {pg.root}")
    print(f"  {len(pg.functions)} funcs, {n_edges} edges, {len(pg.errors)} parse error(s)")
    eps = entrypoints(cg)
    print(f"entrypoints ({len(eps)}): " +
          ", ".join(e.split(':')[-1] for e in eps[:15]) + (" ..." if len(eps) > 15 else ""))
    print("hubs (fan-in <- / fan-out ->):")
    for fq, fi, fo in hubs(cg, 12):
        if fi or fo:
            print(f"  {fi:3}<- {fo:3}->  {fq}")
    cy = cycles(cg)
    print(f"cycles ({len(cy)}):")
    for c in cy[:10]:
        names = [x.split(':')[-1] for x in c]
        label = "recursive: " + names[0] if len(c) == 1 else " <-> ".join(names[:6])
        print(f"  {label}")


def _print_state(pg, name: Optional[str]) -> None:
    rows = state_rows(pg, focus=name)
    flagged = [c for c in rows if c.flags]
    print(f"STATE map: {pg.root}  ({len(rows)} cells, {len(flagged)} flagged)")
    for c in rows[:30]:
        writers = sorted(w.split(':')[-1] for w in c.writers)
        wlist = ",".join(writers[:4]) + (" ..." if len(writers) > 4 else "")
        fl = " [" + ",".join(c.flags) + "]" if c.flags else ""
        print(f"  {c.name}  W{len(c.writers)}=[{wlist}] R{len(c.readers)}{fl}")


def _print_trace(pg, value: str, origin: Optional[str], depth: int) -> None:
    origin_fq = None
    if origin:
        origin_fq = _resolve_fqname(pg, origin)
        if origin_fq is None:
            raise StructuralError(f"--from origin not found: {origin}")
    steps = trace_value(pg, value, origin=origin_fq, max_depth=depth)
    src = origin_fq or "(all defs)"
    print(f"TRACE value={value} from={src}  "
          f"({len(steps)} hops, conservative forward may-flow)")
    for s in steps:
        short = s.fq.split(":")[-1]
        detail = f"  {s.detail}" if s.detail else ""
        print("  " + "  " * s.depth + f"[{s.kind}] {short}:{s.var}{detail}")


def _print_verify(old: FunctionGraph, new: FunctionGraph) -> None:
    from .analysis.verify import verify_refactor
    r = verify_refactor(old, new)
    print(f"VERIFY {old.qualname} -> {new.qualname}")
    if r["behavior_ok"]:
        print("  behavior: PRESERVED — each output depends on the same external "
              "inputs (conservative flow-equivalence check, not a proof)")
    else:
        print("  behavior: CHANGED — outputs now depend on different inputs:")
        for label, removed, added in r["diffs"]:
            chg = (f" -{removed}" if removed else "") + (f" +{added}" if added else "")
            print(f"    {label}:{chg}")
    print("  complexity:")
    for k, (o, n) in r["deltas"].items():
        arrow = "v" if n < o else ("^" if n > o else "=")
        print(f"    {k}: {o} -> {n} {arrow}")
    if r["behavior_ok"] and r["simpler"]:
        verdict = "SAFE + SIMPLER"
    elif r["behavior_ok"]:
        verdict = "SAFE (no complexity reduction)"
    else:
        verdict = "BEHAVIOR MAY HAVE CHANGED — review before applying"
    print(f"  verdict: {verdict}")


def _print_program_opportunities(pg, only=None) -> None:
    from collections import Counter
    from .analysis.opportunities import run_passes
    rows = []  # (opportunity, fqname)
    for fq, g in pg.functions.items():
        for o in run_passes(g):
            if only is not None and o.pass_name not in only:
                continue
            rows.append((o, fq))

    rank = {("sound", "must"): 0, ("sound", "may"): 1,
            ("heuristic", "must"): 2, ("heuristic", "may"): 3}
    rows.sort(key=lambda r: (rank.get((r[0].soundness, r[0].modality), 9),
                             r[1], r[0].line or 0))
    by_pass = Counter(o.pass_name for o, _ in rows)
    print(f"PROGRAM opportunities: {pg.root}")
    print(f"  {len(pg.functions)} functions, {len(rows)} findings, "
          f"{len(pg.errors)} parse error(s)")
    print("  by pass: " + ", ".join(f"{p}={n}" for p, n in by_pass.most_common()))
    cap = 60
    for o, fq in rows[:cap]:
        print(f"  [{o.soundness}/{o.modality}] {o.pass_name}: {o.title}  ({fq})")
    if len(rows) > cap:
        print(f"  ... +{len(rows) - cap} more")


def _print_show(ref: str, g: FunctionGraph) -> None:
    r = parse_ref(ref)
    info = resolve(g, r)
    print(f"ref: {ref}")
    if not info.get("found"):
        print(f"  {info.get('error', 'not found in graph')}")
        return
    if "block" in info:
        b = info["block"]
        print(f"  bb{b.id}: ops={[o.kind for o in b.ops]} "
              f"succs={b.succs} except={b.except_succs} preds={b.preds}")
        rin = b.attrs.get("reaching_in")
        if rin:
            names = sorted({d.name for d in g.defs if d.id in rin})
            print(f"  reaching_in={names}")
    elif "op" in info:
        o = info["op"]
        print(f"  op{o.id} {o.kind} targets={o.targets} uses={o.uses} (bb {_op_block(g, o.id)})")
    elif "def" in info:
        d = info["def"]
        ln = f"@{d.source[0]}" if d.source else ""
        print(f"  def:{d.id} {d.name}{ln} (op {d.op_id}, bb {d.block_id})")
    elif "use" in info:
        u = info["use"]
        reached = ", ".join(f"def:{i}" for i in u.reaching_defs) or "(none)"
        print(f"  use:{u.id} {u.name} (op {u.op_id}, bb {u.block_id}) <- {reached}")


def _op_block(g: FunctionGraph, op_id: int):
    for b in g.blocks:
        if any(o.id == op_id for o in b.ops):
            return b.id
    return None


if __name__ == "__main__":
    sys.exit(main())
