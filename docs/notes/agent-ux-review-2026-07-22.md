# Agent-UX review: why agents distrust `opportunities` and skip the graphs

Date: 2026-07-22. Scope: whole repo, dogfooded on pflow itself (`pflow <cmd> pflow`).
Two reported problems: (1) agents call `opportunities` output "not true or
excessive"; (2) agents default to `opportunities` instead of walking the
CFG/dataflow graphs. Both reproduced immediately. Diagnosis below, then what
was changed in this pass, then the roadmap that remains.

## Diagnosis

### Problem 2 first: the affordance gradient pointed at the verdict layer

Agents follow tool affordances, not documentation. Before this pass:

- `pflow report pflow/` → `error: module-level CFG not implemented` (exit 2)
- `pflow report pflow/ir/cfg.py` → same error
- `pflow opportunities pflow/` → 60 findings, review-shaped output

The ONLY command that accepted "the repo" and returned something shaped like a
deliverable was the pass layer. An agent told "analyze this repo with pflow"
tries the orient command, gets refused, tries `opportunities`, gets a ranked
list it can paste, and never comes back. `callgraph`/`state`/`trace` exist but
are three separate views with no unifying summary and no `targets →` handoff
into the per-function IR commands. The gradient, not agent laziness, produced
the behavior.

### Problem 1: four compounding causes of distrust

1. **The soundness mix is ~100% heuristic in practice.** On pflow itself:
   59/60 findings were `[heuristic/may]`, zero `[sound/*]`. Working code
   rarely has unreachable blocks or constant branches; complexity thresholds
   and input-mutation patterns fire constantly. So the *experienced* product
   was a style linter with compiler branding — and the RFC's careful
   sound/heuristic tagging was invisible because everything carried the same
   tag.

2. **Duplicate findings.** input-mutation emitted one finding per write site
   (`out.append()` ×2 → two identically-worded lines); complexity emitted up
   to four findings (cyclomatic/cognitive/nesting/live-span) for one function.
   One hotspot triple-counted reads as a tool inflating its own importance.

3. **Program mode dropped the refs.** `_print_program_opportunities` rendered
   `(.:_CFGBuilder._add_projection_attrs)` — no line, no walkable ref. The
   documented loop ("confirm each finding by walking to its ref") was
   physically impossible at exactly the scale agents use the command.
   Unverifiable verdict → dismissed verdict.

4. **True-but-not-actionable findings.** `mutates parameter out in place —
   calls out.append()` on a recursive collector whose *contract* is the
   out-param is technically correct and pragmatically noise. The pass had no
   way to distinguish "hidden mutation of caller data" from "in-place
   procedure by design".

Also found while dogfooding (both feed the "not true" perception):

- **Stale-cache bug**: `_pflow_fingerprint()` joined `"ir/cfg.py"` etc.
  against `pflow/analysis/`, so only `program.py` resolved — edits to
  cfg.py/graph.py/dataflow.py/interproc.py never invalidated the program
  cache. Agents could see graphs from before their own edits.
- **WIP registration break**: `scope-coupling` was in `PASS_CATALOG` but not
  `_registry()` → every `opportunities` invocation died with `error:
  'scope-coupling'` (raw KeyError).
- Single-file program roots rendered fqnames as `.:qualname`.

## What changed in this pass

- **`pflow report DIR|FILE` — the program census, new front door.** Files/fn/
  edges/errors, callgraph hubs, cycles, flagged state cells, entrypoints,
  largest functions (bb·branch·exit·span), ending in runnable `targets →`.
  Verdict-free, same contract as the function report. One command now answers
  "where is the structure in this repo and what do I walk next".
- **Merged findings.** complexity: one `hotspot` finding per function with a
  branch-heavy vs value-heavy reading (the actionable split). input-mutation:
  one finding per (param, kind) listing all sites, plus a contract
  discriminator — "returns nothing (procedure-style)" vs "also returns a
  value — mixed contract". Repo went 60 findings → 41, none duplicated.
- **Every finding is now verifiable.** Full file-qualified runnable refs in
  every mode, and a per-finding `verify →` line with the exact IR command that
  shows the structure behind the claim (slice --backward for branch/def
  findings, walk --edges data for stores, report for hotspots). Output is
  sectioned `sound (holds by construction)` / `heuristic (hypotheses)` with
  honest counts in the header, plus a header line naming the pass layer a
  cross-check and pointing at `pflow report`.
- **Cache fingerprint fixed** (+ format bump), single-file fqnames fixed,
  scope-coupling registered, `--exclude-tests` on all program commands,
  bare-file errors redirect to `pflow report`, generic CLI errors include the
  exception type.

## Phase 2 (same day): debugging views + trust upgrades — IMPLEMENTED

Goal directive: make pflow maximally useful for debugging and reasoning about
the structure of Python libraries; deep, decompiler-grade detail preferred.

- **`pflow impact DIR --focus FQ | --diff [REV]`** — blast radius. Reverse
  call closure (who can observe the change) by depth, forward closure (what
  the change stands on), state cells written-here-read-outside and
  read-here-written-outside, entrypoints reached, changed-line attribution
  per function in `--diff` mode (git hunks → innermost enclosing function via
  block line spans), smeared-edge accounting.
- **`pflow callgraph DIR --to FUNC [--focus FROM]`** — call chains reaching a
  function, shortest first, each hop carrying its `~k` resolution fan-out;
  entry annotated. Dynamic-only reachability comes back as an explicit
  "none found — maybe dispatch/getattr/callback" rather than silence.
- **`pflow catches DIR|file|func`** — exception-flow census from the IR's
  `enter_except`/`raise`/`return` ops: what each handler catches, `as` name,
  exit class (re-raise / raise-new / return / swallow / mixed), names set and
  calls made inside the handler region (dominance-scoped), `!broad`/`!bare`
  marks. Validated on asyncio: 280 handlers, 126 swallow, 59 broad, 0.8s.
- **`live:modname` program targets** — every program command now takes an
  importable module name, resolved via importlib.find_spec without importing;
  out-of-tree roots print once and rows go root-relative.
- **Edge-confidence (`~k`)** — program_callgraph records per-edge resolution
  fan-out (min over call sites: one sharp site beats many smeared ones); hubs
  whose fan-in is mostly smear are marked `~` in the census (asyncio's
  `set_result` "hubs" stop masquerading as architecture).
- **Percentile complexity** — program mode annotates each complexity finding
  with its per-metric percentile over the whole program (`p97 cyclomatic in
  this program`); metrics are memoized on the graph so this is free.
- **implied_condition (sound tier)** — dominator-implication redundant
  branches: `if C` under a dominating `if C` with no def of C's names on ANY
  path between (paths-between region, cycles included). SOUND/MUST for pure
  name conditions; call/attribute conditions demoted or — when any call
  intervenes (the wait()-then-recheck concurrency idiom, caught on
  queue.Queue in the corpus) — suppressed entirely. Closure `nonlocal`
  hazard demotes sound→heuristic when nested defs + intervening calls exist.

## Phase 3 (same day): structure + exception-propagation views — IMPLEMENTED

- **`pflow imports`** — the architecture layer above the call graph: eager
  vs lazy (function-level) import edges, import-time cycles, DEFERRED cycles
  (a lazy edge whose reverse path exists eagerly — deliberate cycle-breaking,
  all 10 in pflow's own pass registry surfaced), longest-path layering,
  fan-in hubs, external dependency surface, per-module edge listing.
  ModuleInfo gained `lazy_imports` (cache format 3).
- **`pflow raises`** — interprocedural exception escape to fixpoint:
  E(f) = local ∪ (callee escapes − types caught at the call's block), with a
  builtin exception-hierarchy table, bare-`raise` re-raise semantics, and
  propagation restricted to sharp (~k==1) call edges with the smeared-edge
  count reported. Focus mode prints witness chains (`TypeError ⇐ _load_graph
  L75 → build_cfg_from_live ⇐ … raise TypeError @_unwrap L24`). Program mode
  prints the entrypoint escape surface — what a library can throw at users.
  Validation: `json.loads` → JSONDecodeError · TypeError (matches docs);
  dogfooding it found a real bug in the freshly-written catches/raises code
  (get_block raises KeyError, code assumed None) — fixed.
- **`# pflow: ok(pass, ...)` suppression pragma** — line-level, per-pass
  scoping, suppressed counts surfaced in headers. Accepted heuristics stop
  re-firing across runs.

## Phase 4 (same day): precision + queryability — IMPLEMENTED

- **Negation-aware implication**: `if not c` under a dominating `if c` (and
  vice versa) now resolves through polarity normalization — decided value =
  (key value == re-test polarity). Same soundness ladder as phase 2.
- **Import-time work detection**: `imports` reports top-level statements that
  RUN on import (loops, calls, non-idiom try/with), with the optional-import
  try and `__main__`/TYPE_CHECKING guards whitelisted; top-level call-assigns
  (`LOG = getLogger(...)`) counted separately. ModuleInfo cache format 4.
- **`pflow report DIR --json`**: the census as ndjson — one object per
  function (structure, metrics, fan-in/out, handler + swallow counts) so
  agents query structure with jq instead of parsing tables
  (`select(.swallow > 0 and .fan_in > 5)`).

## Phase 5 (same day): the type layer — IMPLEMENTED

- **`pflow classes`** — inheritance forest with per-class method lists,
  overrides marked `*` (the sites where dynamic dispatch actually forks —
  i.e. where the callgraph's `~k` smears come from), diamonds referenced not
  re-rendered, external bases counted. asyncio's whole event-loop policy/
  loop/watcher hierarchy renders on one screen.

With this, every structural level has a view: modules (`imports`), classes
(`classes`), functions (`report`), calls (`callgraph`, `--to`), state
(`state`), values (`trace`, `slice`, `walk`), exceptions (`catches`,
`raises`), and change (`impact`). The pass layer (`opportunities`) remains
the cross-check sidecar over all of it.

## Phase 6 (same day): stdlib-scale hardening + resolution precision — IMPLEMENTED

Ran the whole of /usr/lib/python3.14 including site-packages (3,701 files,
72,190 fn) as a stress target. Everything below was found BY that run:

- **Decode robustness**: one latin-1 file killed the entire program build.
  Now `tokenize.open` (honors PEP 263 cookies) + per-file catch of
  UnicodeDecodeError/ValueError — 3 parse errors recorded, build survives.
- **Cache-save failures were silent** (`except: pass`) — now a one-line
  stderr warning; a lost 164MB cache no longer masquerades as a slow tool.
- **Resolution precision, three layers** (2.5M → 283k edges, 9×):
  1. Imported names resolve THROUGH the import and stop there —
     `os.path.join(...)` is external, not an edge to every in-program `join`
     (the old code literally commented "fall through to simple-name match").
     Handles aliases, `from x import f`, relative imports, lazy imports, and
     resolves class names to `__init__` (constructor edges are new).
  2. Bare builtin-name calls (`len(x)`) stop resolving to same-named methods.
  3. Global fallback capped at 24 candidates (past that a match carries no
     information), and builtin-type method names (`join`, `items`, `get`…)
     on attribute calls require near-uniqueness (≤3).
  Also fixed a pre-existing off-by-one: `from . import x` recorded as `..x`
  (one package too high) — both eager and lazy recordings.
- **Perf**: warm full-stdlib census 69s (~1ms/fn incl. 164MB cache load);
  asyncio-sized libraries stay sub-second. Per-module import maps memoized.
- **`--help` epilog** now teaches the layer model, so agents that only ever
  see the CLI discover report-first + verify-refs without external docs.

## Phase 7 (same day): review deltas + JSON symmetry — IMPLEMENTED

- **Structural deltas in `impact --diff`**: old file states rebuilt from
  `git show REV:path` through `_build_file`, per-function metrics diffed
  REV → worktree (`bb 24→30 · span 26→55`), added/removed functions listed
  with their full profile, flow-identical edits omitted. The review question
  "did this change make the flow heavier?" is now one command.
- **`--json` on `catches` and `raises`** (matching `report --json`): every
  major program view is now both a human screen and a queryable dataset.

## Phase 8 (same day): every deferred item closed

- **`raises --implicit`** (was "out of scope"): the IR now tallies subscript
  loads at lowering (`graph.attrs["subscript_loads"]`, one counter in
  `_extract_uses`); opt-in mode adds KeyError/IndexError from subscript
  loads and AttributeError from attribute access, function-granular,
  suppressed when any handler in the function catches the type. Default
  output unchanged — the noise argument held, so it's a flag, exactly the
  "right future shape" the roadmap predicted.
- **`pflow check`** (was "rename is the user's call"): added as an argparse
  alias — `opportunities` remains canonical, nothing breaks, both names work.
- **Cold-build parallelism** (was "lazy cache loading"): measured, not
  guessed. Lazy loading can't help census-style commands (they touch every
  graph). A ProcessPoolExecutor build was implemented and benchmarked on the
  full stdlib tree: 2m27 wall / 5m07 CPU on 8 cores vs ~2m15 serial —
  IPC-bound (shipping lowered graphs ≈ lowering them). Removed, with the
  measurement recorded in a comment so it isn't re-attempted blindly. The
  operating envelope stands: warm sub-second to ~1s for real libraries
  (≤5k fn), ~69s warm for the 72k-fn pathological whole-installation tree.

At this point the roadmap is empty: every item raised during the session was
either implemented, or measured and rejected with the evidence written down.

## Phase 9: seeker-dev as reference workload — sound-tier audit + IR ergonomics

Ran the full tour against seeker-dev (98 files, 2,584 fn, 5.9s cold) and
VERIFIED every sound/must claim against source. All five were false
positives — two new detector bug classes, both fixed + regression-tested:

- **Merge-arm implication bug**: for `if X: body` with no else, succs[1] IS
  the merge block and dominates everything after; the implied-condition
  detector read that as "X decided False" and flagged every later re-test
  (sign(), cmd_lease_status, cmd_node_show). Fix: a deciding arm must not
  post-dominate the branch.
- **Closure-write blindness**: `stop = False` + `nonlocal stop; stop = True`
  in a signal handler closure → const-prop called `stop` constant-False
  [sound/must]. Fix: lowering now records `nonlocal_writes` (names nested
  defs rebind) and `nonlocal_decls`/`global_decls` (own-scope declarations)
  on every graph; const-prop treats nonlocal-written names as volatile,
  implied-condition skips them, dead-store skips write-through stores
  (the closure's own `stop = True` was also flagged dead).

After the fixes seeker reports 0 sound / 96 heuristic findings, and the
top-ranked heuristic (dead `kind` in cmd_attach's tuple unpack) verified
TRUE against source. `raises` artifacts fixed the same round: `raise e` of a
bound name now re-raises the caught types (no more literal `e` escape), and
dotted spellings normalize (scheduler.AdmissionError == AdmissionError) —
seeker's API escape surfaces now read like documentation.

IR-layer ergonomics found by actually dissecting validate_service_payload
(81bb/41br/32 exits): the report's exits line now splits by kind
(`(32) return: bb4 bb123 · raise: …+28` — the error-gauntlet shape is one
line), `targets →` includes the value-returning exit ops (the canonical
"slice backward from the result" handle), and `cfg -a` prefixes every op
with its id so any op is addressable as `:op:N` on sight.

## Phase 10: systematic FP audit — every pass verified against seeker source

Method: dump every finding from every pass on seeker-dev, read the source at
each ref (all of the small passes, samples of the big ones). Results:

| pass | verdict | action |
|---|---|---|
| dead-store (1) | TRUE (`kind` unpack never read) | — |
| use-before-def (2) | path-infeasible (correlated guards, seeker's own comment says so) | finding now lists assignment sites + names the correlated-guard trap — verification is one look |
| lossy-projection (14) | 10 = validators (raising narrowers), 4 = deliberate wire projectors — `{**input}` advice was actively harmful for wire shapes | raising functions suppressed (validator contract); hint reworded as neutral position statement with key counts; 14 → 7 |
| input-mutation (33) | ~17 = `db.add()/update()` on a session object — container-method match on a non-container receiver | receiver-kind evidence: a param also receiving non-container methods (`commit`, `execute`) is an API object; method-mutation skipped; 33 → 16 |
| scope-coupling (10) | several are returned closures (factories) where capture IS the interface | returned-closure discriminator: detail says "factory pattern, fan-in by design" |
| catches | handlers calling `sys.exit` classified "swallow" | new `terminates` exit class |

Also chased a 234→237 handler-count drift to its true cause: seeker-dev
received two commits DURING the audit (live repo) — pflow was consistent.

Seeker totals after the audit: 96 → 72 findings, 0 sound / 72 heuristic,
top-ranked finding verified true. Regression tests for every fixed class.

## Phase 11: second/third codebases — pacifica + vllm

- **pacifica SDK** (165 fn, 0.5s): exposed the delegate-self-loop FP —
  `Sandbox.create → self._client.create(...)` resolved to ITSELF via the
  same-module suffix match (sharp, so the smear guards never fired) and
  rendered five fake "recursive:" cycles. Rule added: an attribute call
  through a non-self receiver never resolves to the calling method itself
  (`self.m()` stays step-1; bare-name recursion for plain functions kept —
  regression-tested both ways). Smeared self-loops also dropped from
  `cycles()`, and multi-node cycles that ride only ambiguous edges render
  with a `~`. pacifica now correctly shows ZERO cycles, and its raises
  surface reads as clean typed-SDK documentation (0 smeared edges).
- **vllm** (1,729 files, 22,771 fn, 49,492 edges, 0 parse errors, 73s cold):
  third-party scale validation. The census surfaces the real structure
  (VllmConfig.__post_init__ 162bb/87br, Scheduler.schedule,
  GPUModelRunner.input_batch W6/R32 shared channel) with fake container-name
  hubs honestly ~-marked.

## Phase 12: obol audit — four FP classes, one in the analysis CORE

obol (769 fn, 1.7s) surfaced the deepest bug of the session plus three more:

- **Exceptional liveness (core fix)**: liveness merged normal and except
  successors uniformly, so a block's kills applied on exception paths where
  the killing assignment may never have run — flagging the defensive-init
  idiom (`raw = None; try: raw = fetch(); except: pass; use(raw)`) as a dead
  store. compute_liveness is now hand-rolled with the split transfer
  (`live_in = use ∪ (out_normal − defs) ∪ out_exceptional`) and the per-op
  walk keeps handler-visible names alive. Three obol FPs died; the stdlib
  corpus golden held (true dead stores unaffected).
- **match-case guard captures**: `case Err(error=e) if "401" in e:` lowers
  to one branch op that binds AND reads `e`; pattern binding precedes the
  guard, so same-op def+use on a branch is neither unbound nor dead.
- **nonlocal reads in closures**: definite-assignment now excludes
  `nonlocal_decls` (reads resolve to the enclosing binding — was reported as
  guaranteed UnboundLocalError).
- **write-only nonlocal captures**: `_captured_names` treated Store-context
  names as locally bound, hiding `nonlocal last; last = now()` captures from
  the parent's liveness — nonlocal/global names now always count as free.

After the audit obol reports exactly ONE dead store — verified TRUE against
source (`is_error` defensive init overwritten on every reading path). Its 8
use-before-def findings all proved to be the above FP classes and are gone.

vllm deep views at scale: catches 856 handlers/5s (the ROCm feature-probe
try-import swallows render with their `sets:` flags), raises fixpoint 60s
over 22,771 fn (52 types; API routers show VLLMValidationError/
GenerationError surfaces), classes 4,621/5s. pacifica earlier in the session
killed the delegate-self-loop cycle FP.

## Phase 13: co-design round 1 — task-driven utility audit

Method change (otto): stop testing correctness, run REAL tasks per context
and log where the tool carries the work vs. where grep/reading takes over.

Scenario A — traceback debugging (seeker, "ValueError at validators.py:92"):
FAILED at step one — tracebacks speak file:line, every command spoke
file:qualname. Built **`pflow at file.py:LINE [--in DIR]`**: owning function
(line→innermost-span resolution), the line's ops, the transitive guard chain
via control dependence with condition text + decided arm (`L91 `not
isinstance(raw, list) or len(raw) > MAX_VOLUMES`=True`), reaching-def
provenance for the line's values, and inbound call chains sorted
production-first. One command replaces a 4-step manual workflow.

Scenario A also exposed the deepest modeling gap: app.py reaches the
validator as `_validated(validate_service_payload, payload)` — the function
travels as a VALUE, so production routes didn't exist in the callgraph (only
tests called it directly). Built **higher-order (address-taken) edges**: a
bare name passed as a call argument that resolves sharply (k<=2, not a local
variable) adds a caller→function edge, marked `-fn->` in chains. seeker's
production route (`create_service -fn-> validate_service_payload ->
parse_volumes`) now renders.

Scenario B — newcomer comprehension (vllm, "understand the scheduler"):
`callgraph --focus` drowned in `record` smear at depth 2. Built **`--sharp`**
(follow only k==1 edges; smeared children shown one-deep with `~k`, never
recursed into). The sharp subtree of Scheduler.schedule reads as actual
architecture: allocate_slots / get_computed_blocks / preemption.

Scenario C — flow tracing (obol, "how does a tool result reach the
conversation"): `trace` CARRIED IT unchanged (result → msg → new_messages →
run_turn:state → run_step). No build needed; noted trace hops could carry
line numbers (minor, deferred).

## Phase 14: co-design round 2 — vllm scale (22,771 fn)

Scenario D — traceback anchor inside a 126-block function
(scheduler.py:794, "Invalid request status"): the guard chain NARRATES the
bug in one line (status ≠ PREEMPTED ⇐ ≠ WAITING ⇐ not load_kv_async ⇐
new_blocks not None ⇐ token guards), 0.59s local. `--in` cost 90s because
program_callgraph recomputed per invocation → built the **callgraph disk
cache** (persisted beside the program cache, keyed by the build's file
digest, memoized on the ProgramGraph): 90s → 6.6s warm; every
interprocedural command inherits it.

Scenario E — state-corruption hunt (GPUModelRunner.input_batch W6/R32):
`--name` rows named writers but not SITES, and method-call mutation
(`self.input_batch.block_table.commit_block_table()`) was invisible → built
the **cell dossier**: `state --name X` matching ≤3 cells expands to every
write site with lines, every mutating method call on the cell, and readers.

Scenario F — "what does Scheduler.schedule touch?": no per-function state
view existed → the function report gains a **state footprint line**
(`writes self.X · mutates? self.Y (deep call paths) · reads self.Z`), with
`self.m()` method roots correctly excluded from reads.

## Phase 15: external-agent review response + first compile-level oracle

An independent agent reviewed pflow against seeker and filed three friction
items — all fixed, plus two the diagnosis pointed at:

- **Cycle fabrications dissolved at the root**: an attribute call can only
  land on a METHOD — `state.db.get_rental()` no longer edges to the app.py
  route function. seeker: 9 reported cycles → 4, all real. Census/callgraph
  headlines now tier `(N sharp · M smeared~)` when smear remains.
- **`--focus` accepts every pasteable spelling** (bidirectional path-suffix +
  qualname match): the census prints `server/cloud.py:f`, the user pastes
  `seeker/server/cloud.py:f`, both resolve.
- **`dataflow --anomalies`**: only path-dependent uses (multi-def, with def
  lines) and never-read defs; explicit one-line null result.
- **Bytecode definite-assignment oracle** (first compile-level integration):
  CPython 3.12+ emits LOAD_FAST_CHECK exactly where ITS flow analysis cannot
  prove a local bound. use-before-def findings on compiler-proven locals are
  dropped as artifacts; findings the compiler also flags carry "compiler
  agrees". compile() only, lazy (zero cost on clean functions), module-code
  cached per (path, mtime); graphs carry _abs_path so program mode works.
- **Walrus-in-comprehension FP** (found by the oracle's blind spot — the
  walrus target is a CELLVAR, invisible to co_varnames): `sum(now - t ... if
  (t := parse(r)))` leaked `t` as an unbound use; _collect_loads now binds
  NamedExpr targets left-to-right per PEP 572.

seeker after this round: use-before-def 2 → 1 (the survivor is
compiler-corroborated), cycles 9 → 4 (all real).

## Phase 16: the interpreter stack as co-analyst (goal: maximal use of
## CPython's own representations)

Premise correction first: CPython is not JIT-compiled by default — it always
compiles to bytecode (the 3.11+ adaptive interpreter specializes at runtime;
the 3.13 copy-and-patch JIT is experimental/off). The static artifacts of
that pipeline ARE available without executing anything, and pflow now uses
the two highest-value ones:

- **symtable (compiler symbol table)** — attached on every source-bearing
  build path via one pass per file (`ir/scopes.py`): compiler-verified
  locals, frees (captures, incl. read-only), declared globals, and
  descendant nonlocal writes now OVERWRITE the AST-walked approximations
  behind the same attrs (`nonlocal_decls`/`global_decls`/`nonlocal_writes`),
  with the AST walkers demoted to fallback for source-less builds. The four
  closure FP classes from the obol audit are now prevented by construction,
  not by our re-implementation of scope rules.
- **LOAD_FAST_CHECK oracle (3.12+)** — the compiler's own definite-assignment
  analysis: use-before-def findings on locals the compiler PROVED bound are
  dropped as artifacts; findings it also cannot prove carry "compiler
  agrees". compile()-only, lazy, module-code cached.

Measured/reasoned skips (so they aren't re-attempted blindly):
- **Exception-table tightening**: the table's protected offset ranges
  coincide with AST try-regions, and any instruction inside one may raise —
  the delta over current block-level edges is ~zero. (The existing
  `correct_exception_edges` is ADDITIVE, line-heuristic — do not wire it
  program-wide.) The real precision lever here would be per-op
  raise-potential, an IR change, not a bytecode read.
- **co_positions()**: AST spans already carry full line/col ranges; the
  bytecode mapping only wins when source is unavailable.
- **co_consts folding**: compiler folds constant expressions, but mapping
  folded results back to branch ops is not worth it for const-branch's
  literal scope.
- **Adaptive specialization / JIT uops**: runtime type feedback requires
  EXECUTING warm code — out of pflow's static stance. Documented as the
  possible future `live-hot:` mode (run a workload, read specialized
  instructions as poor-man's type inference — would collapse ~k smear for
  hot paths); Tier-2 uops have no stable API.

## Roadmap — all items above resolved (implemented or measured-and-rejected)

1. **Raise the sound-tier yield — this is the trust ceiling.** Distrust is
   rational when the sound tier is empty on real code. Candidates: dominator-
   implication redundant-branch (guard re-tested under a dominating guard),
   sound always-raises/never-returns propagation, exception-edge-aware dead
   cleanup. Every sound/must finding that survives contact builds credit the
   heuristic tier spends.
2. **Percentile thresholds in program mode.** Absolute thresholds
   (cyclomatic>12) fire on every mature codebase. In program mode the
   distribution is available: flag the top ~5% within the build (keep absolute
   floors as a minimum). "Largest in this repo" is defensible; "bigger than a
   constant" invites dismissal.
3. **Baseline / suppression.** `# pflow: ok(input-mutation)` pragma or a
   committed baseline file so accepted contracts stop re-firing. Without it,
   repeat runs re-surface the same accepted heuristics and re-teach agents to
   skim.
4. **Confidence on interprocedural edges.** Name-based resolution means
   `obj.method()` fans out to every same-named method; hubs/trace hops should
   carry the candidate count (`~k` marker) so agents know which edges are
   sharp and which are smeared.
5. **Consider renaming `opportunities` → `check`** (keep the old name as an
   alias). "Opportunities" sounds like the deliverable; "check" sounds like a
   linter you run after the real work. Naming is part of the gradient.
6. **Module-level code** is still invisible (functions only). Fine for now;
   the census should eventually count top-level statements per module.
