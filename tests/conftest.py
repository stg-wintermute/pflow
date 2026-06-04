"""Shared test helpers for pflow."""

from __future__ import annotations

import textwrap
from collections import deque

import pytest

from pflow.ir import build_cfg_from_source
from pflow.analysis import run_dataflow
from pflow.analysis.dominance import compute_dominators, compute_post_dominators


def build(src: str, name: str):
    """Parse `src`, build the CFG for `name`, and run reaching definitions."""
    g = build_cfg_from_source(textwrap.dedent(src), name, source_path="<test>")
    return run_dataflow(g)


def build_with_dominance(src: str, name: str):
    g = build(src, name)
    g.attrs["dominators"] = compute_dominators(g)
    g.attrs["post_dominators"] = compute_post_dominators(g)
    return g


def reachable(g, include_except=True):
    """Set of block ids reachable from the entry via control edges."""
    seen = {g.entry}
    q = deque([g.entry])
    while q:
        b = g.get_block(q.popleft())
        nbrs = list(b.succs) + (list(b.except_succs) if include_except else [])
        for s in nbrs:
            if s not in seen:
                seen.add(s)
                q.append(s)
    return seen


def block_of_op_kind(g, kind):
    """The block id containing the first op of the given kind."""
    for b in g.blocks:
        if any(o.kind == kind for o in b.ops):
            return b.id
    return None


def defs_named(g, name):
    return [d for d in g.defs if d.name == name]


def uses_named(g, name):
    return [u for u in g.uses if u.name == name]


def reaching_def_ids(g, use):
    return set(use.reaching_defs)


@pytest.fixture
def make():
    return build
