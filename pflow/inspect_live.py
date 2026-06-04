"""The `live:` target path — build a CFG from a live function/method object.

We prefer to parse the whole source file (so methods resolve via their
`Class.method` qualname and source line numbers stay absolute). If the file
isn't available we fall back to dedenting `inspect.getsource`, which loses
absolute line numbers but still produces a usable graph.
"""

import inspect
import os
import textwrap
from types import MethodType
from typing import Any

from .ir import build_cfg_from_source
from .bytecode import get_basic_blocks, get_exception_table


def _unwrap(obj: Any):
    if isinstance(obj, (MethodType, staticmethod, classmethod)):
        obj = obj.__func__
    obj = inspect.unwrap(obj)
    if not hasattr(obj, "__code__"):
        raise TypeError(f"no __code__ on {type(obj).__name__}")
    return obj


def build_cfg_from_live(obj: Any):
    obj = _unwrap(obj)
    code = obj.__code__
    qualname = getattr(obj, "__qualname__", getattr(obj, "__name__", "unknown"))
    filename = code.co_filename

    g = None
    src_file = inspect.getsourcefile(obj) or filename
    if src_file and os.path.exists(src_file):
        try:
            whole = open(src_file, encoding="utf-8").read()
            g = build_cfg_from_source(whole, qualname, source_path=filename)
        except Exception:
            g = None

    if g is None:
        # Fallback: dedent the extracted snippet. Line numbers become
        # relative to the snippet rather than the file.
        src = textwrap.dedent(inspect.getsource(obj))
        g = build_cfg_from_source(src, qualname.split(".")[-1], source_path=filename)
        g.qualname = qualname

    g.attrs["live"] = True
    g.attrs["code_object"] = code
    g.attrs["firstlineno"] = code.co_firstlineno
    g.attrs["bytecode_blocks"] = get_basic_blocks(code)
    g.attrs["exception_table"] = get_exception_table(code)
    return g
