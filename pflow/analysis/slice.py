from .walk import walk
from ..ir import FunctionGraph


def slice_backward(g: FunctionGraph, start, max_depth=5):
    """Backward data slice from a ref (simple implementation)."""
    return walk(g, start=start, direction="backward", edges="data", max_depth=max_depth)


def slice_forward(g: FunctionGraph, start, max_depth=5):
    return walk(g, start=start, direction="forward", edges="data", max_depth=max_depth)
