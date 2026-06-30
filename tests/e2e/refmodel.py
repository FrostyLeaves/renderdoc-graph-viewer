# -*- coding: utf-8 -*-
"""Synthetic normalized graph model for YAML e2e scenes."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from renderdoc_graph_viewer import graph_model as gm
from renderdoc_graph_viewer.parse import usage_access
from gen import schema as S
import compare

# YAML resource kind -> graph_model res_kind
_RES_KIND = {
    S.KIND_BUFFER: gm.RES_BUFFER,
    S.KIND_UAV_TEX: gm.RES_UAV_TEX,
    S.KIND_COLOR: gm.RES_COLOR,
    S.KIND_DEPTH: gm.RES_DEPTH,
    S.KIND_SAMPLED: gm.RES_SAMPLED,
    S.KIND_SWAPCHAIN: gm.RES_SWAPCHAIN,
    # constant, vertex and index buffers use the viewer buffer kind
    S.KIND_CBUFFER: gm.RES_BUFFER,
    S.KIND_VBUFFER: gm.RES_BUFFER,
    S.KIND_IBUFFER: gm.RES_BUFFER,
}

# YAML pass type -> LeafAction kind
_LEAF_KIND = {
    S.PASS_COMPUTE: gm.KIND_DISPATCH,
    S.PASS_GRAPHICS: gm.KIND_DRAW,
    S.PASS_TRANSFER: gm.KIND_TRANSFER,
    S.PASS_PRESENT: gm.KIND_PRESENT,
}

# refine verdict per UAV access (refined mode)
_UAV_VERDICT = {
    S.ACC_READ: gm.READ,
    S.ACC_WRITE: gm.WRITE,
    S.ACC_RW: gm.RW,
    S.ACC_NONE: 'unused',
}

# depth access -> the post-refine usage name depth_access.refine yields
_DEPTH_USAGE = {S.ACC_READ: 'DepthTestRead', S.ACC_RW: 'DepthTestRW',
                S.ACC_WRITE: 'DepthStencilTarget'}


def _instances(p):
    """Pass -> list of (instance_name, marker_path). repeat>1 yields distinct
    names so build_passes keeps them separate (bundling then re-merges them). A
    markerless pass (marker=False) contributes leaves with no name segment, so
    build_passes fine-groups them (Compute #N / Pass #N)."""
    names = ([p.name] if p.repeat <= 1
             else ['%s %d' % (p.name, i) for i in range(p.repeat)])
    out = []
    for nm in names:
        mpath = tuple(p.scope) if not p.marker else tuple(p.scope) + (nm,)
        out.append((nm, mpath))
    return out


def build_synth(scene, api):
    """Scene -> (bundle, shader_access). Synthetic eids run in declaration order.

    api-aware: in Vulkan a StructuredBuffer SRV and an RWStructuredBuffer UAV are
    both VK storage buffers, so RenderDoc reports both as CS_RWResource and the
    refiner reclassifies the SRV to read; in D3D a StructuredBuffer is a real SRV
    (CS_Resource, read). Other binding kinds match across APIs."""
    vk = (api == 'vulkan')
    leaves = []
    usage = {}
    shader_access = {}
    res_names = {}
    res_info = {}
    for r in scene.resources.values():
        res_names[r.name] = r.name
        res_info[r.name] = {'kind': _RES_KIND[r.kind],
                            'info': {'dims': '', 'format': r.fmt or '', 'msaa': 1}}

    eid = 1

    def add_usage(res, uname):
        usage.setdefault(res, []).append((eid, uname))

    for p in scene.passes:
        for name, mpath in _instances(p):
            kind = _LEAF_KIND[p.type]
            outputs = []
            depth_out = None
            copy_src = copy_dst = None

            if p.type == S.PASS_COMPUTE:
                for b in p.binds:
                    if b.bind in S.UAV_BINDS:
                        add_usage(b.res, 'CS_RWResource')
                        shader_access[(eid, b.res)] = _UAV_VERDICT[b.access]
                        if b.access in (S.ACC_WRITE, S.ACC_RW):
                            outputs.append(b.res)
                    elif b.bind == S.BIND_CBV:
                        add_usage(b.res, 'CS_Constants')
                    elif b.bind == S.BIND_SRV_BUF and vk:
                        # VK structured-buffer SRV uses storage-buffer usage
                        add_usage(b.res, 'CS_RWResource')
                        shader_access[(eid, b.res)] = _UAV_VERDICT[b.access]
                    else:  # srv_buf (D3D) / sampled -> real read-only SRV
                        add_usage(b.res, 'CS_Resource')

            elif p.type == S.PASS_GRAPHICS:
                for c in p.color:
                    add_usage(c, 'ColorTarget')
                    outputs.append(c)
                for b in p.sample:
                    add_usage(b.res, 'PS_Resource')
                for v in p.vertex:
                    add_usage(v, 'VertexBuffer')
                if p.index:
                    add_usage(p.index, 'IndexBuffer')
                if p.depth:
                    add_usage(p.depth['res'], _DEPTH_USAGE[p.depth['access']])
                    depth_out = p.depth['res']

            elif p.type == S.PASS_TRANSFER:
                if p.copy:
                    add_usage(p.copy['src'], 'CopySrc')
                    add_usage(p.copy['dst'], 'CopyDst')
                    copy_src = p.copy['src']
                    copy_dst = p.copy['dst']
                    outputs.append(p.copy['dst'])

            elif p.type == S.PASS_PRESENT:
                if p.swapchain:
                    copy_src = p.swapchain  # build_scoped reads the present src hint

            leaves.append(gm.LeafAction(
                eid, kind, group_outputs=outputs, group_depth=depth_out,
                marker_path=mpath, name=name, copy_src_hint=copy_src,
                copy_dst_hint=copy_dst))
            eid += 1

    # capture-end orphan present for scenes without an explicit present pass
    if not any(p.type == S.PASS_PRESENT for p in scene.passes):
        leaves.append(gm.LeafAction(eid, gm.KIND_PRESENT,
                                    name='End of Capture'))

    bundle = {
        'leaves': leaves,
        'usage_by_res': usage,
        'res_names': res_names,
        'res_info': res_info,
        'rid_objects': {},
        'refinements': {
            'usage_cleanup': {'labels': []},
            'depth_access': {'access': {}},
            'shader_access': {},
        },
        'refinement_cache': {'depth_access': {'access': {}}},
        'warnings': [],
        'seconds': 0.0,
    }
    usage_access.apply_shader_access(bundle['usage_by_res'], shader_access)
    bundle['refinements']['shader_access'] = dict(shader_access)
    return bundle, shader_access


def _scope_range(bundle, scope_path):
    """eid span of the leaves under scope_path (None for root)."""
    if not scope_path:
        return None
    eids = [l.eid for l in bundle['leaves']
            if tuple(l.marker_path[:len(scope_path)]) == tuple(scope_path)]
    if not eids:
        return None
    return (min(eids), max(eids))


def scope_instances(scene, api='vulkan'):
    """Scope instances for precomputed e2e graph levels."""
    bundle, _sa = build_synth(scene, api)
    leaves = bundle['leaves']
    out = [('', (), None)]
    seen = []
    for l in leaves:
        for d in range(1, len(l.marker_path) + 1):
            p = tuple(l.marker_path[:d])
            if p not in seen:
                seen.append(p)
    for path in seen:
        for rng in gm._leaf_runs(leaves, path):
            out.append((compare.instance_key(leaves, path, rng), path, rng))
    return out


def expected_instance(scene, path, rng, bundling=False, api='vulkan'):
    """-> (node_keys, edge_keys) for one scope instance given its explicit run
    range (not the spanning min/max, which conflates same-named instances)."""
    bundle, _sa = build_synth(scene, api)
    fg = gm.build_scoped(bundle, path, rng, bundling=bundling)
    return compare.canon(fg)


def expected(scene, scope_path=(), bundling=False, api='vulkan'):
    """-> (node_keys, edge_keys) for one scope level. Uses the spanning range,
    correct for single-instance paths; for repeated markers use expected_instance
    with the per-occurrence range."""
    bundle, _sa = build_synth(scene, api)
    return expected_instance(scene, scope_path, _scope_range(bundle, scope_path),
                             bundling, api)


def expected_merged(scene, api='vulkan', marker_depth=None):
    """Whole-frame merged view (build_from_bundle): one node per resource, full
    marker-path grouping, rank edges, self-RW fold."""
    bundle, _sa = build_synth(scene, api)
    fg = gm.build_from_bundle(bundle, marker_depth=marker_depth, versioned=False)
    return compare.canon(fg)
