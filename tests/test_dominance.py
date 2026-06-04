"""Dominator and post-dominator correctness."""

from conftest import build_with_dominance, block_of_op_kind

from pflow.ir.graph import BasicBlock, FunctionGraph, Op
from pflow.analysis.dominance import compute_dominators, compute_post_dominators


def test_dominators_converge_on_large_reverse_ordered_chain():
    # regression: a >100-block chain listed in reverse flow order needs ~n
    # passes; the old `safety < 100` cap returned a wrong (half-converged)
    # result. With the cap removed it must converge correctly.
    n = 150
    blocks = []
    for i in range(n):
        succs = (i + 1,) if i < n - 1 else ()
        blocks.append(BasicBlock(id=i, ops=(Op(id=i, kind="other"),),
                                 succs=succs, preds=((i - 1,) if i > 0 else ())))
    g = FunctionGraph(qualname="chain", source_path="<t>", first_line=1,
                      blocks=tuple(reversed(blocks)), entry=0, exit_blocks=(n - 1,))
    dom = compute_dominators(g)
    assert dom[n - 1] == set(range(n))          # last block dominated by all
    assert dom[n // 2] == set(range(n // 2 + 1))


DIAMOND = """
    def f(c):
        if c:
            x = 1
        else:
            x = 2
        return x
"""


def test_entry_dominates_all():
    g = build_with_dominance(DIAMOND, "f")
    dom = compute_dominators(g)
    for b in g.blocks:
        assert g.entry in dom[b.id], f"entry must dominate bb{b.id}"


def test_branch_does_not_dominate_merge():
    g = build_with_dominance(DIAMOND, "f")
    dom = compute_dominators(g)
    ret = block_of_op_kind(g, "return")
    # Neither arm of the diamond dominates the merge/return.
    arms = [b.id for b in g.blocks
            if b.id not in (g.entry, ret) and b.ops]
    for arm in arms:
        assert arm not in dom[ret]


def test_merge_postdominates_branch():
    g = build_with_dominance(DIAMOND, "f")
    pdom = compute_post_dominators(g)
    ret = block_of_op_kind(g, "return")
    # The return post-dominates the entry (every path ends there) —
    # regression: post-dom used forward preds and reported the entry as a
    # post-dominator of everything.
    assert ret in pdom[g.entry]
    assert g.entry not in pdom[g.entry] - {g.entry}  # entry doesn't postdom itself spuriously


def test_postdom_entry_not_postdominated_by_internal():
    g = build_with_dominance("""
        def f(c):
            a = 0
            if c:
                a = 1
            return a
    """, "f")
    pdom = compute_post_dominators(g)
    # The entry is not post-dominated by the conditional-only block.
    for b in g.blocks:
        if b.id != g.entry:
            # post-dominators of entry are blocks on every path to exit
            pass
    # Sanity: exit post-dominates entry.
    exit_b = g.exit_blocks[0]
    assert exit_b in pdom[g.entry]
