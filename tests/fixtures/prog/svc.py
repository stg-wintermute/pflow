"""Fixture module: a service that threads state across the pipeline and
communicates through a shared mutable attribute (self._cache)."""

from core import make_state, process, bump


class Service:
    def __init__(self, db):
        self._db = db
        self._cache = {}

    def handle(self, req):
        st = make_state(req)
        self._cache = st          # write _cache (2nd writer, after __init__)
        bump()
        return process(st)

    def read(self):
        return self._cache         # read _cache -> shared channel with handle
