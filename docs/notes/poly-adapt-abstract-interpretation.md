# Adapting the Abstract-Interpretation / Monotone-Dataflow Toolkit to pflow

Synthesis from the `poly` research corpus (`/home/lafiel/work/lafiel/poly/docs/research/`).
Source files: `lattice_and_abstraction.miner` (84 rec), `complexity_and_limits.miner`,
`computation_theory.miner`. All claims carry a `[ref: file.miner:id]`. Query with `mq`,
never read raw `.miner`.

Mode: fast (decisive, ranked, honest about overkill).

pflow today: source-level Python tool. Intra/inter-procedural CFGs, dominators/post-dominators,
name-based reaching-definitions / def-use, slices, call graph, shared-state map, conservative
interprocedural value tracing. Mission: help a code-review agent SIMPLIFY control/data flow and
catch flow bugs; "good enough for review" > full soundness. pflow has NO generic
lattice/AI framework, NO liveness, NO SSA, NO constant/value analysis, NO control-dependence/PDG.

This note maps the theory to concrete pflow capabilities. Format per item:
concept -> pflow tool/command -> effort (S/M/L) -> corpus ref.

Honest framing up front: pflow analyzes *dynamic Python at source level*. The corpus theory
is about *affine programs* where analysis is EXACT (decidable, complete). Python is the opposite
extreme: Rice's theorem says every non-trivial semantic property is undecidable in general
[ref: complexity_and_limits.miner:4], and Python adds dynamic typing, `eval`/`getattr`,
monkeypatching, reflection, and arbitrary `__getattr__`. So pflow is permanently in the
SOUND-BUT-IMPRECISE (may-analysis, over-approximate) or PRECISE-BUT-UNSOUND (best-effort review
heuristic) regime. The value of the corpus is not "make pflow sound" — it is "give pflow ONE
clean framework so each new flow check is ~50 lines instead of a bespoke pass, and so each check
states honestly whether it is must/may."

--------------------------------------------------------------------------------

## 1. The monotone dataflow framework: ONE generic worklist solver

### 1.1 The core theory

A dataflow analysis is a triple `D = (L, meet, F)` [ref: lattice_and_abstraction.miner:13]:
- `L` — a bounded meet-semilattice. `top` = "no information yet", `bottom` = "contradiction".
- `meet` — greatest lower bound, applied at control-flow merges.
- `F` — a set of monotone transfer functions `f: L -> L`, one per statement/edge.

Kildall 1973 gave the first lattice-based formulation and the iterative worklist algorithm:
`OUT[i] = f_i(IN[i])`, `IN[i] = meet of OUT[preds(i)]`, initialize to `top`, propagate to fixpoint
[ref: lattice_and_abstraction.miner:15]. Termination is guaranteed when the lattice has finite
height (Ascending Chain Condition) — the Kleene chain `bot <= f(bot) <= f^2(bot) <= ...` stabilizes
in finitely many steps [ref: lattice_and_abstraction.miner:63, lattice_and_abstraction.miner:8].

The fixpoint exists because the lattice is complete and `f` is monotone (Knaster-Tarski:
`lfp(f) = inf{x : f(x) <= x}`, `gfp(f) = sup{x : x <= f(x)}`)
[ref: lattice_and_abstraction.miner:16, lattice_and_abstraction.miner:63]. Tarski guarantees
EXISTENCE; Kleene gives the CONSTRUCTIVE iteration but needs continuity — which finite lattices
get for free [ref: lattice_and_abstraction.miner:16].

MFP vs MOP [ref: lattice_and_abstraction.miner:13]:
- MOP (meet-over-all-paths) = the ideal answer = meet of `f_path(top)` over every path to a point.
- MFP (maximal/least fixpoint) = what the worklist actually computes.
- For DISTRIBUTIVE frameworks `f(a meet b) = f(a) meet f(b)`: MFP = MOP (exact).
- For merely MONOTONE (non-distributive) frameworks: MFP <= MOP (the solver is SAFE but may be
  less precise than the ideal). Reaching-defs is distributive; constant-propagation is NOT
  [ref: lattice_and_abstraction.miner:85].

Backward analyses (liveness) are the same framework with edges reversed and meet = union
[ref: lattice_and_abstraction.miner:85].

### 1.2 pflow capability

**Concept** -> Generic monotone worklist solver parameterized by a lattice.
**pflow tool/command** -> A new `pflow.dataflow` core module exposing a `Lattice` protocol
(`top`, `bottom`, `join`/`meet`, `leq`, `widen` optional) and a `Transfer` protocol
(`apply(node, in_value) -> out_value`), plus one `solve(cfg, lattice, transfer, direction)`
worklist driver that returns `IN[]`/`OUT[]` per CFG node. Refactor the existing ad-hoc gen/kill
reaching-defs into the FIRST client of this solver: reaching-defs becomes
`lattice = Powerset(defs)`, `meet = union`, `transfer = (X \ kill) | gen`
[ref: lattice_and_abstraction.miner:85]. After that, every new analysis (liveness, constants,
nullness) is a `Lattice` + `Transfer` pair, not a new graph traversal.
**Effort** -> M. The solver itself is S (the worklist is ~40 lines and pflow already has CFGs +
predecessor/successor edges). The M cost is migrating existing reaching-defs onto it without
regressing def-use/slices, and defining the `direction` (forward/backward) abstraction cleanly.
**Corpus ref** -> [ref: lattice_and_abstraction.miner:13, lattice_and_abstraction.miner:15,
lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:63]

Design notes pulled from the corpus:
- Make `leq` and `join` the primitives; the solver only needs "is OUT changed?" (`not leq(new, old)`)
  and "merge predecessors" (`join`). This is exactly Kildall's loop [ref: lattice_and_abstraction.miner:15].
- Require finite height OR a `widen` hook (see section 3). Document the ACC requirement in the
  `Lattice` protocol docstring; an infinite-height lattice without `widen` is a non-terminating bug
  [ref: lattice_and_abstraction.miner:8].
- Worklist order: process in reverse-postorder for forward analyses to cut iterations. pflow already
  has dominators, so it has the spanning structure to compute RPO cheaply.
- Liveness for free: once the solver supports `direction=backward, meet=union`, liveness is
  `transfer = use | (X \ def)` [ref: lattice_and_abstraction.miner:85]. pflow lacks liveness today;
  this is the cheapest high-value add the framework unlocks (dead-store / unused-assignment findings).

--------------------------------------------------------------------------------

## 2. Concrete abstract domains worth shipping for source review

The corpus repeatedly frames classical analyses as instances of one framework
[ref: lattice_and_abstraction.miner:85]. Below, each domain is given as
(lattice, transfer, review payoff), ranked by value for a Python *review* tool. Each is a
`Lattice`+`Transfer` pair on the section-1 solver.

### 2.1 Definite assignment / use-before-def  (MUST analysis) — HIGHEST VALUE

**Lattice** -> per variable, `{Unassigned, MaybeAssigned, DefinitelyAssigned}` with the order
`DefinitelyAssigned < MaybeAssigned < Unassigned` (think of it as the dual of reaching-defs:
"is there a path on which this name is read before any binding?"). Merge = take the WEAKER state
(meet toward `MaybeAssigned`). This is a MUST-style analysis: a name is definitely-assigned at a
use only if it is assigned on ALL incoming paths. Conceptually it is the "all paths" / greatest-
fixpoint dual of the may-style reaching-defs already in pflow [ref: lattice_and_abstraction.miner:85].
**Transfer** -> a binding (`x = ...`, `for x in`, `with ... as x`, `import x`, function params,
`global`/`nonlocal`) moves the var to `DefinitelyAssigned`; entry to a branch merges with `meet`;
`del x` moves back to `Unassigned`.
**Review payoff** -> flags genuine `UnboundLocalError` / use-before-assignment bugs: a name read
where the lattice value is not `DefinitelyAssigned`. This is one of the few checks that is close to
SOUND for the "must" direction (see section 4) and is a classic Python footgun (assign-in-one-branch,
read-after-loop-that-may-not-run, augmented-assign before bind). Highest review value because the
false-positive rate is naturally low and the bug class is real.
**Effort** -> S once the solver exists (it is a finite 3-point-per-variable lattice, no widening).
**Corpus ref** -> framework instance [ref: lattice_and_abstraction.miner:85]; MUST vs MAY
distinction [ref: lattice_and_abstraction.miner:50]; fixpoint backing [ref: lattice_and_abstraction.miner:63].

### 2.2 Nullness / optional ("may be None here")  (MAY analysis) — HIGH VALUE

**Lattice** -> per variable/SSA-name: `{Bottom < NonNull, Null < MaybeNull(top)}` — a small finite
lattice. This is structurally the constant-propagation lattice [ref: lattice_and_abstraction.miner:85]
specialized to the two-point value domain {is-None, is-not-None} plus top/bottom.
**Transfer** -> `x = None` -> `Null`; `x = <literal/constructor/non-None expr>` -> `NonNull`;
`x = f(...)` where return is unknown -> `MaybeNull`; guard refinement: inside `if x is not None:`
narrow `x` to `NonNull` on the true edge, `Null` on the false edge (guards intersect, exactly the
interval-domain "guards intersect" rule [ref: lattice_and_abstraction.miner:85]). Merge of two paths
giving `Null` and `NonNull` = `MaybeNull` (top), the same join-to-top behavior as constants
[ref: lattice_and_abstraction.miner:85].
**Review payoff** -> "this attribute access / subscript / call happens on a value that may be None
here" — the single most common runtime crash class in Python review. Because it is a MAY analysis it
is SOUND for "definitely-not-None" claims only in the absence of dynamic escape hatches; in practice
ship it as a high-signal *review hint*, not a hard error.
**Effort** -> M. The lattice is S, but it needs guard-aware transfer (reading `if`/`while` conditions
and narrowing on the branch edges) which pflow does not do today. The guard-refinement plumbing is
the cost; it is reusable by 2.4 and 2.5.
**Corpus ref** -> [ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:9]

### 2.3 Constant propagation  (MAY, non-distributive) — MEDIUM VALUE

**Lattice** -> `Variable -> {Top, <constant c>, Bottom}`, three levels: `Top` (unknown/not-constant)
> `{c1, c2, ...}` (the specific constants) > `Bottom` (unreachable). Join is per-variable meet; two
paths with different constants -> `Top` [ref: lattice_and_abstraction.miner:85].
**Transfer** -> `x := c` sets `c`; arithmetic on constant operands folds to a constant; anything
non-constant -> `Top`. NOTE: constant propagation is NOT distributive — merge of "x=5 OR x=7" is
`Top` even though each path is individually constant — so MFP < MOP here; the solver is still sound,
just imprecise at merges [ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:13].
**Review payoff** -> dead-branch detection (`if FLAG:` where FLAG is a known constant), constant
guard simplification, and feeding the simplifier ("this condition is always true -> the reviewer can
delete the branch"). Directly serves pflow's SIMPLIFY mission.
**Effort** -> M. Lattice + folding transfer is S; the value comes from wiring it into a "branch is
dead / condition is constant" review finding and the simplifier.
**Corpus ref** -> [ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:1]

### 2.4 Sign  (MAY) — LOW/MEDIUM VALUE

**Lattice** -> `{Bottom < (+), (0), (-) < (+-)=Top}` (the original Cousot-Cousot worked example:
abstract-evaluating `-1515*17` over `{(+),(-),(+-)}` yields `(-)` [ref: lattice_and_abstraction.miner:1]).
**Transfer** -> sign arithmetic rules (`(-)*(-) = (+)`, etc.); guards `x > 0` narrow to `(+)`.
**Review payoff** -> negative-index / negative-size hints, "this can be zero -> possible div-by-zero
or empty-range." Niche for general Python review; more useful in numeric/array code.
**Effort** -> S (tiny finite lattice, reuses 2.2's guard plumbing).
**Corpus ref** -> [ref: lattice_and_abstraction.miner:1, lattice_and_abstraction.miner:85]

### 2.5 Intervals / ranges  (MAY, INFINITE lattice — needs widening) — MEDIUM, but costly

**Lattice** -> `Variable -> [l, u] or bottom`, ordered by containment, `Top = [-inf, +inf]`. Join =
`[min(l1,l2), max(u1,u2)]` [ref: lattice_and_abstraction.miner:85]. Storage O(n), all ops polynomial
[ref: lattice_and_abstraction.miner:29]. This lattice has INFINITE ascending chains, so the worklist
does NOT terminate without widening [ref: lattice_and_abstraction.miner:8, lattice_and_abstraction.miner:74].
**Transfer** -> affine assignments compute a new interval; guards intersect
[ref: lattice_and_abstraction.miner:85].
**Review payoff** -> out-of-bounds index hints, off-by-one on ranges, loop-counter bounds. Real value
for numeric code, but for general review the precision rarely survives Python's dynamic operations.
**Effort** -> L. This is the first domain that forces the widening machinery (section 3) into the
solver, plus interval arithmetic and guard intersection. Honestly: likely overkill for pflow's
"good enough for review" bar on general Python — recommend deferring unless a numeric-code reviewer
is a target user.
**Corpus ref** -> [ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:29,
lattice_and_abstraction.miner:74]

### 2.6 Type-state — MEDIUM VALUE, but Python-specific design

**Lattice** -> per resource/object: a finite state machine lattice (e.g. file: `{Unopened, Open,
Closed, Error=Top}`). This is a finite-domain analysis (ACC holds, no widening). It is a Galois-
connection / finite-lattice instance per the framework [ref: lattice_and_abstraction.miner:17].
**Transfer** -> method calls drive state transitions (`open()` -> Open, `close()` -> Closed, use after
Closed -> Error); merges meet to the conservative state.
**Review payoff** -> "file/socket/lock used after close", "resource opened but not closed on this
path", double-close. High-value bug class; pairs naturally with pflow's shared-state map.
**Effort** -> M. Finite lattice is S; the cost is the per-API state-machine spec and tracking object
identity across the call graph (which pflow's interprocedural value tracing partially supports).
**Corpus ref** -> [ref: lattice_and_abstraction.miner:17, lattice_and_abstraction.miner:85]

### 2.7 (Reference only) Relational equality domains — OVERKILL for pflow

Karr 1976 computes the strongest AFFINE EQUALITY invariants (`c0 + c1*x1 + ... = 0`) exactly for
affine programs over a finite lattice of affine subspaces [ref: lattice_and_abstraction.miner:71].
Octagons (`+-xi +- xj <= c`, O(n^2) memory, O(n^3) ops) and full polyhedra (arbitrary `Ax <= b`,
join exponential) sit higher on the precision/cost hierarchy
[ref: lattice_and_abstraction.miner:54, lattice_and_abstraction.miner:29,
lattice_and_abstraction.miner:74]. These are EXACT and powerful for affine numeric loops, which is
precisely what Python source review is NOT. Verdict: do not ship. They are the right tool for a
polyhedral compiler [ref: lattice_and_abstraction.miner:43], not a dynamic-Python review aid. Listed
so a future contributor does not mistake "more precise domain" for "more useful here."

--------------------------------------------------------------------------------

## 3. Widening / narrowing, and why approximation is principled

### 3.1 The theory

On a lattice with INFINITE ascending chains (intervals, octagons, polyhedra), Kleene iteration may
diverge and the worklist never terminates [ref: lattice_and_abstraction.miner:60,
lattice_and_abstraction.miner:8]. Widening `nabla: L x L -> L` fixes this
[ref: lattice_and_abstraction.miner:60]:
1. Upper bound: `a <= a nabla b` and `b <= a nabla b` (like join).
2. Convergence: any ascending chain fed through `nabla` stabilizes in finite steps.
Replace `b_{i+1} = f(b_i)` with `b_{i+1} = b_i nabla f(b_i)` — converges in finitely many steps to a
SOUND OVER-APPROXIMATION of `lfp(f)` [ref: lattice_and_abstraction.miner:60]. Interval widening:
if the lower bound is decreasing push it to `-inf`, if the upper bound is increasing push it to
`+inf` [ref: lattice_and_abstraction.miner:85]. Narrowing `Delta` then refines the over-approximation
back DOWN toward the fixpoint from above, recovering precision lost by widening
[ref: lattice_and_abstraction.miner:60].

Why this is principled, not a hack — Galois connections + soundness. An abstraction is a pair
`(alpha, gamma)` with `alpha(c) <= a  <=>  c <= gamma(a)` [ref: lattice_and_abstraction.miner:9].
`alpha` = best abstract approximation, `gamma` = concretization. The analysis is SOUND iff the
abstract transfer over-approximates the concrete one: `alpha(F(c)) <= F#(alpha(c))`
[ref: lattice_and_abstraction.miner:9]. Soundness needs only `<=` (over-approximate);
COMPLETENESS (exact `=`) is "an ideal and rare situation"
[ref: lattice_and_abstraction.miner:50, lattice_and_abstraction.miner:39]. The 1979 framework proves
a BEST abstract transformer always exists: `f# = alpha . f . gamma`
[ref: lattice_and_abstraction.miner:32, lattice_and_abstraction.miner:59]. So "approximate" here has a
precise meaning: every widened/abstracted result still safely contains every real behavior — you only
ever lose precision, never soundness, provided the transfer functions over-approximate.

Two facts that bound the design space:
- Widening/narrowing is STRICTLY more powerful than finite-lattice (Galois-only) analysis: it lets
  infinite domains terminate and express invariants no finite domain can
  [ref: lattice_and_abstraction.miner:67, lattice_and_abstraction.miner:17].
- For FINITE-height lattices you need NEITHER widening nor narrowing — the Kleene chain just
  terminates [ref: lattice_and_abstraction.miner:8, lattice_and_abstraction.miner:63].

### 3.2 pflow capability

**Concept** -> Optional `widen(old, new)` (and later `narrow`) hook on the `Lattice` protocol;
the solver calls it at loop-header merge points after a few iterations.
**pflow tool/command** -> Solver gains a per-node iteration counter; once a node (a CFG loop header,
which pflow can identify via back-edges / dominators) exceeds a threshold, apply `widen` instead of
`join`. Domains that are finite-height (2.1 definite-assign, 2.2 nullness, 2.3 constants, 2.4 sign,
2.6 type-state) leave `widen` unimplemented and the solver never calls it. Only intervals (2.5)
need it.
**Effort** -> M for the solver hook + interval widening; the narrowing pass is an additional M and is
optional (precision-only, never affects soundness).
**Verdict for pflow** -> Build the `widen` HOOK now (cheap, keeps the framework honest and prevents a
silent non-termination bug if someone adds an infinite-height lattice), but do NOT prioritize an
actual widening-based domain. Every high-value domain in section 2 (2.1-2.4, 2.6) is finite-height
and needs no widening at all [ref: lattice_and_abstraction.miner:8]. Widening only pays off if you
ship intervals (2.5), which is itself marginal for dynamic Python review.
**Corpus ref** -> [ref: lattice_and_abstraction.miner:60, lattice_and_abstraction.miner:9,
lattice_and_abstraction.miner:32, lattice_and_abstraction.miner:50, lattice_and_abstraction.miner:67,
lattice_and_abstraction.miner:8]

--------------------------------------------------------------------------------

## 4. What this buys toward "absolute truth": soundness, must vs may, and where Python forces unsoundness

### 4.1 The honest ceiling

Rice's theorem: EVERY non-trivial semantic property of programs is undecidable in general — proven by
reduction from the halting problem [ref: complexity_and_limits.miner:4]. "Does S1 depend on S2?",
"does this loop run exactly k times?", "is x ever None here?" are all semantic properties, hence
undecidable for arbitrary programs [ref: complexity_and_limits.miner:4]. The collecting (exact)
semantics — the set of all reachable states per program point — is the most precise analysis possible
and is exactly as hard as the halting problem [ref: lattice_and_abstraction.miner:61]. So pflow can
NEVER compute exact flow facts for arbitrary Python. This is not an engineering gap; it is a theorem.

The escape hatch the corpus identifies is RESTRICTION: affine programs make dependence/legality
DECIDABLE via Presburger arithmetic [ref: complexity_and_limits.miner:4,
complexity_and_limits.miner:15]. But that escape is unavailable to pflow: Python source is the
unrestricted class, plus dynamic typing, `getattr`/`setattr`, `eval`/`exec`, decorators,
metaclasses, `__getattr__`, monkeypatching, C extensions, and reflection. pflow lives in the regime
where only SOUND APPROXIMATION (over-approximate, may-analysis) or DELIBERATE UNSOUND HEURISTIC
(best-effort review hint) is available.

### 4.2 Must vs may — the one distinction pflow should make explicit per finding

- MAY analysis (union/over-approximate, least-fixpoint forward): "X *can* happen on some path."
  Sound for ABSENCE claims when nothing escapes: if a may-reaching-defs analysis says no definition
  of `x` reaches a use, that is trustworthy (modulo dynamic escapes). Reaching-defs (pflow already
  has it), nullness (2.2), constants (2.3), sign (2.4) are may [ref: lattice_and_abstraction.miner:85].
- MUST analysis (intersection/"all paths"): "X happens on EVERY path." Sound for PRESENCE claims.
  Definite-assignment (2.1) is must: "this name is NOT definitely assigned on all paths reaching this
  use" is a sound bug report (again modulo dynamic escapes) [ref: lattice_and_abstraction.miner:50,
  lattice_and_abstraction.miner:85]. Liveness "must" facts and available-expressions are the other
  classic must analyses [ref: lattice_and_abstraction.miner:85].

Soundness requires only the `<=` (over-approximation) direction of the Galois condition
[ref: lattice_and_abstraction.miner:9, lattice_and_abstraction.miner:50]; pflow does NOT need
completeness/exactness for any of this to be useful. The actionable rule: every pflow finding should
be tagged `MUST` or `MAY` and phrased accordingly ("definitely unbound on path P" vs "may be None
here"), so the review agent knows whether the tool is asserting a fact or raising a hint.

### 4.3 Where Python forces UNSOUNDNESS (be explicit, do not pretend)

The corpus tells us soundness holds only when abstract transfer functions over-approximate the
concrete ones [ref: lattice_and_abstraction.miner:9]. Python features where pflow CANNOT
over-approximate without going to `Top` everywhere (which destroys all value), so pflow must
deliberately under-approximate and accept unsoundness:
- Dynamic attribute access / reflection (`getattr`, `setattr`, `__getattr__`, `__dict__` writes):
  pflow's name-based def-use cannot see these binds; definite-assignment and nullness may MISS defs.
- `eval`/`exec`/`compile`: arbitrary unseen effects; any analysis must treat these as `Top` to stay
  sound, but pflow's review mission favors ignoring them for signal.
- Aliasing / mutation through shared references: pflow's shared-state map is a conservative
  approximation, not a sound points-to; interprocedural value tracing is explicitly "conservative,"
  i.e. best-effort, not sound.
- Decorators / metaclasses / monkeypatching: rebind functions and methods invisibly to source-level
  name analysis; the call graph is an approximation.
- Exceptions and `finally`: control edges pflow may not model fully; affects which paths "reach" a use.

Recommended pflow posture, justified by the theory: pflow is a PRECISE-leaning, INTENTIONALLY-UNSOUND
review aid for these dynamic features, and a SOUND over-approximate analyzer for the static core
(plain assignments, straight-line + structured control flow). Surface the distinction in output: a
finding is "sound (no dynamic escapes detected in scope)" vs "best-effort (dynamic features present;
may miss cases)". That is the closest pflow gets to "absolute truth" — and it is genuinely useful:
the static core of most real functions IS analyzable, and Rice only bites on the dynamic tail
[ref: complexity_and_limits.miner:4].

**Effort to add the MUST/MAY + sound/best-effort tagging** -> S. It is a metadata field on findings
plus a per-analysis declaration of its lattice direction; no new algorithms.
**Corpus ref** -> [ref: complexity_and_limits.miner:4, complexity_and_limits.miner:15,
lattice_and_abstraction.miner:61, lattice_and_abstraction.miner:9, lattice_and_abstraction.miner:50,
lattice_and_abstraction.miner:85]

--------------------------------------------------------------------------------

## 5. Effort / value summary table

| # | Capability                              | Effort | Value to pflow | Needs widening? |
|---|-----------------------------------------|--------|----------------|-----------------|
| 1 | Generic monotone worklist solver        | M      | Foundational   | hook only       |
| 2.1| Definite-assignment (use-before-def)   | S      | HIGHEST        | no (finite)     |
| 2.2| Nullness / optional (may be None)      | M      | HIGH           | no (finite)     |
| 2.6| Type-state (use-after-close etc.)      | M      | HIGH           | no (finite)     |
| 4.2| MUST/MAY + sound/best-effort tagging   | S      | HIGH           | n/a             |
| 1b| Liveness (backward, from solver)        | S      | MEDIUM-HIGH    | no (finite)     |
| 2.3| Constant propagation (dead branches)   | M      | MEDIUM         | no (finite)     |
| 2.4| Sign                                    | S      | LOW-MEDIUM     | no (finite)     |
| 2.5| Intervals / ranges                      | L      | MEDIUM (numeric only) | YES      |
| 3 | Widening/narrowing machinery            | M      | LOW (only if 2.5) | -            |
| 2.7| Octagons / polyhedra / Karr equalities | L      | OVERKILL       | yes / n/a       |

--------------------------------------------------------------------------------

## 6. Bottom line

The corpus's single most valuable gift to pflow is structural, not algorithmic: collapse the ad-hoc
gen/kill reaching-defs into ONE generic worklist solver parameterized by a (Lattice, Transfer, direction)
triple [ref: lattice_and_abstraction.miner:13, lattice_and_abstraction.miner:15]. Once that exists,
the high-value review checks — definite-assignment (use-before-def), nullness, type-state, and liveness
— are each a small finite-height lattice with no widening needed
[ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:8]. Widening, narrowing,
octagons, polyhedra, and Karr-style equality domains are the machinery of EXACT affine analysis
[ref: lattice_and_abstraction.miner:60, lattice_and_abstraction.miner:54, lattice_and_abstraction.miner:71];
they are overkill for dynamic Python source review and should be skipped or stubbed. The "absolute
truth" ceiling is Rice's theorem [ref: complexity_and_limits.miner:4]: pflow can be soundly
over-approximate on the static core and must be honestly best-effort on the dynamic tail — and it
should TAG every finding with which regime it is in.
