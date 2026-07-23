# pflow

Explicit program flow graphs (CFG, def-use, slices, walk) for Python code
review agents.

pflow turns Python source (or live objects) into addressable, queryable
control-flow and data-flow artifacts — basic blocks, exception edges,
reaching definitions, def-use chains, dominators, and program slices — so a
review agent can operate on real compiler-style structures instead of
reconstructing them from linear text.

See `docs/rfcs/RFC-0001-pflow-python-program-flow-graphs.txt` for the full
design.

## Install

```sh
uv pip install -e .          # provides the `pflow` command
# or run without installing:
PYTHONPATH=. python3 -m pflow.cli <command> ...
```

## Usage

Default output is dense and agent-oriented; pass `--human` for the decorated
view.

```sh
# orient — start here at any scale (function, file, or whole package)
pflow report DIR                     # program census: hubs, cycles, state, largest fns, targets →
pflow report DIR --json              # one JSON object per function — query with jq
pflow report file.py                 # per-file census
pflow report file.py:func            # per-function positional census

# structure & flow (per function)
pflow cfg file.py:func [-a] [--human] [--view source|bytecode]
pflow dataflow file.py:func [--var NAME]
pflow walk  --from <ref> [--depth N] [--direction forward|backward|both] [--edges control|data|both]
pflow slice --from <ref> [--backward|--forward] [--depth N]   # PDG slice (control + data)
pflow paths --from <ref> --to <ref> [--depth N]
pflow show  <ref>
pflow metrics file.py:func           # cyclomatic, cognitive, nesting, depdegree, live-span

# whole-program (a directory/package, a file, or `live:modname` for any
# importable library — resolved via importlib, no path needed)
pflow callgraph DIR [--focus relpath:Class.method]
pflow callgraph DIR --to FUNC [--focus FROM]   # call chains reaching FUNC ("how does execution get here")
pflow state DIR [--name X]
pflow trace --value NAME DIR [--from relpath:func]

# debugging views
pflow at file.py:LINE [--in DIR]               # traceback-frame anchor: owning function, the line's
                                               # ops, guard chain with conditions, value provenance,
                                               # and (--in) production-first call chains reaching it
pflow impact DIR --focus FQNAME [--depth N]    # blast radius: reverse call closure + state coupling
pflow impact DIR --diff [REV]                  # ...for every function touched by the git diff (default HEAD),
                                               # plus per-function structural deltas (bb/br/exit/cyc/cog/span)
pflow catches DIR                              # every except handler: what it catches, how it exits
                                               # (re-raise / raise-new / return / swallow), what it sets/calls
pflow raises DIR [--focus FQ]                  # exception escape: what calling a function can raise,
                                               # with witness chains (explicit raises, sharp edges only)
pflow raises DIR --implicit                    # also count subscript loads (KeyError/IndexError) and
                                               # attribute access (AttributeError), function-granular
pflow imports DIR                              # module import graph: layers, cycles, lazy (function-level)
                                               # edges, deferred cycles, import-time work, external deps
pflow classes DIR                              # inheritance forest, method overrides (*), external bases

# cross-check passes — verdicts to confirm against the graph, not a fix-list
pflow opportunities file.py:func     # or a file, or a DIRECTORY (alias: pflow check)
pflow verify old.py:func new.py:func # does a refactor preserve flow + reduce complexity?
```

The review workflow is graph-first: `report` orients (positions, no verdicts),
`cfg`/`walk`/`slice`/`paths`/`callgraph`/`state`/`trace` are the review itself,
and `opportunities` is a sidecar cross-check run afterwards. Its findings are
ranked, ref-tagged, soundness-tagged (`[sound/must]` = holds by construction;
`[heuristic/may]` = hypothesis, confirm against source), merged one-per-cause,
and each carries a `verify →` command — the exact IR-layer invocation that
shows the structure behind the claim. Passes: dead stores, unreachable code,
use-before-def (incl. read-in-`finally`), redundant/constant branches,
complexity hotspots, function-split candidates, scope coupling, lossy
projection, input mutation. See `docs/rfcs/RFC-0002-*` for the design.

Program commands accept `--exclude-tests` (skip `test_*` / `tests/`) and
`--no-cache` (ignore the per-file program cache).

Accepted findings can be silenced in place with `# pflow: ok` (all passes) or
`# pflow: ok(pass-name, ...)` on the flagged line; suppressed counts are
reported in the header.

Targets: `file.py:func`, `file.py:Class.method`, `live:module:qualname`.

Refs are stable, quotable handles an agent can feed back into the tool:

```
file.py:func:bb:3            a basic block
file.py:func:op:17           an operation
file.py:func:def:state@87    a definition site (name @ source line)
file.py:func:use:result@142  a use site
```

`walk`, `slice`, `paths`, and `show` accept the target inline in the ref, so
`pflow slice --from 'orchestrator.py:Reconciler.step:def:lease@87' --backward`
needs no separate target argument.

## Exit codes

| code | meaning |
| ---- | ------- |
| 0    | success |
| 2    | structural signal (target not found / no structure) |
| 3    | partial lowering failure |
| 4    | unsupported Python feature for v1 |

## Library use

```python
import pflow
g = pflow.analyze_with_hybrid(open("file.py").read(), "func")  # CFG + dis + dataflow + dominance
print(pflow.format_cfg_agent(g, include_dataflow=True, include_dominance=True))
```

## Tests

```sh
uv pip install -e '.[dev]'
pytest
```
