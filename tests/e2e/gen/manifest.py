# -*- coding: utf-8 -*-
"""Scene IR -> API-agnostic manifest dict the generic runtimes interpret. Carries
resources, passes, the canonical binding layout, and per-pass shader stems (the
runtime appends .spv/.dxil/.cso). Python 3.6."""

from . import schema as S
from .layout import assign


def emit_manifest(scene):
    resources = []
    for r in scene.resources.values():
        resources.append({
            'name': r.name, 'kind': r.kind,
            'dims': list(r.dims) if r.dims else None,
            'fmt': r.fmt, 'elements': r.elements,
        })
    passes = []
    for p in scene.passes:
        passes.append({
            'name': p.name,
            'type': p.type,
            'scope': list(p.scope),
            'marker': p.marker,
            'groups': p.groups,
            'repeat': p.repeat,
            'binds': assign(p.binds),
            'sample': assign(p.sample) if p.sample else [],
            'color': list(p.color),
            'depth': p.depth,
            'vertex': list(p.vertex),    # vbuffer res names, one per IA stream
            'index': p.index,            # ibuffer res name or null
            'copy': p.copy,
            'swapchain': p.swapchain,
            'cs': p.name if p.type == S.PASS_COMPUTE else None,
            'vs': (p.name + '_vs') if p.type == S.PASS_GRAPHICS else None,
            'ps': (p.name + '_ps') if p.type == S.PASS_GRAPHICS else None,
        })
    return {'name': scene.name, 'frame_prior': scene.frame_prior,
            'resources': resources, 'passes': passes}
