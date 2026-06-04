"""CFG construction from AST (v1).

The builder maintains a single invariant:

    self.current is either an *open* BasicBlock that the next statement
    falls into, or None when control cannot reach the next statement
    (i.e. the previous statement was a return / raise / break / continue).

Every control construct (if / while / for / try / with) is responsible
for leaving self.current pointing at the block that code *after* the
construct should attach to (the "merge" block), or at None when the
construct never falls through.
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional, Tuple

from .graph import BasicBlock, FunctionGraph, Op


class _CFGBuilder:
    def __init__(self, qualname: str, source_path: str, first_line: int):
        self.qualname = qualname
        self.source_path = source_path
        self.first_line = first_line
        self.blocks: List[BasicBlock] = []
        self.next_block_id = 0
        self.next_op_id = 0
        self.current: Optional[BasicBlock] = None
        self.pending_edges: List[Tuple[int, int]] = []
        self._block_ops: Dict[int, List[Op]] = {}
        # loop_stack entries: (continue_target_id, break_target_id)
        self.loop_stack: List[Tuple[int, int]] = []
        # the first function visited is the target; nested defs are just
        # name bindings, not control flow to recurse into (Bug A).
        self._top_done = False

    # -- block / op primitives -------------------------------------------

    def new_block(self) -> BasicBlock:
        blk = BasicBlock(id=self.next_block_id)
        self.next_block_id += 1
        self.blocks.append(blk)
        self._block_ops[blk.id] = []
        return blk

    def start(self, blk: BasicBlock) -> None:
        self.current = blk

    def seal(self) -> None:
        """Mark control as not falling through (after return/raise/break)."""
        self.current = None

    def _ensure_current(self) -> BasicBlock:
        """Get the current block, materializing an (unreachable) one if needed.

        Statements that appear after an unconditional terminator are dead
        code; we still give them a home block so they are representable,
        but it stays disconnected and is pruned if empty.
        """
        if self.current is None:
            self.start(self.new_block())
        return self.current

    def emit(self, kind: str, targets: tuple[str, ...] = (),
             uses: tuple[str, ...] = (), source: Any = None,
             attrs: Optional[Dict[str, Any]] = None) -> Op:
        blk = self._ensure_current()
        op = Op(id=self.next_op_id, kind=kind, targets=targets, uses=uses,
                source=source, attrs=dict(attrs or {}))
        self.next_op_id += 1
        self._block_ops[blk.id].append(op)
        return op

    def edge(self, from_id: int, to_id: int) -> None:
        self.pending_edges.append((from_id, to_id))

    def goto(self, target: BasicBlock) -> None:
        """Add a fallthrough edge from current (if open) to target."""
        if self.current is not None:
            self.edge(self.current.id, target.id)

    # -- dispatch --------------------------------------------------------

    def visit(self, node: ast.AST) -> None:
        method = getattr(self, f"visit_{type(node).__name__}", self.generic_visit)
        method(node)

    def generic_visit(self, node: ast.AST) -> None:
        # Statements we don't model specially (Import, Pass, Global, ...).
        # Treat as a no-op "other" op so it still appears in the block.
        self.emit("other", source=self._src(node),
                  attrs={"ast": type(node).__name__})

    def _visit_body(self, body: List[ast.stmt]) -> None:
        for stmt in body:
            self.visit(stmt)

    # -- function entry --------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def _visit_def(self, node) -> None:
        if self._top_done:
            # A NESTED def: it only binds a name in this scope; its body is a
            # separate FunctionGraph, not part of this function's control flow
            # (Bug A). Record the binding + what evaluating the def reads
            # (decorators, default args) + the enclosing names its body
            # captures (so those aren't seen as dead).
            uses = (self._def_eval_uses(node) + self._captured_names(node))
            self.emit("assign", targets=(node.name,),
                      uses=tuple(dict.fromkeys(uses)), source=self._src(node),
                      attrs={"kind": "nested_def"})
            return
        self._top_done = True
        self._visit_function_like(node)

    def _visit_function_like(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        entry = self.new_block()
        self.start(entry)

        arg_names = self._all_arg_names(node.args)
        if arg_names:
            self.emit("assign", targets=tuple(arg_names),
                      attrs={"kind": "args",
                             "arg_annotations": self._arg_annotations(node.args),
                             "kwarg_name": node.args.kwarg.arg if node.args.kwarg else None})

        self._visit_body(node.body)

        # Implicit return on the fall-through path.
        if self.current is not None:
            self.emit("return", attrs={"implicit": True})
            self.seal()

    @staticmethod
    def _all_arg_names(args: ast.arguments) -> List[str]:
        names: List[str] = []
        for a in (*args.posonlyargs, *args.args):
            names.append(a.arg)
        if args.vararg:
            names.append(args.vararg.arg)
        for a in args.kwonlyargs:
            names.append(a.arg)
        if args.kwarg:
            names.append(args.kwarg.arg)
        return names

    @staticmethod
    def _arg_annotations(args: ast.arguments) -> Dict[str, Optional[str]]:
        """{param_name: unparsed annotation or None}. Used by the lossy-
        projection pass to decide which params look like open mappings."""
        out: Dict[str, Optional[str]] = {}
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            out[a.arg] = ast.unparse(a.annotation) if a.annotation is not None else None
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                out[extra.arg] = (ast.unparse(extra.annotation)
                                  if extra.annotation is not None else None)
        return out

    # -- simple statements ----------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        targets = self._extract_targets(node.targets)
        # uses = RHS reads + names read while evaluating the store targets
        # (`m[i] = v` reads m and i; `obj.x = v` reads obj) — Bug C.
        uses = tuple(dict.fromkeys(
            self._extract_uses(node.value) + self._target_uses(node.targets)))
        attrs = self._calls_attr(node.value)
        self._add_projection_attrs(attrs, node.value)
        if isinstance(node.value, ast.Constant):
            attrs["const_value"] = node.value.value   # x = <literal> (for const-prop)
        sb = self._subscript_store_bases(node.targets)
        if sb:
            attrs["store_bases"] = sb
        self.emit("assign", targets=targets, uses=uses, source=self._src(node),
                  attrs=attrs)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return  # bare annotation defines nothing and reads nothing
        targets = self._extract_targets([node.target])
        uses = tuple(dict.fromkeys(
            self._extract_uses(node.value) + self._target_uses([node.target])))
        attrs = self._calls_attr(node.value)
        self._add_projection_attrs(attrs, node.value)
        sb = self._subscript_store_bases([node.target])
        if sb:
            attrs["store_bases"] = sb
        self.emit("assign", targets=targets, uses=uses, source=self._src(node),
                  attrs=attrs)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # x += e reads x (the target, in Store ctx so _extract_uses skips it)
        # and reads e, then writes x. For a[i] += e, also reads a and i (Bug C).
        targets = self._extract_targets([node.target])
        uses = tuple(dict.fromkeys(
            targets + self._extract_uses(node.value)
            + self._target_uses([node.target])))
        attrs = {"aug": True, **self._calls_attr(node.value)}
        sb = self._subscript_store_bases([node.target])
        if sb:
            attrs["store_bases"] = sb
        self.emit("assign", targets=targets, uses=uses, source=self._src(node),
                  attrs=attrs)

    def visit_Expr(self, node: ast.Expr) -> None:
        uses = self._extract_uses(node.value)
        kind = "call" if isinstance(node.value, (ast.Call, ast.Await)) else "other"
        attrs = self._calls_attr(node.value)
        self._add_projection_attrs(attrs, node.value)
        self.emit(kind, uses=uses, source=self._src(node), attrs=attrs)

    def visit_Assert(self, node: ast.Assert) -> None:
        # `assert test[, msg]` reads the test (and message) expression. Without
        # an explicit visitor it falls to generic_visit, which records no uses —
        # so a value whose only read is an assert looks dead (a false dead-store,
        # a dropped live range, a missed use-before-def). Caught by the stdlib
        # corpus: `is_sync, cb = ...; assert is_sync` flagged `is_sync` dead.
        uses = list(self._extract_uses(node.test))
        if node.msg is not None:
            uses.extend(self._extract_uses(node.msg))
        self.emit("assert", uses=tuple(dict.fromkeys(uses)), source=self._src(node),
                  attrs=self._calls_attr(node.test))

    def visit_Delete(self, node: ast.Delete) -> None:
        # `del d[k]` / `del obj.attr` read their base and index (Load context);
        # `del x` reads nothing as a load. Without this they fall to
        # generic_visit (no uses), so `del d[k]` falsely flags d and k dead.
        uses: List[str] = []
        for t in node.targets:
            uses.extend(self._extract_uses(t))
        attrs: Dict[str, Any] = {}
        sb = self._subscript_store_bases(node.targets)
        if sb:
            attrs["store_bases"] = sb
        self.emit("delete", uses=tuple(dict.fromkeys(uses)), source=self._src(node),
                  attrs=attrs)

    def visit_Return(self, node: ast.Return) -> None:
        uses = self._extract_uses(node.value) if node.value else ()
        attrs = {"is_return": True, **self._calls_attr(node.value)}
        if node.value is not None:
            self._add_projection_attrs(attrs, node.value)
        self.emit("return", uses=uses, source=self._src(node), attrs=attrs)
        self.seal()

    def visit_Raise(self, node: ast.Raise) -> None:
        uses: List[str] = []
        if node.exc:
            uses.extend(self._extract_uses(node.exc))
        if node.cause:
            uses.extend(self._extract_uses(node.cause))
        self.emit("raise", uses=tuple(dict.fromkeys(uses)), source=self._src(node))
        self.seal()

    def visit_Break(self, node: ast.Break) -> None:
        self.emit("branch", source=self._src(node), attrs={"kind": "break"})
        if self.loop_stack and self.current is not None:
            _, break_target = self.loop_stack[-1]
            self.edge(self.current.id, break_target)
        self.seal()

    def visit_Continue(self, node: ast.Continue) -> None:
        self.emit("branch", source=self._src(node), attrs={"kind": "continue"})
        if self.loop_stack and self.current is not None:
            continue_target, _ = self.loop_stack[-1]
            self.edge(self.current.id, continue_target)
        self.seal()

    # -- compound statements --------------------------------------------

    def visit_If(self, node: ast.If) -> None:
        cond_uses = self._extract_uses(node.test)
        self.emit("branch", uses=cond_uses, source=self._src(node),
                  attrs={"condition": ast.unparse(node.test), "construct": "if"})
        branch = self.current
        if branch is None:
            # if-statement is itself unreachable; nothing to wire.
            return
        branch_id = branch.id

        then_b = self.new_block()
        self.edge(branch_id, then_b.id)
        self.start(then_b)
        self._visit_body(node.body)
        then_exit = self.current

        else_b = self.new_block()
        self.edge(branch_id, else_b.id)
        self.start(else_b)
        self._visit_body(node.orelse)
        else_exit = self.current

        merge = self.new_block()
        if then_exit is not None:
            self.edge(then_exit.id, merge.id)
        if else_exit is not None:
            self.edge(else_exit.id, merge.id)
        self.start(merge)

    def visit_Match(self, node: ast.Match) -> None:
        subj_uses = self._extract_uses(node.subject)
        self.emit("branch", uses=subj_uses, source=self._src(node),
                  attrs={"kind": "match", "condition": ast.unparse(node.subject)})
        head = self.current
        if head is None:
            return
        head_id = head.id

        merge = self.new_block()
        has_catch_all = False
        for case in node.cases:
            cb = self.new_block()
            self.edge(head_id, cb.id)
            self.start(cb)
            bound = self._pattern_names(case.pattern)
            guard_uses = self._extract_uses(case.guard) if case.guard else ()
            self.emit("branch", targets=tuple(bound), uses=guard_uses,
                      source=self._src(case),
                      attrs={"kind": "case", "pattern": ast.unparse(case.pattern)})
            self._visit_body(case.body)
            if self.current is not None:
                self.edge(self.current.id, merge.id)
            if isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None \
                    and case.guard is None:
                has_catch_all = True  # `case _:` / `case x:` with no guard

        if not has_catch_all:
            self.edge(head_id, merge.id)  # no case matched -> fall through
        self.start(merge)

    def _pattern_names(self, pattern: Optional[ast.pattern]) -> List[str]:
        """Best-effort capture-name extraction from a match pattern."""
        names: List[str] = []
        if pattern is None:
            return names
        for n in ast.walk(pattern):
            if isinstance(n, ast.MatchAs) and n.name:
                names.append(n.name)
            elif isinstance(n, ast.MatchStar) and n.name:
                names.append(n.name)
            elif isinstance(n, ast.MatchMapping) and n.rest:
                names.append(n.rest)
        return list(dict.fromkeys(names))

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node, is_for=False)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node, is_for=True)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node, is_for=True)

    def _visit_loop(self, node, is_for: bool) -> None:
        if is_for:
            # The iterable is evaluated ONCE, before the loop. The target is
            # (re)bound per iteration inside the body (below) — NOT here —
            # so it is correctly not-definitely-assigned after an empty loop.
            iter_uses = self._extract_uses(node.iter)
            self.emit("other", uses=iter_uses, source=self._src(node),
                      attrs={"kind": "for_iter_eval"})

        header = self.new_block()
        self.goto(header)
        self.start(header)
        if is_for:
            self.emit("branch", source=self._src(node),
                      attrs={"condition": "<has-next>", "kind": "for", "construct": "for"})
        else:
            cond_uses = self._extract_uses(node.test)
            self.emit("branch", uses=cond_uses, source=self._src(node),
                      attrs={"condition": ast.unparse(node.test), "construct": "while"})

        body = self.new_block()
        after = self.new_block()
        self.edge(header.id, body.id)

        self.loop_stack.append((header.id, after.id))
        self.start(body)
        if is_for:
            # per-iteration binding of the loop target, reachable only via the
            # body (header -> body), so the zero-iteration exit leaves it unbound.
            self.emit("assign", targets=self._extract_targets([node.target]),
                      source=self._src(node), attrs={"kind": "for_iter"})
        self._visit_body(node.body)
        if self.current is not None:
            self.edge(self.current.id, header.id)  # back edge
        self.loop_stack.pop()

        # The loop's normal (condition-false) exit goes to the else clause
        # if present, otherwise straight to `after`. break edges jump to
        # `after` directly, bypassing else (correct Python semantics).
        if node.orelse:
            orelse_b = self.new_block()
            self.edge(header.id, orelse_b.id)
            self.start(orelse_b)
            self._visit_body(node.orelse)
            if self.current is not None:
                self.edge(self.current.id, after.id)
        else:
            self.edge(header.id, after.id)

        self.start(after)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def _visit_with(self, node) -> None:
        # enter_with (acquire the context managers) precedes the body.
        for item in node.items:
            uses = self._extract_uses(item.context_expr)
            targets = (self._extract_targets([item.optional_vars])
                       if item.optional_vars is not None else ())
            self.emit("enter_with", targets=targets, uses=uses,
                      source=self._src(node),
                      attrs={"is_synthetic": True, "with": True})

        # Body in its own block so the with-region is cleanly bounded.
        body_b = self.new_block()
        self.goto(body_b)
        self.start(body_b)
        self._visit_body(node.body)
        body_exit = self.current

        # The implicit __exit__ runs as cleanup. We model ONLY the normal
        # fall-through (body -> cleanup -> post-with code). We deliberately do
        # NOT add body->cleanup exception edges: __exit__ is synthetic (defines
        # and uses no user variable), and a non-suppressing context manager
        # (the common case) re-raises, so the body's exception path does NOT
        # reach the post-with continuation. Routing it there poisoned
        # definite-assignment — flagging any `x = ...` inside a `with` that is
        # read afterwards as maybe-unbound (a frequent false positive). An
        # enclosing try still wires the body's exceptions to its handlers /
        # finally (so try/finally reads of with-body vars are still caught); with
        # no enclosing handler the exception simply leaves the function.
        # Known trade: a *suppressing* context manager (e.g. contextlib.suppress)
        # could fall through with the value unbound — a rare real bug given up
        # for precision on the overwhelmingly common pattern.
        #
        # Only build the cleanup block when the body actually falls through.
        # For the lock-guarded-accessor idiom `with self._lock: return X`, the
        # body always returns, so there is no normal edge into cleanup — and
        # with no exception edges either it would be an orphan holding only the
        # synthetic `exit_with`. The unreachable pass (a SOUND/MUST tier) then
        # falsely reported it as "dead code — remove it", and a no-successor
        # orphan also pollutes exit_blocks. So when the body has closed control
        # (returned/raised on every path) we emit no cleanup block and leave
        # control closed; any code after the `with` is then correctly dead.
        if body_exit is not None:
            cleanup = self.new_block()
            cleanup.attrs["is_finally_region"] = True
            self.edge(body_exit.id, cleanup.id)
            self.start(cleanup)
            self.emit("exit_with", source=self._src(node),
                      attrs={"is_synthetic": True, "with": True, "finally": True})
            # Code after the with continues from the cleanup block.

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    # Python 3.11+: TryStar (except*) — model like Try for v1.
    def visit_TryStar(self, node) -> None:
        self._visit_try(node)

    def _visit_try(self, node) -> None:
        # --- try body, in its own block so the region is cleanly bounded ---
        body_b = self.new_block()
        self.goto(body_b)
        self.start(body_b)
        body_start = len(self.blocks) - 1
        self._visit_body(node.body)
        body_exit = self.current
        body_ids = [b.id for b in self.blocks[body_start:]]

        # --- handlers ---
        handler_ids: List[int] = []
        handler_exits: List[Optional[BasicBlock]] = []
        for h in node.handlers:
            hb = self.new_block()
            handler_ids.append(hb.id)
            hb.attrs["is_exception_handler"] = True
            self.start(hb)
            htype = ast.unparse(h.type) if h.type is not None else None
            self.emit("enter_except", source=self._src(h),
                      attrs={"is_synthetic": True, "handler": True, "exc_type": htype})
            if h.name:
                self.emit("assign", targets=(h.name,), attrs={"kind": "except_as"})
            self._visit_body(h.body)
            handler_exits.append(self.current)

        # --- else (runs only on the no-exception path) ---
        orelse_id: Optional[int] = None
        orelse_exit: Optional[BasicBlock] = None
        if node.orelse:
            ob = self.new_block()
            orelse_id = ob.id
            self.start(ob)
            self._visit_body(node.orelse)
            orelse_exit = self.current

        # --- finally (single shared region). NOTE: a single region means an
        #     exception entering the finally before the body finished can leak
        #     its "unbound" definite-assignment state past the finally into the
        #     post-try code, so a value assigned in a multi-block try body
        #     (e.g. one containing a `with`) and read after the try can be a
        #     possibly-unbound FALSE POSITIVE. Duplicating the finally
        #     (normal-path copy vs exception-path copy) fixes that, but trades it
        #     for a worse, common FP: when the body always raises/returns
        #     (`try: raise X finally: cleanup()` — twice in stdlib contextlib),
        #     the normal copy is unreachable and its real cleanup statements get
        #     flagged as dead code, and the doubled finally ops inflate the
        #     complexity metrics. So v1 keeps one region; the post-try FP is a
        #     documented, rare heuristic/may limitation (needs edge-sensitive
        #     dataflow to fix cleanly). See RFC-0002 §8.
        finally_id: Optional[int] = None
        finally_exit: Optional[BasicBlock] = None
        if node.finalbody:
            fb = self.new_block()
            finally_id = fb.id
            fb.attrs["is_finally_region"] = True
            self.start(fb)
            self.emit("enter_finally", attrs={"is_synthetic": True, "finally": True})
            self._visit_body(node.finalbody)
            self.emit("exit_finally", attrs={"is_synthetic": True, "finally": True})
            finally_exit = self.current

        after = self.new_block()

        # Normal-path wiring.
        normal_after_body = (
            orelse_id if orelse_id is not None
            else (finally_id if finally_id is not None else after.id)
        )
        if body_exit is not None:
            self.edge(body_exit.id, normal_after_body)

        post = finally_id if finally_id is not None else after.id
        if orelse_exit is not None:
            self.edge(orelse_exit.id, post)
        for he in handler_exits:
            if he is not None:
                self.edge(he.id, post)
        if finally_exit is not None:
            self.edge(finally_exit.id, after.id)

        # Exceptional wiring: every body block can transfer to each handler,
        # and (uncaught) to finally. Handlers can raise into finally too.
        for bid in body_ids:
            for hid in handler_ids:
                self._add_except_succ(bid, hid)
            if finally_id is not None:
                self._add_except_succ(bid, finally_id)
        if finally_id is not None:
            for hid in handler_ids:
                self._add_except_succ(hid, finally_id)

        self.start(after)

    # -- region helpers --------------------------------------------------

    def _add_except_succ(self, block_id: int, target_id: int) -> None:
        blk = self._block_by_id(block_id)
        if blk is None or target_id == block_id:
            return
        if target_id not in blk.except_succs:
            blk.except_succs = blk.except_succs + (target_id,)

    def _block_by_id(self, block_id: int) -> Optional[BasicBlock]:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None

    # -- name extraction -------------------------------------------------

    def _extract_targets(self, targets: List[ast.expr]) -> Tuple[str, ...]:
        names: List[str] = []
        for t in targets:
            self._collect_target(t, names)
        return tuple(dict.fromkeys(names))

    def _collect_target(self, t: ast.expr, out: List[str]) -> None:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, ast.Attribute):
            out.append(ast.unparse(t))
        elif isinstance(t, ast.Starred):
            self._collect_target(t.value, out)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                self._collect_target(elt, out)
        # Subscript targets (a[i] = ...) define no simple name in v1.

    def _subscript_store_bases(
        self, targets: List[ast.expr]
    ) -> Tuple[Tuple[str, str], ...]:
        """(root_name, base_path) for each Subscript store target:
        `cfg[k] = v` -> ('cfg', 'cfg'); `p.q[k] = v` -> ('p', 'p.q'). Tuple/list
        targets recurse. A subscript store otherwise defines no name (see
        _collect_target), so without this attr a write `p[k]=v` is
        indistinguishable from a read `y=p[k]` — both only read the base. The
        input-mutation pass gates on root_name (reaching defs) and labels with
        base_path. Additive only."""
        out: List[Tuple[str, str]] = []

        def rec(t: ast.expr) -> None:
            if isinstance(t, ast.Subscript):
                root = _root_name(t.value)
                if root is not None:
                    out.append((root, ast.unparse(t.value)))
            elif isinstance(t, ast.Starred):
                rec(t.value)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for elt in t.elts:
                    rec(elt)

        for t in targets:
            rec(t)
        return tuple(dict.fromkeys(out))

    def _calls_attr(self, node: Optional[ast.expr]) -> Dict[str, Any]:
        calls = self._extract_calls(node)
        return {"calls": calls} if calls else {}

    def _extract_calls(self, node: Optional[ast.expr]) -> Tuple[dict, ...]:
        """Collect callee name + positional-arg root names for each Call in expr.

        Used by interprocedural value tracing to map arguments to parameters.
        Order follows ast.walk (outermost call first is not guaranteed, but the
        set of calls and their args is what tracing needs)."""
        if node is None:
            return ()
        calls = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                func = n.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = ast.unparse(func)
                else:
                    name = None
                args = tuple(self._arg_root(a) for a in n.args)
                calls.append({"func": name, "args": args})
        return tuple(calls)

    @staticmethod
    def _arg_root(a: ast.expr) -> Optional[str]:
        if isinstance(a, ast.Name):
            return a.id
        if isinstance(a, ast.Attribute):
            return ast.unparse(a)
        if isinstance(a, ast.Starred):
            return _CFGBuilder._arg_root(a.value)
        return None  # literal / complex expression — no single root name

    # -- lossy-projection lowering (RFC-0002; consumed by analysis/projection) --

    def _add_projection_attrs(self, attrs: Dict[str, Any], value: ast.expr) -> None:
        """Attach read_keys (literal keys read from a mapping) and dict_build
        (shape of a dict-literal/comprehension RHS) so the lossy-projection
        pass can spot open records narrowed to closed dicts. Additive only."""
        rk = self._read_keys(value)
        if rk:
            attrs["read_keys"] = rk
        db = self._dict_build_shape(value)
        if db is not None:
            attrs["dict_build"] = db

    def _read_keys(self, node: ast.AST) -> Tuple[Tuple[str, str], ...]:
        """(base, literal_key) pairs READ from a mapping in this expression:
        base[<str const>], base.get/pop/setdefault(<str const>), and the
        validator accessor convention helper(base, <str const>, ...) — seeker
        reads via req_str(p, "name") / opt_int(p, "port"), not p["name"].
        Heuristic: a helper(mapping, literal) that ignores the literal yields a
        spurious 'read', which only ever SUPPRESSES a finding (the safe way)."""
        out: List[Tuple[str, str]] = []
        for n in ast.walk(node):
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
                base, key = self._base_path(n.value), self._str_const(n.slice)
                if base is not None and key is not None:
                    out.append((base, key))
            elif isinstance(n, ast.Call):
                f = n.func
                if (isinstance(f, ast.Attribute)
                        and f.attr in ("get", "pop", "setdefault") and n.args):
                    base, key = self._base_path(f.value), self._str_const(n.args[0])
                    if base is not None and key is not None:
                        out.append((base, key))
                elif isinstance(f, ast.Name) and len(n.args) >= 2:
                    base, key = self._base_path(n.args[0]), self._str_const(n.args[1])
                    if base is not None and key is not None:
                        out.append((base, key))
        return tuple(dict.fromkeys(out))

    @staticmethod
    def _str_const(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    @staticmethod
    def _base_path(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return ast.unparse(node)
        return None

    def _dict_build_shape(self, value: ast.expr) -> Optional[Dict[str, Any]]:
        """Shape of a dict construction, else None. `dynamic` marks a dict
        comprehension or any non-literal key (keys can't be enumerated)."""
        if isinstance(value, ast.DictComp):
            return {"keys": [], "has_spread": True, "dynamic": True, "fields": {}}
        if not isinstance(value, ast.Dict):
            return None
        keys: List[str] = []
        has_spread = False
        dynamic = False
        spread_names: List[str] = []
        fields: Dict[str, Dict[str, Any]] = {}
        for k, v in zip(value.keys, value.values):
            if k is None:                       # {**x} — carries x's keys through
                has_spread = True
                spread_names.extend(self._extract_uses(v))
                continue
            ks = self._str_const(k)
            if ks is None:                       # {var: ...} — non-literal key
                dynamic = True
                continue
            keys.append(ks)
            fields[ks] = {"container": self._is_container_expr(v),
                          "reads": list(self._extract_uses(v))}
        return {"keys": keys, "has_spread": has_spread, "dynamic": dynamic,
                "spread_names": spread_names, "fields": fields}

    def _is_container_expr(self, v: ast.expr) -> bool:
        """Does this value build a container literal (dict/list/set/comp),
        possibly through a conditional? Container-valued output fields are the
        high-signal lossy case (a nested record dropped wholesale)."""
        if isinstance(v, (ast.Dict, ast.List, ast.Set,
                          ast.DictComp, ast.ListComp, ast.SetComp)):
            return True
        if isinstance(v, ast.IfExp):
            return self._is_container_expr(v.body) or self._is_container_expr(v.orelse)
        return False

    def _extract_uses(self, node: Optional[ast.expr]) -> Tuple[str, ...]:
        """Names this expression reads in the ENCLOSING scope.

        Comprehensions/generators/lambdas are their own scopes: their loop
        targets and parameters are bound there and do NOT count as enclosing
        uses (Bug B). Only the leftmost comprehension iterable and a lambda's
        defaults are evaluated in the enclosing scope.
        """
        if node is None:
            return ()
        out: List[str] = []
        self._collect_loads(node, set(), out)
        return tuple(dict.fromkeys(out))

    def _collect_loads(self, node: ast.AST, bound: set, out: List[str]) -> None:
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = set(bound)
            for i, gen in enumerate(node.generators):
                # leftmost iterable runs in the enclosing scope; the rest in the
                # comprehension's own scope (after its targets are bound).
                self._collect_loads(gen.iter, bound if i == 0 else inner, out)
                for t in ast.walk(gen.target):
                    if isinstance(t, ast.Name):
                        inner.add(t.id)
                for cond in gen.ifs:
                    self._collect_loads(cond, inner, out)
            if isinstance(node, ast.DictComp):
                self._collect_loads(node.key, inner, out)
                self._collect_loads(node.value, inner, out)
            else:
                self._collect_loads(node.elt, inner, out)
            return
        if isinstance(node, ast.Lambda):
            inner = set(bound) | set(self._all_arg_names(node.args))
            for d in (*node.args.defaults, *node.args.kw_defaults):
                if d is not None:
                    self._collect_loads(d, bound, out)  # defaults: enclosing scope
            self._collect_loads(node.body, inner, out)
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id not in bound:
                out.append(node.id)
            return
        if isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Load):
                root = _root_name(node)
                if root is None or root not in bound:
                    out.append(ast.unparse(node))
            self._collect_loads(node.value, bound, out)  # base names (e.g. self)
            return
        for child in ast.iter_child_nodes(node):
            self._collect_loads(child, bound, out)

    def _target_uses(self, targets: List[ast.expr]) -> Tuple[str, ...]:
        """Names READ while evaluating store targets: subscript index/base and
        attribute base (`m[i] = v` reads m and i; `obj.x = v` reads obj) — Bug C."""
        out: List[str] = []
        for t in targets:
            self._collect_target_loads(t, out)
        return tuple(dict.fromkeys(out))

    def _collect_target_loads(self, t: ast.expr, out: List[str]) -> None:
        if isinstance(t, ast.Attribute):
            self._collect_loads(t.value, set(), out)
        elif isinstance(t, ast.Subscript):
            self._collect_loads(t.value, set(), out)
            self._collect_loads(t.slice, set(), out)
        elif isinstance(t, ast.Starred):
            self._collect_target_loads(t.value, out)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                self._collect_target_loads(elt, out)
        # a plain Name target is a pure binding — no read.

    def _def_eval_uses(self, node) -> Tuple[str, ...]:
        """Names read when a (nested) def is evaluated: decorators + defaults."""
        out: List[str] = []
        for dec in getattr(node, "decorator_list", ()):
            self._collect_loads(dec, set(), out)
        args = node.args
        for d in (*args.defaults, *args.kw_defaults):
            if d is not None:
                self._collect_loads(d, set(), out)
        return tuple(out)

    @staticmethod
    def _captured_names(node) -> Tuple[str, ...]:
        """Free variables of a nested function: names it Loads but does not bind
        locally — i.e. names captured from THIS (enclosing) scope. Recording
        them as uses keeps closure-captured locals from looking dead (Bug A)."""
        bound: set = {node.name}
        loaded: set = set()
        for n in ast.walk(node):
            if isinstance(n, ast.arg):
                bound.add(n.arg)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name):
                (bound if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        return tuple(loaded - bound)

    def _src(self, node: ast.AST) -> Optional[Tuple[int, int, int, int]]:
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        if lineno is None or col is None:
            return None
        end_ln = getattr(node, "end_lineno", lineno) or lineno
        end_col = getattr(node, "end_col_offset", col) or col
        return (lineno, col, end_ln, end_col)

    # -- finalization ----------------------------------------------------

    def build(self) -> FunctionGraph:
        for blk in self.blocks:
            blk.ops = tuple(self._block_ops.get(blk.id, []))

        # Dedupe + materialize normal successor edges.
        edge_map: Dict[int, List[int]] = {b.id: [] for b in self.blocks}
        for src, dst in self.pending_edges:
            if dst in edge_map and dst not in edge_map[src]:
                edge_map[src].append(dst)
        for blk in self.blocks:
            blk.succs = tuple(edge_map.get(blk.id, ()))

        self._prune_empty_blocks()
        self._compute_preds()

        for blk in self.blocks:
            lines: List[int] = []
            for op in blk.ops:
                if op.source:
                    lines.append(op.source[0])
                    if op.source[2]:
                        lines.append(op.source[2])
            if lines:
                blk.attrs["line_range"] = (min(lines), max(lines))

        exit_blocks = []
        for blk in self.blocks:
            has_term = any(op.kind in ("return", "raise") for op in blk.ops)
            if has_term or not blk.succs:
                exit_blocks.append(blk.id)

        all_ops = [op for blk in self.blocks for op in blk.ops]
        entry = self.blocks[0].id if self.blocks else 0
        return FunctionGraph(
            qualname=self.qualname, source_path=self.source_path,
            first_line=self.first_line, blocks=tuple(self.blocks),
            entry=entry, exit_blocks=tuple(exit_blocks), ops=tuple(all_ops),
        )

    def _prune_empty_blocks(self) -> None:
        """Collapse empty pass-through blocks (no ops, single normal succ,
        no exceptional succ). Redirect every reference to the survivor."""
        entry_id = self.blocks[0].id if self.blocks else 0
        changed = True
        while changed:
            changed = False
            for blk in list(self.blocks):
                if blk.id == entry_id or blk.ops:
                    continue
                if blk.except_succs or len(blk.succs) != 1:
                    continue
                succ = blk.succs[0]
                if succ == blk.id:
                    continue
                for b in self.blocks:
                    b.succs = tuple(succ if s == blk.id else s for s in b.succs)
                    b.except_succs = tuple(
                        succ if s == blk.id else s for s in b.except_succs)
                self.blocks = [b for b in self.blocks if b.id != blk.id]
                changed = True  # a block was pruned -> iterate again
                break

        # Drop fully-dead empty blocks (no ops, no edges, not the entry).
        self.blocks = [
            b for b in self.blocks
            if b.id == entry_id or b.ops or b.succs or b.except_succs
        ]

    def _compute_preds(self) -> None:
        pred_map: Dict[int, List[int]] = {b.id: [] for b in self.blocks}
        for b in self.blocks:
            for s in (*b.succs, *b.except_succs):
                if s in pred_map and b.id not in pred_map[s]:
                    pred_map[s].append(b.id)
        for b in self.blocks:
            b.preds = tuple(pred_map.get(b.id, ()))


def _root_name(node: ast.AST) -> Optional[str]:
    """Leftmost Name id of an attribute/subscript chain, else None."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def build_cfg_from_ast(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_path: str = "<unknown>",
    qualname: Optional[str] = None,
) -> FunctionGraph:
    first_line = getattr(func_node, "lineno", 1)
    builder = _CFGBuilder(qualname or func_node.name, source_path, first_line)
    builder.visit(func_node)
    return builder.build()


def _find_function(tree: ast.AST, qualname: str):
    """Find a FunctionDef/AsyncFunctionDef by (possibly dotted) qualname.

    Supports 'func', 'Class.method', and 'outer.<locals>.inner' style by
    matching on the trailing dotted path of enclosing def/class names.
    """
    target_parts = [p for p in qualname.split(".") if p and p != "<locals>"]
    matches: List[Tuple[List[str], ast.AST]] = []

    def walk(node: ast.AST, scope: List[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = scope + [child.name]
                if qn[-len(target_parts):] == target_parts:
                    matches.append((qn, child))
                walk(child, qn)
            elif isinstance(child, ast.ClassDef):
                walk(child, scope + [child.name])
            else:
                walk(child, scope)

    walk(tree, [])
    if not matches:
        return None
    # Prefer the shallowest / most exact match.
    matches.sort(key=lambda m: (len(m[0]), m[0]))
    return matches[0][1]


def build_cfg_from_source(
    source: str,
    qualname: str,
    source_path: str = "<string>",
) -> FunctionGraph:
    tree = ast.parse(source, filename=source_path)
    node = _find_function(tree, qualname)
    if node is None:
        raise ValueError(f"no function named {qualname!r} found in {source_path}")
    return build_cfg_from_ast(node, source_path, qualname=qualname)
