"""pflow.ir — Core intermediate representation.

Public API (v1):

    from pflow.ir import (
        Op, BasicBlock, FunctionGraph, Def, Use, ModuleGraph,
        build_cfg_from_source, build_cfg_from_ast,
    )
"""

from .graph import (
    Op,
    BasicBlock,
    FunctionGraph,
    Def,
    Use,
    ModuleGraph,
)

from .cfg import build_cfg_from_source, build_cfg_from_ast

__all__ = [
    "Op",
    "BasicBlock",
    "FunctionGraph",
    "Def",
    "Use",
    "ModuleGraph",
    "build_cfg_from_source",
    "build_cfg_from_ast",
]
