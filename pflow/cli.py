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
from .analysis.program import build_program
from .analysis.interproc import (
    program_callgraph, entrypoints, hubs, cycles, state_rows, trace_value,
)
from .ir.refs import parse_ref, resolve


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
        raise StructuralError(
            f"target names a whole file — run `pflow report {path}` for the "
            f"per-file census, or name one function: {path}:func")

    src = p.read_text(encoding="utf-8")
    try:
        g = build_cfg_from_source(src, qual, source_path=str(p))
    except SyntaxError as e:
        raise UnsupportedFeature(f"could not parse {path}: {e}") from e
    except ValueError as e:
        raise StructuralError(str(e)) from e
    return _enrich(g)


def _load_program(target: str, use_cache: bool = True, exclude_tests: bool = False):
    """Build a ProgramGraph from a directory/package, a single file, or an
    installed module via `live:modname` (resolved through importlib, so any
    importable library can be analyzed without knowing where pip put it)."""
    if target.startswith("live:"):
        path = _module_source_root(target.split(":", 1)[1])
    else:
        path = target
        if not os.path.exists(path) and ":" in target:
            path = target.split(":", 1)[0]
    if not os.path.exists(path):
        raise StructuralError(f"path not found: {path}")
    pg = build_program(path, use_cache=use_cache, exclude_tests=exclude_tests)
    if not pg.functions:
        detail = f" ({len(pg.errors)} parse error(s))" if pg.errors else ""
        raise StructuralError(f"no functions found in {path}{detail}")
    return pg


# -- argument parsing ----------------------------------------------------

def _module_source_root(modname: str) -> str:
    """Source dir/file of an importable module, without importing it."""
    import importlib.util
    try:
        spec = importlib.util.find_spec(modname)
    except (ImportError, ValueError) as e:
        raise StructuralError(f"cannot resolve module {modname}: {e}") from e
    if spec is None:
        raise StructuralError(f"module not found: {modname}")
    if spec.submodule_search_locations:
        return next(iter(spec.submodule_search_locations))
    if spec.origin and spec.origin.endswith(".py"):
        return spec.origin
    raise StructuralError(
        f"{modname} has no Python source (builtin or C extension)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pflow",
        description="Explicit program flow graphs for Python code review agents.",
        epilog=(
            "workflow: `pflow report DIR` orients (positions, no verdicts); "
            "walk its `targets →` with report/cfg/slice/walk; use the "
            "debugging views (impact/catches/raises/imports/classes, "
            "callgraph --to) for specific questions; run `opportunities` "
            "last as a cross-check and confirm each ref in the graph. "
            "Program targets: a directory, a file, or live:modname "
            "(any importable library)."),
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

    def add_program_flags(sp):
        sp.add_argument("--no-cache", action="store_true",
                        help="ignore the on-disk program-graph cache")
        sp.add_argument("--exclude-tests", action="store_true",
                        help="skip test_* files and tests/ directories")

    p = sub.add_parser("callgraph", help="Cross-file call graph + control architecture")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--focus", help="show the call subtree reachable from this fqname")
    p.add_argument("--sharp", action="store_true",
                   help="focus tree: follow only unambiguous (k==1) edges — "
                        "cuts same-name smear on duck-typed codebases")
    p.add_argument("--to", dest="goal",
                   help="enumerate call chains reaching this fqname "
                        "(from --focus if given, else from the entrypoints)")
    p.add_argument("--depth", type=int, default=3)
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("impact",
                       help="Blast radius of a change: reverse call closure + state coupling")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--focus", action="append", metavar="FQNAME",
                   help="changed/inspected function (repeatable; suffix match ok)")
    p.add_argument("--diff", nargs="?", const="HEAD", metavar="REV",
                   help="derive focus functions from `git diff REV` (default HEAD)")
    p.add_argument("--depth", type=int, default=4)
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("state", help="Program-wide shared-state / coupling map")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--name", help="filter to cells whose key contains this string")
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("trace", help="Trace a value's flow across functions")
    p.add_argument("target", help="a directory/package, or a single file.py")
    p.add_argument("--value", required=True, help="the variable/param name to trace")
    p.add_argument("--from", dest="origin", help="fqname (relpath:qualname) to start at")
    p.add_argument("--depth", type=int, default=6)
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("classes",
                       help="Class structure: inheritance forest, method overrides, external bases")
    p.add_argument("target", help="a directory/package, a file.py, or live:modname")
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("raises",
                       help="Exception escape: what can calling a function raise "
                            "(explicit raises propagated over sharp call edges)")
    p.add_argument("target", help="file.py:func, a file.py, a directory, or live:modname")
    p.add_argument("--focus", metavar="FQNAME",
                   help="show this function's escape set with witness chains")
    p.add_argument("--implicit", action="store_true",
                   help="also count language-level sources: subscript loads "
                        "(KeyError/IndexError) and attribute access "
                        "(AttributeError), function-granular")
    p.add_argument("--json", action="store_true",
                   help="program mode: one JSON object per escaping function")
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("imports",
                       help="Module import graph: layering, cycles, hubs, external deps")
    p.add_argument("target", help="a directory/package, or live:modname")
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("catches",
                       help="Exception-handler census: what each except catches and how it exits "
                            "(re-raise / raise-new / return / swallow)")
    p.add_argument("target", help="file.py:func, a file.py, or a directory")
    p.add_argument("--json", action="store_true",
                   help="one JSON object per handler")
    add_program_flags(p)
    add_agent_flags(p)

    p = sub.add_parser("at",
                       help="Anchor at file.py:LINE (a traceback frame): owning function, "
                            "the line's ops, its guard chain, reaching defs, callers")
    p.add_argument("target", help="file.py:LINE — exactly as a traceback frame gives it")
    p.add_argument("--in", dest="scope", metavar="DIR",
                   help="program scope: also show call chains reaching the function")
    p.add_argument("--depth", type=int, default=8)
    p.add_argument("--no-cache", action="store_true",
                   help="ignore the program-graph cache (--in mode)")

    p = sub.add_parser("show", help="Expand a reference")
    p.add_argument("ref")
    p.add_argument("target", nargs="?", help=tgt_help)

    p = sub.add_parser("report",
                       help="Positional census: structural coordinates + targets to walk (no verdicts). "
                            "Takes a function, a file, or a directory — start here")
    p.add_argument("target", help="file.py:func for one function; a file.py or directory for the program census")
    p.add_argument("--json", action="store_true",
                   help="program mode: one JSON object per function (structure, "
                        "metrics, fan, handlers) for programmatic queries")
    add_program_flags(p)

    p = sub.add_parser("verify",
                       help="Check a proposed refactor preserves flow + reduces complexity")
    p.add_argument("old", help="original  file.py:func")
    p.add_argument("new", help="refactored file.py:func")

    p = sub.add_parser("opportunities", aliases=["check"],
                       help="Run simplification passes; emit ranked, tagged opportunities "
                            "(alias: check)")
    p.add_argument("target", nargs="?", help="file.py:func, a file.py, a directory, or live:module:qualname")
    p.add_argument("--pass", dest="passes", action="append", metavar="NAME",
                   help="run only this pass (repeatable; NAME,NAME also ok). "
                        "Implies --by-pass. See --list-passes")
    p.add_argument("--by-pass", action="store_true",
                   help="group findings under each pass instead of one merged ranked list")
    p.add_argument("--list-passes", action="store_true", help="list available passes and exit")
    add_program_flags(p)
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
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
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
    cmd = "opportunities" if args.cmd == "check" else args.cmd

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
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        if args.goal:
            _print_call_paths(pg, args.goal, args.focus, args.depth)
        else:
            _print_callgraph(pg, focus=args.focus, depth=args.depth,
                             sharp=args.sharp)

    elif cmd == "impact":
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        _print_impact(pg, focus=args.focus, diff_rev=args.diff, depth=args.depth)

    elif cmd == "state":
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        _print_state(pg, args.name)

    elif cmd == "trace":
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        _print_trace(pg, args.value, args.origin, args.depth)

    elif cmd == "classes":
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        _print_classes(pg)

    elif cmd == "raises":
        if _is_single_function_target(args.target):
            from .analysis.raises import analyze_function
            g = _load_graph(args.target)
            fr = analyze_function(None, None, g.qualname, g,
                                  implicit=args.implicit)
            print(f"RAISES: {g.qualname}  (local explicit raises only — "
                  f"give a directory for interprocedural escape)")
            if fr.local:
                for exc in sorted(fr.local):
                    print(f"  escapes  {exc}  @L{fr.local_lines.get(exc, 0)}")
            else:
                print("  escapes  none (no uncaught explicit raise)")
            if fr.caught_local:
                print(f"  caught in own try: {', '.join(sorted(fr.caught_local))}")
        else:
            pg = _load_program(args.target, use_cache=not args.no_cache,
                               exclude_tests=args.exclude_tests)
            if args.json and not args.focus:
                import json as _json
                from .analysis.raises import program_raises
                pr = program_raises(pg, implicit=args.implicit)
                for fq in sorted(fq for fq, e in pr.escapes.items() if e):
                    relpath, qual = fq.split(":", 1)
                    print(_json.dumps({
                        "path": pg.display_path(relpath), "qualname": qual,
                        "escapes": sorted(pr.escapes[fq])}, ensure_ascii=False))
                return 0
            _print_raises(pg, args.focus, implicit=args.implicit)

    elif cmd == "imports":
        pg = _load_program(args.target, use_cache=not args.no_cache,
                           exclude_tests=args.exclude_tests)
        _print_imports(pg)

    elif cmd == "catches":
        from .analysis.catches import function_handlers, program_handlers
        if _is_single_function_target(args.target):
            g = _load_graph(args.target)
            path = args.target.rsplit(":", 1)[0] if ":" in args.target else ""
            fq = f"{path}:{g.qualname}" if path else g.qualname
            _print_catches({fq: g}, header=fq, handlers=function_handlers(fq, g))
        else:
            pg = _load_program(args.target, use_cache=not args.no_cache,
                               exclude_tests=args.exclude_tests)
            hs = []
            for fq, g in pg.functions.items():
                relpath, qual = fq.split(":", 1)
                shown = fq if pg.out_of_tree else f"{pg.display_path(relpath)}:{qual}"
                hs.extend(function_handlers(shown, g))
            hs.sort(key=lambda h: (h.fq, h.line or 0))
            if args.json:
                import json as _json
                for h in hs:
                    print(_json.dumps({
                        "fq": h.fq, "block": h.block_id, "line": h.line,
                        "catches": h.exc_type, "binds": h.binds,
                        "exit": h.exit, "broad": h.broad,
                        "sets": h.sets, "calls": h.calls}, ensure_ascii=False))
                return 0
            name = (os.path.basename(pg.root.rstrip(os.sep)) if pg.out_of_tree
                    else pg.display_root())
            _print_catches(pg.functions, header=name, handlers=hs,
                           root=pg.root if pg.out_of_tree else None)

    elif cmd == "at":
        _print_at(args.target, scope=args.scope, depth=args.depth,
                  use_cache=not args.no_cache)

    elif cmd == "show":
        g = _load_graph(_resolve_target(args.target, args.ref))
        _print_show(args.ref, g)

    elif cmd == "report":
        if _is_single_function_target(args.target):
            _print_report(_load_graph(args.target))
        else:
            pg = _load_program(args.target, use_cache=not args.no_cache,
                               exclude_tests=args.exclude_tests)
            if args.json:
                _print_report_json(pg)
            else:
                from .format.agent import format_program_report_agent
                print(format_program_report_agent(pg))

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
            src_lines = _source_lines_for(args.target)
            if grouped:
                groups = run_passes_grouped(g, only)
                if src_lines:
                    from .analysis.opportunities import filter_suppressed
                    groups = [(n, filter_suppressed(f, src_lines)[0]) for n, f in groups]
                print(format_grouped(g, groups, target=args.target))
            else:
                opps = run_passes(g)
                if src_lines:
                    from .analysis.opportunities import filter_suppressed
                    opps, n_sup = filter_suppressed(opps, src_lines)
                    if n_sup:
                        print(f"({n_sup} finding(s) suppressed by `pflow: ok` pragma)")
                print(format_opportunities(g, opps, target=args.target))
        else:
            pg = _load_program(args.target, use_cache=not args.no_cache,
                               exclude_tests=args.exclude_tests)
            _print_program_opportunities(pg, only=only)

    return 0


def _source_lines_for(target: str) -> Optional[list]:
    if target.startswith("live:") or ":" not in target:
        return None
    path = target.rsplit(":", 1)[0]
    try:
        return Path(path).read_text(encoding="utf-8",
                                    errors="replace").splitlines()
    except OSError:
        return None


def _is_single_function_target(target: str) -> bool:
    if target.startswith("live:"):
        # live:module:qualname -> one function; live:module -> whole program
        return ":" in target.split(":", 1)[1]
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
    from .ir.bytecode import get_basic_blocks, get_exception_table, offset_to_line_map

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


def _print_callgraph(pg, focus: Optional[str], depth: int,
                     sharp: bool = False) -> None:
    cg = program_callgraph(pg)
    n_edges = sum(len(v) for v in cg.edges.values())

    if focus:
        root = _resolve_fqname(pg, focus)
        if root is None:
            raise StructuralError(f"focus not found: {focus}")
        note = "; sharp edges only" if sharp else "; ~k = resolution fan-out"
        print(f"callgraph from {root} (depth {depth}{note}):")
        seen = set()

        def walk(fq, d, prefix):
            short = fq.split(":")[-1]
            mark = " *recursion" if fq in seen else ""
            print(f"{prefix}{short}{mark}")
            if fq in seen or d >= depth:
                return
            seen.add(fq)
            for c in sorted(cg.edges.get(fq, ())):
                k = cg.ambiguity.get((fq, c), 1)
                if sharp and k > 1:
                    continue
                sub = f"{prefix}  "
                if k > 1:
                    short_c = c.split(":")[-1]
                    print(f"{sub}{short_c} ~{k}")
                    continue            # don't recurse through smear either way
                walk(c, d + 1, sub)

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


def _print_classes(pg) -> None:
    from collections import Counter
    from .analysis.classes import class_graph, roots, render_tree
    nodes = class_graph(pg)
    name = (os.path.basename(pg.root.rstrip(os.sep)) if pg.out_of_tree
            else pg.display_root())
    n_over = sum(len(n.overrides) for n in nodes.values())
    ext = Counter(b for n in nodes.values() for b in n.bases_out)
    print(f"CLASSES: {name}  ·  {len(nodes)} classes · "
          f"{sum(1 for n in nodes.values() if n.bases_in)} with in-program bases · "
          f"{n_over} overriding methods")
    if pg.out_of_tree:
        print(f"  root {pg.root}   (rows are root-relative)")
    if not nodes:
        return

    rts = roots(nodes)
    if rts:
        print("  hierarchy (method counts; * = overrides a base method):")
        seen: set = set()
        for r in rts:
            for depth, key, revisit in render_tree(nodes, r, seen=seen):
                n = nodes[key]
                if revisit:
                    print(f"    {'  ' * depth}{n.name}  ↑ (diamond — shown above)")
                    continue
                marked = sorted(m + "*" if m in n.overrides else m
                                for m in n.methods)
                shown = " ".join(marked[:8]) + (f" (+{len(marked) - 8})"
                                                if len(marked) > 8 else "")
                ext_note = ("  (also: " + ", ".join(n.bases_out) + ")"
                            if n.bases_out else "")
                print(f"    {'  ' * depth}{n.name}  [{n.relpath}]  "
                      f"{len(n.methods)}m{ext_note}  {shown}")

    flat = sorted((n for n in nodes.values() if not n.bases_in and not n.children),
                  key=lambda n: -len(n.methods))
    if flat:
        rows = " · ".join(f"{n.name}×{len(n.methods)}m" for n in flat[:10])
        more = f" (+{len(flat) - 10})" if len(flat) > 10 else ""
        print(f"  standalone ({len(flat)}): {rows}{more}")

    if ext:
        top = " ".join(f"{b}×{k}" for b, k in ext.most_common(10))
        print(f"  external bases: {top}")


def _print_report_json(pg) -> None:
    """One JSON object per function: the census as a queryable dataset, so an
    agent can `jq 'select(.swallow > 0 and .fan_in > 5)'` instead of parsing
    the table rendering."""
    import json as _json
    from .analysis.catches import function_handlers
    from .analysis.complexity import compute_metrics
    cg = program_callgraph(pg)
    for fq in sorted(pg.functions):
        g = pg.functions[fq]
        relpath, qual = fq.split(":", 1)
        m = compute_metrics(g)
        hs = function_handlers(fq, g)
        print(_json.dumps({
            "path": pg.display_path(relpath), "qualname": qual,
            "line": g.first_line,
            "blocks": m["blocks"], "branches": m["branches"], "exits": m["exits"],
            "cyclomatic": m["cyclomatic"], "cognitive": m["cognitive"],
            "nesting": m["max_nesting"], "depdegree": m["depdegree"],
            "live_span": m["max_live_span"], "defs": m["defs"], "uses": m["uses"],
            "fan_in": cg.fan_in(fq), "fan_out": cg.fan_out(fq),
            "handlers": len(hs),
            "swallow": sum(1 for h in hs if h.exit == "swallow"),
        }, ensure_ascii=False))


def _print_raises(pg, focus: Optional[str], implicit: bool = False) -> None:
    from .analysis.interproc import entrypoints, program_callgraph
    from .analysis.raises import program_raises
    pr = program_raises(pg, implicit=implicit)
    name = (os.path.basename(pg.root.rstrip(os.sep)) if pg.out_of_tree
            else pg.display_root())

    def disp(fq: str) -> str:
        return fq if pg.out_of_tree else \
            f"{pg.display_path(fq.split(':', 1)[0])}:{fq.split(':', 1)[1]}"

    if focus:
        fq = _resolve_fqname(pg, focus)
        if fq is None:
            raise StructuralError(f"--focus not found: {focus}")
        excs = sorted(pr.escapes.get(fq, ()))
        print(f"RAISES: {disp(fq)}  ·  {len(excs)} escaping type(s)"
              + (f"  [{pr.smeared_calls} smeared call edges not propagated]"
                 if pr.smeared_calls else ""))
        for exc in excs:
            chain = pr.chain(fq, exc)
            print(f"  {exc:<28} " + ("  ⇐  ".join(chain) if chain else ""))
        if not excs:
            print("  none — no explicit raise reaches this function over sharp edges")
        return

    raisers = {fq: e for fq, e in pr.escapes.items() if e}
    all_types = sorted({t for e in raisers.values() for t in e})
    print(f"RAISES: {name}  ·  {len(raisers)}/{len(pg.functions)} fn with escapes · "
          f"{len(all_types)} distinct types · {pr.smeared_calls} smeared call "
          f"edges not propagated")
    if pg.out_of_tree:
        print(f"  root {pg.root}   (rows are root-relative)")

    cg = program_callgraph(pg)
    eps = [fq for fq in entrypoints(cg) if fq in raisers]
    if eps:
        print("  entrypoint escape surface (what callers of the API can see):")
        for fq in sorted(eps, key=lambda f: -len(raisers[f]))[:15]:
            excs = sorted(raisers[fq])
            shown = " · ".join(excs[:6]) + (f" (+{len(excs) - 6})" if len(excs) > 6 else "")
            print(f"    {disp(fq) if not pg.out_of_tree else fq:<52} {shown}")

    rows = sorted(raisers.items(), key=lambda kv: -len(kv[1]))[:15]
    print("  widest escape sets:")
    for fq, excs in rows:
        e = sorted(excs)
        shown = " · ".join(e[:6]) + (f" (+{len(e) - 6})" if len(e) > 6 else "")
        print(f"    {fq if pg.out_of_tree else disp(fq):<52} {shown}")
    print(f"  drill down: pflow raises {pg.display_root()} --focus <qualname>")


def _print_imports(pg) -> None:
    from .analysis.imports import (
        module_import_graph, sccs, layers, fan, broken_cycles)
    eager, lazy, external = module_import_graph(pg)
    n_eager = sum(len(v) for v in eager.values())
    n_lazy = sum(len(v) for v in lazy.values())
    name = (os.path.basename(pg.root.rstrip(os.sep)) if pg.out_of_tree
            else pg.display_root())
    print(f"IMPORTS: {name}  ·  {len(eager)} modules · {n_eager} eager edges · "
          f"{n_lazy} lazy (function-level) · {len(external)} external roots")
    if pg.out_of_tree:
        print(f"  root {pg.root}   (rows are root-relative)")

    cy = sccs(eager)
    if cy:
        print(f"  cycles ({len(cy)})  — import-time, load-order hazards:")
        for comp in cy[:6]:
            print("    " + " ↔ ".join(comp))

    broken = broken_cycles(eager, lazy)
    if broken:
        print(f"  deferred cycles ({len(broken)})  — lazy edge closes an eager path back:")
        for a, b in broken[:8]:
            print(f"    {a} ⇢ {b}  (and {b} reaches {a} eagerly)")

    rows = [r for r in fan(eager) if r[1] or r[2]]
    if rows:
        cells = " · ".join(f"{fi}←{fo}→ {m}" for m, fi, fo in rows[:6])
        print(f"  hubs     {cells}")

    depth = layers(eager)
    by_depth: dict = {}
    for m, d in depth.items():
        by_depth.setdefault(d, []).append(m)
    print("  layers   (d0 = imports nothing in-program; eager edges only)")
    for d in sorted(by_depth):
        mods = sorted(by_depth[d])
        shown = " · ".join(mods[:8]) + (f" (+{len(mods) - 8})" if len(mods) > 8 else "")
        print(f"    d{d}  {shown}")

    if external:
        top = " ".join(f"{m}×{n}" for m, n in external.most_common(12))
        print(f"  external {top}")

    effectful = [(rp, m) for rp, m in sorted(pg.modules.items()) if m.side_effects]
    n_ca = sum(m.call_assigns for m in pg.modules.values())
    if effectful or n_ca:
        print(f"  import-time work (runs on `import`):"
              + (f"  [{n_ca} top-level call-assigns across the program]" if n_ca else ""))
        for rp, m in effectful[:10]:
            cells = " · ".join(f"L{ln} {desc}" for ln, desc in m.side_effects[:3])
            more = f" (+{len(m.side_effects) - 3})" if len(m.side_effects) > 3 else ""
            print(f"    {rp:<40} {cells}{more}")

    print("  by module (→ eager · ⇢ lazy):")
    for m in sorted(eager):
        if eager[m] or lazy[m]:
            tgts = sorted(eager[m])
            shown = " ".join(tgts[:8]) + (f" (+{len(tgts) - 8})" if len(tgts) > 8 else "")
            lz = sorted(lazy[m])
            if lz:
                shown += ("  ⇢ " if shown else "⇢ ") + " ".join(lz[:6]) \
                         + (f" (+{len(lz) - 6})" if len(lz) > 6 else "")
            print(f"    {m:<40} → {shown}")


def _print_catches(functions, header: str, handlers, root: Optional[str] = None) -> None:
    from .analysis.catches import census_counts
    n_fn = len({h.fq for h in handlers})
    c = census_counts(handlers)
    mix = " · ".join(f"{c[k]} {k}" for k in
                     ("swallow", "mixed", "return", "terminates",
                      "raise-new", "re-raise") if c.get(k))
    extra = " · ".join(f"{c[k]} {k}" for k in ("broad", "bare") if c.get(k))
    print(f"CATCHES: {header}  ·  {len(handlers)} handler(s) in {n_fn} fn"
          + (f"  ({mix})" if mix else "") + (f"  [{extra}]" if extra else ""))
    if root:
        print(f"  root {root}   (rows are root-relative)")
    if not handlers:
        return

    sections = (
        ("swallow", "swallow — handler neither raises nor returns; the error path continues:"),
        ("mixed",   "mixed exits (raises on some paths, not others):"),
        ("return",  "return — error converted to a return value:"),
        ("terminates", "terminates — handler exits the process (sys.exit/os._exit):"),
        ("raise-new", "raise-new — exception transformed/wrapped:"),
        ("re-raise", "re-raise — propagates the original:"),
    )
    for key, title in sections:
        rows = [h for h in handlers if h.exit == key]
        if not rows:
            continue
        print(f"  {title}")
        for h in rows:
            caught = h.exc_type or "<bare>"
            mark = ("  !bare" if h.exc_type is None
                    else "  !broad" if h.broad else "")
            binds = f" as {h.binds}" if h.binds else ""
            bits = [f"    {h.fq}:bb:{h.block_id}  @{h.line}  except {caught}{binds}{mark}"]
            if h.sets:
                bits.append(f"sets: {','.join(h.sets[:5])}")
            if h.calls:
                bits.append(f"calls: {','.join(h.calls[:5])}")
            print("  ".join(bits))


def _print_at(target: str, scope: Optional[str], depth: int,
              use_cache: bool = True) -> None:
    """The traceback-frame anchor: everything a debugger wants to know about
    one source line, in one shot. Tracebacks speak file:line; every other
    pflow command speaks file:qualname — this command is the adapter."""
    from .analysis.impact import function_spans, functions_at

    path, _, line_s = target.rpartition(":")
    if not path or not line_s.isdigit():
        raise StructuralError("at: target must be file.py:LINE (a traceback frame)")
    line = int(line_s)
    if not os.path.isfile(path):
        raise StructuralError(f"file not found: {path}")

    mini = _load_program(path, use_cache=use_cache)
    relpath = next(iter(mini.modules), None)
    hits = functions_at(mini, relpath, {line}) if relpath else set()
    if not hits:
        raise StructuralError(
            f"no function spans line {line} (module-level code is not lowered); "
            f"run `pflow report {path}` for the file census")
    qual = sorted(hits)[0].split(":", 1)[1]

    g = _load_graph(f"{path}:{qual}")
    spans = function_spans(mini)
    s, e = spans.get(sorted(hits)[0], (g.first_line, g.first_line))

    line_ops = [op for op in g.iter_ops()
                if op.source and op.source[0] <= line <= op.source[2]]
    blocks_of = {op.id: b.id for b in g.blocks for op in b.ops}
    anchor_bbs = sorted({blocks_of[op.id] for op in line_ops})
    print(f"AT {path}:{line} — {g.qualname}  (spans {s}-{e})  "
          + (f"bb{'/bb'.join(map(str, anchor_bbs))}" if anchor_bbs
             else "(line carries no ops — comment/blank?)"))

    if line_ops:
        cells = []
        for op in line_ops:
            sop = f"{op.id}:{op.kind}"
            if op.targets:
                sop += f"→{','.join(op.targets)}"
            if op.uses:
                sop += f"←{','.join(op.uses[:6])}"
            cells.append(sop)
        print(f"  ops      {'  '.join(cells)}")

    # guard chain: every branch this line is (transitively) control-dependent
    # on, innermost first, with the taken arm where determinable
    cd = g.attrs.get("control_deps", {})
    doms = g.attrs.get("dominators", {})
    block = {b.id: b for b in g.blocks}
    guards, frontier, seen = [], set(anchor_bbs), set()
    while frontier:
        nxt = set()
        for bid in frontier:
            for br in cd.get(bid, ()):
                if br in seen:
                    continue
                seen.add(br)
                nxt.add(br)
                bb = block.get(br)
                bop = next((o for o in bb.ops if o.kind == "branch"), None) if bb else None
                cond = (bop.attrs.get("condition") if bop else None) or \
                       (",".join(bop.uses[:3]) if bop and bop.uses else "?")
                ln = bop.source[0] if bop and bop.source else None
                arm = "?"
                if bb and len(bb.succs) >= 2:
                    for label, sid in (("True", bb.succs[0]), ("False", bb.succs[1])):
                        if sid in anchor_bbs or any(
                                sid == a or sid in doms.get(a, set())
                                for a in anchor_bbs):
                            arm = label
                            break
                guards.append((ln or 0, br, cond, arm))
        frontier = nxt
    if guards:
        guards.sort(reverse=True)               # innermost (closest line) first
        chain = "  ⇐  ".join(f"L{ln} `{c}`={arm} (bb{br})"
                             for ln, br, c, arm in guards[:6])
        more = f"  (+{len(guards) - 6})" if len(guards) > 6 else ""
        print(f"  guards   {chain}{more}")
    else:
        print("  guards   none — the line runs on every path through the function")

    # where the values used on this line come from
    ids = {op.id for op in line_ops}
    def_line = {d.id: (d.name, d.source[0] if d.source else None) for d in g.defs}
    prov: dict = {}
    for u in g.uses:
        if u.op_id in ids:
            lns = sorted({def_line[i][1] for i in u.reaching_defs
                          if i in def_line and def_line[i][1]})
            if lns:
                prov.setdefault(u.name, set()).update(lns)
    if prov:
        cells = " · ".join(
            f"{n} ← " + ",".join(f"L{x}" for x in sorted(ls)[:4])
            for n, ls in sorted(prov.items()) if "." not in n)
        if cells:
            print(f"  values   {cells}")

    if scope:
        from .analysis.interproc import call_paths
        pg = _load_program(scope, use_cache=use_cache)
        fq_rel = os.path.relpath(os.path.abspath(path), pg.root)
        fq = f"{fq_rel}:{qual}"
        if fq in pg.functions:
            cg = program_callgraph(pg)
            chains = call_paths(cg, fq, max_depth=depth, max_paths=12)
            # a debugger wants the production route; test entrypoints last
            chains.sort(key=lambda c: ("test" in c[0].lower(), len(c)))
            for chainlist in chains[:3]:
                hops = " -> ".join(x.split(":")[-1] for x in chainlist)
                print(f"  called   {hops}   [entry: {chainlist[0]}]")

    refs = [f"{path}:{qual}:bb:{b}" for b in anchor_bbs[:2]]
    refs += [f"{path}:{qual}:use:{n}@{line}" for n in list(prov)[:2] if "." not in n]
    if refs:
        print("  targets →  " + "  ".join(refs))


def _print_call_paths(pg, goal: str, origin: Optional[str], depth: int) -> None:
    from .analysis.interproc import call_paths
    cg = program_callgraph(pg)
    to_fq = _resolve_fqname(pg, goal)
    if to_fq is None:
        raise StructuralError(f"--to not found: {goal}")
    origin_fq = None
    if origin:
        origin_fq = _resolve_fqname(pg, origin)
        if origin_fq is None:
            raise StructuralError(f"--focus not found: {origin}")
    chains = call_paths(cg, to_fq, origin=origin_fq, max_depth=max(depth, 8))
    src = origin_fq or "entrypoints"
    print(f"CALL PATHS to {to_fq}  (from {src}; {len(chains)} chain(s), "
          f"shortest first; ~k = name-resolution fan-out)")
    if not chains:
        print("  none found — the callee may only be reached dynamically "
              "(dispatch table, getattr, framework callback)")
        return
    for chain in chains:
        hops = [chain[0].split(":")[-1]]
        for a, b in zip(chain, chain[1:]):
            mark = cg.edge_mark(a, b)
            if (a, b) in cg.via_arg:
                mark = (mark + "·fn") if mark else "fn"   # passed as a value
            hops.append(f"-{mark}-> {b.split(':')[-1]}" if mark
                        else f"-> {b.split(':')[-1]}")
        entry_note = "" if origin_fq else f"   [entry: {chain[0]}]"
        print("  " + " ".join(hops) + entry_note)


def _print_impact(pg, focus, diff_rev: Optional[str], depth: int) -> None:
    from .analysis.impact import (
        impact, changed_functions_from_git, function_spans)

    focus_lines = {}
    seeds = []
    if diff_rev:
        try:
            focus_lines = changed_functions_from_git(pg, diff_rev)
        except RuntimeError as e:
            raise StructuralError(f"--diff: {e}") from e
        seeds = sorted(focus_lines)
    for f in (focus or []):
        fq = _resolve_fqname(pg, f)
        if fq is None:
            raise StructuralError(f"--focus not found: {f}")
        if fq not in seeds:
            seeds.append(fq)
    if not seeds:
        raise StructuralError(
            "impact: give --focus FQNAME (repeatable) and/or --diff [REV]"
            + (f" — no .py changes vs {diff_rev} inside this root" if diff_rev else ""))

    rep = impact(pg, seeds, depth=depth)
    cg = rep.cg
    spans = function_spans(pg)

    def disp(fq: str) -> str:
        relpath, qual = fq.split(":", 1)
        return f"{pg.display_path(relpath)}:{qual}"

    n_up = sum(len(v) for v in rep.callers_by_depth.values())
    n_down = sum(len(v) for v in rep.callees_by_depth.values())
    root_disp = pg.display_root()
    via = f"--diff {diff_rev}" if diff_rev else "--focus"
    print(f"IMPACT: {root_disp}  ·  {len(seeds)} focus fn ({via}) · "
          f"closure ↑{n_up} ↓{n_down} · {len(rep.entry_reached)} entrypoint(s) reached")

    print("  focus:")
    for fq in rep.focus:
        s, e = spans.get(fq, (0, 0))
        lines_note = ""
        if fq in focus_lines:
            shown = sorted(focus_lines[fq])
            head = ",".join(map(str, shown[:8])) + ("…" if len(shown) > 8 else "")
            lines_note = f"   [changed lines {head}]"
        print(f"    {disp(fq)}  @{s}-{e}  fan {cg.fan_in(fq)}←{cg.fan_out(fq)}→"
              f"{lines_note}")

    def print_depth_rows(by_depth, edge_key) -> None:
        for d in sorted(by_depth):
            row = []
            for fq in by_depth[d]:
                mark = ""
                if d == 1:
                    k = max((cg.ambiguity.get(edge_key(fq, s), 1) for s in rep.focus),
                            default=1)
                    mark = f"~{k}" if k > 1 else ""
                row.append(disp(fq) + mark)
            label = f"d{d}"
            for i in range(0, len(row), 4):       # chunk: one giant line is unreadable
                print(f"    {label:<3} " + "  ".join(row[i:i + 4]))
                label = ""

    if rep.callers_by_depth:
        print(f"  upstream (callers, depth ≤{depth}) — can observe this change:")
        print_depth_rows(rep.callers_by_depth, lambda fq, s: (fq, s))
        if rep.entry_reached:
            print("    entry reached:  " + " · ".join(disp(f) for f in rep.entry_reached))
    else:
        print("  upstream: no in-program callers (entrypoint, handler, or dynamic dispatch)")

    if rep.callees_by_depth:
        print("  downstream (callees, depth ≤2) — what this code stands on:")
        print_depth_rows(rep.callees_by_depth, lambda fq, s: (s, fq))

    for label, rows in (("state written here, read outside focus", rep.writes_read_by),
                        ("state read here, written outside focus", rep.reads_written_by)):
        if rows:
            print(f"  {label}:")
            for cell, others in rows[:6]:
                shown = " · ".join(o.split(":")[-1] for o in others[:6])
                tail = f" (+{len(others) - 6})" if len(others) > 6 else ""
                print(f"    {cell}  ↔ {len(others)}: {shown}{tail}")

    if diff_rev:
        from .analysis.impact import structural_deltas, _DELTA_KEYS
        try:
            deltas = structural_deltas(pg, diff_rev)
        except RuntimeError:
            deltas = []
        if deltas:
            short = {"blocks": "bb", "branches": "br", "exits": "exit",
                     "cyclomatic": "cyc", "cognitive": "cog",
                     "max_live_span": "span"}
            print(f"  structural deltas ({diff_rev} → worktree; flow-identical edits omitted):")
            for fq, om, nm in deltas[:20]:
                if om is None:
                    note = "(added)  " + " ".join(
                        f"{short[k]} {nm[k]}" for k in _DELTA_KEYS)
                elif nm is None:
                    note = "(removed)"
                else:
                    note = " ".join(f"{short[k]} {om[k]}→{nm[k]}"
                                    for k in _DELTA_KEYS if om[k] != nm[k])
                print(f"    {disp(fq):<52} {note}")
            if len(deltas) > 20:
                print(f"    ... +{len(deltas) - 20} more")

    if rep.total_edges:
        print(f"  edges walked: {rep.total_edges} ({rep.smeared_edges} smeared ~k>1 "
              f"— name-resolution over-approximation)")

    targets, seen = [], set()
    for fq in rep.focus[:2] + [f for fqs in rep.callers_by_depth.values() for f in fqs][:2]:
        t = disp(fq)
        if t not in seen:
            seen.add(t)
            targets.append(t)
    if targets:
        print("  targets →  " + "  ".join(targets))


def _print_state(pg, name: Optional[str]) -> None:
    rows = state_rows(pg, focus=name)
    flagged = [c for c in rows if c.flags]
    print(f"STATE map: {pg.root}  ({len(rows)} cells, {len(flagged)} flagged)")
    if name and 0 < len(rows) <= 3:
        for c in rows:
            _print_state_cell(pg, c)
        return
    for c in rows[:30]:
        writers = sorted(w.split(':')[-1] for w in c.writers)
        wlist = ",".join(writers[:4]) + (" ..." if len(writers) > 4 else "")
        fl = " [" + ",".join(c.flags) + "]" if c.flags else ""
        print(f"  {c.name}  W{len(c.writers)}=[{wlist}] R{len(c.readers)}{fl}")
    if name and len(rows) > 3:
        print(f"  (narrow --name to ≤3 cells for per-site detail)")


def _print_state_cell(pg, c) -> None:
    """Full dossier for one state cell: every write site with its line, every
    mutating METHOD CALL on the cell (invisible to writer counts — the usual
    corruption vector), and the reader list."""
    attr = c.name.rsplit(".", 1)[-1] if c.kind == "attr" else c.name.split(":")[-1]
    fl = " [" + ",".join(c.flags) + "]" if c.flags else ""
    print(f"  {c.name}  W{len(c.writers)} R{len(c.readers)}{fl}")

    def sites(fq, match_target):
        g = pg.functions.get(fq)
        if g is None:
            return []
        out = []
        for op in g.iter_ops():
            for t in op.targets:
                if match_target(t):
                    out.append(op.source[0] if op.source else None)
        return out

    prefix = f"self.{attr}"
    for fq in sorted(c.writers):
        lns = sites(fq, lambda t: t == prefix or t.startswith(prefix + ".")
                    or t.startswith(prefix + "[")) if c.kind == "attr" else \
              sites(fq, lambda t: t == attr)
        where = ",".join(f"L{x}" for x in lns if x) or "?"
        print(f"    write   {fq}  @{where}")

    if c.kind == "attr":
        muts = []
        for fq, g in pg.functions.items():
            for op in g.iter_ops():
                for call in op.attrs.get("calls", ()):
                    f = call.get("func") or ""
                    if f.startswith(prefix + "."):
                        muts.append((fq, f[len(prefix) + 1:],
                                     op.source[0] if op.source else None))
        for fq, meth, ln in sorted(muts)[:20]:
            print(f"    call    {fq}  .{meth}()" + (f" @L{ln}" if ln else ""))
        if len(muts) > 20:
            print(f"    ... +{len(muts) - 20} more method calls")

    readers = sorted(c.readers - c.writers)
    if readers:
        shown = " · ".join(r.split(":")[-1] for r in readers[:10])
        tail = f" (+{len(readers) - 10})" if len(readers) > 10 else ""
        print(f"    read    {shown}{tail}")


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
    from .analysis.opportunities import run_passes, verify_hint
    from .analysis.complexity import program_percentiles, compute_metrics, THRESHOLDS

    # Relative footing for complexity: `cyclomatic 16` is noise in the
    # abstract; `p97 in this program` is a defensible outlier claim.
    pcts = program_percentiles(pg.functions) if len(pg.functions) >= 10 else None

    from .analysis.opportunities import filter_suppressed
    src_cache: dict = {}

    def lines_of(relpath: str):
        if relpath not in src_cache:
            base = (pg.root if os.path.isfile(pg.root)
                    else os.path.join(pg.root, relpath))
            try:
                src_cache[relpath] = Path(base).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                src_cache[relpath] = []
        return src_cache[relpath]

    n_suppressed = 0
    rows = []  # (opportunity, display file path, qualname)
    for fq, g in pg.functions.items():
        relpath, qual = fq.split(":", 1)
        disp = pg.display_path(relpath)
        opps = run_passes(g)
        src = lines_of(relpath)
        if src:
            opps, n_sup = filter_suppressed(opps, src)
            n_suppressed += n_sup
        for o in opps:
            if only is not None and o.pass_name not in only:
                continue
            if pcts and o.pass_name == "complexity":
                m = compute_metrics(g)
                marks = " · ".join(
                    f"p{pcts[k](m[k])} {k.replace('max_', '').replace('_', '-')}"
                    for k in THRESHOLDS if m[k] > THRESHOLDS[k])
                o.detail = f"{o.detail}  [{marks} in this program]"
            rows.append((o, disp, qual))

    rank = {("sound", "must"): 0, ("sound", "may"): 1,
            ("heuristic", "must"): 2, ("heuristic", "may"): 3}
    rows.sort(key=lambda r: (rank.get((r[0].soundness, r[0].modality), 9),
                             r[1], r[2], r[0].line or 0))
    by_pass = Counter(o.pass_name for o, _, _ in rows)
    n_sound = sum(1 for o, _, _ in rows if o.soundness == "sound")
    root_disp = pg.display_root()
    print(f"PROGRAM opportunities: {root_disp}")
    sup = f" · {n_suppressed} suppressed (pflow: ok)" if n_suppressed else ""
    print(f"  {len(pg.functions)} functions · {len(rows)} findings "
          f"({n_sound} sound · {len(rows) - n_sound} heuristic){sup} · "
          f"{len(pg.errors)} parse error(s)")
    if by_pass:
        print("  by pass: " + ", ".join(f"{p}={n}" for p, n in by_pass.most_common()))
    print(f"  cross-check layer — confirm each ref by walking it before acting; "
          f"the review itself lives in the graphs (start: pflow report {root_disp})")

    section = {"sound": "  sound (holds by construction):",
               "heuristic": "  heuristic (hypotheses — confirm against source):"}
    cap = 60
    current = None
    for o, disp, qual in rows[:cap]:
        if o.soundness != current:
            current = o.soundness
            print(section.get(current, f"  {current}:"))
        print(f"    {o.render(disp)}")
        hint = verify_hint(o, f"{disp}:{qual}", disp)
        if hint:
            print(f"        verify → {hint}")
    if len(rows) > cap:
        print(f"  ... +{len(rows) - cap} more — narrow with --pass NAME or a per-file target")


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
