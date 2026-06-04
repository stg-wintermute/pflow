"""Reference parsing and resolution."""

from conftest import build, defs_named

from pflow.refs import parse_ref, resolve, resolve_node, Ref


def test_parse_full_ref():
    r = parse_ref("orchestrator.py:Reconciler.step:def:lease@87")
    assert r.path == "orchestrator.py"
    assert r.qualname == "Reconciler.step"
    assert r.kind == "def"
    assert r.ident == "lease@87"


def test_parse_bracketed_ref():
    r = parse_ref("[mod.py:f:bb:3]")
    assert r.kind == "bb"
    assert r.ident == "3"
    assert r.path == "mod.py"


def test_parse_short_forms():
    assert parse_ref("bb:3").kind == "bb"
    assert parse_ref("def:0").kind == "def"
    assert parse_ref("use:7").kind == "use"
    assert parse_ref("op:5").kind == "op"


def test_ref_str_roundtrip():
    r = Ref(path="m.py", qualname="f", kind="bb", ident="2")
    assert str(parse_ref(str(r))) == str(r)


def test_resolve_block():
    g = build("def f(a):\n    return a\n", "f")
    info = resolve(g, "bb:0")
    assert info["found"] and "block" in info


def test_resolve_def_by_id_and_canonical_key():
    g = build("def f(a):\n    x = a\n    return x\n", "f")
    info = resolve(g, "def:0")
    assert info["found"]
    # Regression: resolve used to write 'def_' while callers read 'def'.
    assert "def" in info
    assert "def_" not in info


def test_resolve_def_by_name_line():
    g = build("""
        def f(a):
            x = 1
            x = a
            return x
    """, "f")
    # x is defined on source lines 3 and 4 (after dedent).
    node = resolve_node(g, "f:def:x@4")
    assert node is not None
    ntype, nid = node
    assert ntype == "def"
    d = next(d for d in g.defs if d.id == nid)
    assert d.source[0] == 4


def test_resolve_use_by_name_line():
    g = build("""
        def f(a):
            x = a
            return x
    """, "f")
    node = resolve_node(g, "use:x@4")
    assert node is not None
    assert node[0] == "use"


def test_resolve_stale_ref_reports_error():
    g = build("def f(a):\n    return a\n", "f")
    info = resolve(g, "bb:999")
    assert not info["found"]
    assert "error" in info
