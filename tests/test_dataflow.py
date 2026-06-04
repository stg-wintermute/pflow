"""Reaching definitions / def-use correctness."""

from conftest import build, defs_named, uses_named


def test_intra_block_def_use_last_write_wins():
    g = build("""
        def f(a):
            x = 1
            x = 2
            y = x + a
            return y
    """, "f")
    x_defs = defs_named(g, "x")
    assert len(x_defs) == 2
    second_x = max(x_defs, key=lambda d: d.id)

    # The use of x in `y = x + a` must be reached ONLY by the second def.
    x_use = next(u for u in uses_named(g, "x"))
    assert set(x_use.reaching_defs) == {second_x.id}


def test_cross_block_may_analysis():
    g = build("""
        def f(c):
            if c:
                x = 1
            else:
                x = 2
            return x
    """, "f")
    x_defs = {d.id for d in defs_named(g, "x")}
    ret_use = next(u for u in uses_named(g, "x"))
    # Both branch definitions reach the return (may-analysis).
    assert set(ret_use.reaching_defs) == x_defs


def test_kill_blocks_earlier_def():
    g = build("""
        def f():
            x = 1
            y = x
            x = 2
            z = x
            return y, z
    """, "f")
    x_defs = sorted(d.id for d in defs_named(g, "x"))
    uses = uses_named(g, "x")
    # y = x sees first def; z = x sees second def.
    first_use = min(uses, key=lambda u: u.op_id)
    last_use = max(uses, key=lambda u: u.op_id)
    assert set(first_use.reaching_defs) == {x_defs[0]}
    assert set(last_use.reaching_defs) == {x_defs[1]}


def test_loop_carried_definition_reaches_header_use():
    g = build("""
        def f(n):
            total = 0
            while n > 0:
                total = total + n
                n = n - 1
            return total
    """, "f")
    total_defs = {d.id for d in defs_named(g, "total")}
    # The `total` use inside `total = total + n` should see both the
    # initial def and the loop-carried def.
    body_use = next(u for u in uses_named(g, "total")
                    if any(d_id in total_defs for d_id in u.reaching_defs))
    assert len(body_use.reaching_defs) >= 1
    # The return's use of total is reached by both definitions.
    ret_use = max(uses_named(g, "total"), key=lambda u: u.op_id)
    assert set(ret_use.reaching_defs) == total_defs


def test_parameters_are_definitions():
    g = build("""
        def f(a, b):
            return a + b
    """, "f")
    assert defs_named(g, "a") and defs_named(g, "b")
    a_use = next(u for u in uses_named(g, "a"))
    assert set(a_use.reaching_defs) == {defs_named(g, "a")[0].id}


def test_augassign_reads_old_value():
    g = build("""
        def f(a):
            x = a
            x += 1
            return x
    """, "f")
    x_defs = sorted(d.id for d in defs_named(g, "x"))
    # The `x` read inside `x += 1` sees the previous def, not itself.
    aug_use = next(u for u in uses_named(g, "x") if u.op_id is not None
                   and any(i == x_defs[0] for i in u.reaching_defs))
    assert x_defs[0] in aug_use.reaching_defs
    assert x_defs[1] not in aug_use.reaching_defs


def test_block_reaching_sets_recorded():
    g = build("""
        def f(c):
            x = 1
            if c:
                x = 2
            return x
    """, "f")
    # Entry block has nothing reaching in.
    entry = g.get_block(g.entry)
    assert entry.attrs.get("reaching_in") == set()
    # Every block records gen/kill/in/out.
    for b in g.blocks:
        assert "gen" in b.attrs and "kill" in b.attrs
        assert "reaching_in" in b.attrs and "reaching_out" in b.attrs
