# Corpus provenance

These files are a **frozen, verbatim** snapshot of CPython standard-library
modules, vendored as a real-world false-positive / regression corpus for
`tests/test_corpus.py`. They are real, idiomatic, third-party Python (not code
written with pflow's analyses in mind), which is the point: the unit tests are
toy functions, and every false positive we have shipped was found only on
idiomatic code in the field. This corpus moves that detection in-tree.

| file           | source                          | exercises                                              |
|----------------|---------------------------------|--------------------------------------------------------|
| `contextlib.py`| CPython 3.14 `Lib/contextlib.py`| context managers, `with`, try/finally, generators, decorators, classes |
| `queue.py`     | CPython 3.14 `Lib/queue.py`     | locks/conditions, `with self._lock: …`, exceptions, the accessor idiom |
| `textwrap.py`  | CPython 3.14 `Lib/textwrap.py`  | comprehensions, regex, recursion, string munging       |

- Snapshot taken under Python 3.14.3 (the test interpreter at the time).
- License: PSF License Agreement (CPython). Vendored unmodified for test use.
- These are **frozen** — do not "update" them to track a newer stdlib. The
  golden findings file (`corpus_findings.golden`) is computed against exactly
  these bytes, so freezing keeps the regression check reproducible.

To refresh intentionally (e.g. broaden coverage), replace a file, then
regenerate the golden with `PFLOW_UPDATE_GOLDEN=1 pytest tests/test_corpus.py`
and **review the diff** — especially any new `[sound/must]` finding, which must
be a genuine, removable defect (see the test's docstring).
