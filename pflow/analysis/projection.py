"""Lossy-projection detector (RFC-0002): an open record narrowed to a closed dict.

A boundary that takes an OPEN mapping (a free-form request/spec dict) and
returns/stores a CLOSED dict literal silently drops every key it does not
enumerate. The acute case (seeker `validate_service_payload`): an output key is
a nested record the function never sources from the input at all —
`"workload_spec": {"container_port": c}` while `p["workload_spec"]` is never
read — so caller-supplied `workload_spec` is dropped.

Two ranked signals, both heuristic/may (this is undecidable in general — Rice —
and intentional projection is common, so it flags, never asserts):

  unsourced-field   (strong) — an output key whose value is a container literal
                               that is never sourced from the open input.
  closed-projection (hint)   — the whole returned dict is a closed projection of
                               an open input (>=2 of its keys), with no
                               `{**input, ...}` carry-rest.

Name-based, intra-function, no type inference. Consumes the `read_keys` /
`dict_build` op attrs attached at lowering (pflow/ir/cfg.py) plus the args op's
`arg_annotations`. NOT wired into run_passes yet — awaiting corpus FP dogfooding.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

from ..ir import FunctionGraph
from .opportunities import Opportunity

# Annotation that reads as an open mapping (or "anything"). A concrete
# annotation (str, int, a TypedDict/dataclass name, ...) is treated as closed.
_OPEN_ANN = re.compile(r"^(typing\.)?(dict|Dict|Mapping|MutableMapping|"
                       r"OrderedDict|defaultdict|Any|object)\b")


def _open_params(graph: FunctionGraph) -> Set[str]:
    """Params that look like open mappings: annotated dict/Mapping/Any/object,
    unannotated, or **kwargs."""
    if not graph.blocks:
        return set()
    args_op = next((op for op in graph.blocks[0].ops
                    if op.attrs.get("kind") == "args"), None)
    if args_op is None:
        return set()
    ann = args_op.attrs.get("arg_annotations", {})
    kwarg = args_op.attrs.get("kwarg_name")
    out: Set[str] = set()
    for name in args_op.targets:
        a = ann.get(name)
        if name == kwarg or a is None or _OPEN_ANN.match(a):
            out.add(name)
    return out


def _read_keys_by_base(graph: FunctionGraph) -> Dict[str, Set[str]]:
    by_base: Dict[str, Set[str]] = {}
    for op in graph.iter_ops():
        for base, key in op.attrs.get("read_keys", ()):
            by_base.setdefault(base, set()).add(key)
    return by_base


def find_lossy_projection(graph: FunctionGraph) -> List[Opportunity]:
    if not graph.blocks:
        return []
    open_params = _open_params(graph)
    if not open_params:
        return []
    reads = _read_keys_by_base(graph)
    # Anchor both signals to a real projection context: only open params that
    # are actually consumed as a record (>=1 key read from them).
    record_params = {p for p in open_params if reads.get(p)}
    if not record_params:
        return []
    read_any: Set[str] = set().union(*(reads[p] for p in record_params))
    # Names handed back to the caller — a dict built into one of these (or into
    # an open param itself) is a projection boundary; a mid-function temp is not.
    returned: Set[str] = set()
    for op in graph.iter_ops():
        if op.kind == "return":
            returned.update(op.uses)
    proj_targets = record_params | returned

    op_line = {op.id: (op.source[0] if op.source else None) for op in graph.iter_ops()}
    block_of = {op.id: b.id for b in graph.blocks for op in b.ops}

    strong: List[Opportunity] = []
    hints: List[Opportunity] = []
    seen_field: Set[Tuple[int, str]] = set()
    for op in graph.iter_ops():
        db = op.attrs.get("dict_build")
        if not db:
            continue
        is_proj_site = op.kind == "return" or bool(set(op.targets) & proj_targets)
        if not is_proj_site:
            continue
        ln = op_line.get(op.id)
        ref = f"{graph.qualname}:op:{op.id}"
        bid = block_of.get(op.id, graph.entry)

        # `{**input, ...}` carries the rest through — nothing is dropped, so no
        # field is "unsourced". Suppress the strong signal at such sites.
        carries_rest = any(s in open_params for s in db.get("spread_names", ()))

        # strong: container-valued output keys unsourced from the open input.
        for key in () if carries_rest else db.get("keys", ()):
            f = db["fields"].get(key, {})
            if not f.get("container") or key in read_any:
                continue
            if any(r in record_params for r in f.get("reads", ())):
                continue  # value reads the open record directly (e.g. {**p[...]})
            if (op.id, key) in seen_field:
                continue
            seen_field.add((op.id, key))
            strong.append(Opportunity(
                pass_name="lossy-projection", kind="unsourced-field",
                title=f"output field `{key}` is a nested record never sourced "
                      f"from the open input — caller-supplied `{key}` is silently dropped",
                ref=ref, op_id=op.id, block_id=bid, line=ln,
                modality="may", soundness="heuristic",
                detail=f'source it from the input (e.g. input.get("{key}")) or carry it through'))

        # hint: closed projection of an open record (>=2 keys), no carry-rest.
        if (op.kind == "return" and not db.get("has_spread") and not db.get("dynamic")
                and any(len(reads[p]) >= 2 for p in record_params)):
            hints.append(Opportunity(
                pass_name="lossy-projection", kind="closed-projection",
                title="closed reconstruction of an open input — keys not enumerated "
                      "here are dropped; use `{**input, ...}` to preserve them",
                ref=ref, op_id=op.id, block_id=bid, line=ln,
                modality="may", soundness="heuristic"))
    return strong + hints  # strong findings ranked before weak hints
