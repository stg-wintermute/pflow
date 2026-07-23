"""Debugging-oriented views: impact, call paths, catches, live: program targets."""

import subprocess
import textwrap
from pathlib import Path

import pytest

from pflow.cli import main


@pytest.fixture
def prog(tmp_path):
    (tmp_path / "core.py").write_text(textwrap.dedent("""
        def leaf(x):
            return x + 1

        def mid(x):
            try:
                return leaf(x) * 2
            except ValueError:
                return None

        def top(x):
            y = mid(x)
            try:
                return leaf(y)
            except Exception as e:
                log(e)
    """))
    (tmp_path / "other.py").write_text(textwrap.dedent("""
        def sweep(items):
            out = []
            for it in items:
                try:
                    out.append(it.strip())
                except AttributeError:
                    pass
            return out
    """))
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# -- impact ----------------------------------------------------------------

def test_impact_focus_upstream_and_downstream(capsys, prog):
    code, out, _ = run(capsys, "impact", str(prog), "--focus", "leaf", "--no-cache")
    assert code == 0
    assert "IMPACT" in out
    assert "upstream" in out and "mid" in out and "top" in out
    assert "entry reached" in out            # top is an entrypoint that reaches leaf


def test_impact_requires_focus_or_diff(capsys, prog):
    code, _, err = run(capsys, "impact", str(prog), "--no-cache")
    assert code == 2
    assert "--focus" in err and "--diff" in err


def test_impact_diff_maps_changed_lines_to_functions(capsys, prog, monkeypatch):
    git = lambda *a: subprocess.run(["git", "-C", str(prog), *a],
                                    capture_output=True, text=True, check=True)
    try:
        git("init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    src = (prog / "core.py").read_text()
    (prog / "core.py").write_text(src.replace(
        "return x + 1", "if x > 9:\n        return 9\n    return x + 2"))

    code, out, _ = run(capsys, "impact", str(prog), "--diff", "HEAD", "--no-cache")
    assert code == 0
    assert "core.py:leaf" in out             # the changed function was located
    assert "changed lines" in out
    assert "mid" in out                      # and its callers are in the closure
    # structural deltas: leaf gained a branch and an exit
    assert "structural deltas" in out
    assert "br 0→1" in out and "exit 1→2" in out


# -- call paths -------------------------------------------------------------

def test_call_paths_to_leaf(capsys, prog):
    code, out, _ = run(capsys, "callgraph", str(prog), "--to", "leaf", "--no-cache")
    assert code == 0
    assert "CALL PATHS to" in out
    assert "top -> mid -> leaf" in out or "mid -> leaf" in out
    assert "[entry:" in out


def test_call_paths_from_origin(capsys, prog):
    code, out, _ = run(capsys, "callgraph", str(prog),
                       "--focus", "top", "--to", "leaf", "--no-cache")
    assert code == 0
    assert "from core.py:top" in out


# -- catches ----------------------------------------------------------------

def test_catches_classifies_exits(capsys, prog):
    code, out, _ = run(capsys, "catches", str(prog), "--no-cache")
    assert code == 0
    assert "CATCHES" in out
    # mid: except ValueError -> return None => return class
    assert "return — error converted" in out
    # sweep: except AttributeError: pass => swallow class
    assert "swallow" in out and "except AttributeError" in out
    # top: except Exception as e -> log(e), falls through => swallow + broad + call list
    assert "!broad" in out and "calls: log" in out


def test_catches_single_function(capsys, prog):
    code, out, _ = run(capsys, "catches", f"{prog}/core.py:mid")
    assert code == 0
    assert "1 handler(s)" in out and "except ValueError" in out


def test_catches_sys_exit_is_terminates_not_swallow(capsys, tmp_path):
    (tmp_path / "cliish.py").write_text(
        "import sys\n\n"
        "def main():\n"
        "    try:\n        run()\n"
        "    except RuntimeError as e:\n"
        "        print(e)\n        sys.exit(2)\n")
    code, out, _ = run(capsys, "catches", str(tmp_path), "--no-cache")
    assert code == 0
    assert "terminates" in out and "1 terminates" in out
    assert "1 swallow" not in out


def test_lossy_projection_silent_for_validators(capsys, tmp_path):
    # a raising narrower is a validator: closing the record is its contract
    (tmp_path / "v.py").write_text(
        "def validate(p):\n"
        "    if not p.get('name'):\n        raise ValueError('name')\n"
        "    if not p.get('kind'):\n        raise ValueError('kind')\n"
        "    return {'name': p['name'], 'kind': p['kind']}\n\n"
        "def quiet_narrow(p):\n"
        "    a = p.get('a')\n"
        "    b = p.get('b')\n"
        "    return {'a': a, 'b': b}\n")
    code, out, _ = run(capsys, "opportunities", str(tmp_path),
                       "--pass", "lossy-projection", "--no-cache")
    assert code == 0
    assert "validate" not in out.split("quiet_narrow")[0] or "validate" not in out
    assert "quiet_narrow" in out               # the silent narrower still flags


def test_maybe_unbound_detail_names_assignment_sites(capsys, tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def f(c):\n"
                 "    if c:\n        y = 1\n"
                 "    return y\n")
    code, out, _ = run(capsys, "opportunities", f"{f}:f",
                       "--pass", "use-before-def")
    assert code == 0
    assert "assigned at L3" in out and "correlated guards" in out


def test_catches_handler_refs_are_quotable(capsys, prog):
    _, out, _ = run(capsys, "catches", str(prog), "--no-cache")
    assert ":bb:" in out                     # every handler row carries a block ref


# -- live: program targets ----------------------------------------------------

def test_report_live_module_census(capsys):
    code, out, _ = run(capsys, "report", "live:json", "--no-cache")
    assert code == 0
    assert "PROGRAM census" in out and "targets →" in out


# -- raises -------------------------------------------------------------------

def test_raises_propagates_over_sharp_edges(capsys, prog):
    # leaf raises nothing; add a raiser and check escape reaches the caller.
    (prog / "boom.py").write_text(
        "def blow(x):\n"
        "    if x:\n"
        "        raise ValueError('x')\n"
        "    return 1\n\n"
        "def outer(x):\n"
        "    return blow(x) + 1\n\n"
        "def guarded(x):\n"
        "    try:\n"
        "        return blow(x)\n"
        "    except ValueError:\n"
        "        return None\n")
    code, out, _ = run(capsys, "raises", str(prog), "--focus", "outer", "--no-cache")
    assert code == 0
    assert "ValueError" in out and "blow" in out          # witness chain
    code, out, _ = run(capsys, "raises", str(prog), "--focus", "guarded", "--no-cache")
    assert code == 0
    assert "none" in out                                  # caught at the call site


def test_raises_implicit_optin(capsys, prog):
    (prog / "imp.py").write_text(
        "def lookup(d, k):\n    return d[k]\n\n"
        "def guarded(d, k):\n"
        "    try:\n        return d[k]\n"
        "    except KeyError:\n        return None\n")
    # default: implicit sources invisible
    code, out, _ = run(capsys, "raises", f"{prog}/imp.py:lookup")
    assert code == 0 and "KeyError" not in out
    # opt-in: subscript load surfaces KeyError/IndexError with a count
    code, out, _ = run(capsys, "raises", f"{prog}/imp.py:lookup", "--implicit")
    assert code == 0
    assert "KeyError" in out and "IndexError" in out
    # a function-level KeyError handler suppresses the implicit KeyError
    code, out, _ = run(capsys, "raises", f"{prog}/imp.py:guarded", "--implicit")
    assert code == 0
    assert "KeyError" not in out.replace("caught in own try", "")


def test_check_is_an_alias_for_opportunities(capsys, prog):
    code, out, _ = run(capsys, "check", f"{prog}/core.py:leaf")
    assert code == 0
    assert "cross-check" in out or "no findings" in out


def test_raises_bound_name_reraise_and_dotted_normalization(capsys, prog):
    (prog / "renames.py").write_text(
        "import scheduler\n\n"
        "def a(x):\n"
        "    try:\n        return work(x)\n"
        "    except ValueError as e:\n"
        "        log(e)\n        raise e\n\n"       # re-raise, NOT type `e`
        "def b(x):\n"
        "    raise scheduler.AdmissionError(x)\n")  # normalizes to last component
    code, out, _ = run(capsys, "raises", f"{prog}/renames.py:a")
    assert code == 0
    assert "ValueError" in out and " e " not in out
    code, out, _ = run(capsys, "raises", f"{prog}/renames.py:b")
    assert code == 0
    assert "AdmissionError" in out and "scheduler.AdmissionError" not in out


def test_raises_single_function_local(capsys, prog):
    (prog / "one.py").write_text(
        "def f(x):\n"
        "    try:\n"
        "        raise KeyError(x)\n"
        "    except KeyError:\n"
        "        pass\n"
        "    raise RuntimeError('boom')\n")
    code, out, _ = run(capsys, "raises", f"{prog}/one.py:f")
    assert code == 0
    assert "RuntimeError" in out
    assert "caught in own try: KeyError" in out


# -- imports ------------------------------------------------------------------

def test_imports_layers_and_lazy_cycle(capsys, tmp_path):
    (tmp_path / "base.py").write_text("X = 1\n")
    (tmp_path / "mid.py").write_text("import base\n\ndef f():\n    return base.X\n")
    (tmp_path / "top.py").write_text(
        "import mid\n\ndef g():\n    import top_helper\n    return mid.f()\n")
    (tmp_path / "top_helper.py").write_text(
        "import top\n\ndef h():\n    return top.g()\n")
    code, out, _ = run(capsys, "imports", str(tmp_path), "--no-cache")
    assert code == 0
    assert "IMPORTS" in out and "layers" in out
    assert "base.py" in out and "d0" in out
    # top ⇢ top_helper is lazy while top_helper -> top is eager: deferred cycle
    assert "deferred cycles" in out and "top.py ⇢ top_helper.py" in out


# -- import-time work + JSON census -------------------------------------------

def test_imports_flags_import_time_work(capsys, tmp_path):
    (tmp_path / "worker.py").write_text(
        "import os\n"
        "CACHE = {}\n"
        "for k in os.environ:\n"          # runs at import: flagged
        "    CACHE[k] = 1\n"
        "LOG = make_logger()\n"           # call-assign: counted, not flagged
        "def f():\n    return CACHE\n")
    (tmp_path / "clean.py").write_text(
        "try:\n    import fastjson\nexcept ImportError:\n    fastjson = None\n"
        "if __name__ == '__main__':\n    print('cli')\n"
        "def g():\n    return 1\n")
    code, out, _ = run(capsys, "imports", str(tmp_path), "--no-cache")
    assert code == 0
    assert "import-time work" in out and "worker.py" in out and "for k in" in out
    assert "call-assigns" in out
    assert "clean.py" not in out.split("import-time work")[1].split("by module")[0]


def test_catches_and_raises_json(capsys, prog):
    import json
    code, out, _ = run(capsys, "catches", str(prog), "--json", "--no-cache")
    assert code == 0
    rows = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert any(r["exit"] == "swallow" and r["catches"] == "AttributeError"
               for r in rows)
    (prog / "boomer.py").write_text(
        "def blow():\n    raise ValueError('x')\n")
    code, out, _ = run(capsys, "raises", str(prog), "--json", "--no-cache")
    assert code == 0
    rows = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert any(r["qualname"] == "blow" and r["escapes"] == ["ValueError"]
               for r in rows)


def test_report_json_rows(capsys, prog):
    import json
    code, out, _ = run(capsys, "report", str(prog), "--json", "--no-cache")
    assert code == 0
    rows = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    by_name = {r["qualname"]: r for r in rows}
    assert by_name["mid"]["handlers"] == 1
    assert by_name["mid"]["fan_in"] == 1          # called by top
    assert by_name["leaf"]["fan_in"] == 2         # called by mid and top
    assert {"blocks", "cyclomatic", "live_span", "swallow"} <= set(by_name["top"])


# -- state cell dossier + footprint ----------------------------------------------

def test_state_cell_detail_shows_sites_and_method_calls(capsys, tmp_path):
    (tmp_path / "runner.py").write_text(
        "class Runner:\n"
        "    def __init__(self):\n"
        "        self.batch = []\n"
        "    def refill(self, items):\n"
        "        self.batch = list(items)\n"
        "    def poke(self, x):\n"
        "        self.batch.append(x)\n"          # mutation via method call
        "    def peek(self):\n"
        "        return self.batch\n")
    code, out, _ = run(capsys, "state", str(tmp_path), "--name", "Runner.batch",
                       "--no-cache")
    assert code == 0
    assert "write" in out and "Runner.refill" in out and "@L5" in out
    assert ".append()" in out and "Runner.poke" in out    # the invisible vector
    assert "read" in out and "peek" in out


def test_report_state_footprint_line(capsys, tmp_path):
    f = tmp_path / "r.py"
    f.write_text(
        "class R:\n"
        "    def step(self, x):\n"
        "        self.count = self.count + 1\n"
        "        self.q.push(x)\n"
        "        if self.mode:\n"
        "            return self._helper(x)\n"
        "        return None\n")
    code, out, _ = run(capsys, "report", f"{f}:R.step")
    assert code == 0
    assert "state" in out
    assert "writes self.count" in out
    assert "mutates? self.q" in out
    assert "mode" in out
    assert "_helper" not in out.split("state")[1].split("\n")[0]  # method ≠ read


def test_callgraph_cache_roundtrip(tmp_path, monkeypatch):
    import subprocess as sp
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    from pflow.analysis.program import build_program
    from pflow.analysis.interproc import program_callgraph
    fixture = str(Path(__file__).parent / "fixtures" / "prog")
    a = program_callgraph(build_program(fixture, use_cache=True))
    b = program_callgraph(build_program(fixture, use_cache=True))  # from disk
    assert {k: sorted(v) for k, v in a.edges.items()} == \
           {k: sorted(v) for k, v in b.edges.items()}
    assert a.ambiguity == b.ambiguity and a.via_arg == b.via_arg


# -- at: the traceback anchor ----------------------------------------------------

def test_at_anchors_a_traceback_line(capsys, tmp_path):
    f = tmp_path / "svc.py"
    f.write_text(
        "MAX = 4\n\n"
        "def parse(raw, strict):\n"
        "    limit = MAX\n"
        "    if strict:\n"
        "        if not isinstance(raw, list) or len(raw) > limit:\n"
        "            raise ValueError('bad')\n"       # line 7
        "    return raw\n\n"
        "def handler(req):\n"
        "    return parse(req, True)\n")
    code, out, _ = run(capsys, "at", f"{f}:7", "--in", str(tmp_path), "--no-cache")
    assert code == 0
    assert "AT" in out and "parse" in out and "spans" in out
    assert "guards" in out and "strict" in out          # the guard chain
    assert "limit ← L4" in out                          # value provenance
    assert "handler -> parse" in out                    # inbound chain
    assert "targets →" in out


def test_at_rejects_non_line_targets(capsys, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("def f():\n    return 1\n")
    code, _, err = run(capsys, "at", f"{f}:notaline")
    assert code == 2 and "file.py:LINE" in err


# -- higher-order (address-taken) edges -------------------------------------------

def test_function_passed_as_argument_creates_marked_edge(capsys, tmp_path):
    (tmp_path / "ho.py").write_text(
        "def check(p):\n    return p\n\n"
        "def _guarded(fn, p):\n    return fn(p)\n\n"
        "def route(p):\n    return _guarded(check, p)\n")
    code, out, _ = run(capsys, "callgraph", str(tmp_path), "--to", "check",
                       "--no-cache")
    assert code == 0
    assert "route -fn-> check" in out


# -- review-feedback round (external agent on seeker) ----------------------------

def test_attribute_call_cannot_hit_module_function(capsys, tmp_path):
    # `state.db.get_rental()` must NOT edge to a module-level route named
    # get_rental in another file — attribute calls land on methods only
    # (fabricated a 4-node "rental cycle" in the seeker review).
    (tmp_path / "app.py").write_text(
        "def get_rental(rid):\n    return rid\n")
    (tmp_path / "cloud.py").write_text(
        "def patch(state, rid):\n    return state.db.get_rental(rid)\n")
    code, out, _ = run(capsys, "callgraph", str(tmp_path), "--to", "get_rental",
                       "--no-cache")
    assert code == 0
    assert "patch" not in out                       # no fabricated chain


def test_focus_accepts_longer_pasted_path(capsys, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("def f():\n    return g()\n\ndef g():\n    return 1\n")
    # census prints `mod.py:f`; a user pastes the longer CWD-relative form
    code, out, _ = run(capsys, "callgraph", str(pkg),
                       "--focus", f"{pkg}/mod.py:f", "--no-cache")
    assert code == 0 and "callgraph from" in out and "g" in out


def test_dataflow_anomalies_mode(capsys, tmp_path):
    f = tmp_path / "a.py"
    f.write_text(
        "def merged(c):\n"
        "    if c:\n        x = 1\n"
        "    else:\n        x = 2\n"
        "    return x\n\n"
        "def plain(a):\n"
        "    y = a + 1\n"
        "    return y\n")
    code, out, _ = run(capsys, "dataflow", f"{f}:merged", "--anomalies")
    assert code == 0 and "path-dependent" in out and "2 defs" in out
    code, out, _ = run(capsys, "dataflow", f"{f}:plain", "--anomalies")
    assert code == 0 and "no anomalies" in out


# -- callgraph precision ---------------------------------------------------------

def test_delegate_same_name_is_not_recursion(capsys, tmp_path):
    # Sandbox.create -> self._client.create(...) must not fabricate a
    # self-loop: an attribute call through a non-self receiver never
    # resolves to the calling method itself (pacifica SDK false cycles).
    (tmp_path / "sdk.py").write_text(
        "class Client:\n"
        "    def create(self, spec):\n        return spec\n\n"
        "class Sandbox:\n"
        "    def __init__(self, client):\n        self._client = client\n"
        "    def create(self, spec):\n"
        "        return self._client.create(spec)\n\n"
        "def countdown(n):\n"
        "    return 0 if n <= 0 else countdown(n - 1)\n")
    code, out, _ = run(capsys, "callgraph", str(tmp_path), "--no-cache")
    assert code == 0
    assert "recursive: Sandbox.create" not in out
    assert "recursive: countdown" in out      # real recursion still reported


# -- classes --------------------------------------------------------------------

def test_classes_hierarchy_and_overrides(capsys, tmp_path):
    (tmp_path / "shapes.py").write_text(
        "class Base:\n"
        "    def area(self):\n        return 0\n"
        "    def name(self):\n        return 'base'\n\n"
        "class Circle(Base):\n"
        "    def area(self):\n        return 3\n\n"
        "class Square(Base):\n"
        "    def area(self):\n        return 4\n"
        "    def corners(self):\n        return 4\n\n"
        "class Weird(Circle, Square):\n"
        "    def area(self):\n        return 5\n\n"
        "class Loner(dict):\n"
        "    def peek(self):\n        return None\n")
    code, out, _ = run(capsys, "classes", str(tmp_path), "--no-cache")
    assert code == 0
    assert "CLASSES" in out and "hierarchy" in out
    assert "Base" in out and "Circle" in out
    assert "area*" in out                       # override marked
    assert "diamond" in out                     # Weird reached via two parents
    assert "dict×1" in out                      # external base surfaced


# -- suppression pragma ---------------------------------------------------------

def test_pragma_suppresses_findings(capsys, tmp_path):
    f = tmp_path / "s.py"
    f.write_text("def f(a):\n"
                 "    x = a + 1  # pflow: ok(dead-store)\n"
                 "    y = a + 2\n"
                 "    return a\n")
    code, out, _ = run(capsys, "opportunities", f"{f}:f")
    assert code == 0
    assert "suppressed" in out
    assert ":def:x@" not in out            # pragma'd line gone
    assert ":def:y@" in out                # the other dead store stays
