POLYHEDRAL DEPENDENCE ANALYSIS AND PROGRAM REPRESENTATIONS — WHAT TO ADAPT FOR PFLOW

Synthesis
Authors:   --
Created:   2026-05-29
Sources:   8 records across 3 files of the poly corpus
           (/home/lafiel/work/lafiel/poly/docs/research/)
           plus the L4 failure synthesis
           (poly/docs/research/synthesis/L4_what_failed_and_why.md)
Mode:      participatory
Scope:     Extract dependence-analysis and program-representation ideas
           adaptable to pflow (Python source-level CFG / def-use / dominators /
           post-dominators / slicing / call-graph for a code-review agent).
           Each item is framed as ADAPT (concrete pflow capability) or
           SKIP (with reason). Effort and corpus ref attached to each.


0.  Critical scoping note — read first

    The poly corpus is about POLYHEDRAL COMPILATION (loop optimization for
    dense numeric code), not about control-flow/program-dependence analysis
    of general programs. Three consequences shape this whole document:

    (a) The dependence-CLASSIFICATION theory pflow wants (flow / anti / output;
        loop-carried vs loop-independent; the dependence graph) IS present and
        well-grounded in the poly corpus, because polyhedral compilation is
        built on top of it. These records are directly usable. [HIGH confidence]

    (b) The Program Dependence Graph in the FERRANTE-OTTENSTEIN-WARREN sense
        (control dependence + data dependence, control dependence derived from
        post-dominators) is NOT in the poly corpus. The only "PDG" mention in
        the poly corpus (Allen-Kennedy, complexity_and_limits.miner:93) is a
        DATA-dependence-only graph with NO control-dependence edges. The FOW
        algorithm pflow needs to ground is therefore UNGROUNDED IN THIS CORPUS.
        See section 2 — I do not fabricate a poly ref for it. It is instead
        covered by pflow's OWN corpus file docs/notes/pdg-slicing-research.miner,
        which is where that algorithm should be grounded.

    (c) The polyhedral machinery itself (iteration domains, affine schedules,
        exact ILP dependence via Presburger/Omega/ISL) is exactly what pflow
        should NOT adopt. Section 3 gives the specific, corpus-grounded reasons.

    So the poly corpus is a strong source for #1 (classification), a NON-source
    for #2 (control dependence / FOW — look in pflow's own corpus), and a strong
    source for #3 (why polyhedral is overkill).


1.  ADAPT — Dependence classification from def-use

    1.1.  Bernstein's three conditions = the formal definition of flow/anti/output
          ADAPT. Effort: LOW.

          Bernstein (1966) gives the read-set/write-set formalism pflow already
          has the raw material for. For two statements P1, P2 with read set R(P)
          and write set W(P), the three dependence types are exactly:
            - FLOW / true / RAW: W(P1) ∩ R(P2) ≠ ∅  (P2 reads what P1 wrote)
            - ANTI / WAR:        R(P1) ∩ W(P2) ≠ ∅  (P2 writes what P1 read)
            - OUTPUT / WAW:      W(P1) ∩ W(P2) ≠ ∅  (both write the same location)
          Two statements are independent (freely reorderable) iff all three
          intersections are empty. [ref: computation_theory.miner:26,
          computation_theory.miner:44]
          Confidence: HIGH

          pflow capability: pflow's def-use already computes, per statement, the
          names defined (≈ write set) and used (≈ read set). Classifying an
          ordered pair of statements into RAW / WAR / WAW is a direct set
          intersection over those sets. This is the single highest-value, lowest-
          effort adaptation: it turns pflow's existing def-use into a labelled
          dependence relation with almost no new machinery.

          Use for the review agent:
            - RAW between two mutations of the same object → genuine ordering
              constraint; flag if a refactor reorders them.
            - WAR / WAW = "false dependences", removable by renaming
              [ref: computation_theory.miner:26]. pflow can SUGGEST renaming
              (introduce a fresh local) to break an accidental WAW/WAR that is
              forcing a brittle statement order. This is precisely the kind of
              "simplify the data flow" advice the agent exists to give.
            - All three empty → the two statements are independent and can be
              reordered/extracted safely; pflow can mark a region as
              reorder-safe. [ref: computation_theory.miner:44]

          Caveat for Python (not in corpus, but load-bearing): R/W sets are only
          sound if pflow's def-use already models aliasing/attribute/subscript
          writes conservatively. Bernstein's theorem assumes the read/write sets
          are EXACT memory locations. In dynamic Python, `a.x = ...` and
          `b.x = ...` may alias. pflow must treat the classification as
          MAY-depend (conservative) unless it can prove non-aliasing, otherwise
          the "freely reorderable" conclusion is unsound. Mark independence
          results as "no PROVABLE dependence" rather than "independent".

    1.2.  Loop-carried vs loop-independent dependence
          ADAPT (definition); SKIP the polyhedral distance/direction-vector
          machinery. Effort: LOW for the binary classification.

          The distinction is defined cleanly: for statements at loop iterations
          i and j, a dependence is LOOP-CARRIED if it holds with i ≠ j (carried
          across iterations) and LOOP-INDEPENDENT if it only holds with i = j
          (within one iteration). [ref: computation_theory.miner:26]
          Confidence: HIGH

          pflow capability: pflow does not need iteration vectors to get value
          from this. A useful, sound, source-level approximation:
            - A name that is WRITTEN inside a loop body and READ on a later
              iteration (i.e. read on a path that re-enters the loop header
              before the write that produced it) is loop-carried state.
            - Operationally in pflow terms: a def inside the loop that reaches a
              use inside the same loop via a back-edge of the CFG = loop-carried
              dependence. A def that only reaches uses within the same iteration
              (no back-edge on the def→use path) = loop-independent.
          This is computable from pflow's existing CFG + def-use + (the loop
          back-edge, identifiable from dominators) without ANY affine/iteration-
          domain modelling.

          Use for the review agent: surface "accidental loop-carried state" —
          e.g. a variable that silently accumulates across iterations when the
          author likely intended a per-iteration fresh value (classic
          list-default / running-accumulator bugs). Also flag the inverse:
          statements with NO loop-carried dependence as candidate "independent /
          parallelizable / map-able" iterations — for a review agent this reads
          as "this loop body could be a comprehension / map, each iteration is
          independent".

          SKIP within this item: dependence DISTANCE and DIRECTION VECTORS
          (d = (<, =, >, *) per loop level) [ref: complexity_and_limits.miner:93].
          These require an affine iteration space pflow does not have and cannot
          assume in dynamic Python. pflow should stop at the binary carried/
          not-carried classification, which is all a review agent needs to flag
          order-dependence. Do not implement direction vectors.

    1.3.  The dependence graph (statements = nodes, dependences = edges)
          ADAPT. Effort: LOW-MEDIUM.

          Allen-Kennedy build a graph whose nodes are statements and whose edges
          are dependences, then find strongly connected components; an SCC with a
          loop-carried edge is a recurrence that blocks reordering, and the rest
          is reorderable. PDG construction is O(n^2) in statements, SCC detection
          is O(n+m) via Tarjan — all polynomial. [ref: complexity_and_limits.miner:93]
          Karp-Miller-Winograd give the same idea at the theory layer: a
          computation is schedulable iff its dependence graph has no
          (zero-weight) cycle; a cycle in the dependence graph = an inherent
          ordering constraint. [ref: lattice_and_abstraction.miner:51]
          Confidence: HIGH

          IMPORTANT — what "PDG" means here: Allen-Kennedy's graph is a
          DATA-dependence graph only. It is NOT the Ferrante-Ottenstein-Warren
          PDG (control + data). Do not conflate them. The poly corpus's "PDG"
          has no control-dependence edges. [ref: complexity_and_limits.miner:93]

          pflow capability: build a per-region dependence graph by labelling
          edges of the def-use relation with the RAW/WAR/WAW type from 1.1, then:
            - Run Tarjan SCC (pflow likely already has SCC for call-graph
              cycles) over it.
            - An SCC containing a loop-carried RAW edge = a true recurrence: the
              statements in it cannot be separated or reordered. Report as "this
              cluster of statements is mutually order-dependent — simplifying it
              requires breaking the cycle, not just moving lines".
            - A node/SCC with only outgoing edges, or only WAR/WAW edges, = a
              candidate for extraction, renaming, or hoisting.
          This gives the review agent a principled answer to "which statements
          MUST stay in this order, and which are tangled only by removable false
          dependences". That maps directly onto pflow's stated mission of
          simplifying control/data flow.

    1.4.  Dependence TESTS (GCD / Banerjee / Omega)
          SKIP. Effort: N/A (deliberately not adapted).

          GCD, Banerjee, and Omega tests answer "do array references A[f(i)] and
          A[g(j)] ever touch the same element across iterations?" They operate on
          AFFINE array subscripts inside loops and form a precision hierarchy:
          GCD (fast, may miss), Banerjee (polynomial, conservative real-valued),
          Omega/ISL (exact for affine, exponential worst case). For NON-affine
          subscripts (A[i*i], A[i**j]) the question is UNDECIDABLE by Rice's
          theorem. [ref: complexity_and_limits.miner:87]
          Confidence: HIGH

          Why SKIP for pflow: these tests exist to get PRECISE per-element
          array dependences in numeric loops. pflow works at name/object
          granularity on dynamic Python, where (a) subscripts are almost never
          affine and (b) the exact-element question is undecidable anyway. pflow
          should treat any same-container access as MAY-depend (the conservative
          answer GCD/Banerjee fall back to) and stop there. Implementing integer-
          programming dependence tests would be large effort for a precision pflow
          cannot soundly use. The correct pflow stance is the conservative one the
          corpus itself names: when you cannot prove independence, assume the
          dependence. [ref: complexity_and_limits.miner:87]


2.  The Program Dependence Graph (control + data) and control dependence
    from post-dominators

    2.1.  STATUS: NOT GROUNDED IN THE POLY CORPUS.

          pflow asked to ground the Ferrante-Ottenstein-Warren control-dependence
          algorithm (control dependence defined via the post-dominator tree:
          node Y is control-dependent on node X iff X has two successors, Y
          post-dominates one but not the other — equivalently, Y post-dominates a
          successor of X but does not post-dominate X). The poly corpus contains
          NO record describing this. A whole-corpus search for "control
          dependence", "program dependence graph" (in the FOW sense),
          "post-dominator", "Ferrante", "Ottenstein", "Warren", and "program
          slicing" returned only LOOSE matches on unrelated polyhedral/complexity
          records — none of them describe control dependence or the FOW PDG.
          Confidence: HIGH that this is a genuine corpus gap, not a missed query.

          I therefore do NOT attach a poly corpus ref to the FOW algorithm. Doing
          so would be a fabricated citation. The algorithm itself is sound and
          standard (Ferrante, Ottenstein & Warren, "The Program Dependence Graph
          and Its Use in Optimization", TOPLAS 1987) — but it must be grounded
          from a source that actually contains it.

    2.2.  WHERE TO GROUND IT INSTEAD.

          pflow's OWN research corpus already has a dedicated file for exactly
          this: /home/lafiel/work/lafiel/pflow/docs/notes/pdg-slicing-research.miner.
          That is the corpus to query for the FOW control-dependence algorithm and
          for program slicing — not the poly corpus. This document does not query
          pflow's own corpus (out of the requested scope), but flags it as the
          correct grounding source. Recommend a follow-up synthesis over
          pdg-slicing-research.miner to ground 2.x with real refs before
          implementation.

    2.3.  ADAPT (algorithm sketch, to be grounded from pdg-slicing-research.miner,
          NOT from poly). Effort: MEDIUM.

          pflow already has post-dominators, which is the one expensive
          prerequisite. Adding control-dependence edges is then a well-bounded
          pass:
            1. Build the post-dominator tree (pflow HAS this).
            2. Find the set S of CFG edges (A → B) where B does NOT post-dominate
               A (these are the "branch points that decide something").
            3. For each such edge A → B, walk up the post-dominator tree from B
               until reaching the parent of A (the least common ancestor of A and
               B in the post-dom tree); every node L on that walk (excluding the
               LCA) is control-dependent on A.
          The resulting control-dependence edges, UNIONED with pflow's existing
          data-dependence (def-use) edges, ARE the Program Dependence Graph in
          the FOW sense. Once pflow has the PDG, backward program slicing is a
          straightforward graph reachability over PDG edges (which is the natural
          next capability after this one).

          Note the contrast that justifies building it: the poly corpus's
          Allen-Kennedy "PDG" [ref: complexity_and_limits.miner:93] has data
          edges only. The FOW PDG pflow wants adds the control-dependence edges
          described above. They are different graphs; only the FOW one supports
          slicing and "why does this statement run?" reasoning, which is what a
          control-flow-simplifying review agent needs.

          Use for the review agent: control-dependence edges let pflow answer
          "which branch condition controls whether this statement executes",
          enabling dead-branch / redundant-guard detection and slice-based
          "minimal set of statements that affect this value" explanations — the
          core of control-flow simplification advice.


3.  SKIP — the polyhedral model itself (and why), for general dynamic Python

    Bottom line: every load-bearing assumption of the polyhedral model is FALSE
    for general dynamic Python. Adopting iteration domains, affine schedules, or
    exact ILP/Presburger dependence would be large effort for machinery pflow
    cannot soundly apply. The poly corpus's own post-mortem says so repeatedly.

    3.1.  Affine / static-shape requirement vs dynamic Python.
          SKIP. The polyhedral model requires loop bounds and array subscripts
          expressible as affine functions of compile-time constants or symbolic
          parameters. Iteration domains are integer polytopes; this only exists
          for static control parts (SCoPs). [ref: lattice_and_abstraction.miner:51,
          complexity_and_limits.miner:87]
          The empirical ceiling: even with aggressive analysis, static polyhedral
          analysis covers only ~10% of general-program execution, ~29% with
          runtime extensions; "real ML workloads ... well below 10% in practice"
          and "programs with truly irregular memory access patterns (sparse
          matrices, graph algorithms, dynamic data structures) remain outside the
          model." [ref: polyhedral_compilation.miner:84,
          polyhedral_compilation.miner:50]
          Confidence: HIGH
          Why this kills it for pflow: pflow operates on arbitrary dynamic Python
          — dynamic types, data-dependent control flow, no affine-loop guarantee,
          pointer/reference aliasing. That is the >90% the polyhedral model
          cannot even represent, by the corpus's own numbers. There is no SCoP to
          extract from a typical reviewed Python function.

    3.2.  Dynamic shapes are a FUNDAMENTAL (not engineering) limit.
          SKIP. "Dynamic shapes are NOT solvable within polyhedral model
          (fundamental)." The model needs static/affine bounds; variable sizes
          force re-compilation per shape, which is why production systems
          (BladeDISC at Alibaba) were built explicitly to REPLACE polyhedral/XLA
          for dynamic shapes. [ref: polyhedral_compilation.miner:102]
          Confidence: HIGH
          Why this kills it for pflow: Python has no static shapes at all at the
          source level pflow analyzes. The single most important polyhedral
          precondition is simply absent.

    3.3.  Exact ILP dependence is expensive AND undecidable off the affine path.
          SKIP. Exact dependence via the Omega test / ISL is Presburger
          arithmetic: decidable but doubly-exponential in the worst case for
          affine subscripts, and UNDECIDABLE (Rice's theorem) for non-affine
          subscripts. [ref: complexity_and_limits.miner:87] At the scheduling
          layer, Pluto's ILP "poses a scalability issue when scaling to tens or
          hundreds of statements" and polyhedral code generation is "sometimes
          exponential time complexity." [ref: polyhedral_compilation.miner:102]
          Confidence: HIGH
          Why this kills it for pflow: pflow is an interactive review tool that
          must run fast on whole modules. The corpus's cheap, polynomial,
          conservative analyses (Bernstein set-intersection, Allen-Kennedy
          O(n^2) graph + Tarjan SCC — section 1) are the right complexity class.
          Exact ILP dependence is the wrong one, and undecidable for the Python
          subscripts pflow actually sees.

    3.4.  Even the polyhedral community is retreating from full ILP.
          SKIP (corroborating evidence). PolyBlocks (2026), built by the Pluto
          author, deliberately does NOT use full ILP-based scheduling and uses
          ISL "only in rare cases", because "compile times must stay [in the] few
          to tens of seconds." [ref: L4 doc §2.1; underlying
          polyhedral_compilation.miner:34] The measured objective itself is also
          a poor proxy — learned/RL schedulers beat Pluto's analytical objective
          by up to 3.36x [ref: L4 doc §2.1], i.e. the analytical cost model the
          ILP optimizes is itself wrong.
          Confidence: HIGH
          Why this matters for pflow: the strongest argument against investing in
          polyhedral machinery is that its own originators concluded full
          polyhedral ILP is too slow for production and that its analytical
          objective is inaccurate. pflow has even weaker preconditions (dynamic
          Python) and a stricter latency budget than a batch compiler, so the
          case against is only stronger.

    3.5.  What pflow SHOULD borrow from polyhedral — only the conservative stance.
          The one transferable lesson, not the machinery: when you cannot prove
          two accesses are independent, assume they are dependent (the GCD/
          Banerjee conservative fallback). [ref: complexity_and_limits.miner:87]
          pflow should apply this to aliasing/containers in Python: same-name,
          same-attribute, or same-container accesses are MAY-depend unless proven
          otherwise. This keeps pflow's dependence classification SOUND without
          any polyhedral apparatus.


4.  Open Questions

    4.1.  How sound is pflow's def-use under Python aliasing? Section 1's
          RAW/WAR/WAW classification is only as sound as the underlying read/write
          sets. Whether `a.x` and `b.x` are treated as may-alias determines
          whether "independent / reorderable" conclusions are safe. Not answerable
          from the poly corpus (it assumes exact memory locations). This is a
          pflow-internal question.

    4.2.  The FOW control-dependence / PDG / slicing material is absent from the
          poly corpus and present in pflow's own pdg-slicing-research.miner. A
          follow-up synthesis over that file is warranted to ground section 2
          with real refs before implementing the control-dependence pass. Flag:
          new synthesis run recommended (against pflow's own corpus, not poly).

    4.3.  Does pflow want any iteration-level reasoning at all, or strictly the
          binary loop-carried/not-carried classification (1.2)? The corpus offers
          a precision continuum (binary → direction vectors → distance vectors →
          exact). This doc recommends stopping at binary; whether the review agent
          ever needs more is a product-scope decision for the human.


5.  Reading Trail

    For a human grounding this before implementation, in order:

    1.  computation_theory.miner:44
        Bernstein's conditions stated as read-set/write-set intersections, with
        RAW/WAR/WAW spelled out and the loop-carried example. The cleanest single
        record; start here. Maps most directly onto pflow's def-use.

    2.  computation_theory.miner:26
        The same conditions plus the explicit "WAR and WAW are false dependences,
        removable by renaming; only RAW is fundamental" framing — this is the
        basis for pflow's rename-to-simplify suggestions.

    3.  complexity_and_limits.miner:93
        Allen-Kennedy: the dependence GRAPH + SCC view, complexity bounds
        (O(n^2), Tarjan), and direction vectors. Read for the graph construction;
        note its "PDG" is data-only (no control edges).

    4.  lattice_and_abstraction.miner:51
        Karp-Miller-Winograd: the theory layer — dependence graph cycles = hard
        ordering constraints. Read for WHY an SCC with a carried edge is
        irreducible.

    5.  complexity_and_limits.miner:87
        GCD / Banerjee / Omega tests and the P / NP / undecidable trichotomy.
        Read to understand exactly what pflow is SKIPPING and why the
        conservative fallback is the right stance for Python.

    6.  polyhedral_compilation.miner:102
        The polyhedral failure-mode post-mortem. The single best record for "why
        not polyhedral" — dynamic shapes fundamental, ~10% coverage, exponential
        codegen, SSA mismatch.

    7.  polyhedral_compilation.miner:84
        The empirical ~10% / ~29% coverage numbers, quantifying 3.1.

    8.  polyhedral_compilation.miner:50
        "More widely applicable than you think" — the steelman FOR polyhedral,
        and its own admission that irregular/dynamic programs stay outside the
        model. Read this to be fair to the other side before deciding to skip.

    9.  pflow/docs/notes/pdg-slicing-research.miner  (pflow's OWN corpus, NOT poly)
        Where to actually ground section 2 (FOW control dependence + slicing).
