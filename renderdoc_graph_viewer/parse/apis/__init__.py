# -*- coding: utf-8 -*-
"""Per-API implementation registry.

Add a new API by creating apis/<api>.py with API_KEYS, DEPTH_PASS,
REFINE_PASS and/or PRESENT_RESOLVER, then adding the module to _MODULES.
"""

from . import vulkan, d3d12, d3d11   # registry import ordering
_MODULES = (vulkan, d3d12, d3d11)

_DEPTH = {}
_REFINE = {}
_PRESENT = {}


def _rebuild():
    _DEPTH.clear()
    _REFINE.clear()
    _PRESENT.clear()
    for mod in _MODULES:
        for key in getattr(mod, 'API_KEYS', ()):
            if getattr(mod, 'DEPTH_PASS', None) is not None:
                _DEPTH[key] = mod.DEPTH_PASS
            if getattr(mod, 'REFINE_PASS', None) is not None:
                _REFINE[key] = mod.REFINE_PASS
            if getattr(mod, 'PRESENT_RESOLVER', None) is not None:
                _PRESENT[key] = mod.PRESENT_RESOLVER


_rebuild()


def api_key(controller):
    """Lowercased API tag ('d3d12', 'vulkan', ...) the per-API lookups key off,
    derived from the replay controller's pipeline type. Raises if the controller
    cannot report its API; callers derive it inside their own guard."""
    return str(controller.GetAPIProperties().pipelineType).split('.')[-1].lower()


def depth_pass(api):
    """Return the depth pass class for an API, or None when unregistered."""
    return _DEPTH.get(api)


def refine_pass(api):
    """Return the shader-access pass class for an API, or None."""
    return _REFINE.get(api)


def present_resolver(api):
    """Return the Present source resolver class for an API, or None."""
    return _PRESENT.get(api)
