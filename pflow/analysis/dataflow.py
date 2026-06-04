"""Reaching definitions dataflow pass (v1).

Classic forward, may-analysis reaching definitions (RFC §8) with one
important addition the first cut got wrong: a *local* forward pass inside
each block so that a use sees definitions made earlier in the same block.

    gen[b]   = the last definition of each name in b (last-write-wins)
    kill[b]  = the set of names (re)defined anywhere in b
    in[b]    = union over preds of out[p]
    out[b]   = gen[b] | (in[b] \\ {d in in[b] : d.name in kill[b]})

After the block-entry sets converge we walk each block op-by-op,
threading a name -> {reaching def ids} map, to attach reaching_defs to
every Use (including intra-block dependencies).
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from ..ir.graph import FunctionGraph, Def, Use
from .solver import solve


def run(graph: FunctionGraph) -> FunctionGraph:
    if not graph.blocks:
        graph.defs = []
        graph.uses = []
        return graph

    _ensure_preds(graph)

    # 1. Materialize every definition site, in deterministic order, and index
    #    (op_id, name) -> def_id so the local pass can find them in O(1).
    all_defs: List[Def] = []
    def_by_op_name: Dict[Tuple[int, str], int] = {}
    name_of: Dict[int, str] = {}
    next_id = 0
    for blk in graph.blocks:
        for op in blk.ops:
            for name in op.targets:
                d = Def(id=next_id, op_id=op.id, block_id=blk.id,
                        name=name, source=op.source)
                all_defs.append(d)
                def_by_op_name[(op.id, name)] = next_id
                name_of[next_id] = name
                next_id += 1
    graph.defs = all_defs

    # 2. gen (last-write-wins) and kill (by name) per block.
    gen: Dict[int, Set[int]] = {}
    kill: Dict[int, Set[str]] = {}
    for blk in graph.blocks:
        last: Dict[str, int] = {}
        for op in blk.ops:
            for name in op.targets:
                last[name] = def_by_op_name[(op.id, name)]
        gen[blk.id] = set(last.values())
        kill[blk.id] = set(last.keys())

    # 3. Forward may-analysis fixpoint via the generic solver (RFC-0002 §3.1):
    #    in[b] = union of preds' out;  out[b] = gen[b] | (in[b] \ {killed names}).
    def _transfer(blk, in_set):
        killed = {d for d in in_set if name_of[d] in kill[blk.id]}
        return gen[blk.id] | (in_set - killed)

    reaching_in, reaching_out = solve(
        graph, "forward",
        init=set, boundary=set,
        meet=lambda vals: set().union(*vals),
        transfer=_transfer,
    )

    for blk in graph.blocks:
        blk.attrs["reaching_in"] = set(reaching_in[blk.id])
        blk.attrs["reaching_out"] = set(reaching_out[blk.id])
        blk.attrs["gen"] = set(gen[blk.id])
        blk.attrs["kill"] = set(kill[blk.id])

    # 4. Local forward pass: thread reaching defs through each block so uses
    #    pick up definitions from earlier in the same block.
    uses: List[Use] = []
    use_id = 0
    for blk in graph.blocks:
        cur: Dict[str, Set[int]] = {}
        for d in reaching_in[blk.id]:
            cur.setdefault(name_of[d], set()).add(d)
        for op in blk.ops:
            for name in op.uses:
                reaching = tuple(sorted(cur.get(name, ())))
                uses.append(Use(id=use_id, op_id=op.id, block_id=blk.id,
                                name=name, reaching_defs=reaching))
                use_id += 1
            for name in op.targets:
                cur[name] = {def_by_op_name[(op.id, name)]}

    graph.uses = uses
    return graph


def _ensure_preds(graph: FunctionGraph) -> None:
    """Fill .preds from succ + except_succ edges if they look unset."""
    if any(b.preds for b in graph.blocks):
        return
    pred_map: Dict[int, List[int]] = {b.id: [] for b in graph.blocks}
    for blk in graph.blocks:
        for s in (*blk.succs, *blk.except_succs):
            if s in pred_map and blk.id not in pred_map[s]:
                pred_map[s].append(blk.id)
    for blk in graph.blocks:
        blk.preds = tuple(pred_map.get(blk.id, ()))
