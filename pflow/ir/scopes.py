"""Compiler scope truth via the `symtable` module.

CPython's compiler builds a symbol table before emitting bytecode; the
`symtable` stdlib module exposes it without executing anything. It answers,
per scope and with compiler authority, exactly the questions pflow spent an
audit session getting right by hand: which names are local, which are free
(captured), which are declared global, which cells feed nested closures.

Four false-positive classes this session (nonlocal reads flagged unbound,
closure writes hidden from liveness, const-prop missing closure flips,
write-only captures) came from AST-walked approximations of this table. The
integration keeps those AST walkers as fallbacks (graphs built from a bare
AST node have no module source) but OVERWRITES their outputs with the
compiler's answers whenever source is available — same attrs, better truth:

    nonlocal_decls   names THIS scope declares free-and-assigned
    global_decls     names declared global here (merged with AST-found)
    nonlocal_writes  my locals some descendant scope rebinds via nonlocal
    sym_frees        every name captured from enclosing scopes (even read-only)
    sym_locals       compiler-verified local set
    scope_truth      "symtable" marker — consumers can tell which source ran
"""

from __future__ import annotations

import symtable
from typing import Dict, Tuple

from .graph import FunctionGraph


def module_scope_index(src: str, filename: str = "<module>"
                       ) -> Dict[Tuple[str, int], "symtable.SymbolTable"]:
    """{(function name, def lineno): SymbolTable} for every function scope in
    the module — one symtable pass per file, matched to graphs by
    (simple name, first_line)."""
    try:
        top = symtable.symtable(src, filename, "exec")
    except (SyntaxError, ValueError):
        return {}
    idx: Dict[Tuple[str, int], symtable.SymbolTable] = {}

    def walk(t: "symtable.SymbolTable") -> None:
        for ch in t.get_children():
            if ch.get_type() == "function":
                idx.setdefault((ch.get_name(), ch.get_lineno()), ch)
            walk(ch)

    walk(top)
    return idx


def attach_scope_truth(g: FunctionGraph,
                       idx: Dict[Tuple[str, int], "symtable.SymbolTable"]) -> None:
    t = idx.get((g.qualname.split(".")[-1], g.first_line))
    if t is None:
        return
    syms = t.get_symbols()
    frees_assigned = {s.get_name() for s in syms
                      if s.is_free() and s.is_assigned()}
    declared_global = {s.get_name() for s in syms if s.is_declared_global()}
    my_locals = {s.get_name() for s in syms if s.is_local()}

    nonlocal_writes: set = set()

    def descend(tt: "symtable.SymbolTable") -> None:
        for ch in tt.get_children():
            for s in ch.get_symbols():
                if s.is_free() and s.is_assigned() and s.get_name() in my_locals:
                    nonlocal_writes.add(s.get_name())
            descend(ch)

    descend(t)

    g.attrs["scope_truth"] = "symtable"
    g.attrs["sym_frees"] = frozenset(t.get_frees())
    g.attrs["sym_locals"] = frozenset(my_locals)
    if frees_assigned:
        g.attrs["nonlocal_decls"] = frees_assigned
    if declared_global:
        g.attrs["global_decls"] = (set(g.attrs.get("global_decls", set()))
                                   | declared_global)
    if nonlocal_writes:
        g.attrs["nonlocal_writes"] = frozenset(nonlocal_writes)
