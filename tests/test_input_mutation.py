"""Input-mutation detector (pflow.analysis.input_mutation.find_input_mutation).

Flags a function writing through a parameter — attr store, subscript store/del,
or a mutating method — but only while the ORIGINAL (caller-supplied) binding
still reaches the write. `self`/`cls` are excluded; defensive-copy-then-mutate
is not flagged.
"""

from conftest import build

from pflow.analysis.input_mutation import find_input_mutation


def _kinds(g):
    return sorted(o.kind for o in find_input_mutation(g))


def test_flags_each_mutation_form():
    g = build('''
        def f(p, items, cfg):
            p.name = "x"
            items.append(1)
            cfg["k"] = 2
            del cfg["j"]
    ''', "f")
    assert _kinds(g) == [
        "attr-store", "method-mutation", "subscript-del", "subscript-store"]


def test_pure_reads_not_flagged():
    g = build('''
        def f(p, items):
            a = p.get("z")          # read accessor, not a mutator
            b = items[0]            # subscript LOAD
            c = sorted(items)       # builtin, not a method on the param
            return a, b, c
    ''', "f")
    assert find_input_mutation(g) == []


def test_defensive_copy_not_flagged():
    # The rebind kills the args def, so `p` no longer holds the caller's object.
    g = build('''
        def f(p):
            p = dict(p)
            p["k"] = 1
            p.update({"a": 2})
            return p
    ''', "f")
    assert find_input_mutation(g) == []


def test_self_and_cls_excluded():
    g = build('''
        def m(self, x):
            self.x = x
            self._cache.append(x)
            self.d["k"] = x
            return self.x
    ''', "m")
    assert find_input_mutation(g) == []


def test_plain_name_rebind_and_aug_not_flagged():
    # Rebinding a param name / `p += e` on a plain name is not a write THROUGH
    # the caller's object in the IR (no dotted target, no store_bases).
    g = build('''
        def f(p, n):
            p = p or {}
            n += 1
            return p, n
    ''', "f")
    assert find_input_mutation(g) == []


def test_local_same_name_not_confused_with_param():
    # `acc` is a local here; mutating it is fine. Only the param `dst` counts.
    g = build('''
        def f(dst):
            acc = []
            acc.append(1)
            dst.append(acc)
            return dst
    ''', "f")
    kinds = [(o.kind, o.title) for o in find_input_mutation(g)]
    assert len(kinds) == 1
    assert kinds[0][0] == "method-mutation"
    assert "`dst`" in kinds[0][1]


def test_two_param_mutations_on_one_line():
    g = build('''
        def f(dst, src):
            dst.append(src.pop())
    ''', "f")
    found = find_input_mutation(g)
    assert {o.title.split("`")[1] for o in found} == {"dst", "src"}


def test_all_findings_are_heuristic_may():
    g = build('''
        def f(p):
            p["k"] = 1
    ''', "f")
    found = find_input_mutation(g)
    assert found and all(o.soundness == "heuristic" and o.modality == "may"
                         for o in found)
