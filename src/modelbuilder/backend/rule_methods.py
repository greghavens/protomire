"""Load rule methods only inside an admitted, source-pinned native component."""

from copy import deepcopy
from contextlib import contextmanager
import random
import sys
from ..rules import validate_method
from ..scientific import Method


@contextmanager
def preserve_training_rng():
    """Restore Python/NumPy legacy/Torch CPU RNG around reviewed methods.

    Do not import optional ML libraries or initialize CUDA. This is state
    isolation for already loaded training libraries, not an I/O/code sandbox.
    """
    numpy = sys.modules.get("numpy")
    torch = sys.modules.get("torch")
    python_state = random.getstate()
    numpy_state = numpy.random.get_state() if numpy is not None else None
    torch_state = torch.get_rng_state().clone() if torch is not None else None
    try:
        yield
    finally:
        random.setstate(python_state)
        if numpy_state is not None:
            numpy.random.set_state(numpy_state)
        if torch_state is not None:
            torch.set_rng_state(torch_state)


def load_methods(policy, spec):
    with preserve_training_rng():
        return _load_methods(policy, spec)


def _load_methods(policy, spec):
    from .components.runtime import load_callable

    registered = {}
    for rule in policy["rules"]:
        if rule["kind"] != "method/v1":
            continue
        declaration = validate_method(rule["method"])
        key = declaration["id"], declaration["version"]
        if key in registered:
            if registered[key].implementation != declaration:
                raise ValueError("Conflicting custom rule method declarations")
            continue
        reference = declaration["implementation_ref"]
        if reference["commit"] != spec["source"]["commit"]:
            raise ValueError("Rule method source commit differs from admitted recipe source")
        if {"path": reference["path"], "sha256": reference["sha256"]} not in spec["source"]["files"]:
            raise ValueError("Rule method source is outside the admitted source closure")
        function = load_callable(
            reference, spec["source_root"], spec["trusted_code_roots"], spec["source"]["files"]
        )
        registered[key] = Method(
            key[0], key[1], deepcopy(declaration), deepcopy(declaration["parameter_schema"]), function
        )
    return tuple(registered.values())
