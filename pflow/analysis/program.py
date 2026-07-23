"""Program-level model: many files -> one resolvable graph (v1, conservative).

This is the substrate for pflow's interprocedural views (call graph, shared
state map, value tracing). It deliberately stays best-effort and name-based,
in keeping with RFC §5.6 — "good enough for review agents", not sound for a
compiler. Call resolution is import-aware where it can be and falls back to
matching by simple name / Class.method across the whole program.

Functions are keyed by a fully-qualified ref `relpath:qualname`, e.g.
`server/cloud.py:create_rental` or `server/app.py:Reconciler.step`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import pickle
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..ir import FunctionGraph
from ..ir.cfg import build_cfg_from_ast
from .dataflow import run as run_dataflow

import builtins as _builtins

_BUILTIN_NAMES = frozenset(dir(_builtins))
_BUILTIN_TYPE_METHODS = frozenset(
    m for t in (str, bytes, dict, list, set, tuple, frozenset)
    for m in dir(t) if not m.startswith("_"))

_SKIP_DIRS = {"__pycache__", ".venv", "venv", ".git", "build", "dist",
              ".mypy_cache", ".pytest_cache", "node_modules", ".tox", ".eggs"}

# Max candidates a simple-name call resolution may return before it is
# treated as unresolved (see ProgramGraph.resolve_call step 4).
_SMEAR_CAP = 24


@dataclass
class ClassInfo:
    name: str
    bases: Tuple[str, ...]            # base names as written (last component)
    methods: Set[str] = field(default_factory=set)   # simple method names


@dataclass
class ModuleInfo:
    relpath: str
    module_name: str
    imports: Dict[str, str] = field(default_factory=dict)   # local name -> dotted source
    import_modules: Set[str] = field(default_factory=set)   # `import a.b` style module aliases
    globals: Set[str] = field(default_factory=set)          # module-level assigned names
    classes: Dict[str, ClassInfo] = field(default_factory=dict)
    func_qualnames: List[str] = field(default_factory=list)
    # imports written INSIDE functions/methods — usually deliberate
    # (cycle-breaking, optional deps, startup cost); a distinct edge class.
    lazy_imports: Dict[str, str] = field(default_factory=dict)
    # top-level statements that RUN at import time (loops, calls, with/try
    # beyond the optional-import idiom): (line, short description)
    side_effects: List[Tuple[int, str]] = field(default_factory=list)
    call_assigns: int = 0        # top-level `X = f(...)` — milder import-time work


@dataclass
class ProgramGraph:
    root: str
    functions: Dict[str, FunctionGraph] = field(default_factory=dict)   # fqname -> graph
    modules: Dict[str, ModuleInfo] = field(default_factory=dict)        # relpath -> info
    errors: Dict[str, str] = field(default_factory=dict)                # relpath -> parse error

    # cache identity of this build: digest of (relpath, mtime, size) for every
    # file — derived artifacts (the call graph) key their own caches on it
    cache_digest: str = ""
    cache_enabled: bool = True

    # indices (built after load)
    _by_simple: Dict[str, Set[str]] = field(default_factory=dict)       # simple name -> fqnames
    _class_methods: Dict[str, Set[str]] = field(default_factory=dict)   # "relpath:Class" -> method fqnames
    _func_module: Dict[str, str] = field(default_factory=dict)          # fqname -> relpath
    _import_map: Dict[str, Dict[str, str]] = field(default_factory=dict)  # relpath -> merged (eager+lazy) imports

    # -- ref helpers -----------------------------------------------------

    def fqname(self, relpath: str, qualname: str) -> str:
        return f"{relpath}:{qualname}"

    def display_path(self, relpath: str) -> str:
        """Runnable path for a program-relative relpath — so a rendered
        ref/target can be fed straight back into the CLI. CWD-relative when
        that is shorter; absolute for out-of-tree roots (site-packages)."""
        base = self.root if os.path.isfile(self.root) else os.path.join(self.root, relpath)
        rel = os.path.relpath(base)
        return rel if not rel.startswith("..") else base

    def display_root(self) -> str:
        rel = os.path.relpath(self.root)
        return rel if not rel.startswith("..") else self.root

    @property
    def out_of_tree(self) -> bool:
        """True when the program root is outside the CWD (an installed
        library) — row displays should then use in-root relpaths and let the
        header's root line carry the prefix once."""
        return os.path.relpath(self.root).startswith("..")

    def module_of(self, fqname: str) -> Optional[ModuleInfo]:
        return self.modules.get(self._func_module.get(fqname, ""))

    def class_of(self, fqname: str) -> Optional[str]:
        """Return 'relpath:Class' if the function is a method, else None."""
        relpath = self._func_module.get(fqname)
        if relpath is None:
            return None
        qual = fqname[len(relpath) + 1:]
        parts = [p for p in qual.split(".") if p != "<locals>"]
        if len(parts) >= 2:
            mod = self.modules.get(relpath)
            # the immediate enclosing scope is the class iff it is a known class
            enclosing = parts[-2]
            if mod and enclosing in mod.classes:
                return f"{relpath}:{enclosing}"
        return None

    # -- call resolution -------------------------------------------------

    def resolve_call(self, caller_fqname: str, call_name: str) -> Set[str]:
        """Best-effort resolution of a call name to callee fqnames."""
        if not call_name:
            return set()
        relpath = self._func_module.get(caller_fqname)
        mod = self.modules.get(relpath) if relpath else None
        parts = call_name.split(".")
        last = parts[-1]

        # 0. super().method -> the SAME method on in-program base classes only
        #    (never the caller itself — avoids spurious __init__ self-cycles).
        if len(parts) == 2 and parts[0].startswith("super(") and mod is not None:
            owner = self.class_of(caller_fqname)
            if owner:
                return self._base_methods_named(owner, last, mod)

        # 1. self.method / cls.method -> methods of the caller's class (+ bases).
        if len(parts) == 2 and parts[0] in ("self", "cls") and mod is not None:
            owner = self.class_of(caller_fqname)
            if owner:
                hits = self._methods_named(owner, last, mod)
                if hits:
                    return hits

        # 2. imported name: resolve THROUGH the import, never past it.
        # `os.path.join(...)` is a call into os.path — if that module isn't in
        # this program, the call is EXTERNAL, not an excuse to edge to every
        # in-program `join` (that fall-through smeared stdlib-scale graphs
        # into millions of junk edges). Import bindings shadow globals, so an
        # empty result here is final.
        if mod is not None:
            src = self._import_map.get(mod.relpath)
            if src is None:
                src = {**getattr(mod, "lazy_imports", {}), **mod.imports}
                self._import_map[mod.relpath] = src
            dotted = rest = None
            if call_name in src:
                dotted, rest = src[call_name], []
            elif parts[0] in src:
                dotted, rest = src[parts[0]], parts[1:]
            if dotted is not None:
                return self._resolve_via_import(dotted, rest, mod)

        # An attribute call through a non-self receiver (`self._client.create()`)
        # matching the CALLING method's own name is a coincidence, not
        # recursion — real self-calls arrive via `self.m()` (step 1) or a bare
        # name. Without this, every delegate-with-same-name fabricated a
        # self-loop (pacifica's Sandbox.create -> client.create).
        # (`self.m()` itself is step-1 territory; `self._client.m()` is a call
        # on the ATTRIBUTE, so it counts as an other-receiver call here.)
        dotted_other = "." in call_name and not (
            len(parts) == 2 and parts[0] in ("self", "cls"))

        # 3. same-module function by qualname suffix (or class -> __init__).
        if mod is not None:
            same = {fq for fq in mod.func_qualnames
                    if fq.split(":", 1)[1].split(".")[-1] == last}
            if dotted_other:
                same.discard(caller_fqname)
            if same:
                return same
            if last in mod.classes:
                return self._constructor_of(mod.relpath, last)

        # 4. bare builtin-name call (`len(x)`, `print(...)`): it's the
        # builtin — not the same-named method somewhere in the program.
        if call_name == last and last in _BUILTIN_NAMES:
            return set()

        # 5. global match by simple name (across the whole program) — capped:
        # past ~two dozen candidates a "match" carries no information (think
        # `run`/`close` across a big tree: hundreds of hits) and at stdlib
        # scale the smear multiplied the edge count into the millions. Too
        # smeared counts as unresolved, which consumers already report.
        hits = set(self._by_simple.get(last, set()))
        if len(hits) > _SMEAR_CAP:
            return set()
        # `sep.join(...)`, `d.items()` — an attribute call whose method name
        # belongs to a builtin type is far more likely str/dict/list traffic
        # than a hit on one of several same-named in-program methods; only a
        # near-unique name keeps evidential value.
        if "." in call_name and last in _BUILTIN_TYPE_METHODS and len(hits) > 3:
            return set()
        if dotted_other:
            hits.discard(caller_fqname)
        return hits

    def _module_by_name(self, name: str) -> Optional[str]:
        """relpath of the in-program module `name`, matching exactly or by
        dotted suffix (the program root may sit below the import's prefix)."""
        if not name:
            return None
        exact = [rp for rp, m in self.modules.items() if m.module_name == name]
        if exact:
            return exact[0]
        hits = [rp for rp, m in self.modules.items()
                if m.module_name and name.endswith("." + m.module_name)]
        return hits[0] if len(hits) == 1 else None

    def _constructor_of(self, relpath: str, cls_name: str) -> Set[str]:
        key = f"{relpath}:{cls_name}"
        return {m for m in self._class_methods.get(key, set())
                if m.split(".")[-1] == "__init__"}

    def _resolve_via_import(self, dotted: str, rest: List[str], mod) -> Set[str]:
        """Resolve an imported binding (+ trailing attribute path) to
        in-program functions; set() means the call leaves the program."""
        if dotted.startswith("."):
            n = len(dotted) - len(dotted.lstrip("."))
            pkg = mod.module_name.split(".") if mod.module_name else []
            if not mod.relpath.endswith("__init__.py") and pkg:
                pkg = pkg[:-1]                    # module -> its package
            pkg = pkg[:len(pkg) - (n - 1)] if n > 1 else pkg
            stem = dotted.lstrip(".")
            dotted = ".".join(pkg + (stem.split(".") if stem else []))
        full = ([p for p in dotted.split(".") if p] + list(rest))
        if not full:
            return set()
        symbol = full[-1]
        target = self._module_by_name(".".join(full[:-1]))
        if target is None:
            # `from pkg import mod` then `mod()` — the binding itself may BE
            # an in-program module; a bare-module call resolves to nothing.
            return set()
        tmod = self.modules[target]
        fns = {fq for fq in tmod.func_qualnames
               if fq.split(":", 1)[1].split(".")[-1] == symbol}
        if fns:
            return fns
        if symbol in tmod.classes:
            return self._constructor_of(target, symbol)
        return set()

    def _methods_named(self, owner_key: str, name: str, mod: ModuleInfo) -> Set[str]:
        hits = set(m for m in self._class_methods.get(owner_key, set())
                   if m.split(".")[-1] == name)
        if hits:
            return hits
        return self._base_methods_named(owner_key, name, mod)

    def _base_methods_named(self, owner_key: str, name: str, mod: ModuleInfo) -> Set[str]:
        """Methods `name` on the in-program base classes of owner (shallow, no MRO)."""
        cls_name = owner_key.split(":", 1)[1]
        cinfo = mod.classes.get(cls_name)
        hits: Set[str] = set()
        if cinfo:
            for base in cinfo.bases:
                bkey = f"{mod.relpath}:{base}"
                hits |= {m for m in self._class_methods.get(bkey, set())
                         if m.split(".")[-1] == name}
        return hits

    def calls_of(self, fqname: str) -> List[str]:
        """All callee names referenced by a function's ops (from call metadata)."""
        g = self.functions.get(fqname)
        if g is None:
            return []
        names: List[str] = []
        for op in g.iter_ops():
            for c in op.attrs.get("calls", ()):  # type: ignore[union-attr]
                if c.get("func"):
                    names.append(c["func"])
        return names


_CACHE_FORMAT = 4  # bump when the cached entry shape changes


def build_program(root: str, exclude_tests: bool = False,
                  use_cache: bool = True) -> ProgramGraph:
    """Build the whole-program graph, reusing a per-file on-disk cache.

    Each file's parsed-and-lowered result (its ModuleInfo + FunctionGraphs) is
    cached keyed by (mtime, size); only changed files are rebuilt. The cache is
    invalidated wholesale when pflow's own analysis code changes. This lets the
    agent run callgraph/state/trace back-to-back without re-lowering the tree
    each time — one build, many scans.
    """
    pg = ProgramGraph(root=os.path.abspath(root))
    cache = _load_cache(pg.root) if use_cache else {}
    new_cache: dict = {}
    root_is_file = os.path.isfile(root)

    # Pass 1: stat everything, split cache hits from misses.
    entries = []                                  # (relpath, key, cached | None)
    misses = []                                   # (path, relpath)
    for path in _iter_py_files(root, exclude_tests):
        # For a single-file root, relpath(path, root) degenerates to "." and
        # every fqname renders as ".:qualname" — use the basename instead.
        relpath = os.path.basename(path) if root_is_file else os.path.relpath(path, root)
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = [st.st_mtime_ns, st.st_size]
        entry = cache.get(relpath)
        if entry is not None and entry.get("key") != key:
            entry = None
        entries.append((relpath, key, entry))
        if entry is None:
            misses.append((path, relpath))

    # Pass 2: lower the misses. Deliberately serial: a ProcessPoolExecutor
    # variant was measured on the full stdlib tree (3,701 files, 8 cores) at
    # 2m27 wall / 5m07 CPU vs ~2m15 serial — shipping the lowered graphs back
    # through pickle costs as much as lowering them, so parallelism buys
    # nothing here. Don't re-add without changing what workers return.
    built: dict = {}
    for job in misses:
        relpath, mod, funcs, err = _build_one(job)
        built[relpath] = (mod, funcs, err)

    # Pass 3: assemble in deterministic file order.
    for relpath, key, entry in entries:
        if entry is not None:
            mod, funcs, err = entry["mod"], entry["funcs"], entry.get("err")
        else:
            mod, funcs, err = built[relpath]
        new_cache[relpath] = {"key": key, "mod": mod, "funcs": funcs, "err": err}
        if err is not None:
            pg.errors[relpath] = err
            continue
        pg.modules[relpath] = mod
        for fq, g in funcs.items():
            pg.functions[fq] = g
            pg._func_module[fq] = relpath

    dirty = bool(misses) or bool(set(cache) - set(new_cache))
    if use_cache and dirty:
        _save_cache(pg.root, new_cache)

    pg.cache_digest = hashlib.sha1(
        repr([(rp, e["key"]) for rp, e in sorted(new_cache.items())]).encode()
    ).hexdigest()[:16]
    pg.cache_enabled = use_cache
    _build_indices(pg)
    return pg


def _build_one(job):
    """(path, relpath) -> (relpath, mod, funcs, err); pure per file."""
    path, relpath = job
    try:
        import tokenize
        with tokenize.open(path) as f:            # honors PEP 263 coding cookies
            src = f.read()
        mod, funcs = _build_file(relpath, src)
        return relpath, mod, funcs, None
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as e:
        # one undecodable/unparseable file must not kill the build —
        # record it and keep going (found on stdlib latin-1 fixtures)
        return relpath, None, None, str(e)


# -- on-disk cache -------------------------------------------------------

def _pflow_fingerprint() -> str:
    """Hash of pflow's own analysis sources; invalidates the cache on edits."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the pflow package
    stamps = [_CACHE_FORMAT]
    for sub in ("ir/cfg.py", "ir/graph.py", "analysis/dataflow.py",
                "analysis/program.py", "analysis/interproc.py"):
        try:
            st = os.stat(os.path.join(here, sub))
            stamps.append((sub, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return hashlib.sha1(repr(stamps).encode()).hexdigest()[:16]


def _cache_path(root: str) -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    key = hashlib.sha1(root.encode()).hexdigest()[:16]
    return os.path.join(base, "pflow", "program", f"{key}.pkl")


def _load_cache(root: str) -> dict:
    try:
        with open(_cache_path(root), "rb") as f:
            blob = pickle.load(f)
        if blob.get("fp") != _pflow_fingerprint():
            return {}
        return blob.get("files", {})
    except Exception:
        return {}


def _save_cache(root: str, files: dict) -> None:
    path = _cache_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"fp": _pflow_fingerprint(), "files": files}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        # a failed save must not break analysis, but a SILENT failure makes
        # every future run pay full rebuild cost with no explanation
        import sys
        print(f"pflow: program cache not saved ({type(e).__name__}: {e})",
              file=sys.stderr)


def _iter_py_files(root: str, exclude_tests: bool):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if exclude_tests and (fn.startswith("test_") or "/tests/" in dirpath + "/"):
                continue
            yield os.path.join(dirpath, fn)


def _module_name(relpath: str) -> str:
    no_ext = relpath[:-3] if relpath.endswith(".py") else relpath
    parts = [p for p in no_ext.replace("\\", "/").split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _build_file(relpath: str, src: str):
    """Parse one file and build (ModuleInfo, {fqname: FunctionGraph}).

    Pure w.r.t. the program: returns its result so it can be cached per file.
    Raises SyntaxError on unparseable source (caller records it).
    """
    tree = ast.parse(src, filename=relpath)
    mod = ModuleInfo(relpath=relpath, module_name=_module_name(relpath))
    funcs: Dict[str, FunctionGraph] = {}

    # module-level imports + globals (top-level only)
    top_level_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.add(node)
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mod.imports[local] = alias.name
                mod.import_modules.add(local)
        elif isinstance(node, ast.ImportFrom):
            top_level_imports.add(node)
            base = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                local = alias.asname or alias.name
                # `from . import x` must record ".x", not "..x" — a trailing
                # dot in base already separates
                mod.imports[local] = (f"{base}{alias.name}" if base.endswith(".")
                                      else f"{base}.{alias.name}" if base
                                      else alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    mod.globals.add(t.id)

    # import-time work: top-level statements that execute on import. Benign:
    # defs, classes, imports, plain assigns, docstrings, the __main__ guard,
    # TYPE_CHECKING blocks, and try-blocks containing only imports/assigns
    # (the optional-dependency idiom). Everything else is doing work when
    # someone imports this module — a thing a debugger wants to know.
    def _benign_if(node: ast.If) -> bool:
        src = ast.unparse(node.test)
        return "__name__" in src or "TYPE_CHECKING" in src

    def _benign_try(node) -> bool:
        stmts = list(node.body) + [s for h in node.handlers for s in h.body]
        stmts += list(getattr(node, "orelse", ())) + list(getattr(node, "finalbody", ()))
        return all(isinstance(s, (ast.Import, ast.ImportFrom, ast.Assign,
                                  ast.AnnAssign, ast.Pass, ast.Raise)) for s in stmts)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef, ast.Pass,
                             ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            if any(isinstance(n, ast.Call) for n in ast.walk(node.value)):
                mod.call_assigns += 1
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                                   # docstring
        if isinstance(node, ast.If) and _benign_if(node):
            continue
        if isinstance(node, (ast.Try,)) and _benign_try(node):
            continue
        desc = ast.unparse(node).split("\n", 1)[0]
        mod.side_effects.append((node.lineno,
                                 desc[:60] + ("…" if len(desc) > 60 else "")))

    # function-level (lazy) imports — a distinct edge class for the import
    # graph: they don't run at import time, so they don't create load-order
    # cycles, but they ARE architectural dependencies.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and node not in top_level_imports:
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mod.lazy_imports.setdefault(local, alias.name)
        elif isinstance(node, ast.ImportFrom) and node not in top_level_imports:
            base = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                local = alias.asname or alias.name
                mod.lazy_imports.setdefault(
                    local, f"{base}{alias.name}" if base.endswith(".")
                    else f"{base}.{alias.name}" if base else alias.name)

    # functions + classes (recursive, tracking dotted qualnames)
    def walk(node: ast.AST, scope: List[str], cls_stack: List[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                bases = tuple(_name_of(b) for b in child.bases if _name_of(b))
                ci = ClassInfo(name=child.name, bases=bases)
                mod.classes[child.name] = ci
                walk(child, scope + [child.name], cls_stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(scope + [child.name])
                fq = f"{relpath}:{qual}"
                try:
                    g = run_dataflow(build_cfg_from_ast(child, relpath, qualname=qual))
                    g.attrs["global_decls"] = _global_decls(child)
                    funcs[fq] = g
                    mod.func_qualnames.append(fq)
                    if cls_stack:
                        mod.classes[cls_stack[-1]].methods.add(child.name)
                except Exception:
                    pass
                walk(child, scope + [child.name], cls_stack)
            else:
                walk(child, scope, cls_stack)

    walk(tree, [], [])
    return mod, funcs


def _global_decls(funcnode: ast.AST) -> Set[str]:
    """Names declared `global` directly in this function (not in nested defs)."""
    names: Set[str] = set()

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, ast.Global):
                names.update(child.names)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested scope owns its own globals
            else:
                walk(child)

    walk(funcnode)
    return names


def _name_of(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _build_indices(pg: ProgramGraph) -> None:
    for fq in pg.functions:
        qual = fq.split(":", 1)[1]
        simple = qual.split(".")[-1]
        pg._by_simple.setdefault(simple, set()).add(fq)
    for relpath, mod in pg.modules.items():
        for cls_name in mod.classes:
            key = f"{relpath}:{cls_name}"
            for fq in mod.func_qualnames:
                qual = fq.split(":", 1)[1]
                parts = [p for p in qual.split(".") if p != "<locals>"]
                if len(parts) >= 2 and parts[-2] == cls_name:
                    pg._class_methods.setdefault(key, set()).add(fq)  # store fqname
