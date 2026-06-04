"""CFG construction: connectivity and exception structure.

These lock in the original root-cause bug where if/else/loop merges were
never wired, leaving everything after a branch unreachable.
"""

from conftest import build, reachable, block_of_op_kind


def succs(g, bid):
    return set(g.get_block(bid).succs)


def test_straight_line_is_one_block():
    g = build("""
        def f(a, b):
            x = a + b
            y = x * 2
            return y
    """, "f")
    assert len(g.blocks) == 1
    assert g.blocks[0].id == g.entry


def test_if_else_merges_and_stays_connected():
    g = build("""
        def f(c):
            if c:
                x = 1
            else:
                x = 2
            return x
    """, "f")
    # No orphan blocks: everything reachable from entry.
    assert reachable(g) == {b.id for b in g.blocks}
    # The return must be reachable (regression: it used to be orphaned).
    ret = block_of_op_kind(g, "return")
    assert ret in reachable(g)
    # Entry branches two ways, both reach the return block.
    assert len(succs(g, g.entry)) == 2


def test_if_without_else_connected():
    g = build("""
        def f(c):
            x = 0
            if c:
                x = 1
            return x
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}


def test_while_has_back_edge_and_exit():
    g = build("""
        def f(n):
            while n > 0:
                n = n - 1
            return n
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}
    # Some block must loop back to the while header.
    header = block_of_op_kind(g, "branch")
    assert any(header in g.get_block(b.id).succs and b.id != header
               for b in g.blocks)


def test_for_else_and_break_continue_connected():
    g = build("""
        def f(items):
            total = 0
            for it in items:
                if it < 0:
                    continue
                total += it
                if total > 100:
                    break
            else:
                total = -1
            return total
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}


def test_try_except_finally_exception_edges():
    g = build("""
        def f(x):
            try:
                y = risky(x)
            except KeyError as e:
                y = handle(e)
            finally:
                cleanup()
            return y
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}

    handler = block_of_op_kind(g, "enter_except")
    finally_b = block_of_op_kind(g, "enter_finally")
    assert handler is not None and finally_b is not None

    # The try body is the block that can transfer exceptionally to BOTH the
    # handler and the finally region.
    body_blocks = [b for b in g.blocks
                   if {handler, finally_b} <= set(b.except_succs)]
    assert body_blocks, "expected a body block with except edges to handler+finally"
    # handler falls through to finally; finally reaches the return.
    assert finally_b in set(g.get_block(handler).succs)
    ret = block_of_op_kind(g, "return")
    assert ret in set(g.get_block(finally_b).succs)


def test_with_body_falls_through_to_cleanup_without_exception_pollution():
    # A value assigned inside a `with` and read AFTER it must not be reported
    # as maybe-unbound: a non-suppressing context manager re-raises, so the
    # body's exception path never reaches the post-with code. The body falls
    # through to the synthetic __exit__ (exit_with) on the NORMAL path only —
    # we deliberately add no body->cleanup exception edge (it poisoned
    # definite-assignment and was a frequent false positive).
    from pflow.analysis.definite_assignment import find_use_before_def
    g = build("""
        def f(path):
            with open(path) as fh:
                data = fh.read()
            return data
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}
    cleanup = block_of_op_kind(g, "exit_with")
    assert any(cleanup in set(b.succs) for b in g.blocks)        # normal fall-through
    assert not any(cleanup in set(b.except_succs) for b in g.blocks)  # no exception pollution
    assert not any(":use:data@" in o.ref for o in find_use_before_def(g))


def test_match_cases_are_lowered():
    g = build("""
        def f(cmd):
            match cmd:
                case "go":
                    r = 1
                case _:
                    r = 0
            return r
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}
    # The case bodies must exist as ops (regression: generic_visit dropped them).
    kinds = [o.kind for b in g.blocks for o in b.ops]
    assert kinds.count("assign") >= 3  # cmd arg + r=1 + r=0


def test_nested_def_does_not_corrupt_cfg():
    # Bug A: a nested def must be a single name-binding, not inlined into the
    # enclosing CFG (which used to disconnect the real body).
    g = build("""
        def f(xs):
            acc = []
            def helper(x):
                return x + 1
            for x in xs:
                acc.append(helper(x))
            return acc
    """, "f")
    assert reachable(g) == {b.id for b in g.blocks}        # no orphaned blocks
    assert any("helper" in op.targets for op in g.iter_ops())  # name is bound


def test_return_terminates_block():
    g = build("""
        def f(c):
            if c:
                return 1
            return 2
    """, "f")
    # The then-branch return block has no normal successors.
    ret_blocks = [b for b in g.blocks
                  if any(o.kind == "return" for o in b.ops)]
    assert all(not b.succs for b in ret_blocks)
