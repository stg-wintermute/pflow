import dis
from types import CodeType
from typing import List, Tuple


def get_basic_blocks(code: CodeType) -> List[Tuple[int, List[dis.Instruction]]]:
    bc = list(dis.Bytecode(code))
    if not bc:
        return []

    targets = set()
    for instr in bc:
        if instr.is_jump_target:
            targets.add(instr.offset)
        # 3.13+ exposes instr.jump_target (absolute target offset, or None).
        jt = getattr(instr, "jump_target", None)
        if jt is not None:
            targets.add(jt)
        elif "JUMP" in instr.opname and isinstance(instr.argval, int):
            targets.add(instr.argval)

    blocks = []
    current = []
    start = bc[0].offset

    for instr in bc:
        if instr.offset in targets and current:
            blocks.append((start, current))
            current = []
            start = instr.offset
        current.append(instr)

    if current:
        blocks.append((start, current))
    return blocks


_MODULE_CODE_CACHE: dict = {}    # (path, mtime_ns) -> compiled module code | None


def function_code(source_path: str, qualname: str):
    """The compiled code object for `qualname` inside `source_path`, or None.
    compile() only — nothing is executed. Cached per (path, mtime)."""
    import os
    try:
        st = os.stat(source_path)
    except OSError:
        return None
    key = (source_path, st.st_mtime_ns)
    if key not in _MODULE_CODE_CACHE:
        _MODULE_CODE_CACHE.clear()               # one file at a time is plenty
        try:
            import tokenize
            with tokenize.open(source_path) as f:
                _MODULE_CODE_CACHE[key] = compile(f.read(), source_path, "exec")
        except (OSError, SyntaxError, ValueError):
            _MODULE_CODE_CACHE[key] = None
    module_code = _MODULE_CODE_CACHE[key]
    if module_code is None:
        return None

    want = qualname.replace(".<locals>", "")

    def find(code):
        for const in code.co_consts:
            if isinstance(const, CodeType):
                if const.co_qualname.replace(".<locals>", "") == want:
                    return const
                hit = find(const)
                if hit is not None:
                    return hit
        return None

    return find(module_code)


def unbound_checked_names(code: CodeType) -> frozenset:
    """Locals the COMPILER could not prove definitely assigned: CPython 3.12+
    emits LOAD_FAST_CHECK (instead of LOAD_FAST) for exactly those loads.
    A local absent from this set had every load proven bound by the compiler
    — a free, compiler-grade definite-assignment oracle."""
    return frozenset(i.argval for i in dis.get_instructions(code)
                     if i.opname == "LOAD_FAST_CHECK")


def get_exception_table(code: CodeType) -> List[dict]:
    raw = getattr(code, "co_exceptiontable", None)
    if not raw:
        return []
    try:
        return dis._parse_exception_table(raw)
    except Exception:
        return []


def bytecode_to_simple_cfg(code: CodeType):
    """Return a minimal dict view: blocks + control edges derived purely from dis."""
    blocks = get_basic_blocks(code)
    if not blocks:
        return {"blocks": [], "edges": []}

    edges = []
    for i, (start, instrs) in enumerate(blocks):
        last = instrs[-1]
        if "JUMP" in last.opname and last.argval is not None:
            edges.append((start, last.argval))
        elif last.opname not in ("RETURN_VALUE", "RETURN_CONST", "RAISE", "RERAISE"):
            if i + 1 < len(blocks):
                edges.append((start, blocks[i+1][0]))
    return {"blocks": [b[0] for b in blocks], "edges": edges}


def offset_to_line_map(code: CodeType) -> dict:
    """Return {bytecode_offset: lineno} for the code object (best effort for 3.12+)."""
    m = {}
    try:
        # Preferred: walk instructions with their line numbers.
        # 3.13+ uses instr.line_number; older versions used starts_line.
        for instr in dis.Bytecode(code):
            ln = getattr(instr, "line_number", None)
            if ln is None:
                # On 3.13+ starts_line is a bool (is-line-start), not a line
                # number; only treat it as a line on older versions.
                sl = getattr(instr, "starts_line", None)
                if isinstance(sl, int) and not isinstance(sl, bool):
                    ln = sl
            if instr.offset is not None and ln is not None:
                m[instr.offset] = ln
        if m:
            return m
    except Exception:
        pass
    # Fallback: co_lines (3.10+)
    try:
        for start, _end, lineno in code.co_lines():
            if lineno is not None:
                # rough: assign to start offset if we have it
                m[start] = lineno
    except Exception:
        pass
    # Last resort: dis.findlinestarts
    try:
        for offset, lineno in dis.findlinestarts(code):
            m[offset] = lineno
    except Exception:
        pass
    return m
