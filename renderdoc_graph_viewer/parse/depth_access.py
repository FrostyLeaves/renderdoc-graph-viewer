# -*- coding: utf-8 -*-
"""Static, zero-replay refinement of depth/stencil attachment access.

RenderDoc reports depth bindings as write-class DepthStencilTarget. This module
dispatches to the per-API depth pass and returns cached-bundle shaped data:
{'access': {eid: read|write|rw|none}}.
"""

from . import apis
from .apis._common import walk_actions


def refine(controller, rd, usage_by_res, leaves, cached=None,
           progress=None, warnings=None):
    """Classify each depth-attachment binding from the bound pipeline's baked
    state, read statically from structured data. cached skips the walk and is
    returned as-is. progress(done, total) reports target draw count."""
    if cached is not None:
        return cached

    try:
        roots = controller.GetRootActions()
    except Exception:
        return {'access': {}}

    pass_cls = None
    chunks = None
    api_key = ''
    try:
        api_key = apis.api_key(controller)
        pass_cls = apis.depth_pass(api_key)
        if pass_cls is not None:
            chunks = controller.GetStructuredFile().chunks
    except Exception as exc:
        # unexpected: API type or structured data unavailable. (A missing
        # per-API adapter is not an exception - depth_pass returns None - and
        # stays silent below.)
        if warnings is not None:
            warnings.append(
                'depth refinement: pipeline/structured data unavailable for '
                '%s (%s); depth bindings keep write semantics'
                % (api_key or '?', exc))
        pass_cls = None

    leaf_eids = set(l.eid for l in leaves)
    targets = set()
    if pass_cls is not None:
        for evs in usage_by_res.values():
            for (eid, uname) in evs:
                if uname == 'DepthStencilTarget' and eid in leaf_eids:
                    targets.add(eid)
    if progress is not None:
        progress(0, len(targets))

    p = None
    if pass_cls is not None:
        try:
            p = pass_cls(chunks)
        except Exception:
            p = None
        if p is not None and p.seen and not p.tables:
            if warnings is not None:
                warnings.append(
                    'depth refinement: %s pipeline parsing failed '
                    '(unvalidated adapter) - depth bindings keep '
                    'write semantics' % api_key)
            p = None

    access = {}
    if p is not None:
        def on_action(a):
            if a.eventId in targets:
                acc = p.current()
                if acc is not None:
                    access[a.eventId] = acc
        walk_actions(roots, on_action, chunks, p.on_chunk)
    if progress is not None:
        progress(len(targets), len(targets))
    return {'access': access}
