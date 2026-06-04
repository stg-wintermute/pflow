"""Interprocedural (whole-program) analyses: call graph, state map, tracing."""

import os

import pytest

from pflow.program import build_program
from pflow.analysis.interproc import (
    program_callgraph, entrypoints, cycles, state_map, trace_value,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "prog")


@pytest.fixture(scope="module")
def pg():
    return build_program(FIXTURE, use_cache=False)


def fq(pg, suffix):
    hits = [f for f in pg.functions if f.endswith(suffix)]
    assert hits, f"no function ending {suffix!r}"
    return hits[0]


# -- program model -------------------------------------------------------

def test_builds_both_modules(pg):
    assert len(pg.modules) == 2
    # core: make_state, process, transform, store, bump (5)
    # svc: Service.__init__, handle, read (3)
    assert len(pg.functions) == 8


def test_cross_file_call_resolution(pg):
    handle = fq(pg, "svc.py:Service.handle")
    targets = pg.resolve_call(handle, "process")
    assert any(t.endswith("core.py:process") for t in targets)


# -- call graph ----------------------------------------------------------

def test_callgraph_edges(pg):
    cg = program_callgraph(pg)
    handle = fq(pg, "svc.py:Service.handle")
    process = fq(pg, "core.py:process")
    transform = fq(pg, "core.py:transform")
    assert process in cg.edges[handle]            # cross-file edge
    assert transform in cg.edges[process]         # same-file edge
    assert cg.fan_in(process) >= 1


def test_pipeline_has_no_cycles(pg):
    cg = program_callgraph(pg)
    assert cycles(cg) == []


def test_entrypoints_include_uncalled(pg):
    cg = program_callgraph(pg)
    eps = set(entrypoints(cg))
    # nobody in-program calls handle/read/__init__
    assert fq(pg, "svc.py:Service.handle") in eps


# -- shared-state map ----------------------------------------------------

def test_shared_mutable_attribute_flagged(pg):
    cells = state_map(pg)
    cache = next(c for k, c in cells.items() if k.endswith("Service._cache"))
    writers = {w.split(":")[-1] for w in cache.writers}
    readers = {r.split(":")[-1] for r in cache.readers}
    assert writers == {"Service.__init__", "Service.handle"}
    assert readers == {"Service.read"}
    assert "shared-mutable-channel" in cache.flags


def test_method_calls_not_counted_as_state(pg):
    # self.handle / self.read references must not appear as state cells.
    cells = state_map(pg)
    assert not any(k.endswith("Service.handle") or k.endswith("Service.read")
                   for k in cells)


def test_global_write_via_global_decl(pg):
    cells = state_map(pg)
    total = next((c for k, c in cells.items() if k.endswith("GLOBAL:TOTAL")), None)
    assert total is not None
    assert any(w.endswith("core.py:bump") for w in total.writers)


# -- value tracing -------------------------------------------------------

def test_cache_roundtrip_is_identical(tmp_path, monkeypatch):
    # Point the cache at an isolated dir; build cold then warm.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cold = build_program(FIXTURE, use_cache=True)
    warm = build_program(FIXTURE, use_cache=True)
    assert set(cold.functions) == set(warm.functions)
    cg_cold = program_callgraph(cold)
    cg_warm = program_callgraph(warm)
    assert {k: sorted(v) for k, v in cg_cold.edges.items()} == \
           {k: sorted(v) for k, v in cg_warm.edges.items()}
    # the warm build's graphs survived a pickle round-trip
    h = next(f for f in warm.functions if f.endswith("Service.handle"))
    assert warm.functions[h].num_blocks() == cold.functions[h].num_blocks()


def test_no_cache_matches_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    a = build_program(FIXTURE, use_cache=True)
    b = build_program(FIXTURE, use_cache=False)
    assert set(a.functions) == set(b.functions)


def test_trace_threads_value_through_pipeline(pg):
    handle = fq(pg, "svc.py:Service.handle")
    steps = trace_value(pg, "st", origin=handle, max_depth=6)
    kinds = {(s.fq.split(":")[-1], s.var, s.kind) for s in steps}
    # st is forwarded as an argument into process(...)
    assert any(k == "arg" and "process" in f for f, v, k in kinds)
    # st is stored to self._cache, which read() consumes -> store-escape hop
    assert any(v == "self._cache" for f, v, k in kinds)
    assert any(f == "Service.read" for f, v, k in kinds)
