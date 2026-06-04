"""RFC-0002 passes: generic solver, liveness, dead-store opportunities."""

from conftest import build

from pflow.analysis.solver import solve
from pflow.analysis.liveness import compute_liveness, find_dead_stores
from pflow.analysis.reachability import find_unreachable
from pflow.analysis.definite_assignment import find_use_before_def
from pflow.analysis.redundant_branch import find_redundant_branches
from pflow.analysis.complexity import compute_metrics, find_complexity_hotspots
from pflow.analysis.decomposition import find_decomposition
from pflow.analysis.const_prop import find_constant_branches
from pflow.analysis.verify import verify_refactor
from pflow.analysis.opportunities import run_passes


# -- solver --------------------------------------------------------------

def test_solver_reproduces_reaching_defs():
    # reaching-defs is now a solver client; the cross-block may-analysis
    # must still see both branch definitions at the return.
    g = build("""
        def f(c):
            if c:
                x = 1
            else:
                x = 2
            return x
    """, "f")
    x_defs = {d.id for d in g.defs if d.name == "x"}
    ret_use = max((u for u in g.uses if u.name == "x"), key=lambda u: u.op_id)
    assert set(ret_use.reaching_defs) == x_defs


def test_solver_backward_is_well_defined():
    g = build("def f(a):\n    y = a + 1\n    return y\n", "f")
    live_in, live_out = solve(
        g, "backward", init=set, boundary=set,
        meet=lambda vs: set().union(*vs),
        transfer=lambda b, lo: lo,  # identity transfer = boundary propagation
    )
    assert isinstance(live_in, dict) and isinstance(live_out, dict)


# -- liveness ------------------------------------------------------------

def test_returned_value_is_live_across_blocks():
    # y is defined in the first block and read after the if -> it must be
    # live across a block boundary.
    g = build("""
        def f(a, c):
            y = a + 1
            if c:
                a = 0
            return y
    """, "f")
    _, live_out = compute_liveness(g)
    assert any("y" in s for s in live_out.values())


# -- dead-store pass -----------------------------------------------------

def _dead_names(g):
    return {o.ref.split(":def:")[1].split("@")[0]
            for o in find_dead_stores(g) if ":def:" in o.ref}


def test_dead_store_flags_unread_local_not_returned():
    g = build("""
        def f(a):
            x = a + 1      # dead — never read
            y = a * 2
            return y
    """, "f")
    names = _dead_names(g)
    assert "x" in names
    assert "y" not in names          # returned -> live


def test_dead_store_skips_attrs_underscore_and_params():
    g = build("""
        def f(self, a, _unused):
            self._cache = 1     # attribute may escape -> not flagged
            _ = a               # underscore convention -> not flagged
            return a
    """, "f")
    refs = " ".join(o.ref for o in find_dead_stores(g))
    assert "self._cache" not in refs
    assert ":def:_@" not in refs
    assert "_unused" not in refs     # a parameter, not a dead store


def test_dead_store_overwritten_before_read():
    g = build("""
        def f(a):
            x = 1     # dead — overwritten before any read
            x = a
            return x
    """, "f")
    # the FIRST x assignment is dead (overwritten before read); the second
    # (returned) is live. Compare by op id, not source line.
    x_defs = sorted((d for d in g.defs if d.name == "x"), key=lambda d: d.id)
    first_op, second_op = x_defs[0].op_id, x_defs[1].op_id
    dead_ops = {o.op_id for o in find_dead_stores(g) if ":def:x@" in o.ref}
    assert first_op in dead_ops
    assert second_op not in dead_ops


def test_dead_store_with_call_rhs_is_distinguished():
    g = build("""
        def f(a):
            r = compute(a)    # dead store, but RHS has a side-effecting call
            return a
    """, "f")
    assert any(o.kind == "dead_store_call" for o in find_dead_stores(g))


def test_loop_variable_not_flagged():
    g = build("""
        def f(items):
            total = 0
            for it in items:
                total = total + it
            return total
    """, "f")
    assert "total" not in _dead_names(g)
    assert "it" not in _dead_names(g)


# -- unreachable ---------------------------------------------------------

def test_unreachable_after_return():
    g = build("""
        def f():
            return 1
            x = 2
    """, "f")
    opps = find_unreachable(g)
    assert opps and all(o.soundness == "sound" and o.modality == "must" for o in opps)


def test_no_unreachable_in_straight_code():
    g = build("def f(a):\n    x = a + 1\n    return x\n", "f")
    assert find_unreachable(g) == []


def test_with_returning_on_all_paths_is_not_unreachable():
    # The lock-guarded-accessor idiom `with self._lock: return X`. The body
    # always returns, so the synthetic __exit__ cleanup has no predecessor —
    # but it is NOT deletable code. This was a false positive in the SOUND/MUST
    # tier (the tool's "fact" tier), reported across two services.
    for src in (
        "def f(self):\n    with self._lock:\n        return self._x\n",
        "def f(self, c):\n    with self._lock:\n"
        "        if c:\n            return 1\n        else:\n            return 2\n",
        "def f(u):\n    with urlopen(u) as r:\n        return r.read()\n",
    ):
        g = build(src, "f")
        assert find_unreachable(g) == [], f"false unreachable on: {src!r}"


def test_real_dead_code_after_with_still_flagged():
    # Precision must not cost the true positive: genuinely unreachable user
    # code after an all-paths-return `with` is still flagged (and points at the
    # dead statement, not the synthetic cleanup or the `with`).
    g = build("def f():\n    with lock():\n        return 1\n    dead()\n", "f")
    opps = find_unreachable(g)
    assert len(opps) == 1
    assert opps[0].soundness == "sound" and opps[0].modality == "must"
    assert opps[0].line == 4          # the dead() line, not the `with`


# -- use-before-def ------------------------------------------------------

def test_use_before_def_maybe_unbound_one_branch():
    g = build("""
        def f(c):
            if c:
                y = 1
            return y
    """, "f")
    opps = find_use_before_def(g)
    assert any(o.kind == "maybe_unbound" and ":use:y@" in o.ref for o in opps)


def test_use_before_def_unbound_used_before_assignment():
    g = build("""
        def f():
            print(x)
            x = 1
            return x
    """, "f")
    opps = find_use_before_def(g)
    assert any(o.kind == "unbound" and ":use:x@" in o.ref for o in opps)


def test_use_before_def_catches_read_in_finally():
    # The headline case: a value assigned in a with/try body and read in the
    # finally is possibly-unbound (the exception may fire before assignment).
    # No mainstream Python tool detects this.
    g = build("""
        def f(a):
            try:
                with lock(a) as h:
                    state = h.read()
            finally:
                audit(state)
            return state
    """, "f")
    opps = find_use_before_def(g)
    assert any("state" in o.ref and o.kind == "maybe_unbound" for o in opps)


def test_use_before_def_skips_params_and_builtins():
    g = build("def f(a):\n    return a + len(a)\n", "f")
    assert find_use_before_def(g) == []  # `a` is a param, `len` a builtin


# -- redundant branch ----------------------------------------------------

def test_redundant_branch_constant_condition():
    g = build("def f(a):\n    if True:\n        return 1\n    return a\n", "f")
    opps = find_redundant_branches(g)
    assert any(o.kind == "constant_condition" and o.soundness == "sound" for o in opps)


def test_redundant_branch_skips_while_true_and_nonconstant():
    g = build("def f(a):\n    while True:\n        if a:\n            break\n    return a\n", "f")
    assert find_redundant_branches(g) == []


# -- complexity ----------------------------------------------------------

def test_nesting_distinguishes_sequential_from_nested():
    seq = build("def f(a, b, c):\n    if a: x = 1\n    if b: y = 2\n"
                "    if c: z = 3\n    return 0\n", "f")
    nested = build("def f(a, b, c):\n    if a:\n        if b:\n            if c:\n"
                   "                return 1\n    return 0\n", "f")
    assert compute_metrics(nested)["max_nesting"] == 3
    assert compute_metrics(seq)["max_nesting"] < compute_metrics(nested)["max_nesting"]
    # sequential branches must NOT inflate cognitive complexity to nested levels
    assert compute_metrics(seq)["cognitive"] < compute_metrics(nested)["cognitive"]


def test_complexity_hotspot_for_deep_nesting():
    g = build("def f(a, b, c, d, e):\n    if a:\n        if b:\n            if c:\n"
              "                if d:\n                    if e:\n"
              "                        return 1\n    return 0\n", "f")
    assert any(o.pass_name == "complexity" for o in find_complexity_hotspots(g))


# -- constant propagation ------------------------------------------------

def test_const_branch_constant_variable():
    g = build("def f(a):\n    debug = False\n    if debug:\n        log(a)\n    return a\n", "f")
    opps = find_constant_branches(g)
    assert any(o.kind == "constant_var_condition" and o.soundness == "sound" for o in opps)


def test_const_branch_skips_variable_condition():
    g = build("def g(a):\n    flag = a > 0\n    if flag:\n        return 1\n    return 0\n", "g")
    assert find_constant_branches(g) == []


# -- decomposition (the flagship) ----------------------------------------

def test_decomposition_flags_independent_outputs():
    g = build("""
        class S:
            def compute(self, a, b):
                p = a + 1
                q = p * 2
                r = q - a
                self.left = r + p
                m = b + 1
                n = m * 2
                o = n - b
                self.right = o + m
                return None
    """, "S.compute")
    assert any(o.kind == "low_cohesion" for o in find_decomposition(g))


def test_decomposition_skips_cohesive_function():
    g = build("""
        def f(a, b):
            t = a + b
            u = t * 2
            v = u - a
            w = v + b
            return w + t + u + v
    """, "f")
    assert find_decomposition(g) == []


def test_decomposition_skips_dunder_init():
    g = build("""
        class S:
            def __init__(self, a, b, c, d):
                self.a = a + 1
                self.b = b + 1
                self.c = c + 1
                self.d = d + 1
    """, "S.__init__")
    assert find_decomposition(g) == []


# -- analysis-correctness regressions (found by dogfooding) --------------

def test_comprehension_var_not_use_before_def():
    # Bug B: the `b` bound by a comprehension is its own scope, not a
    # function-level use-before-def.
    g = build("def f(items):\n    return {b.id for b in items}\n", "f")
    assert find_use_before_def(g) == []


def test_subscript_index_counts_as_use():
    # Bug C: `m[start] = v` reads `start` (and `m`); only `end` is truly dead.
    g = build("""
        def f(rows):
            m = {}
            for start, end, lineno in rows:
                m[start] = lineno
            return m
    """, "f")
    dead = _dead_names(g)
    assert "start" not in dead
    assert "end" in dead


def test_assert_expression_counts_as_use():
    # Bug found by the stdlib corpus (contextlib ExitStack.__exit__): `assert`
    # reads its test expression, so a value whose only read is an assert is NOT
    # a dead store. Previously `assert` fell to generic_visit with no uses.
    g = build("def f(items):\n    x = items.pop()\n    assert x\n    return 0\n", "f")
    assert "x" not in _dead_names(g)


def test_delete_subscript_counts_base_and_index_as_use():
    # `del d[k]` reads both d and k (Load context); they must not be dead.
    g = build("def f():\n    d = {}\n    k = key()\n    del d[k]\n    return 0\n", "f")
    dead = _dead_names(g)
    assert "d" not in dead and "k" not in dead


def test_closure_captured_var_not_dead():
    # Bug A side effect: a local captured by a nested function is used, not dead.
    g = build("""
        def f():
            total = 0
            def add(x):
                return total + x
            return add
    """, "f")
    assert "total" not in _dead_names(g)


# -- correctness fixes from the code-review pass -------------------------

def test_for_loop_var_unbound_after_empty_loop():
    # the loop target is bound in the body, so it's possibly-unbound after the
    # loop (NameError if the iterable is empty).
    g = build("def f(items):\n    for x in items:\n        pass\n    return x\n", "f")
    assert any(o.kind in ("maybe_unbound", "unbound") and ":use:x@" in o.ref
               for o in find_use_before_def(g))


def test_constant_survives_loop_back_edge():
    # const-prop must not poison `k` to TOP at the loop header's back-edge join.
    g = build("def f(c, n):\n    k = 0\n    while n > 0:\n        n = n - 1\n"
              "    if k:\n        return 1\n    return 2\n", "f")
    assert any(o.kind == "constant_var_condition" for o in find_constant_branches(g))


# -- transform verification (Phase D) ------------------------------------

def test_verify_detects_behavior_change():
    old = build("def f(a, b):\n    return a + b\n", "f")
    new = build("def f(a, b):\n    c = 9\n    return a + c\n", "f")
    r = verify_refactor(old, new)
    assert not r["behavior_ok"]            # return lost its dependence on b


def test_verify_preserved_when_equivalent():
    old = build("def f(a, b):\n    if a:\n        if b:\n            return a + b\n"
                "    return 0\n", "f")
    new = build("def f(a, b):\n    if not a:\n        return 0\n"
                "    if not b:\n        return 0\n    return a + b\n", "f")
    r = verify_refactor(old, new)
    assert r["behavior_ok"]                # same external inputs feed the return
    assert "deltas" in r


# -- opportunities runner ------------------------------------------------

def test_opportunities_are_ranked_and_tagged():
    g = build("def f(a):\n    x = a + 1\n    return a\n", "f")
    opps = run_passes(g)
    assert opps
    for o in opps:
        assert o.soundness in ("sound", "heuristic")
        assert o.modality in ("must", "may")
        assert o.ref and o.render()
