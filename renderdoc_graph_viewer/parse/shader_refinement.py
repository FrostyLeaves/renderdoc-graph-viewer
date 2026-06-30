# -*- coding: utf-8 -*-
"""Static shader-resource access refinement dispatcher.

Per-API passes reconstruct binding-to-resource state from structured data and
use shader_access parsers to classify read-write resources as read, write,
read-write or unused. APIs without a registered refinement pass return {}.
"""

from . import apis
from .apis._common import _walk_executables


def refine(controller, rd, warnings=None):
    """Dispatch to the per-API static refiner -> {(eid, res_key): access}.

    The result is intentionally sparse: unresolved bindings and parser UNKNOWN
    verdicts are omitted so the graph keeps RenderDoc's conservative RW edges.
    """
    try:
        api = apis.api_key(controller)
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                'shader-access refinement: API type unavailable (%s: %s); '
                'bindings keep conservative read+write edges'
                % (type(exc).__name__, exc))
        return {}
    pass_cls = apis.refine_pass(api)
    if pass_cls is None:
        return {}
    try:
        return _walk_executables(controller, rd, pass_cls, warnings=warnings)
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                'shader-access refinement failed for %s (%s: %s); bindings '
                'keep conservative read+write edges'
                % (api, type(exc).__name__, exc))
        return {}
