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

from .ir import FunctionGraph
from .ir.cfg import build_cfg_from_ast
from .analysis.dataflow import run as run_dataflow

_SKIP_DIRS = {"__pycache__", ".venv", "venv", ".git", "build", "dist",
              ".mypy_cache", ".pytest_cache", "node_modules", ".tox", ".eggs"}


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


@dataclass
class ProgramGraph:
    root: str
    functions: Dict[str, FunctionGraph] = field(default_factory=dict)   # fqname -> graph
    modules: Dict[str, ModuleInfo] = field(default_factory=dict)        # relpath -> info
    errors: Dict[str, str] = field(default_factory=dict)                # relpath -> parse error

    # indices (built after load)
    _by_simple: Dict[str, Set[str]] = field(default_factory=dict)       # simple name -> fqnames
    _class_methods: Dict[str, Set[str]] = field(default_factory=dict)   # "relpath:Class" -> method fqnames
    _func_module: Dict[str, str] = field(default_factory=dict)          # fqname -> relpath

    # -- ref helpers -----------------------------------------------------

    def fqname(self, relpath: str, qualname: str) -> str:
        return f"{relpath}:{qualname}"

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

        # 2. imported symbol: `from x import f` then `f(...)`, or `import m` then `m.f(...)`.
        if mod is not None:
            if parts[0] in mod.imports or call_name in mod.imports:
                # we matched on a name; fall through to simple-name match on `last`
                pass

        # 3. same-module function by qualname suffix.
        if mod is not None:
            same = {fq for fq in mod.func_qualnames
                    if fq.split(":", 1)[1].split(".")[-1] == last}
            if same:
                return same

        # 4. global match by simple name (across the whole program).
        return set(self._by_simple.get(last, set()))

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


_CACHE_FORMAT = 1  # bump when the cached entry shape changes


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
    dirty = False

    for path in _iter_py_files(root, exclude_tests):
        relpath = os.path.relpath(path, root)
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = [st.st_mtime_ns, st.st_size]
        entry = cache.get(relpath)
        if entry is not None and entry.get("key") == key:
            mod, funcs, err = entry["mod"], entry["funcs"], entry.get("err")
        else:
            dirty = True
            try:
                src = open(path, encoding="utf-8").read()
                mod, funcs = _build_file(relpath, src)
                err = None
            except (OSError, SyntaxError) as e:
                mod, funcs, err = None, None, str(e)
        new_cache[relpath] = {"key": key, "mod": mod, "funcs": funcs, "err": err}

        if err is not None:
            pg.errors[relpath] = err
            continue
        pg.modules[relpath] = mod
        for fq, g in funcs.items():
            pg.functions[fq] = g
            pg._func_module[fq] = relpath

    if set(cache) - set(new_cache):
        dirty = True  # files were deleted
    if use_cache and dirty:
        _save_cache(pg.root, new_cache)

    _build_indices(pg)
    return pg


# -- on-disk cache -------------------------------------------------------

def _pflow_fingerprint() -> str:
    """Hash of pflow's own analysis sources; invalidates the cache on edits."""
    here = os.path.dirname(os.path.abspath(__file__))
    stamps = [_CACHE_FORMAT]
    for sub in ("ir/cfg.py", "ir/graph.py", "analysis/dataflow.py",
                "program.py", "analysis/interproc.py"):
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
    except Exception:
        pass


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
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mod.imports[local] = alias.name
                mod.import_modules.add(local)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                local = alias.asname or alias.name
                mod.imports[local] = f"{base}.{alias.name}" if base else alias.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    mod.globals.add(t.id)

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
