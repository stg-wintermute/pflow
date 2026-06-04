"""Fixture module: a small data pipeline + a module global."""

TOTAL = 0


def make_state(seed):
    return {"v": seed}


def process(state):
    enriched = transform(state)
    return store(enriched)


def transform(s):
    return s


def store(s):
    return s


def bump():
    global TOTAL
    TOTAL = TOTAL + 1
    return TOTAL
