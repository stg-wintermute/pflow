"""walk / slice traversal over control and data edges."""

from conftest import build, defs_named, uses_named

from pflow.analysis import walk
from pflow.analysis.walk import format_walk
from pflow.analysis.slice import slice_backward, slice_forward


SAMPLE = """
    def step(self, lease):
        x = 1
        x = 2
        y = x + lease
        if y > 0:
            z = y
        else:
            z = 0
        return z
"""


def test_walk_from_real_ref_does_not_crash():
    g = build(SAMPLE, "step")
    # The RFC headline form that used to raise ValueError.
    steps = walk(g, "step:def:y@5", direction="forward", edges="data", max_depth=3)
    assert steps
    assert steps[0].kind == "def"


def test_walk_forward_data_reaches_dependents():
    g = build(SAMPLE, "step")
    y_def = defs_named(g, "y")[0]
    steps = walk(g, f"def:{y_def.id}", direction="forward", edges="data", max_depth=4)
    labels = {(s.kind, s.label) for s in steps}
    # y flows into the branch and into z = y.
    assert any(s.kind == "use" and s.label == "use:y" for s in steps)
    assert any(s.kind == "def" and s.label.startswith("def:z") for s in steps)


def test_backward_slice_from_return_reaches_sources():
    g = build(SAMPLE, "step")
    ret_use = max(uses_named(g, "z"), key=lambda u: u.op_id)
    steps = slice_backward(g, f"use:{ret_use.id}", max_depth=8)
    kinds_labels = {s.label for s in steps}
    # Backward slice of the returned z reaches the y and x definitions.
    assert any(lbl.startswith("def:z") for lbl in kinds_labels)
    assert any(lbl.startswith("def:y") for lbl in kinds_labels)
    assert any(lbl.startswith("def:x") for lbl in kinds_labels)


def test_walk_control_forward_and_backward():
    g = build(SAMPLE, "step")
    fwd = walk(g, "bb:0", direction="forward", edges="control", max_depth=10)
    assert {s.id for s in fwd if s.kind == "block"} == {b.id for b in g.blocks}

    # Backward from the exit should reach the entry.
    exit_b = g.exit_blocks[0]
    bwd = walk(g, f"bb:{exit_b}", direction="backward", edges="control", max_depth=10)
    assert g.entry in {s.id for s in bwd if s.kind == "block"}


def test_walk_depth_limit():
    g = build(SAMPLE, "step")
    shallow = walk(g, "bb:0", direction="forward", edges="control", max_depth=1)
    assert max(s.depth for s in shallow) <= 1


def test_walk_steps_form_a_tree():
    g = build(SAMPLE, "step")
    steps = walk(g, "step:def:y@5", direction="forward", edges="data", max_depth=3)
    assert steps[0].parent == -1                      # root
    assert all(s.parent >= 0 for s in steps[1:])       # every other has a parent
    assert all(0 <= s.parent < i for i, s in enumerate(steps) if s.parent >= 0)


def test_uses_carry_reaching_annotation():
    g = build(SAMPLE, "step")
    steps = walk(g, "step:def:y@5", direction="forward", edges="data", max_depth=2)
    use_y = next(s for s in steps if s.label == "use:y")
    assert "reaching:" in use_y.note
    assert use_y.loc.startswith("(bb")


def test_finally_region_annotated():
    g = build("""
        def f(x):
            try:
                y = risky(x)
            finally:
                cleanup(y)
            return y
    """, "f")
    y_def = next(d for d in g.defs if d.name == "y")
    steps = walk(g, f"def:{y_def.id}", direction="forward", edges="data", max_depth=4)
    fin_use = next((s for s in steps if s.kind == "use" and "finally" in s.note), None)
    assert fin_use is not None, "expected a use of y annotated as in the finally region"


def test_format_walk_renders_indented_tree_with_header():
    g = build(SAMPLE, "step")
    steps = walk(g, "step:def:y@5", direction="forward", edges="data", max_depth=2)
    out = format_walk(steps, graph=g)
    lines = out.splitlines()
    assert lines[0].startswith("[step:def:y")        # bracketed ref header
    assert any("->" in ln for ln in lines)            # tree edges
    assert any("reaching:" in ln for ln in lines)     # annotations rendered


def test_backward_slice_includes_control_dependence():
    # RFC-0002 §3.2: the slice of a conditionally-assigned value must
    # include the predicate that selects it (control dependence), not
    # just its data ancestors.
    g = build("""
        def g(cond):
            if cond:
                z = 1
            else:
                z = 2
            return z
    """, "g")
    z_use = max((u for u in g.uses if u.name == "z"), key=lambda u: u.op_id)
    steps = walk(g, f"use:{z_use.id}", direction="backward", edges="data", max_depth=8)
    assert any(s.label == "use:cond" for s in steps), \
        "controlling predicate `cond` must be in the backward slice"
    assert any(s.via == "ctrl-dep" for s in steps), \
        "a control-dependence edge must appear in the slice"


def test_walk_emits_targets_footer_at_depth_limit():
    # A depth-limited walk leaves graph unexplored at the frontier; those
    # frontier nodes are surfaced as `targets →` refs to continue from.
    g = build(SAMPLE, "step")
    steps = walk(g, "bb:0", direction="forward", edges="control", max_depth=1)
    assert any(s.frontier for s in steps), "expected a depth-limited frontier"
    out = format_walk(steps, graph=g)
    assert "targets →" in out
    # the frontier refs are re-resolvable handles, not prose
    footer = [ln for ln in out.splitlines() if ln.startswith("targets →")][0]
    assert ":bb:" in footer


def test_fully_resolved_walk_has_no_targets():
    # When a walk bottoms out before the depth limit (nothing left to explore),
    # there are no targets — the footer is omitted rather than listing terminals.
    g = build(SAMPLE, "step")
    steps = walk(g, "bb:0", direction="forward", edges="control", max_depth=50)
    assert not any(s.frontier for s in steps)
    out = format_walk(steps, graph=g)
    assert "targets →" not in out


def test_walk_unresolvable_start_raises():
    g = build(SAMPLE, "step")
    try:
        walk(g, "def:doesnotexist@1", direction="forward", edges="data")
        assert False, "expected ValueError"
    except ValueError:
        pass
