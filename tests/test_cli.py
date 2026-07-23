"""End-to-end CLI: commands run and exit codes follow RFC §10."""

import textwrap

import pytest

from pflow.cli import main


@pytest.fixture
def sample(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(textwrap.dedent("""
        def step(self, lease):
            x = acquire(lease)
            try:
                y = use(x)
            except KeyError:
                y = None
            finally:
                cleanup()
            return y

        def helper(a):
            return step(None, a)
    """))
    return p


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cfg_success(capsys, sample):
    code, out, _ = run(capsys, "cfg", f"{sample}:step", "-a")
    assert code == 0
    assert "CFG step" in out


def test_dataflow_var(capsys, sample):
    code, out, _ = run(capsys, "dataflow", f"{sample}:step", "--var", "y")
    assert code == 0
    assert "defs of y" in out


def test_walk_runs(capsys, sample):
    code, out, _ = run(capsys, "walk", f"{sample}:step",
                       "--from", "step:def:x@3", "--edges", "data")
    assert code == 0
    assert out.strip()


def test_slice_runs(capsys, sample):
    code, out, _ = run(capsys, "slice", f"{sample}:step",
                       "--from", "use:y@10", "--backward")
    assert code == 0


def test_metrics_runs(capsys, sample):
    code, out, _ = run(capsys, "metrics", f"{sample}:step")
    assert code == 0
    assert "cyclomatic=" in out


def test_callgraph_links_calls(capsys, sample):
    code, out, _ = run(capsys, "callgraph", str(sample))
    assert code == 0
    # helper() calls step() -> exactly one in-program edge, step has a caller.
    assert "1 edges" in out
    assert "step" in out


def test_state_command(capsys, sample):
    code, out, _ = run(capsys, "state", str(sample))
    assert code == 0
    assert "STATE map" in out


def test_trace_command(capsys, sample):
    code, out, _ = run(capsys, "trace", str(sample), "--value", "lease",
                       "--from", "step")
    assert code == 0
    assert "TRACE value=lease" in out


def test_show_def(capsys, sample):
    code, out, _ = run(capsys, "show", "def:0", f"{sample}:step")
    assert code == 0
    assert "def:0" in out


def test_opportunities_runs(capsys, sample):
    code, out, _ = run(capsys, "opportunities", f"{sample}:step")
    assert code == 0
    assert "step:" in out  # qualname header (opportunities or "no opportunities")


def test_report_is_positional_census(capsys, sample):
    code, out, _ = run(capsys, "report", f"{sample}:step")
    assert code == 0
    # positions + targets ...
    assert "entry" in out and "exits" in out
    assert "targets →" in out
    # ... but no verdicts (those live in `opportunities`)
    low = out.lower()
    for verdict in ("dispatch", "split", "candidate", "decompose", "hotspot",
                    "juggles", "too ", "every path", " > "):
        assert verdict not in low, f"report leaked a conclusion: {verdict!r}"


def test_report_file_is_program_census(capsys, sample):
    # a bare file (or directory) target must produce the whole-program census,
    # not an error — this is the front door for "review this repo".
    code, out, _ = run(capsys, "report", str(sample), "--no-cache")
    assert code == 0
    assert "PROGRAM census" in out
    assert "largest" in out
    assert "targets →" in out
    assert "step" in out


def test_bare_file_error_points_at_report(capsys, sample):
    # per-function commands on a whole file must redirect to the census.
    code, _, err = run(capsys, "cfg", str(sample))
    assert code == 2
    assert "pflow report" in err


def test_program_opportunities_have_runnable_refs(capsys, sample):
    code, out, _ = run(capsys, "opportunities", str(sample), "--no-cache")
    assert code == 0
    assert "PROGRAM opportunities" in out
    # every finding must carry a file-qualified ref and a verify command,
    # otherwise the confirm-by-walking loop is broken at program scale.
    if "findings (0 sound · 0 heuristic)" not in out.replace("0 findings", ""):
        for line in out.splitlines():
            if line.strip().startswith("[") and "]" in line:
                assert "ref " in line, f"finding without ref: {line!r}"


def test_metrics_has_no_threshold_verdict(capsys, sample):
    # metrics is a positional measurement; the "(> N!)" hotspot judgment moved
    # to `opportunities`.
    code, out, _ = run(capsys, "metrics", f"{sample}:step")
    assert code == 0
    assert "cyclomatic=" in out
    assert "(>" not in out and "!)" not in out


def test_missing_function_exit_2(capsys, sample):
    code, _, err = run(capsys, "cfg", f"{sample}:nonexistent")
    assert code == 2
    assert "no function named" in err


def test_missing_file_exit_2(capsys):
    code, _, err = run(capsys, "cfg", "/no/such/file.py:f")
    assert code == 2


def test_syntax_error_exit_4(capsys, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n  pass\n")
    code, _, err = run(capsys, "cfg", f"{bad}:f")
    assert code == 4
