from .ir import (
    Op, BasicBlock, FunctionGraph, Def, Use, ModuleGraph,
    build_cfg_from_source, build_cfg_from_ast,
)
from .analysis import run_dataflow, walk, Step, format_walk
from .ir.inspect_live import build_cfg_from_live
from .ir.bytecode import get_basic_blocks, get_exception_table, bytecode_to_simple_cfg, offset_to_line_map
from .ir.refs import parse_ref, resolve, Ref, format_agent_ref
from .analysis.hybrid import attach_dis_info, correct_exception_edges, suggest_exception_edges
from .analysis.callgraph import build_callgraph
from .analysis.paths import find_paths
from .analysis.dominance import compute_dominators, compute_post_dominators
from .format.agent import format_cfg_agent, format_report_agent

__all__ = [
    "Op", "BasicBlock", "FunctionGraph", "Def", "Use", "ModuleGraph",
    "build_cfg_from_source", "build_cfg_from_ast", "build_cfg_from_live",
    "get_basic_blocks", "get_exception_table", "bytecode_to_simple_cfg", "offset_to_line_map",
    "parse_ref", "resolve", "Ref", "format_agent_ref",
    "attach_dis_info", "correct_exception_edges", "suggest_exception_edges",
    "build_callgraph",
    "find_paths",
    "compute_dominators", "compute_post_dominators",
    "run_dataflow", "walk", "Step", "format_walk",
    "analyze_live", "analyze_with_hybrid",
    "format_cfg_agent", "format_report_agent",
    "format_for_agent",
]


def format_for_agent(g: FunctionGraph, **kwargs) -> str:
    """Convenience wrapper around the best current agent formatter."""
    return format_cfg_agent(g, **kwargs)


def analyze_live(obj):
    """One-call analysis for agents: live object → full graph with dis info + dataflow."""
    g = build_cfg_from_live(obj)
    g = attach_dis_info(g)
    g = correct_exception_edges(g)
    g = run_dataflow(g)
    try:
        g.attrs["agent_view"] = format_cfg_agent(g, include_dataflow=True)
    except Exception:
        pass
    return g


def analyze_with_hybrid(obj_or_source, qualname=None, with_dominance: bool = True):
    """Build + attach dis info + correct exceptions + dataflow (+ dominance by default)."""
    if callable(obj_or_source):
        g = build_cfg_from_live(obj_or_source)
    else:
        g = build_cfg_from_source(obj_or_source, qualname or "main")
    g = attach_dis_info(g)
    g = correct_exception_edges(g)
    g = run_dataflow(g)

    if with_dominance:
        try:
            from .analysis.dominance import compute_dominators, compute_post_dominators
            g.attrs["dominators"] = compute_dominators(g)
            g.attrs["post_dominators"] = compute_post_dominators(g)
        except Exception:
            pass

    # Pre-compute a nice agent string for convenience
    try:
        g.attrs["agent_view"] = format_cfg_agent(g, include_dataflow=True, include_dominance=with_dominance)
    except Exception:
        pass

    return g
