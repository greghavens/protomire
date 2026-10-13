"""Load rule methods only inside an admitted, source-pinned native component."""
from copy import deepcopy
from ..rules import validate_method
from ..scientific import Method


def load_methods(policy, spec):
    from .components.runtime import load_callable
    registered = {}
    for rule in policy['rules']:
        if rule['kind'] != 'method/v1':
            continue
        declaration = validate_method(rule['method'])
        key = declaration['id'], declaration['version']
        if key in registered:
            if registered[key].implementation != declaration:
                raise ValueError('Conflicting custom rule method declarations')
            continue
        reference = declaration['implementation_ref']
        if reference['commit'] != spec['source']['commit']:
            raise ValueError('Rule method source commit differs from admitted recipe source')
        if {'path': reference['path'], 'sha256': reference['sha256']} not in spec['source']['files']:
            raise ValueError('Rule method source is outside the admitted source closure')
        function = load_callable(reference, spec['source_root'], spec['trusted_code_roots'], spec['source']['files'])
        registered[key] = Method(key[0], key[1], deepcopy(declaration), deepcopy(declaration['parameter_schema']), function)
    return tuple(registered.values())
