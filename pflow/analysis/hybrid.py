from ..ir import FunctionGraph
from ..bytecode import get_basic_blocks, get_exception_table, offset_to_line_map


def attach_dis_info(g: FunctionGraph, code=None) -> FunctionGraph:
    if code is None:
        code = g.attrs.get("code_object")
    if not code:
        return g

    g.attrs["dis_blocks"] = get_basic_blocks(code)
    g.attrs["dis_exception_table"] = get_exception_table(code)
    return g


def correct_exception_edges(g: FunctionGraph) -> FunctionGraph:
    """Use dis exception table + offset->line map to refine except_succs.

    Matches handler target offsets to source lines, then to blocks by line_range.
    Mutates the graph in place. Works even when AST Try lowering was partial.
    """
    et = g.attrs.get("dis_exception_table", []) or g.attrs.get("exception_table", [])
    if not et:
        return g

    code = g.attrs.get("code_object")
    off2line = offset_to_line_map(code) if code else {}

    # Extract handler target offsets from exception table (list of dicts or tuples)
    handler_offsets = []
    for entry in et:
        if isinstance(entry, dict):
            t = entry.get("target")
            if t is not None:
                handler_offsets.append(int(t))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 3:
            # (start, end, target, ...)
            handler_offsets.append(int(entry[2]))

    g.attrs["exception_handler_offsets"] = sorted(set(handler_offsets))

    # Map handler offsets -> approximate lineno -> candidate handler blocks
    handler_lines = {}
    for off in handler_offsets:
        ln = off2line.get(off)
        if ln:
            handler_lines[off] = ln

    # Find blocks that look like handlers (synthetic enter_except or enter_finally, or line match)
    handler_blocks = []
    for b in g.blocks:
        if any(op.attrs.get("is_synthetic") and op.attrs.get("handler") for op in b.ops):
            handler_blocks.append(b.id)
            b.attrs["is_exception_handler"] = True
            continue
        if any(op.attrs.get("is_synthetic") and "finally" in str(op.attrs) for op in b.ops):
            b.attrs["is_finally_region"] = True
        # line-based: if block lines overlap a known handler line, treat as handler target
        lr = b.attrs.get("line_range")
        if lr and handler_lines:
            b0, b1 = lr
            for ln in handler_lines.values():
                if b0 <= ln <= b1 or b0 <= ln <= b0 + 2:
                    if b.id not in handler_blocks:
                        handler_blocks.append(b.id)
                    b.attrs["is_exception_handler"] = True

    g.attrs["computed_handler_blocks"] = handler_blocks

    if not handler_blocks:
        # fallback to any synthetic block
        for b in g.blocks:
            if any(op.attrs.get("is_synthetic") for op in b.ops):
                handler_blocks.append(b.id)

    # Attach except_succs to blocks whose line_range precedes or overlaps handler lines
    # (i.e. the try region blocks)
    for b in g.blocks:
        lr = b.attrs.get("line_range")
        if not lr:
            continue
        b0, b1 = lr
        for ln in handler_lines.values():
            if b1 < ln or (b0 <= ln <= b1 + 1):
                # this block is before or at the handler line -> likely can reach it exceptionally
                existing = set(b.except_succs)
                for hb in handler_blocks:
                    if hb not in existing and hb != b.id:
                        b.except_succs = b.except_succs + (hb,)

    g.attrs["raw_exception_table"] = et
    return g


def suggest_exception_edges(g: FunctionGraph) -> list:
    return g.attrs.get("dis_exception_table", [])
