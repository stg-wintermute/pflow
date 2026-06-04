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
# structure & flow (per function)
pflow cfg file.py:func [-a] [--human] [--view source|bytecode]
pflow dataflow file.py:func [--var NAME]
pflow walk  --from <ref> [--depth N] [--direction forward|backward|both] [--edges control|data|both]
pflow slice --from <ref> [--backward|--forward] [--depth N]   # PDG slice (control + data)
pflow paths --from <ref> --to <ref> [--depth N]
pflow show  <ref>
pflow metrics file.py:func           # cyclomatic, cognitive, nesting, depdegree, live-span
pflow report file.py:func

# simplification / defect passes — the tool flags opportunities, the agent decides
pflow opportunities file.py:func     # or a file, or a DIRECTORY (whole package)
pflow verify old.py:func new.py:func # does a refactor preserve flow + reduce complexity?

# whole-program (pass a directory/package)
pflow callgraph DIR [--focus relpath:Class.method]
pflow state DIR [--name X]
pflow trace --value NAME DIR [--from relpath:func]
```

`pflow opportunities` runs the analysis passes and emits ranked, ref-tagged,
soundness-tagged findings (`[sound/must]` = fact; `[heuristic/may]` = confirm
against source): dead stores, unreachable code, use-before-def (incl.
read-in-`finally`), redundant/constant branches, complexity hotspots, and
function-split candidates. See `docs/rfcs/RFC-0002-*` for the design.

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
