"""Lossy-projection detector (pflow.analysis.projection.find_lossy_projection).

The motivating bug (seeker validate_service_payload): a free-form `workload_spec`
is rebuilt as a closed `{"container_port": ...}` while `p["workload_spec"]` is
never read, silently dropping caller-supplied keys.
"""

from conftest import build

from pflow.analysis.projection import find_lossy_projection


# The real bug shape: keys read through validator helpers (req_str/opt_int),
# not direct subscript; `workload_spec` emitted but never sourced from `p`.
SEEKER = '''
def req_str(p, key, **kw): return ""
def opt_int(p, key, **kw): return 0
def as_int(p, key, **kw): return 0

def validate_service_payload(p: dict) -> dict:
    name = req_str(p, "name")
    digest = req_str(p, "image_digest")
    port = opt_int(p, "port")
    container_port = opt_int(p, "container_port")
    nodes = p.get("placement_nodes")
    desired = as_int(p, "desired_replicas")
    return {
        "name": name, "image_ref": digest.split("@")[0], "image_digest": digest,
        "port": port,
        "workload_spec": {"container_port": container_port} if container_port is not None else {},
        "desired_replicas": desired,
        "placement_nodes": nodes,
    }
'''


def _find(src, name):
    return find_lossy_projection(build(src, name))


def test_flags_unsourced_nested_record():
    opps = _find(SEEKER, "validate_service_payload")
    unsourced = [o for o in opps if o.kind == "unsourced-field"]
    # exactly one strong finding: the workload_spec nested record
    assert len(unsourced) == 1
    assert "workload_spec" in unsourced[0].title
    assert unsourced[0].soundness == "heuristic" and unsourced[0].modality == "may"


def test_derived_scalar_not_flagged():
    # image_ref is derived (not read from p) but it is a SCALAR, not a nested
    # record — the container-only restriction must keep it quiet.
    opps = _find(SEEKER, "validate_service_payload")
    assert not any("image_ref" in o.title for o in opps if o.kind == "unsourced-field")


def test_closed_projection_hint_ranks_after_strong():
    opps = _find(SEEKER, "validate_service_payload")
    kinds = [o.kind for o in opps]
    assert "unsourced-field" in kinds and "closed-projection" in kinds
    # strong findings come first
    assert kinds.index("unsourced-field") < kinds.index("closed-projection")


def test_spread_carry_rest_is_clean():
    # `{**p, ...}` preserves every input key -> no strong finding AND no hint,
    # even though `extra` is an empty container.
    src = '''
def req_str(p, key, **kw): return ""
def validate(p: dict) -> dict:
    name = req_str(p, "name")
    return {**p, "name": name, "extra": {}}
'''
    assert _find(src, "validate") == []


def test_sourced_nested_record_not_strong():
    # workload_spec IS sourced from p (via spread of p.get(...)) -> the strong
    # signal must not fire; a closed-projection hint is acceptable.
    src = '''
def req_str(p, key, **kw): return ""
def validate(p: dict) -> dict:
    name = req_str(p, "name")
    return {"name": name, "workload_spec": {**p.get("workload_spec", {})}}
'''
    opps = _find(src, "validate")
    assert not any(o.kind == "unsourced-field" for o in opps)


def test_non_open_param_is_ignored():
    # `l: Lease` is a concrete type, not an open mapping -> out of scope, even
    # though it returns a closed dict with an empty nested container.
    src = '''
def to_wire(l: Lease) -> dict:
    return {"lease_id": l.lease_id, "meta": {}}
'''
    assert _find(src, "to_wire") == []


def test_mid_function_temp_dict_not_flagged():
    # a local dict that is neither returned nor stored to the open param is not
    # a projection boundary -> no strong finding on its empty container field.
    src = '''
def handle(p: dict):
    name = p.get("name")
    cfg = {"defaults": {}}
    return name
'''
    assert not any(o.kind == "unsourced-field" for o in _find(src, "handle"))
