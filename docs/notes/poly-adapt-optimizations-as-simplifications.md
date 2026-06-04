# Classic Compiler Optimizations as Simplification Analyses for pflow

Synthesis
Authors:   --
Created:   2026-05-29
Sources:   17 records across 2 corpora
             - poly compiler theory:  /home/lafiel/work/lafiel/poly/docs/research/
             - pflow detector notes:  /home/lafiel/work/lafiel/pflow/docs/notes/
Mode:      participatory


## 0. Framing

A classic compiler optimization is a transform: "this code can be rewritten to run
faster / smaller." Viewed from a code reviewer's seat, the *precondition* of each
transform is a simplification *signal*: "this flow can be made simpler." pflow does
NOT apply the transform. pflow surfaces the FINDING — the place where a reviewer (human
or agent) should consider simplifying control or data flow — and leaves the edit to them.

This reframing matters for soundness. A compiler must be sound to TRANSFORM: a wrong
dead-store elimination changes behavior. pflow only has to be sound enough to FLAG: a
wrong dead-store finding wastes a reviewer's minute. pflow's "good enough for review"
posture means it can lean on heuristic analyses that a real compiler could never ship,
because the cost of a false positive is a dismissed review comment, not a miscompile.

The theory side is anchored in the poly corpus; the Python-reality side (what is actually
sound vs noisy on dynamic Python, and what existing tools do) is anchored in the pflow
detector-research corpus under docs/notes/.

Every classical dataflow analysis pflow would build is an instance of ONE framework:
a bounded meet-semilattice (L, meet) with monotone transfer functions, solved by
iterate-to-fixpoint. [ref: lattice_and_abstraction.miner:15, lattice_and_abstraction.miner:13]
Constant propagation, CSE/available-expressions, reaching definitions, and live variables
are all named as instances of exactly this framework, each with a published transfer
function. [ref: lattice_and_abstraction.miner:85]


## 1. What pflow already has (verified against source)

  Confirmed by reading pflow/analysis/ and pflow/ir/:
  - CFG construction                          pflow/ir/cfg.py
  - Dominators / post-dominators              pflow/analysis/dominance.py
  - Slicing                                   pflow/analysis/slice.py
  - Path / reachability walks                 pflow/analysis/walk.py, paths.py
  - Call graph / interprocedural              pflow/analysis/callgraph.py, interproc.py
  - REACHING DEFINITIONS (forward, may, iterative fixpoint, gen/kill, def-use threading)
                                              pflow/analysis/dataflow.py

  The reaching-definitions pass is the keystone: it is a textbook forward may-analysis
  with last-write-wins gen[b], by-name kill[b], union join, and an iterate-to-convergence
  loop (out[b] = gen[b] | (in[b] - killed)). This exactly matches the classical instance.
  [ref: lattice_and_abstraction.miner:85]

  What pflow does NOT yet have:
  - LIVENESS (backward dataflow) — needed for dead-store findings
  - Constant/value lattice state — needed for constant/copy propagation findings
  - Available-expressions / value-numbering — needed for CSE/redundancy findings


## 2. The optimization -> analysis -> finding table

For each: the analysis required, whether pflow HAS it or NEEDS it, the review finding
pflow would emit, effort, and corpus ref. Soundness on dynamic Python is in section 4.

  ------------------------------------------------------------------------------------
  2.1  DEAD CODE ELIMINATION (unreachable blocks / dead branches)
  ------------------------------------------------------------------------------------
  Analysis required:  Forward reachability from entry over the CFG. HAS.
                      (Dead *branch* = a conditional whose guard is a constant; that
                      needs the constant lattice from 2.3, so split: pure-unreachable
                      is HAS, constant-guard-dead-branch leans on 2.3.)
  pflow finding:      "Block B / statement S has no path from entry — unreachable, safe
                      to delete." And: "branch arm is never taken."
  Effort:             S (reachability already in walk.py/paths.py).
  Confidence:         HIGH. This is the cleanest, most sound finding pflow can make.
  Theory ref:         [ref: complexity_and_limits.miner:54]  (CompCert ships verified DCE)
  Python-tool ref:    [ref: static-analysis-detectors-research.miner:51, static-analysis-detectors-research.miner:27]
                      (pylint W0101, CodeQL py/unreachable-statement, py/redundant-comparison;
                      the per-detector guide gives the exact algorithm: DFS from entry,
                      unmarked blocks are dead.)

  ------------------------------------------------------------------------------------
  2.2  DEAD STORE ELIMINATION (assignment never read)
  ------------------------------------------------------------------------------------
  Analysis required:  LIVENESS — classic BACKWARD may-analysis. NEEDS.
                      Domain = P(Variables); join = set union; transfer
                      f_S(X) = use_S ∪ (X \ def_S); solved as a backward fixpoint.
                      [ref: lattice_and_abstraction.miner:85]
                      A DEF at S is dead iff S's defined name is not in OUT[S].
                      [ref: static-analysis-detectors-research.miner:51]
  pflow finding:      "Assignment to x at S is never read on any path before being
                      overwritten or function exit — dead store."
  Effort:             M. It is the mirror image of the reaching-defs pass pflow already
                      has (same lattice machinery, reversed CFG, union join). The
                      gen/kill scaffolding in dataflow.py is directly reusable.
  Confidence:         HIGH that the analysis is correct; MEDIUM that the finding is
                      low-noise on Python (see 4).
  Note:               This is a REAL GAP in the Python ecosystem. pyflakes F841 / pylint
                      W0612 do NOT do liveness — they do scope-level "never referenced at
                      all" tracking and explicitly miss the "last write before return is
                      unread" case (pylint issue #5838). beniget builds true def-use
                      chains and CAN find last-write-never-read, but is non-path-sensitive.
                      [ref: static-analysis-detectors-research.miner:11, static-analysis-detectors-research.miner:33]
                      A true CFG+liveness dead-store finding is something pflow could do
                      that the common linters cannot — high differentiating value.

  ------------------------------------------------------------------------------------
  2.3  CONSTANT PROPAGATION / COPY PROPAGATION
  ------------------------------------------------------------------------------------
  Analysis required:  Constant-lattice dataflow. NEEDS.
                      Domain = (Variable -> {Top, constants, Bottom}); per-variable
                      3-level lattice; assignment x:=c sets c; merge of two different
                      constants = Top. [ref: lattice_and_abstraction.miner:85]
                      Copy propagation is the alias variant: x := y makes x an alias of y.
                      The equality/affine-relation version is Karr's domain (affine
                      subspaces, exact for affine programs). [ref: lattice_and_abstraction.miner:71]
  pflow finding:      Constant: "x is always 5 here -> inline it / this guard is constant."
                      Copy:     "x is just an alias of y -> collapse the indirection."
  Effort:             M for constant prop (new lattice + transfer, reuses fixpoint loop);
                      M-L for copy/alias prop (needs alias tracking, messier in Python).
  Confidence:         MEDIUM. The constant case feeds 2.1 (dead branches) and 2.6.
  Critical caveat:    Constant propagation is NOT a distributive framework. The merge of
                      "x=5 on path A" and "x=7 on path B" collapses to Top even though
                      each path is individually constant. So the FIXPOINT (MFP) is strictly
                      weaker than the ideal meet-over-all-paths (MOP) here.
                      [ref: lattice_and_abstraction.miner:85, lattice_and_abstraction.miner:13]
                      Practical effect for pflow: it will MISS some "always-constant"
                      facts (false negatives), but the facts it DOES report are sound.
                      For a reviewer tool, false negatives are acceptable; missing a
                      simplification is fine, asserting a wrong one is not.
  Python-tool ref:    [ref: static-analysis-detectors-research.miner:27]  (CodeQL constant
                      conditional / comparison-of-constants — the literal-constant subset.)
                      [ref: static-analysis-detectors-research.miner:33]  (beniget supports
                      constant folding via use-def chains.)

  ------------------------------------------------------------------------------------
  2.4  COMMON SUBEXPRESSION ELIMINATION / GLOBAL VALUE NUMBERING
  ------------------------------------------------------------------------------------
  Analysis required:  AVAILABLE EXPRESSIONS — forward MUST-analysis (intersection join).
                      NEEDS. Named directly as a Kildall instance.
                      [ref: lattice_and_abstraction.miner:15, lattice_and_abstraction.miner:85]
                      GVN is the value-numbering refinement: assign value numbers so that
                      expressions computing the same value are detected even across copies.
  pflow finding:      "Expression e is computed at S1 and again at S2 with no intervening
                      change to its inputs -> the second is redundant, hoist/reuse."
  Effort:             L. Requires an expression-identity notion (syntactic hashing at
                      minimum; value numbering for the global version) plus a forward
                      MUST-dataflow with intersection join — pflow currently only has a
                      MAY (union) pass. Available-expressions also requires a sound "kill"
                      on any write to a sub-term, which in Python means modeling aliasing
                      and side-effecting calls (hard).
  Confidence:         LOW-MEDIUM as a sound finding; the noise comes from Python's
                      side effects (see 4). Syntactic CSE ("identical subexpression text
                      twice, inputs look unchanged") is a cheap heuristic worth doing as a
                      hint even without full available-expressions.
  Theory ref:         [ref: complexity_and_limits.miner:54]  (CSE is in the verified-pass
                      lineage; poly itself implements classical CSE per the Dragon Book.)
                      [ref: optimization_theory.miner:81]

  ------------------------------------------------------------------------------------
  2.5  (PARTIAL) REDUNDANCY ELIMINATION / LOOP-INVARIANT CODE MOTION
  ------------------------------------------------------------------------------------
  Analysis required:  PRE = available-expressions + anticipated(very-busy)-expressions,
                      combined. LICM = "expression's operands are all defined OUTSIDE the
                      loop (loop-invariant) AND the expression is recomputed each
                      iteration." NEEDS (builds on 2.4 + loop structure, which pflow can
                      get from dominators identifying back-edges/loop headers — HAS the
                      loop-detection substrate).
  pflow finding:      LICM:  "this work does not depend on the loop variable and is
                              recomputed every iteration -> hoist out of the loop."
                      PRE:   "this value is already computed on some incoming paths ->
                              redundant on those paths."
  Effort:             L. LICM specifically is more tractable than full PRE: loop-invariance
                      = "all reaching defs of the operands are outside the loop body,"
                      which is computable from pflow's reaching-defs + dominator-based
                      loop identification. PRE (the partial / some-paths case) is the
                      hardest classical analysis here.
  Confidence:         MEDIUM for LICM hints, LOW for full PRE.
  Theory ref:         Loop optimization / code motion is Dragon Book Ch. 9 territory.
                      [ref: optimization_theory.miner:81]
                      The fixpoint/loop-invariant machinery is the abstract-interpretation
                      loop-invariant computation. [ref: complexity_and_limits.miner:112]

  ------------------------------------------------------------------------------------
  2.6  STRENGTH REDUCTION / USELESS-CONTROL-FLOW / JUMP THREADING / BRANCH SIMPLIFICATION
  ------------------------------------------------------------------------------------
  Analysis required:  Branch simplification / jump threading: constant-condition analysis
                      (from 2.3) + CFG edge reasoning. NEEDS (the constant part); HAS the
                      CFG/dominator substrate. Strength reduction (replace expensive op
                      with cheaper, e.g. i*4 -> i<<2 in a loop) is largely a peephole/
                      pattern transform with little payoff at Python source level.
  pflow finding:      "This branch is redundant / always taken -> the conditional can be
                      removed." "Reaching this elif implies the if-condition was false, so
                      the elif test is dead." "These two blocks can be merged (one is just
                      a jump to the other)."
  Effort:             S-M. The "always-taken / redundant-comparison" finding is cheap and
                      high-value; it is exactly CodeQL's py/redundant-comparison, which
                      needs CFG-level reasoning (knowing the path implies the prior guard).
                      [ref: static-analysis-detectors-research.miner:27]
  Confidence:         HIGH for literal-constant and redundant-comparison cases; MEDIUM
                      once it depends on the constant-propagation lattice.
  Note:               Strength reduction is the weakest fit for pflow's mission — it is a
                      micro-perf transform, not a "flow is simpler" review signal. Recommend
                      deprioritizing it.

  ------------------------------------------------------------------------------------
  2.7  SSA FORM (enabling form, not an optimization itself)
  ------------------------------------------------------------------------------------
  What it buys:       In SSA, every variable is assigned exactly once, and phi-functions
                      merge values at control-flow joins. This makes def-use UNIQUE: each
                      use points to exactly one def, so def-use chains become trivial and
                      most of the above analyses (constant prop = sparse conditional
                      constant prop, GVN, dead-code) get sharper and cheaper.
                      SSA is the standard substrate in real compilers; the Dragon Book
                      covers SSA construction and uses. [ref: optimization_theory.miner:81]
                      It is also the form that makes register allocation polynomial on the
                      SSA interference graph (chordal). [ref: optimization_theory.miner:7]
  What it costs pflow:
                      L. SSA construction needs dominance frontiers (pflow HAS dominators,
                      so the frontier is reachable) plus phi-insertion and variable
                      renaming. The renaming is where Python hurts: attributes, subscripts,
                      globals, and aliasing don't fit single-assignment cleanly, and
                      phi-functions at loop headers require the same fixpoint discipline.
  Recommendation:     Do NOT build full SSA first. pflow already has reaching-defs +
                      def-use threading, which is "good enough for review" and avoids the
                      renaming tax. Revisit SSA only if constant prop / GVN findings prove
                      too imprecise without it. Treat SSA as an OPTIONAL sharpening, not a
                      prerequisite.
  Confidence:         MEDIUM — the cost/benefit is real but the benefit is precision pflow
                      may not need at "good enough for review."


## 3. Pass ordering and the fixpoint / iterate-to-convergence framing

  3.1  The fixpoint framing is the unifying engine.
       Every analysis in section 2 is solved the same way: initialize lattice values,
       apply monotone transfer functions, iterate until nothing changes. Termination is
       guaranteed when the lattice has finite height (ascending/descending chain
       condition). This is Kleene iteration computing the least/greatest fixpoint of a
       monotone function on a complete lattice (Knaster-Tarski existence, Kleene
       constructivity). [ref: lattice_and_abstraction.miner:63, lattice_and_abstraction.miner:15]
       pflow's existing reaching-defs pass already embodies this loop, so the engine
       exists; new analyses are new (lattice, transfer, join direction) plugged into it.

  3.2  Distributive vs monotone -> exact vs approximate.
       If the transfer functions distribute over meet, the fixpoint (MFP) equals the ideal
       meet-over-all-paths (MOP) — the analysis is EXACT. If only monotone (not
       distributive), MFP <= MOP: the fixpoint is SOUND but possibly less precise than
       ideal. [ref: lattice_and_abstraction.miner:13]
       - Reaching definitions, live variables, available expressions: DISTRIBUTIVE -> exact.
         [ref: lattice_and_abstraction.miner:85]
       - Constant propagation: NOT distributive -> the fixpoint loses precision at merges
         (the "x=5 or x=7 -> Top" case). [ref: lattice_and_abstraction.miner:85]
       For pflow this is the dividing line between findings it can state crisply (2.1, 2.2,
       2.4-as-availability) and findings it must hedge (2.3, and anything built on it).

  3.3  Pass ordering is NP-hard, and "optimal order" still isn't "optimal result."
       Finding the globally optimal sequence of optimization passes is NP-hard, and finding
       globally optimal code is outright undecidable (reduction from halting).
       [ref: optimization_theory.miner:32]  Worse, even a perfect pass order does not
       guarantee optimal code, because the set of programs reachable by pass sequences is a
       strict subset of all equivalent programs. [ref: search_and_optimization.miner:20]
       Implication for pflow: pflow is NOT a pass pipeline trying to converge on optimal
       code, so it largely SIDESTEPS phase ordering. It runs analyses to PRODUCE FINDINGS,
       not to chain transforms. The only ordering pflow needs is a dependency order among
       its analyses (reachability -> constant lattice -> dead-branch; reaching-defs ->
       liveness -> dead-store; available-exprs -> CSE/LICM). That dependency DAG is small,
       fixed, and not subject to the NP-hard search. This is a genuine advantage of being a
       review tool rather than a compiler.


## 4. Sound vs heuristic on dynamic Python  [READ THIS BEFORE TRUSTING ANY FINDING]

  The compiler theory above assumes a language whose semantics you can pin down. Python's
  dynamic features break that assumption. Below, honest per-finding soundness:

  4.1  SOUND (low-noise, ship with confidence):
       - Unreachable-block DCE (2.1): pure CFG reachability is structurally sound. The
         only escape is code reached by exec/eval/exception magic, which doesn't create
         hidden CFG entries to a dead block.
       - Always-true/false literal guards and redundant comparisons (2.6): syntactic and
         CFG-level, high precision (CodeQL rates py/comparison-of-constants very-high).
         [ref: static-analysis-detectors-research.miner:27]

  4.2  SOUND ANALYSIS, NOISY ON PYTHON (flag, but expect false positives):
       - Dead store (2.2): liveness is exact on the CFG, but Python defeats it via
         getattr/setattr, locals()/globals() mutation, eval/exec, and intentionally-unused
         bindings. beniget's own documented limits: "cannot reliably handle eval and exec,
         star imports, and assigning to globals() or locals()"; and it is non-path-sensitive,
         so conditional assignments are not guaranteed defs.
         [ref: static-analysis-detectors-research.miner:33]
         Existing linters dodge this by using the weaker "never referenced at all" heuristic
         and accepting the dead-store gap. [ref: static-analysis-detectors-research.miner:11]
         pflow can do the real thing but MUST present it as a hint and honor _-prefix /
         dummy conventions to keep noise down.
       - Constant/copy propagation (2.3): non-distributive (loses precision at merges) AND
         undermined by dynamic rebinding, monkeypatching, and any name that could be touched
         by reflection. Sound as far as it goes; will miss a lot and can be fooled by
         dynamic writes it can't see.

  4.3  HEURISTIC / UNSOUND WITHOUT HEAVY MODELING (treat as suggestions only):
       - CSE / available-expressions / PRE (2.4, 2.5): the "kill" step requires knowing
         when an expression's value could change. In Python ANY attribute access or function
         call may have side effects or be a property with arbitrary behavior, so "this
         expression is unchanged between S1 and S2" is not statically decidable in general.
         A syntactic CSE hint ("identical text, no obvious intervening rebind") is useful but
         must be labeled heuristic. Full sound available-expressions is effectively out of
         reach for arbitrary Python.
       - LICM (2.5): "loop-invariant" similarly assumes the operands and the expression are
         side-effect-free across iterations — not guaranteed in Python. Useful as a hint.

  4.4  The bedrock reason none of this can be made fully sound for arbitrary Python:
       Rice's theorem — every non-trivial SEMANTIC property of programs is undecidable;
       "can this be replaced by a simpler equivalent?" is exactly such a property.
       [ref: complexity_and_limits.miner:88]  The Full Employment Theorem is the corollary:
       no analyzer detects all valid simplifications, and any claim to optimize an arbitrary
       program is either unsound or incomplete. [ref: complexity_and_limits.miner:14]
       The escape that real compilers use is to RESTRICT the language to a decidable subclass
       (e.g. the polyhedral/affine restriction). [ref: complexity_and_limits.miner:88]
       pflow's escape is different and cheaper: it does not transform, so it can be UNSOUND
       in the incomplete direction (miss things, over-suggest) and still be useful, as long
       as it never presents a heuristic hint as a guaranteed fact. The reviewer is the
       soundness backstop.

  4.5  Inherent sequentiality (minor, but worth knowing).
       Reaching-definitions and liveness are P-complete, i.e. not parallelizable to
       polylog depth unless P=NC. [ref: computation_theory.miner:66]  For pflow's per-function
       graphs this is irrelevant to performance, but it means there is no shortcut around the
       iterate-to-fixpoint loop — the sequential worklist is essentially the best you can do.


## 5. Open Questions

  - Does pflow want path-sensitivity anywhere? Every Python tool surveyed (beniget,
    pyflakes, pylint) is path-INsensitive, which is the dominant false-positive/negative
    source for dead-store and constant findings. A new miner run on "path-sensitive vs
    path-insensitive dataflow precision tradeoffs for review tools" would be warranted
    before committing to 2.2/2.3 precision targets.
  - How should pflow model side-effecting calls and attribute access for the "kill" step in
    CSE/available-expressions? The corpus establishes that this is the blocker (4.3) but does
    not give a pflow-grade approximation recipe. Candidate for a focused miner run.
  - SSA cost/benefit at "good enough for review": the corpus tells us SSA sharpens def-use
    and is standard, but not whether the precision gain justifies the Python-renaming tax for
    a non-transforming tool. This is a judgment call, not answered by the evidence.
  - The corpus is GPU/affine-compilation centered; it has the FOUNDATIONS (lattice dataflow,
    fixpoint, Rice, distributivity) but NOT modern Python-specific dataflow engineering. The
    docs/notes corpus partly fills this with detector-tool grounding, but a dedicated
    "intraprocedural dataflow for dynamic languages" run would strengthen 2.2-2.5.


## 6. Reading Trail

  1.  lattice_and_abstraction.miner:85  — START HERE. Names constant prop, reaching defs,
                                          live variables, available expressions as dataflow
                                          instances WITH their exact transfer functions and
                                          the constant-prop-is-not-distributive caveat. This
                                          single record is the spec for sections 2.2-2.4.
  2.  lattice_and_abstraction.miner:15  — Kildall: the lattice+iterative framework all the
                                          above sit in. Confirms pflow's existing fixpoint
                                          loop is the right engine.
  3.  lattice_and_abstraction.miner:13  — Kam-Ullman: MFP vs MOP, distributive=exact vs
                                          monotone=sound-but-approximate. The precision
                                          dividing line for section 3.2 / 4.2.
  4.  static-analysis-detectors-research.miner:51 — The Python-side implementation guide:
                                          per-detector exact CFG/dataflow property and
                                          algorithm (dead store = backward liveness, etc.),
                                          with Python gotchas (finally, except-erasure).
  5.  static-analysis-detectors-research.miner:11 + :33 — Why existing linters MISS true
                                          dead stores and what beniget's def-use chains can
                                          and cannot do. The differentiating-value case for
                                          pflow, plus the dynamic-Python soundness limits.
  6.  complexity_and_limits.miner:88 + :14 — Rice's theorem + Full Employment Theorem: the
                                          hard ceiling on soundness for arbitrary Python.
                                          Read once to internalize why pflow flags rather
                                          than transforms.
  7.  optimization_theory.miner:32      — Phase ordering NP-hardness, only if you are tempted
                                          to make pflow a transform pipeline. Tells you why
                                          not to.
