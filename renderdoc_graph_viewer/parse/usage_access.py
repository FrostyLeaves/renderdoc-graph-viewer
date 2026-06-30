# -*- coding: utf-8 -*-
"""Usage-name taxonomy and refinement application."""

from .apis._common import READ, WRITE, RW, ACCESS_NONE, UNUSED

IGNORE = 'ignore'

_STAGES = ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'TS', 'MS', 'All')

# ResourceUsage enum-name -> READ/WRITE/RW/IGNORE access category.
USAGE_ACCESS = {}
for _s in _STAGES:
    USAGE_ACCESS['%s_Constants' % _s] = READ
    USAGE_ACCESS['%s_Resource' % _s] = READ
    USAGE_ACCESS['%s_RWResource' % _s] = RW
    USAGE_ACCESS['%s_ReadResource' % _s] = READ
    USAGE_ACCESS['%s_WriteResource' % _s] = WRITE
USAGE_ACCESS.update({
    'VertexBuffer': READ,
    'IndexBuffer': READ,
    'InputTarget': READ,
    'Indirect': READ,
    'CopySrc': READ,
    'ResolveSrc': READ,
    'ColorTarget': WRITE,
    'DepthStencilTarget': WRITE,
    'DepthTestRead': READ,
    'DepthTestRW': RW,
    'Clear': WRITE,
    'Discard': WRITE,
    'CopyDst': WRITE,
    'ResolveDst': WRITE,
    'StreamOut': WRITE,
    'Copy': RW,
    'Resolve': RW,
    'GenMips': RW,
    'Barrier': IGNORE,
    'Unused': IGNORE,
    'CPUWrite': IGNORE,
    'Present': READ,
})


def direction(usage_name):
    return USAGE_ACCESS.get(usage_name, IGNORE)


def is_shader_usage(usage_name):
    """Shader-binding usages handled by shader_refinement."""
    return (usage_name.endswith('_Resource') or
            usage_name.endswith('_RWResource'))


DROP = object()   # rename-table sentinel: delete the event entirely


def apply_access_rename(usage_by_res, lookup, usage_filter, rename):
    """Rewrite usage names in place from a refinement verdict.

    lookup(eid, res_key) -> access, None leaves the event alone. rename maps
    access -> new usage name, callable(old_name)->new_name, or DROP.
    """
    for res_key, evs in list(usage_by_res.items()):
        out = []
        changed = False
        for (eid, uname) in evs:
            if usage_filter(uname):
                access = lookup(eid, res_key)
                repl = rename.get(access) if access is not None else None
                if repl is DROP:
                    changed = True
                    continue
                if repl is not None:
                    uname = repl(uname) if callable(repl) else repl
                    changed = True
            out.append((eid, uname))
        if changed:
            usage_by_res[res_key] = out


def apply_depth_access(usage_by_res, access):
    """DepthStencilTarget is conservative write-class usage. Reclassify
    test-only / test+write bindings per draw, dropping no-attachment draws."""
    if not access:
        return
    apply_access_rename(
        usage_by_res,
        lookup=lambda eid, _rk: access.get(eid),
        usage_filter=lambda u: u == 'DepthStencilTarget',
        rename={READ: 'DepthTestRead', RW: 'DepthTestRW', ACCESS_NONE: DROP})


def apply_shader_access(usage_by_res, access_map):
    """Refine *_RWResource per (eid, res_key)."""
    if not access_map:
        return

    def to_read(u):
        return u[:-len('RWResource')] + 'ReadResource'

    def to_write(u):
        return u[:-len('RWResource')] + 'WriteResource'

    apply_access_rename(
        usage_by_res,
        lookup=lambda eid, rk: access_map.get((eid, rk)),
        usage_filter=lambda u: u.endswith('_RWResource'),
        rename={READ: to_read, WRITE: to_write})


def apply_unused_binding_flags(fg, access_map):
    """Mark read edges whose shader-usage events are all unused."""
    res_by_id = dict((n.id, n) for n in fg.resources)
    for edge in fg.edges:
        if edge.kind != READ:
            continue
        node = res_by_id.get(edge.src_id)
        if node is None or getattr(node, 'bundle_members', None):
            continue
        verdicts = []
        for eid, usage_name in edge.usages:
            if not is_shader_usage(usage_name):
                verdicts = None
                break
            verdicts.append(access_map.get((eid, node.res_key)))
        edge.unused_binding = (bool(verdicts) and
                               all(v == UNUSED for v in verdicts))
    return fg
