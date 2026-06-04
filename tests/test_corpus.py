"""Real-world corpus regression / false-positive guard.

The rest of the suite builds toy functions with `build("def f(): ...")`. But
*every* false positive pflow has shipped was found only on idiomatic code in
the field (the lock-guarded `with self._lock: return X` unreachable FP, the
exception-edge cyclomatic inflation, the with-block use-before-def). Toy tests
could not have caught them. This test runs the full opportunity pass-runner
over a frozen snapshot of real CPython stdlib modules (see
`fixtures/corpus/PROVENANCE.md`) and pins the result to a golden file.

What it buys:
  - Any change that introduces a NEW finding (a candidate false positive) or
    drops an existing one (a candidate lost true positive) fails loudly with a
    diff — before the agent has to find it reviewing a real codebase.
  - The `[sound/must]` "fact" tier gets an extra, emphatic guard: that tier is
    billed as ground truth, so a new sound/must finding must be a genuine,
    removable defect. Every sound/must entry in the golden has been reviewed
    (see the comment block beside the golden file).

To regenerate intentionally (after a deliberate pass change), run:
    PFLOW_UPDATE_GOLDEN=1 pytest tests/test_corpus.py
and REVIEW the diff — most of all any new `[sound/must]` line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pflow.program import _build_file
from pflow.analysis.opportunities import run_passes

CORPUS_DIR = Path(__file__).parent / "fixtures" / "corpus"
GOLDEN = Path(__file__).parent / "fixtures" / "corpus_findings.golden"


def _normalized_report() -> list[str]:
    """Deterministic, sorted finding lines over the whole corpus.

    A line is `file :: qualname :: pass/kind :: [soundness/modality] :: Lline`.
    Volatile fields (op/block ids, the numeric detail string) are deliberately
    omitted so the golden only churns when findings actually appear, vanish, or
    move lines — not when block numbering shifts.
    """
    lines: list[str] = []
    for path in sorted(CORPUS_DIR.glob("*.py")):
        src = path.read_text()
        _, funcs = _build_file(path.name, src)
        for fq, graph in funcs.items():
            for o in run_passes(graph):
                lines.append(
                    f"{path.name} :: {graph.qualname} :: "
                    f"{o.pass_name}/{o.kind} :: "
                    f"[{o.soundness}/{o.modality}] :: L{o.line}"
                )
    return sorted(lines)


def _sound_must(lines: list[str]) -> list[str]:
    return [ln for ln in lines if "[sound/must]" in ln]


def test_corpus_findings_match_golden():
    report = _normalized_report()
    text = "\n".join(report) + "\n"

    if os.environ.get("PFLOW_UPDATE_GOLDEN"):
        GOLDEN.write_text(text)
        pytest.skip(f"golden regenerated ({len(report)} findings) — review the diff")

    assert GOLDEN.exists(), (
        "missing golden; generate it with PFLOW_UPDATE_GOLDEN=1 pytest tests/test_corpus.py")
    expected = GOLDEN.read_text().splitlines()
    got = report

    if got != expected:
        exp = set(expected)
        new = [ln for ln in got if ln not in exp]
        cur = set(got)
        lost = [ln for ln in expected if ln not in cur]
        msg = ["corpus findings drifted from golden."]
        if new:
            msg.append(f"\n  NEW findings ({len(new)}) — candidate false positives:")
            msg += [f"    + {ln}" for ln in new[:40]]
        if lost:
            msg.append(f"\n  LOST findings ({len(lost)}) — candidate lost true positives:")
            msg += [f"    - {ln}" for ln in lost[:40]]
        msg.append("\n  If this change is intentional, regenerate with "
                   "PFLOW_UPDATE_GOLDEN=1 and review the diff.")
        pytest.fail("\n".join(msg))


def test_corpus_sound_must_tier_is_stable():
    """The fact tier gets its own loud guard. A new [sound/must] finding on the
    corpus means either a real defect we just started catching (good — review
    and bless it into the golden) or a regression in the tier billed as ground
    truth (bad — the kind of bug that erodes trust in the whole tool)."""
    if os.environ.get("PFLOW_UPDATE_GOLDEN"):
        pytest.skip("golden being regenerated")
    assert GOLDEN.exists()
    got = _sound_must(_normalized_report())
    expected = _sound_must(GOLDEN.read_text().splitlines())
    new = [ln for ln in got if ln not in set(expected)]
    assert not new, (
        "NEW [sound/must] finding(s) on the real-world corpus — the 'fact' tier "
        "must only flag genuinely-removable defects. Confirm each points at real, "
        "deletable source before blessing it into the golden:\n  "
        + "\n  ".join(new))
