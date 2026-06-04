from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(slots=True)
class Op:
    id: int
    kind: str
    targets: Tuple[str, ...] = ()
    uses: Tuple[str, ...] = ()
    attr_paths: Tuple[str, ...] = ()
    source: Optional[Tuple[int, int, int, int]] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"Op({self.id}, {self.kind}, targets={self.targets})"


@dataclass(slots=True)
class BasicBlock:
    id: int
    ops: Tuple[Op, ...] = ()
    succs: Tuple[int, ...] = ()
    except_succs: Tuple[int, ...] = ()
    preds: Tuple[int, ...] = ()
    source_lines: Tuple[int, ...] = ()
    attrs: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"BasicBlock({self.id}, {len(self.ops)} ops, succs={self.succs})"


@dataclass(slots=True)
class Def:
    id: int
    op_id: int
    block_id: int
    name: str
    source: Optional[Tuple[int, ...]] = None


@dataclass(slots=True)
class Use:
    id: int
    op_id: int
    block_id: int
    name: str
    reaching_defs: Tuple[int, ...] = ()


@dataclass(slots=True)
class FunctionGraph:
    qualname: str
    source_path: str
    first_line: int
    blocks: Tuple[BasicBlock, ...] = ()
    entry: int = 0
    exit_blocks: Tuple[int, ...] = ()
    ops: Tuple[Op, ...] = ()
    defs: List[Def] = field(default_factory=list)
    uses: List[Use] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)

    def get_block(self, block_id: int) -> BasicBlock:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(f"no block with id {block_id}")

    def iter_ops(self):
        for bb in self.blocks:
            yield from bb.ops

    def num_blocks(self) -> int:
        return len(self.blocks)

    def num_ops(self) -> int:
        return sum(len(bb.ops) for bb in self.blocks)

    def __repr__(self) -> str:
        return f"FunctionGraph({self.qualname!r}, {self.num_blocks()} blocks, {self.num_ops()} ops)"


@dataclass(slots=True)
class ModuleGraph:
    module_name: str
    source_path: str
    functions: Dict[str, FunctionGraph] = field(default_factory=dict)
    attrs: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"ModuleGraph({self.module_name!r}, {len(self.functions)} functions)"
